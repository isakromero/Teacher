"""Alembic environment for Teacher.

Target database resolution (first match wins):
1. `config.attributes["settings"]` - a `teacher.config.Settings` (used by `upgrade_to_head` and tests);
2. `-x root=/path` on the command line;
3. `Settings.from_env()` ($TEACHER_HOME, else the cwd).

Optionally `config.attributes["connection"]` may hold an open SQLAlchemy Connection to use directly.
The migration engine deliberately does NOT enable `PRAGMA foreign_keys`: batch (table-rebuild)
migrations on SQLite must run with foreign keys off, or dropping the old table would cascade.

Autogenerate ignores the `concept_search` and `concept_trigram` FTS5 virtual tables and their
shadow tables (created with raw SQL by 0005, not ORM models); every other table is compared as
usual.
"""

from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import create_engine
from sqlalchemy.engine import Connection
from sqlalchemy.pool import NullPool

from teacher.config import Settings
from teacher.db import models  # noqa: F401  (registers all tables on Base.metadata)
from teacher.db.base import Base
from teacher.db.fts import is_concept_search_table

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
        return Settings(root=Path(root))
    return Settings.from_env()


def _include_name(name: str | None, type_: str, _parent_names: object) -> bool:
    return not (type_ == "table" and is_concept_search_table(name))


def _configure(**kwargs: object) -> None:
    context.configure(
        target_metadata=target_metadata,
        include_name=_include_name,
        render_as_batch=True,
        compare_type=True,
        compare_server_default=True,
        **kwargs,
    )


def run_migrations_offline() -> None:
    _configure(url=_settings().db_url, literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def _run_with_connection(connection: Connection) -> None:
    _configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        _run_with_connection(connection)
        return
    settings = _settings()
    settings.root.mkdir(parents=True, exist_ok=True)
    engine = create_engine(settings.db_url, poolclass=NullPool)
    try:
        with engine.connect() as conn:
            _run_with_connection(conn)
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
