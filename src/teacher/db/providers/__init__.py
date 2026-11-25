"""Database providers: the local SQLite file or Supabase's Postgres (`teacher.db.providers.base`).

`create_db_provider(settings)` returns the provider for `settings.db`. `SupabaseDbProvider` is
imported lazily, so sqlite mode never imports psycopg or needs `$SUPABASE_DB_URL`.

The kind comes from `$TEACHER_DB` (`supabase`, the default when unset, or `sqlite`; `teacher
--db` overrides it) through `Settings.from_env()`; `DB_KINDS`, `DEFAULT_DB_KIND` and
`default_db_kind()` live in `teacher.config` (the one place environment variables are read) and
are re-exported here.
"""

from __future__ import annotations

from functools import lru_cache

from teacher.config import DB_KINDS, DEFAULT_DB_KIND, ConfigError, Settings, default_db_kind
from teacher.db.providers.base import DbProvider
from teacher.db.providers.sqlite import SqliteDbProvider


@lru_cache(maxsize=256)
def create_db_provider(settings: Settings) -> DbProvider:
    """The provider for `settings.db` (cached per settings)."""
    if settings.db == "sqlite":
        return SqliteDbProvider(settings)
    if settings.db == "supabase":
        from teacher.db.providers.supabase import SupabaseDbProvider

        return SupabaseDbProvider(settings)
    raise ConfigError(f"unknown database kind {settings.db!r}; choose one of: {', '.join(DB_KINDS)}")


__all__ = [
    "DB_KINDS",
    "DEFAULT_DB_KIND",
    "DbProvider",
    "SqliteDbProvider",
    "create_db_provider",
    "default_db_kind",
]
