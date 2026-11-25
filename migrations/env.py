"""Alembic environment for Teacher.

Target database resolution (first match wins):
1. `config.attributes["settings"]` - a `teacher.config.Settings` (used by `upgrade_to_head` and tests);
2. `-x root=/path` on the command line: `Settings.from_env()` with that root;
3. `Settings.from_env()` ($TEACHER_HOME, else the cwd; $TEACHER_DB, default supabase).

One migration chain serves SQLite and Postgres; revisions branch on the dialect where the two
differ (see each revision's docstring). The engine comes from the database provider
(`teacher.db.providers`, `migration_engine()`). Optionally `config.attributes["connection"]` may
hold an open SQLAlchemy Connection to use directly. On SQLite the migration engine deliberately
does NOT enable `PRAGMA foreign_keys`: batch (table-rebuild) migrations must run with foreign
keys off, or dropping the old table would cascade. Autogenerate renders batch operations only
for SQLite.

Autogenerate ignores the concept search tables (`teacher.db.fts.is_concept_search_table`: on
SQLite the `concept_search` and `concept_trigram` FTS5 virtual tables and their shadow tables,
on Postgres `concept_search`, `concept_search_tokens` and `concept_trigram`; all created with
raw SQL by 0005, not ORM models); every other table is compared as usual.
"""

import dataclasses
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from alembic.runtime.migration import MigrationContext
from sqlalchemy import JSON, Column
from sqlalchemy.engine import Connection

from teacher.config import Settings
from teacher.db import models  # noqa: F401  (registers all tables on Base.metadata)
from teacher.db.base import Base
from teacher.db.fts import is_concept_search_table
from teacher.db.providers import create_db_provider

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _settings() -> Settings:
    settings = config.attributes.get("settings")
    if settings is not None:
        return settings
    root = context.get_x_argument(as_dictionary=True).get("root")
    if root:
        return dataclasses.replace(Settings.from_env(), root=Path(root))
    return Settings.from_env()


def _include_name(name: str | None, type_: str, _parent_names: object) -> bool:
    return not (type_ == "table" and is_concept_search_table(name))


def _compare_server_default(
    context: MigrationContext,
    _inspected_column: Column,
    metadata_column: Column,
    inspected_default: str | None,
    _metadata_default: object,
    rendered_metadata_default: str | None,
) -> bool | None:
    """On PostgreSQL, compare a json column's literal default as text: Alembic's own comparison
    runs `SELECT '{}'::json = '{}'`, and json has no `=`. The reflected default reads
    `'<literal>'::json`; the model's is the bare literal (`server_default="{}"`). Every other
    column: Alembic's comparison (None)."""
    if context.dialect.name != "postgresql" or not isinstance(metadata_column.type, JSON):
        return None
    if inspected_default is None or rendered_metadata_default is None:
        return (inspected_default is None) != (rendered_metadata_default is None)
    quoted = rendered_metadata_default.replace("'", "''")
    return inspected_default != f"'{quoted}'::json"


def _configure(dialect: str, **kwargs: object) -> None:
    context.configure(
        target_metadata=target_metadata,
        include_name=_include_name,
        render_as_batch=dialect == "sqlite",
        compare_type=True,
        compare_server_default=_compare_server_default,
        **kwargs,
    )


def run_migrations_offline() -> None:
    provider = create_db_provider(_settings())
    _configure(
        provider.dialect,
        url=provider.url,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _run_with_connection(connection: Connection) -> None:
    _configure(connection.dialect.name, connection=connection)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        _run_with_connection(connection)
        return
    settings = _settings()
    settings.root.mkdir(parents=True, exist_ok=True)
    engine = create_db_provider(settings).migration_engine()
    try:
        with engine.connect() as conn:
            _run_with_connection(conn)
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
