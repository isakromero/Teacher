"""Concept-search backends: how each database stores and queries the concept search indexes.

- `teacher.db.search.base`: the interface (`ConceptSearchBackend`, `WordMatch`, `TrigramRow`,
  `WordIndexRows`).
- `teacher.db.search.sqlite`: `SqliteConceptSearch`, the FTS5 tables.
- `teacher.db.search.postgres`: `PostgresConceptSearch`, plain tables with a word table and
  pg_trgm indexes, giving the same hits, order and scores.
- `teacher.db.search.unicode61`: FTS5's unicode61 tokenizer in Python (for Postgres).

The database provider names its backend (`DbProvider.concept_search`); `ConceptManager` uses
it. Only the interface is imported here, so importing this package loads no backend.
"""

from teacher.db.search.base import ConceptSearchBackend, TrigramRow, WordIndexRows, WordMatch

__all__ = ["ConceptSearchBackend", "TrigramRow", "WordIndexRows", "WordMatch"]
