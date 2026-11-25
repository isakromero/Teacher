"""The database provider interface: what differs between the SQLite file and Supabase's Postgres.

The managers, models and migrations are shared; a provider owns only the connection-level
differences: the SQLAlchemy URL and the engine-cache key, engine creation with its
per-connection setup, the engine Alembic migrates with, reading the current revision, the
backup taken before pending migrations, and the concept-search backend (`concept_search`: how
this database stores and queries the concept search indexes, `teacher.db.search`). `teacher.db.engine` and `teacher.db.migrate` delegate
to it; `teacher.db.providers.create_db_provider(settings)` picks one.

Secrets: `url` carries the password. Never log or print it; `describe()` and `cache_key` are
the only names a provider gives itself, and neither contains a secret.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, Any

from alembic.runtime.migration import MigrationContext
from sqlalchemy import URL, Engine

if TYPE_CHECKING:
    from teacher.config import Settings
    from teacher.db.search import ConceptSearchBackend


class DbProvider(ABC):
    """One database Teacher can run on (see the module docstring)."""

    #: The kind, as `$TEACHER_DB` names it: "sqlite" or "supabase".
    name: str = "db"
    #: The SQLAlchemy dialect name: "sqlite" or "postgresql".
    dialect: str = ""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    @abstractmethod
    def url(self) -> URL:
        """The SQLAlchemy URL, password included: never log it (use `describe()`)."""

    @property
    @abstractmethod
    def cache_key(self) -> str:
        """Identifies the database within this process (the engine cache, the cost meter, the
        vector-index cache). Contains no secret."""

    @abstractmethod
    def describe(self) -> str:
        """Which database this is, for logs and `teacher db info`. Contains no secret."""

    @abstractmethod
    def create_engine(self, **kwargs: Any) -> Engine:
        """A new (uncached) engine for the app, with this database's per-connection setup;
        `kwargs` go to `sqlalchemy.create_engine` (`connect_args` are merged)."""

    @abstractmethod
    def migration_engine(self) -> Engine:
        """A new engine without pooling for Alembic (`migrations/env.py`) and for reading the
        current revision. The caller disposes of it."""

    def database_exists(self) -> bool:
        """Whether there is a database to read a revision from (a remote one always is; the
        connection then says whether it can be reached)."""
        return True

    def current_revision(self) -> str | None:
        """The Alembic revision the database is at, or None if it is missing or unversioned."""
        if not self.database_exists():
            return None
        engine = self.migration_engine()
        try:
            with engine.connect() as conn:
                return MigrationContext.configure(conn).get_current_revision()
        finally:
            engine.dispose()

    @property
    @abstractmethod
    def concept_search(self) -> ConceptSearchBackend:
        """The concept-search backend of this database (one per provider, so per database)."""

    @abstractmethod
    def backup(self) -> Path | None:
        """Back up the database before pending migrations are applied; the backup's path, or
        None when this provider takes no backup (and says so in the log)."""

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.describe()}>"
