"""The concept search on SQLite: the two FTS5 tables of `teacher.db.fts`.

Word matches are FTS5 MATCH queries (`teacher.concept_search.word_expression`) ranked by
`bm25(concept_search, 10, 5, 1)`; `highlight()` with private markers tells which column (and
which alias) a hit matched in. Trigram rows come from MATCH queries on `concept_trigram`
(`substring_expression`, `trigram_expression`). Errors: `teacher.db.fts.fts_errors`.
"""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import AbstractContextManager

from sqlalchemy import text
from sqlalchemy.orm import Session

from teacher.concept_search import FIELD_ALIAS, FIELD_NAME, FIELD_SUMMARY, quote, word_expression
from teacher.db.fts import ALIAS_SEPARATOR, SEARCH_WEIGHTS, TRIGRAM_SLOTS, fts_errors
from teacher.db.search.base import ConceptSearchBackend, TrigramRow, WordIndexRows, WordMatch

# highlight() markers for the word index; only used to see which column matched, never shown.
_MARK_OPEN, _MARK_CLOSE = "[", "]"


def _or(strings: Sequence[str]) -> str:
    quoted = [quote(s) for s in strings]
    return quoted[0] if len(quoted) == 1 else f"({' OR '.join(quoted)})"


class SqliteConceptSearch(ConceptSearchBackend):
    dialect = "sqlite"

    def errors(self) -> AbstractContextManager[None]:
        return fts_errors()

    def word_matches(
        self, session: Session, terms: Sequence[str], *, prefix: bool, limit: int
    ) -> list[WordMatch]:
        expression = word_expression(terms, prefix=prefix)
        weights = ", ".join(str(w) for w in SEARCH_WEIGHTS)
        mark = f"'{_MARK_OPEN}', '{_MARK_CLOSE}'"
        rows = session.execute(
            text(
                f"SELECT rowid, bm25(concept_search, {weights}) AS rank, name, aliases, "
                f"highlight(concept_search, 0, {mark}), "
                f"highlight(concept_search, 1, {mark}) "
                "FROM concept_search WHERE concept_search MATCH :q "
                "ORDER BY rank, rowid LIMIT :limit"
            ),
            {"q": expression, "limit": limit},
        ).all()
        hits = []
        for rowid, rank, name, aliases, marked_name, marked_aliases in rows:
            field, matched = FIELD_SUMMARY, None
            if marked_name != name:
                field, matched = FIELD_NAME, name
            else:
                pairs = zip(
                    aliases.split(ALIAS_SEPARATOR),
                    marked_aliases.split(ALIAS_SEPARATOR),
                    strict=True,
                )
                alias = next((a for a, marked in pairs if marked != a), None)
                if alias is not None:
                    field, matched = FIELD_ALIAS, alias
            hits.append(WordMatch(rowid, rank, field, matched))
        return hits

    def _match_trigrams(self, session: Session, expression: str) -> list[TrigramRow]:
        rows = session.execute(
            text(
                "SELECT rowid, text, compact, label FROM concept_trigram "
                "WHERE concept_trigram MATCH :q"
            ),
            {"q": expression},
        ).all()
        return sorted(tuple(row) for row in rows)  # type: ignore[misc]

    def substring_rows(self, session: Session, clauses: Sequence[Sequence[str]]) -> list[TrigramRow]:
        return self._match_trigrams(session, " AND ".join(_or(c) for c in clauses))

    def fuzzy_rows(self, session: Session, grams: Sequence[str]) -> list[TrigramRow]:
        return self._match_trigrams(session, " OR ".join(quote(g) for g in grams))

    def word_rows(self, session: Session) -> WordIndexRows:
        return WordIndexRows(
            {
                rowid: (name, aliases, summary)
                for rowid, name, aliases, summary in session.execute(
                    text("SELECT rowid, name, aliases, summary FROM concept_search")
                )
            }
        )

    def trigram_rows(self, session: Session) -> list[TrigramRow]:
        return [
            tuple(row)  # type: ignore[misc]
            for row in session.execute(
                text("SELECT rowid, text, compact, label FROM concept_trigram ORDER BY rowid")
            )
        ]

    def insert(
        self,
        session: Session,
        concept_id: int,
        word_row: tuple[str, str, str],
        trigram_rows: Sequence[TrigramRow],
    ) -> None:
        name, aliases, summary = word_row
        session.execute(
            text(
                "INSERT INTO concept_search (rowid, name, aliases, summary) "
                "VALUES (:id, :name, :aliases, :summary)"
            ),
            {"id": concept_id, "name": name, "aliases": aliases, "summary": summary},
        )
        session.execute(
            text(
                "INSERT INTO concept_trigram (rowid, text, compact, label) "
                "VALUES (:rowid, :text, :compact, :label)"
            ),
            [
                {"rowid": rowid, "text": row_text, "compact": row_compact, "label": label}
                for rowid, row_text, row_compact, label in trigram_rows
            ],
        )

    def delete(self, session: Session, concept_id: int) -> None:
        session.execute(text("DELETE FROM concept_search WHERE rowid = :id"), {"id": concept_id})
        session.execute(
            text("DELETE FROM concept_trigram WHERE rowid >= :low AND rowid < :high"),
            {"low": concept_id * TRIGRAM_SLOTS, "high": (concept_id + 1) * TRIGRAM_SLOTS},
        )

    def clear(self, session: Session) -> None:
        session.execute(text("DELETE FROM concept_search"))
        session.execute(text("DELETE FROM concept_trigram"))
