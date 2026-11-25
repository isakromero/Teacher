"""The local SQLite database: the file `teacher.db` under `Settings.root`.

Every connection gets `PRAGMA foreign_keys=ON`, a `busy_timeout` and WAL journaling.

The web app uses one engine from several threads (FastAPI's threadpool plus background generation
jobs), so connections are opened with `check_same_thread=False` (each pooled connection is still
used by one thread at a time); `busy_timeout` makes a writer wait for a competing transaction
instead of failing at once, and WAL lets readers proceed while a write is in progress.

Transactions are started explicitly. Python's sqlite3 driver otherwise only sends BEGIN just
before the first INSERT/UPDATE/DELETE, so a SAVEPOINT issued first (`session.begin_nested()`)
would become the outermost transaction and its RELEASE would commit the caller's work. We turn
the driver's implicit transaction handling off and send BEGIN ourselves (SQLAlchemy's documented
pysqlite recipe), so the reads of one transaction see one snapshot.

A transaction's first statement that is not a read restarts it as a write transaction: the
reads so far are let go (`ROLLBACK`, nothing was written) and `BEGIN IMMEDIATE` takes the write
lock, waiting `busy_timeout` for a competing writer. Writing in the transaction that read would
not do: under WAL it fails at once with "database is locked", whatever the `busy_timeout`, when
another connection committed since the first read (the snapshot is stale). A request that
reads, waits on a model call and then writes hit exactly that whenever a heartbeat was stored
meanwhile. The price: what a transaction read before its first write may have changed by the
time it writes.

The migration engine deliberately sets none of this: batch (table-rebuild) migrations on SQLite
must run with foreign keys off, or dropping the old table would cascade. Before pending
migrations the file is copied to `backups/teacher-<UTC timestamp>.db` with SQLite's backup API.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime
from functools import cached_property
from pathlib import Path
from typing import Any

from sqlalchemy import URL, Connection, Engine, create_engine, event, make_url
from sqlalchemy.pool import NullPool

from teacher.db.providers.base import DbProvider
from teacher.db.search.sqlite import SqliteConceptSearch

BUSY_TIMEOUT_MS = 5000


def _on_connect(dbapi_connection: Any, _connection_record: Any) -> None:
    dbapi_connection.isolation_level = None  # no implicit BEGIN/COMMIT from the driver
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        cursor.execute("PRAGMA journal_mode=WAL")
    finally:
        cursor.close()


_WRITING = "teacher_writing"
# Statements that leave the transaction as it is: reads, and transaction control itself.
_READ = re.compile(r"\s*(SELECT|PRAGMA|EXPLAIN|BEGIN|COMMIT|END|ROLLBACK)\b", re.IGNORECASE)


def _begin(connection: Connection) -> None:
    connection.info[_WRITING] = False
    connection.exec_driver_sql("BEGIN")


def _begin_before_write(
    connection: Connection,
    cursor: Any,
    statement: str,
    _parameters: Any,
    _context: Any,
    _executemany: bool,
) -> None:
    """Restart the transaction as a write transaction before its first statement that is not
    a read (see the module docstring)."""
    if connection.info.get(_WRITING) or _READ.match(statement):
        return
    if cursor.connection.in_transaction:
        cursor.execute("ROLLBACK")
    cursor.execute("BEGIN IMMEDIATE")
    connection.info[_WRITING] = True


class SqliteDbProvider(DbProvider):
    name = "sqlite"
    dialect = "sqlite"

    @property
    def path(self) -> Path:
        return self.settings.db_path

    @property
    def url(self) -> URL:
        return make_url(self.settings.db_url)

    @property
    def cache_key(self) -> str:
        return self.settings.db_url

    def describe(self) -> str:
        return f"sqlite:{self.path}"

    def create_engine(self, **kwargs: Any) -> Engine:
        """Foreign keys enforced, safe to share between threads (`check_same_thread=False`
        unless overridden), explicit BEGIN and the write-lock restart (module docstring)."""
        connect_args = {"check_same_thread": False, **kwargs.pop("connect_args", {})}
        engine = create_engine(self.settings.db_url, connect_args=connect_args, **kwargs)
        event.listen(engine, "connect", _on_connect)
        event.listen(engine, "begin", _begin)
        event.listen(engine, "before_cursor_execute", _begin_before_write)
        return engine

    def migration_engine(self) -> Engine:
        return create_engine(self.settings.db_url, poolclass=NullPool)

    def database_exists(self) -> bool:
        return self.path.exists()

    @cached_property
    def concept_search(self) -> SqliteConceptSearch:
        """The FTS5 tables (`teacher.db.search.sqlite`)."""
        return SqliteConceptSearch()

    def backup(self) -> Path:
        """Copy the database to `backups/teacher-<UTC timestamp>.db` with SQLite's backup API."""
        backups = self.settings.backups_dir
        backups.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S_%fZ")
        target = backups / f"teacher-{stamp}.db"
        src = sqlite3.connect(self.path)
        try:
            dst = sqlite3.connect(target)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        return target
