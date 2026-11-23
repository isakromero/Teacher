"""Programmatic Alembic upgrades, with a database backup taken before any pending migration.

The Alembic `Config` is built in code from absolute paths, so this works from any cwd.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

from teacher.config import Settings

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
    if not settings.db_path.exists():
        return None
    engine = create_engine(settings.db_url, poolclass=NullPool)
    try:
        with engine.connect() as conn:
            return MigrationContext.configure(conn).get_current_revision()
    finally:
        engine.dispose()


def backup_database(settings: Settings) -> Path:
    """Copy the database to `backups/teacher-<UTC timestamp>.db` using SQLite's backup API."""
    settings.backups_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S_%fZ")
    target = settings.backups_dir / f"teacher-{stamp}.db"
    src = sqlite3.connect(settings.db_path)
    try:
        dst = sqlite3.connect(target)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return target


def upgrade_to_head(settings: Settings) -> Path | None:
    """Create the data dirs, back up an existing db if migrations are pending, upgrade to head.

    Returns the backup path if a backup was made, else None.
    """
    for d in (
        settings.root,
        settings.sources_dir,
        settings.artifacts_dir,
        settings.derived_dir,
        settings.backups_dir,
    ):
        d.mkdir(parents=True, exist_ok=True)

    backup: Path | None = None
    if settings.db_path.exists() and current_revision(settings) != head_revision(settings):
        backup = backup_database(settings)

    command.upgrade(alembic_config(settings), "head")
    return backup
