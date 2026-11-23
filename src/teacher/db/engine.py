"""Engine and session helpers.

Engines are cached per database URL so `session_scope(settings)` is cheap to call repeatedly.
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
"""

from __future__ import annotations

import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import Connection, Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from teacher.config import Settings

_lock = threading.Lock()
_engines: dict[str, Engine] = {}
_factories: dict[str, sessionmaker[Session]] = {}

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


def make_engine(settings: Settings, **kwargs: Any) -> Engine:
    """Create a new (uncached) engine for `settings.db_url` with foreign keys enforced.

    The engine is safe to share between threads (`check_same_thread=False` unless overridden).
    """
    connect_args = {"check_same_thread": False, **kwargs.pop("connect_args", {})}
    engine = create_engine(settings.db_url, connect_args=connect_args, **kwargs)
    event.listen(engine, "connect", _on_connect)
    event.listen(engine, "begin", _begin)
    event.listen(engine, "before_cursor_execute", _begin_before_write)
    return engine


def get_engine(settings: Settings) -> Engine:
    """Return the cached engine for `settings`, creating it on first use."""
    url = settings.db_url
    with _lock:
        engine = _engines.get(url)
        if engine is None:
            engine = _engines[url] = make_engine(settings)
        return engine


def session_factory(settings: Settings) -> sessionmaker[Session]:
    """Return the cached `sessionmaker` bound to `get_engine(settings)` (expire_on_commit=False)."""
    url = settings.db_url
    engine = get_engine(settings)
    with _lock:
        factory = _factories.get(url)
        if factory is None:
            factory = _factories[url] = sessionmaker(bind=engine, expire_on_commit=False)
        return factory


@contextmanager
def session_scope(settings: Settings) -> Iterator[Session]:
    """Yield a session; commit on success, roll back on error, always close."""
    session = session_factory(settings)()
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()


def dispose_engine(settings: Settings) -> None:
    """Dispose and forget the cached engine for `settings` (if any)."""
    url = settings.db_url
    with _lock:
        _factories.pop(url, None)
        engine = _engines.pop(url, None)
    if engine is not None:
        engine.dispose()
