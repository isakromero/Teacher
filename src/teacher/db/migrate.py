"""Programmatic Alembic upgrades, with the database provider's backup taken before any pending
migration (a file copy for SQLite; none for Supabase, which logs that its own backups apply).

The Alembic `Config` is built in code from absolute paths, so this works from any cwd. The same
migration chain serves both databases (`migrations/env.py`).
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from teacher.config import Settings
from teacher.db.providers import create_db_provider

# src/teacher/db/migrate.py -> project root is three levels above the package directory.
PROJECT_ROOT = Path(__file__).resolve().parents[3]
MIGRATIONS_DIR = PROJECT_ROOT / "migrations"


def alembic_config(settings: Settings) -> Config:
    """Build an Alembic Config targeting `settings` (env.py reads `attributes["settings"]`)."""
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("path_separator", "os")
    cfg.attributes["settings"] = settings
    return cfg


def head_revision(settings: Settings) -> str | None:
    """The head revision of the migration scripts."""
    return ScriptDirectory.from_config(alembic_config(settings)).get_current_head()


def current_revision(settings: Settings) -> str | None:
    """The revision the database is at, or None if missing/unversioned."""
    return create_db_provider(settings).current_revision()


def backup_database(settings: Settings) -> Path | None:
    """The provider's pre-migration backup: for SQLite a copy of the file in
    `backups/teacher-<UTC timestamp>.db` (SQLite's backup API); for Supabase none (None)."""
    return create_db_provider(settings).backup()


def upgrade_to_head(settings: Settings) -> Path | None:
    """Create the data dirs, back up an existing db if migrations are pending, upgrade to head.

    Returns the backup path if a backup was made, else None (always None on Supabase).
    """
    for d in (
        settings.root,
        settings.data_root,
        settings.sources_dir,
        settings.artifacts_dir,
        settings.derived_dir,
        settings.backups_dir,
    ):
        d.mkdir(parents=True, exist_ok=True)

    backup: Path | None = None
    provider = create_db_provider(settings)
    if provider.database_exists() and provider.current_revision() != head_revision(settings):
        backup = provider.backup()

    command.upgrade(alembic_config(settings), "head")
    return backup
