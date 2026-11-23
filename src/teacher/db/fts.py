"""The concept search indexes: names, availability checks and error translation.

Two SQLite FTS5 virtual tables, both created by migration 0005 with raw SQL (they are not ORM
models), kept in sync by the concept manager in the same savepoint as every concept change, and
hidden from Alembic's autogenerate together with their shadow tables (`is_concept_search_table`,
used by migrations/env.py):

- `concept_search` - the word index. rowid = concepts.id; columns name, aliases (newline
  separated), summary; tokenizer `unicode61 remove_diacritics 2`; prefix indexes for 2, 3 and 4
  characters, so prefix queries are served from the index.
- `concept_trigram` - the trigram index over names and aliases (not summaries). One row per name
  or alias: rowid = concept_id * TRIGRAM_SLOTS + slot (slot 0 the name, 1.. the aliases in
  alias order); columns `text` (the folded name/alias, `teacher.concept_search.fold`),
  `compact` (`text` without separators) and `label` (UNINDEXED: the name or alias as stored);
  tokenizer `trigram case_sensitive 1` (the text is folded in Python, so the tokenizer must not
  fold again). The trigram tokenizer needs SQLite >= 3.34.

Nothing degrades to LIKE scans or a weaker search: a SQLite build without FTS5 or without the
trigram tokenizer, or a database without either table, raises `ConceptSearchUnavailableError`.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy.exc import OperationalError

CONCEPT_SEARCH_TABLE = "concept_search"
CONCEPT_TRIGRAM_TABLE = "concept_trigram"
# rowids of concept_trigram: concept_id * TRIGRAM_SLOTS + slot, so a concept owns one rowid range.
TRIGRAM_SLOTS = 1 << 16
# The tables FTS5 creates for a (non-contentless) table named X: X_data, X_idx, ... .
FTS5_SHADOW_SUFFIXES: tuple[str, ...] = ("_data", "_idx", "_content", "_docsize", "_config")
CONCEPT_SEARCH_TABLES: frozenset[str] = frozenset(
    table + suffix
    for table in (CONCEPT_SEARCH_TABLE, CONCEPT_TRIGRAM_TABLE)
    for suffix in ("", *FTS5_SHADOW_SUFFIXES)
)

class ConceptSearchUnavailableError(RuntimeError):
    """Concept search cannot work: SQLite lacks FTS5 or its trigram tokenizer, or a search
    table is missing."""


def is_concept_search_table(name: str | None) -> bool:
    """Whether `name` is one of the two search tables or one of their FTS5 shadow tables."""
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


@contextmanager
def fts_errors() -> Iterator[None]:
    """Turn SQLite's "no FTS5" / "no trigram tokenizer" / "no such search table" errors into a
    clear ConceptSearchUnavailableError."""
    try:
        yield
    except OperationalError as exc:
        detail = str(exc.orig)
        if "no such module: fts5" in detail:
            raise ConceptSearchUnavailableError(_no_fts5_message()) from exc
        if "no such tokenizer: trigram" in detail:
            raise ConceptSearchUnavailableError(_no_trigram_message()) from exc
        for table, what in (
            (CONCEPT_SEARCH_TABLE, "full-text"),
            (CONCEPT_TRIGRAM_TABLE, "trigram"),
        ):
            if f"no such table: {table}" in detail:
                raise ConceptSearchUnavailableError(
                    f"the {table} {what} table is missing: this database was not created by "
                    "the migrations (run teacher.db.migrate.upgrade_to_head)"
                ) from exc
        raise


__all__ = [
    "CONCEPT_SEARCH_TABLE",
    "CONCEPT_SEARCH_TABLES",
    "CONCEPT_TRIGRAM_TABLE",
    "FTS5_SHADOW_SUFFIXES",
    "TRIGRAM_SLOTS",
    "ConceptSearchUnavailableError",
    "fts_errors",
    "is_concept_search_table",
    "require_concept_search",
    "require_fts5",
    "require_trigram",
]
