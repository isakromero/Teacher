"""Supabase's Postgres, reached directly with psycopg 3 (`postgresql+psycopg://`).

The connection string comes from `$SUPABASE_DB_URL` (`Settings.supabase_db_url`) and is a
secret: `describe()` names only the host and database, `cache_key` adds a hash, and the
engine's own repr masks the password.

Engines ping a connection before handing it out (`pool_pre_ping`, so a connection the server or
a pooler closed is replaced instead of failing the request) and keep a modest pool. Every
connection runs with the session time zone UTC: the app stores naive UTC datetimes, and the
`CURRENT_TIMESTAMP` server defaults then store naive UTC too, as on SQLite. Every connection
also reads floats exactly: with `extra_float_digits` at 0 or below the server rounds a float8
it sends (0.30000000000000004 arrives as 0.3), at 1 or more it sends the shortest exact form.
Both are set at connection start (`options=-c timezone=UTC -c extra_float_digits=3`) and again
with `SET` once connected (Supabase's session pooler accepts the startup options without
applying them, and hands out sessions with extra_float_digits 0), and each new connection then
checks both, failing loudly when either is wrong. On port 6543 (Supabase's transaction pooler)
neither can be used (a `SET` would not stay with the client) and prepared statements must be off
(`prepare_threshold=None`: a pooled server connection is shared between clients), so there the
database's own settings must be right (Supabase's defaults are: UTC, and extra_float_digits 1;
the check says how to set them otherwise).

Transactions behave as on SQLite (`teacher.db.providers.sqlite`): the reads of a transaction
see one snapshot, and writers take turns. A transaction starts as REPEATABLE READ (the engine's
default isolation level), so its reads agree with each other, as one SQLite read transaction's
do (the vector index's reconciliation and re-scoring rely on that). Its first statement that is
not a read restarts it, as the SQLite provider restarts it with `BEGIN IMMEDIATE`: the reads so
far are let go (`ROLLBACK`, nothing was written), a READ COMMITTED transaction begins and takes
the write lock, a transaction-scoped advisory lock (`WRITE_LOCK_KEY`) every app write
transaction takes, waiting at most `WRITE_LOCK_TIMEOUT_MS` (`lock_timeout`, like SQLite's
`busy_timeout`; then the statement fails with "lock timeout"). Why not REPEATABLE READ
throughout: Postgres then fails a transaction that updates a row another transaction changed
since its snapshot ("could not serialize access", to be retried by the caller) -- the failure
the SQLite restart was made to avoid (a request that reads, waits on a model call and then
writes, while heartbeats are stored). Why not plain READ COMMITTED: every statement would see
other transactions' newest commits, so a multi-statement read could disagree with itself, and
concurrent writers could interleave read-modify-write sequences SQLite serializes. The price is
SQLite's: what a transaction read before its first write may have changed by the time it
writes. Transactions whose connection was given an isolation level explicitly
(`execution_options(isolation_level=...)`, as `teacher db copy` reads its source) are left as
they are. Locks only within the transaction (`SET TRANSACTION`, `SET LOCAL`, an `_xact_` lock),
so this also works behind the transaction pooler.

The migration engine sets none of this (READ COMMITTED, no write lock); it still checks the
time zone and the float digits.

No file backup is taken before migrations: `backup()` logs that and returns None; Supabase's own
backups (daily, or point-in-time recovery) are what protect the data.
"""

from __future__ import annotations

import hashlib
import logging
import re
from functools import cached_property
from typing import TYPE_CHECKING, Any

from sqlalchemy import URL, Connection, Engine, create_engine, event, make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.pool import NullPool

from teacher.config import ENV_SUPABASE_DB_URL, ConfigError, Settings
from teacher.db.providers.base import DbProvider

if TYPE_CHECKING:
    from teacher.db.search.postgres import PostgresConceptSearch

logger = logging.getLogger(__name__)

TRANSACTION_POOLER_PORT = 6543
POOL_SIZE = 5
MAX_OVERFLOW = 5
POOL_RECYCLE_SECONDS = 1800
UTC_ZONES = frozenset({"UTC", "ETC/UTC", "UCT", "ETC/UCT", "ZULU", "ETC/ZULU", "GMT", "ETC/GMT"})
STARTUP_OPTIONS = "-c timezone=UTC -c extra_float_digits=3"
# The isolation level transactions start with, until their first write (module docstring).
READ_ISOLATION = "REPEATABLE READ"
WRITE_LOCK_KEY = 0x74656163686572  # "teacher" in ASCII: the advisory lock of app writers
WRITE_LOCK_TIMEOUT_MS = 5000  # as long as SQLite's busy_timeout


def _set_session(dbapi_connection: Any, _connection_record: Any) -> None:
    """Set the session's time zone and float digits (see the module docstring). Committed, so
    the rollback that returns a connection to the pool does not undo it."""
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("SET timezone TO 'UTC'")
        cursor.execute("SET extra_float_digits TO 3")
    finally:
        cursor.close()
    dbapi_connection.commit()


def _check_session(dbapi_connection: Any, _connection_record: Any) -> None:
    """Refuse a connection whose session time zone is not UTC or that rounds the floats it
    sends (see the module docstring)."""
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute(
            "SELECT current_setting('TimeZone'), current_setting('extra_float_digits')"
        )
        zone, float_digits = cursor.fetchone()
    finally:
        cursor.close()
    dbapi_connection.rollback()  # leave no transaction open from the check
    if str(zone).upper() not in UTC_ZONES:
        raise ConfigError(
            f"the Postgres session time zone is {zone!r}, not UTC; Teacher stores naive UTC "
            "timestamps. Set the database's zone with ALTER DATABASE <name> SET timezone TO "
            "'UTC' (Supabase's default)"
        )
    if int(float_digits) < 1:
        raise ConfigError(
            f"the Postgres session's extra_float_digits is {float_digits}, so the server rounds "
            "the floats it sends; Teacher needs them exact. Set it with ALTER DATABASE <name> "
            "SET extra_float_digits TO 1 (the Postgres and Supabase default)"
        )


_WRITING = "teacher_writing"
# Statements that leave the transaction as it is: reads, and transaction control itself.
_READ = re.compile(r"\s*(SELECT|SHOW|EXPLAIN|BEGIN|COMMIT|END|ROLLBACK)\b", re.IGNORECASE)
# One round trip: end the read transaction, begin the write transaction and take the lock.
_RESTART_AS_WRITER = (
    "ROLLBACK; BEGIN ISOLATION LEVEL READ COMMITTED; "
    f"SET LOCAL lock_timeout = {WRITE_LOCK_TIMEOUT_MS}; "
    f"SELECT pg_advisory_xact_lock({WRITE_LOCK_KEY})"
)


def _begin(connection: Connection) -> None:
    # A connection given its own isolation level is left alone (module docstring).
    explicit = "isolation_level" in connection.get_execution_options()
    connection.info[_WRITING] = explicit


def _begin_before_write(
    connection: Connection,
    cursor: Any,
    statement: str,
    _parameters: Any,
    _context: Any,
    _executemany: bool,
) -> None:
    """Restart the transaction as the write transaction before its first statement that is
    not a read (see the module docstring)."""
    if connection.info.get(_WRITING) or _READ.match(statement):
        return
    connection.info[_WRITING] = True
    cursor.execute(_RESTART_AS_WRITER)


class SupabaseDbProvider(DbProvider):
    name = "supabase"
    dialect = "postgresql"

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        if settings.db != "supabase" or not settings.supabase_db_url:
            raise ConfigError("SupabaseDbProvider needs Settings with db='supabase'")
        try:
            self._url = make_url(settings.supabase_db_url)
        except ArgumentError:
            # SQLAlchemy's message quotes the whole string, password included.
            raise ConfigError(
                f"${ENV_SUPABASE_DB_URL} is not a valid connection string"
            ) from None
        self._key_hash = hashlib.sha256(settings.supabase_db_url.encode()).hexdigest()[:16]

    @property
    def url(self) -> URL:
        return self._url

    @property
    def host(self) -> str:
        return self._url.host or ""

    @property
    def database(self) -> str:
        return self._url.database or ""

    @property
    def uses_transaction_pooler(self) -> bool:
        return self._url.port == TRANSACTION_POOLER_PORT

    @property
    def cache_key(self) -> str:
        return f"{self.describe()}#{self._key_hash}"

    def describe(self) -> str:
        return f"supabase:{self.host}/{self.database}"

    def connect_args(self) -> dict[str, Any]:
        """psycopg connection arguments (see the module docstring)."""
        if self.uses_transaction_pooler:
            return {"prepare_threshold": None}
        return {"options": STARTUP_OPTIONS}

    def _engine(self, **kwargs: Any) -> Engine:
        connect_args = {**self.connect_args(), **kwargs.pop("connect_args", {})}
        if kwargs.get("poolclass") is not NullPool:
            kwargs.setdefault("pool_size", POOL_SIZE)
            kwargs.setdefault("max_overflow", MAX_OVERFLOW)
            kwargs.setdefault("pool_recycle", POOL_RECYCLE_SECONDS)
        kwargs.setdefault("pool_pre_ping", True)
        engine = create_engine(self._url, connect_args=connect_args, **kwargs)
        if not self.uses_transaction_pooler:
            event.listen(engine, "connect", _set_session)
        event.listen(engine, "connect", _check_session)
        return engine

    def create_engine(self, **kwargs: Any) -> Engine:
        """Pinged and pooled, checked for UTC and exact floats, with SQLite's transaction
        behaviour: snapshot reads, then one writer at a time (module docstring)."""
        kwargs.setdefault("isolation_level", READ_ISOLATION)
        engine = self._engine(**kwargs)
        event.listen(engine, "begin", _begin)
        event.listen(engine, "before_cursor_execute", _begin_before_write)
        return engine

    def migration_engine(self) -> Engine:
        return self._engine(poolclass=NullPool)

    @cached_property
    def concept_search(self) -> PostgresConceptSearch:
        """Plain tables with pg_trgm indexes (`teacher.db.search.postgres`)."""
        from teacher.db.search.postgres import PostgresConceptSearch

        return PostgresConceptSearch()

    def backup(self) -> None:
        logger.warning(
            "No backup taken before migrating %s: Teacher backs up only the SQLite file; "
            "Supabase's own backups (daily, or point-in-time recovery) apply",
            self.describe(),
        )
        return None
