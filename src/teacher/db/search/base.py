"""The concept-search backend interface: how one database stores and queries the two search
indexes (`teacher.db.fts` describes them).

`ConceptManager` (teacher.managers.concepts) owns the matching rules and the ranking of the
substring and fuzzy tiers (`teacher.concept_search`); a backend owns only what is SQL: writing
and deleting a concept's index rows, finding word-index matches ranked by bm25, and fetching
the trigram-index rows a substring or fuzzy query can match. The database provider names the
backend (`DbProvider.concept_search`): `SqliteConceptSearch` (FTS5) or `PostgresConceptSearch`.
Both return the same hits in the same order for the same rows (tests/test_concept_search.py).

Every method runs on the caller's session, inside the caller's transaction; the manager wraps
index writes in the savepoint of the change they belong to. Errors meaning that the search
cannot work at all (a missing table, an extension or SQLite feature that is not there) leave
`errors()` as `ConceptSearchUnavailableError`; nothing falls back to a weaker search.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

#: A concept_trigram row: (rowid = concept_id * TRIGRAM_SLOTS + slot, folded text, compact
#: text, label).
TrigramRow = tuple[int, str, str, str]


@dataclass(frozen=True, slots=True)
class WordMatch:
    """A word-index hit: `rank` is FTS5's bm25 with `SEARCH_WEIGHTS` (lower is better: it is
    negative); `field` is 'name' or 'alias' when a matched word is in the name or an alias
    (`matched` is that name or alias, the first alias in alias order), else 'summary'."""

    concept_id: int
    rank: float
    field: str
    matched: str | None


@dataclass(frozen=True, slots=True)
class WordIndexRows:
    """The word index as stored, for `ConceptManager.check`: `rows` maps each rowid to its
    (name, aliases, summary); `inconsistent` holds the rowids whose index data (Postgres: their
    tokens) does not match their own text, or that have index data but no row."""

    rows: dict[int, tuple[str, str, str]]
    inconsistent: frozenset[int] = field(default_factory=frozenset)


class ConceptSearchBackend(ABC):
    """The search indexes on one kind of database (see the module docstring)."""

    #: The SQLAlchemy dialect this backend's SQL is for.
    dialect: str = ""

    @abstractmethod
    def errors(self) -> AbstractContextManager[None]:
        """A context in which this database's "the search cannot work" errors become
        `ConceptSearchUnavailableError` with a message saying what is missing."""

    # -- reads ------------------------------------------------------------------------------

    @abstractmethod
    def word_matches(
        self, session: Session, terms: Sequence[str], *, prefix: bool, limit: int
    ) -> list[WordMatch]:
        """The best `limit` word-index hits for `terms` (`ParsedQuery.word_terms`: each a
        phrase, all required; with `prefix` as `teacher.concept_search.word_expression`
        says), best first: by rank, then concept id. `terms` is not empty."""

    @abstractmethod
    def substring_rows(self, session: Session, clauses: Sequence[Sequence[str]]) -> list[TrigramRow]:
        """Every trigram-index row in which, for each clause, some string of the clause occurs
        in `text` or in `compact` (`teacher.concept_search.substring_clauses`), by rowid.
        Every string has at least `TRIGRAM` characters; `clauses` is not empty."""

    @abstractmethod
    def fuzzy_rows(self, session: Session, grams: Sequence[str]) -> list[TrigramRow]:
        """Every trigram-index row whose `text` or `compact` contains one of `grams`
        (`teacher.concept_search.trigrams`, not empty), by rowid."""

    @abstractmethod
    def word_rows(self, session: Session) -> WordIndexRows:
        """The word index as stored (see `WordIndexRows`)."""

    @abstractmethod
    def trigram_rows(self, session: Session) -> list[TrigramRow]:
        """Every trigram-index row, by rowid."""

    # -- writes -----------------------------------------------------------------------------

    @abstractmethod
    def insert(
        self,
        session: Session,
        concept_id: int,
        word_row: tuple[str, str, str],
        trigram_rows: Sequence[TrigramRow],
    ) -> None:
        """Add a concept's rows: its word-index row (name, aliases newline-separated, summary)
        and its trigram rows. The concept must have no rows yet."""

    @abstractmethod
    def delete(self, session: Session, concept_id: int) -> None:
        """Delete a concept's rows from both indexes (none is fine)."""

    @abstractmethod
    def clear(self, session: Session) -> None:
        """Delete every row of both indexes."""

    def __repr__(self) -> str:
        return f"<{type(self).__name__}>"
