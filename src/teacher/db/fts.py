"""The concept search indexes: names, availability checks and error translation.

Two indexes, created by migration 0005 with raw SQL (they are not ORM models), kept in sync by
the concept manager in the same savepoint as every concept change, and hidden from Alembic's
autogenerate (`is_concept_search_table`, used by migrations/env.py). How each database stores
and queries them is its concept-search backend's business (`teacher.db.search`); both give the
same hits in the same order.

On SQLite, two FTS5 virtual tables (hidden from autogenerate with their shadow tables):

- `concept_search` - the word index. rowid = concepts.id; columns name, aliases (newline
  separated), summary; tokenizer `unicode61 remove_diacritics 2`; prefix indexes for 2, 3 and 4
  characters, so prefix queries are served from the index.
- `concept_trigram` - the trigram index over names and aliases (not summaries). One row per name
  or alias: rowid = concept_id * TRIGRAM_SLOTS + slot (slot 0 the name, 1.. the aliases in
  alias order); columns `text` (the folded name/alias, `teacher.concept_search.fold`),
  `compact` (`text` without separators) and `label` (UNINDEXED: the name or alias as stored);
  tokenizer `trigram case_sensitive 1` (the text is folded in Python, so the tokenizer must not
  fold again). The trigram tokenizer needs SQLite >= 3.34.

On PostgreSQL, plain tables with the same names and columns (`rowid` is an ordinary bigint
column there), plus one for the words:

- `concept_search` (rowid, name, aliases, summary) and `concept_search_tokens` (rowid, col,
  pos, token): the words of each column as FTS5's unicode61 tokenizer makes them
  (`teacher.db.search.unicode61`), with a b-tree index on their first `TOKEN_KEY_CHARS`
  characters (collation "C") for whole-word and prefix lookups.
- `concept_trigram` (rowid, text, compact, label) with pg_trgm GIN indexes on `text` and
  `compact`, which serve the substring lookups (`LIKE '%...%'`).

Nothing degrades to LIKE scans or a weaker search: a SQLite build without FTS5 or without the
trigram tokenizer, a Postgres without the pg_trgm extension or the trigram indexes, or a
database without one of the tables, raises `ConceptSearchUnavailableError`.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy.exc import DBAPIError, OperationalError

CONCEPT_SEARCH_TABLE = "concept_search"
CONCEPT_TRIGRAM_TABLE = "concept_trigram"
# Postgres only: the words of the concept_search rows (see the module docstring).
CONCEPT_SEARCH_TOKENS_TABLE = "concept_search_tokens"
# rowids of concept_trigram: concept_id * TRIGRAM_SLOTS + slot, so a concept owns one rowid range.
TRIGRAM_SLOTS = 1 << 16
# The aliases column of concept_search holds a concept's aliases joined by this.
ALIAS_SEPARATOR = "\n"
# bm25 column weights for (name, aliases, summary): a match in the name counts most.
SEARCH_WEIGHTS = (10.0, 5.0, 1.0)
# Postgres: the b-tree on concept_search_tokens indexes this many leading characters of a token
# (a b-tree entry must stay under ~2.7 kB; a whole token can be up to 32 kB). Must equal the
# `substr(token, 1, 200)` of migration 0005's index, or the queries could not use it.
TOKEN_KEY_CHARS = 200
# Postgres: the extension the trigram indexes need, and the schema Supabase keeps extensions in.
PG_TRGM = "pg_trgm"
SUPABASE_EXTENSIONS_SCHEMA = "extensions"
# The tables FTS5 creates for a (non-contentless) table named X: X_data, X_idx, ... .
FTS5_SHADOW_SUFFIXES: tuple[str, ...] = ("_data", "_idx", "_content", "_docsize", "_config")
CONCEPT_SEARCH_TABLES: frozenset[str] = frozenset(
    {
        *(
            table + suffix
            for table in (CONCEPT_SEARCH_TABLE, CONCEPT_TRIGRAM_TABLE)
            for suffix in ("", *FTS5_SHADOW_SUFFIXES)
        ),
        CONCEPT_SEARCH_TOKENS_TABLE,
    }
)
# What each table is, for "table is missing" messages.
TABLE_KINDS = {
    CONCEPT_SEARCH_TABLE: "full-text",
    CONCEPT_SEARCH_TOKENS_TABLE: "full-text",
    CONCEPT_TRIGRAM_TABLE: "trigram",
}


class ConceptSearchUnavailableError(RuntimeError):
    """Concept search cannot work: SQLite lacks FTS5 or its trigram tokenizer, Postgres lacks
    the pg_trgm extension or a trigram index, or a search table is missing."""


def missing_table_message(table: str) -> str:
    """The message for a search table that does not exist."""
    return (
        f"the {table} {TABLE_KINDS[table]} table is missing: this database was not created by "
        "the migrations (run teacher.db.migrate.upgrade_to_head)"
    )


def is_concept_search_table(name: str | None) -> bool:
    """Whether `name` is one of the search tables: SQLite's two with their FTS5 shadow tables,
    or Postgres's three."""
    return name in CONCEPT_SEARCH_TABLES


def _no_fts5_message() -> str:
    return (
        f"this SQLite build (version {sqlite3.sqlite_version}) has no FTS5 module, which the "
        "concept search needs; use a Python/SQLite built with FTS5 (the standard builds are)"
    )


def _no_trigram_message() -> str:
    return (
        f"this SQLite build (version {sqlite3.sqlite_version}) has no FTS5 trigram tokenizer, "
        "which the concept search's substring and typo-tolerant matching needs; it was added in "
        "SQLite 3.34.0: use a Python/SQLite with SQLite 3.34 or newer"
    )


def require_fts5(connection: Any) -> None:
    """Raise ConceptSearchUnavailableError unless `connection` (SQLAlchemy) can use FTS5.

    Creates and drops a probe table in the connection's `temp` schema, so it changes nothing.
    """
    try:
        connection.exec_driver_sql("CREATE VIRTUAL TABLE temp._teacher_fts5_probe USING fts5(x)")
    except OperationalError as exc:
        if "no such module: fts5" in str(exc.orig):
            raise ConceptSearchUnavailableError(_no_fts5_message()) from exc
        raise
    connection.exec_driver_sql("DROP TABLE temp._teacher_fts5_probe")


def require_trigram(connection: Any) -> None:
    """Raise ConceptSearchUnavailableError unless `connection` (SQLAlchemy) has the FTS5
    trigram tokenizer with the `case_sensitive` option (SQLite >= 3.34). Call `require_fts5`
    first. Uses a probe table in the `temp` schema, so it changes nothing."""
    try:
        connection.exec_driver_sql(
            "CREATE VIRTUAL TABLE temp._teacher_trigram_probe USING fts5("
            "x, tokenize = 'trigram case_sensitive 1')"
        )
    except OperationalError as exc:
        if "no such tokenizer: trigram" in str(exc.orig):
            raise ConceptSearchUnavailableError(_no_trigram_message()) from exc
        raise
    connection.exec_driver_sql("DROP TABLE temp._teacher_trigram_probe")


def require_concept_search(connection: Any) -> None:
    """`require_fts5` and `require_trigram`: everything the two search tables need."""
    require_fts5(connection)
    require_trigram(connection)


def require_pg_trgm(connection: Any) -> str:
    """Make sure the pg_trgm extension is installed on a Postgres `connection` (SQLAlchemy)
    and return the schema it is in, for qualifying `gin_trgm_ops`.

    An installed pg_trgm is used wherever it is. Otherwise it is created in the `extensions`
    schema when there is one (Supabase's convention), else in the current schema. pg_trgm is a
    trusted extension: any role with CREATE privilege on the database (Supabase's `postgres`
    role has it) may install it. ConceptSearchUnavailableError when the server does not offer
    pg_trgm or the role may not install it.
    """
    find = (
        "SELECT n.nspname FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace "
        f"WHERE e.extname = '{PG_TRGM}'"
    )
    schema = connection.exec_driver_sql(find).scalar()
    if schema is not None:
        return str(schema)
    offered = connection.exec_driver_sql(
        f"SELECT count(*) FROM pg_available_extensions WHERE name = '{PG_TRGM}'"
    ).scalar()
    if not offered:
        raise ConceptSearchUnavailableError(
            "this PostgreSQL server does not offer the pg_trgm extension, which the concept "
            "search's substring and typo-tolerant matching needs; install PostgreSQL's contrib "
            "modules on the server (Supabase always offers it)"
        )
    supabase_schema = connection.exec_driver_sql(
        f"SELECT count(*) FROM pg_namespace WHERE nspname = '{SUPABASE_EXTENSIONS_SCHEMA}'"
    ).scalar()
    statement = f"CREATE EXTENSION IF NOT EXISTS {PG_TRGM}"
    if supabase_schema:
        statement += f" WITH SCHEMA {SUPABASE_EXTENSIONS_SCHEMA}"
    try:
        connection.exec_driver_sql(statement)
    except DBAPIError as exc:
        reason = str(exc.orig).strip().splitlines()[0] if exc.orig is not None else str(exc)
        raise ConceptSearchUnavailableError(
            f"could not install the pg_trgm extension ({reason}), which the concept search "
            "needs. It needs CREATE privilege on the database (pg_trgm is a trusted "
            "extension); have a role that has it run "
            f"`{statement}` (on Supabase: the postgres role, or Database > Extensions in the "
            "dashboard), then migrate again"
        ) from exc
    schema = connection.exec_driver_sql(find).scalar()
    if schema is None:
        raise ConceptSearchUnavailableError(f"`{statement}` succeeded but pg_trgm is not installed")
    return str(schema)


@contextmanager
def fts_errors() -> Iterator[None]:
    """Turn SQLite's "no FTS5" / "no trigram tokenizer" / "no such search table" errors into a
    clear ConceptSearchUnavailableError (the SQLite backend's `errors()`)."""
    try:
        yield
    except OperationalError as exc:
        detail = str(exc.orig)
        if "no such module: fts5" in detail:
            raise ConceptSearchUnavailableError(_no_fts5_message()) from exc
        if "no such tokenizer: trigram" in detail:
            raise ConceptSearchUnavailableError(_no_trigram_message()) from exc
        for table in (CONCEPT_SEARCH_TABLE, CONCEPT_TRIGRAM_TABLE):
            if f"no such table: {table}" in detail:
                raise ConceptSearchUnavailableError(missing_table_message(table)) from exc
        raise


__all__ = [
    "ALIAS_SEPARATOR",
    "CONCEPT_SEARCH_TABLE",
    "CONCEPT_SEARCH_TABLES",
    "CONCEPT_SEARCH_TOKENS_TABLE",
    "CONCEPT_TRIGRAM_TABLE",
    "FTS5_SHADOW_SUFFIXES",
    "PG_TRGM",
    "SEARCH_WEIGHTS",
    "SUPABASE_EXTENSIONS_SCHEMA",
    "TABLE_KINDS",
    "TOKEN_KEY_CHARS",
    "TRIGRAM_SLOTS",
    "ConceptSearchUnavailableError",
    "fts_errors",
    "is_concept_search_table",
    "missing_table_message",
    "require_concept_search",
    "require_fts5",
    "require_pg_trgm",
    "require_trigram",
]
