"""Engine and session helpers, for whichever database `settings` names.

Engines are cached per database (the provider's `cache_key`) so `session_scope(settings)` is
cheap to call repeatedly. How an engine is built and what each connection gets is up to the
database provider (`teacher.db.providers`): on SQLite foreign keys, a busy timeout, WAL and
explicit write-lock handling (`teacher.db.providers.sqlite`); on Supabase's Postgres a pinged,
pooled psycopg connection in UTC that reads floats exactly, with the same transaction behaviour
as SQLite's: snapshot reads, then one writer at a time (`teacher.db.providers.supabase`).
Engines are safe to share between threads (the web app's threadpool plus background generation
jobs).
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from teacher.config import Settings
from teacher.db.providers import create_db_provider
from teacher.db.providers.sqlite import BUSY_TIMEOUT_MS  # noqa: F401  (re-exported)

_lock = threading.Lock()
_engines: dict[str, Engine] = {}
_factories: dict[str, sessionmaker[Session]] = {}


def make_engine(settings: Settings, **kwargs: Any) -> Engine:
    """Create a new (uncached) engine for `settings`' database with its provider's
    per-connection setup; `kwargs` go to `sqlalchemy.create_engine`."""
    return create_db_provider(settings).create_engine(**kwargs)


def get_engine(settings: Settings) -> Engine:
    """Return the cached engine for `settings`, creating it on first use."""
    key = create_db_provider(settings).cache_key
    with _lock:
        engine = _engines.get(key)
        if engine is None:
            engine = _engines[key] = make_engine(settings)
        return engine


def session_factory(settings: Settings) -> sessionmaker[Session]:
    """Return the cached `sessionmaker` bound to `get_engine(settings)` (expire_on_commit=False)."""
    key = create_db_provider(settings).cache_key
    engine = get_engine(settings)
    with _lock:
        factory = _factories.get(key)
        if factory is None:
            factory = _factories[key] = sessionmaker(bind=engine, expire_on_commit=False)
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
    key = create_db_provider(settings).cache_key
    with _lock:
        _factories.pop(key, None)
        engine = _engines.pop(key, None)
    if engine is not None:
        engine.dispose()
