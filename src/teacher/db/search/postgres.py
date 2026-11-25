"""The concept search on PostgreSQL: the same hits, order and scores as SQLite's FTS5.

The tables (`teacher.db.fts`, created by migration 0005) are plain tables:

- `concept_search` (rowid, name, aliases, summary): the word index's rows, as on SQLite.
- `concept_search_tokens` (rowid, col, pos, token): every word of every column (col 0 name,
  1 aliases, 2 summary; pos counts from 0 per column), made in Python by the same rules as
  FTS5's `unicode61 remove_diacritics 2` tokenizer (`teacher.db.search.unicode61`), so a
  query's words and the stored ones are the same strings on both databases. A b-tree index on
  the first `TOKEN_KEY_CHARS` characters (collation "C", so `LIKE 'grad%'` uses it) serves
  whole-word and prefix lookups.
- `concept_trigram` (rowid, text, compact, label): as on SQLite, with pg_trgm GIN indexes on
  `text` and `compact` serving `LIKE '%dient%'`.

Word matches follow FTS5's semantics: each query term is a phrase (its tokens at consecutive
positions of one column; aliases are one column, as in FTS5), the last token a prefix when the
term asks for it; every phrase is required; a phrase with no tokens is ignored, and a query of
only such phrases matches nothing. Rank is FTS5's bm25, computed here in Python with FTS5's
formula, constants and order of operations (k1 = 1.2, b = 0.75, IDF = ln((N - n + 0.5) /
(n + 0.5)), at least 1e-6; each phrase instance adds its column's weight to the phrase's
frequency; the row length is its token count over all columns, the average over all rows),
including the two fused multiply-adds the compiled FTS5 performs (clang fuses `x + k1 * y` and
`score + idf * t`, as in the arm64 builds of Python's SQLite this project runs on; measured: with
them all of 61,127 ranks are bit-identical, without them 79 %). So ranks are the same doubles as
SQLite's and sort the same, ties by rowid. A SQLite build that does not fuse them (e.g. a
typical x86-64 build) differs in the last bit, which can only swap two hits whose ranks are one
unit in the last place apart.
The matched column (and alias) is the one holding a matched token, as FTS5's highlight() shows.

Trigram rows: a substring clause or a fuzzy trigram matches a row whose `text` or `compact`
contains it (`LIKE '%...%'`), as FTS5's trigram tokenizer (case_sensitive 1) does; the manager
scores them in Python on both databases.

Nothing falls back to a scan: before the first query the backend checks, once per database it
serves, that the three indexes exist and are valid, and raises ConceptSearchUnavailableError
naming the missing ones (the trigram ones need the pg_trgm extension). A missing table is
ConceptSearchUnavailableError too (`errors()`).
"""

from __future__ import annotations

import math
import re
import threading
from collections.abc import Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session

from teacher.concept_search import FIELD_ALIAS, FIELD_NAME, FIELD_SUMMARY, word_phrases
from teacher.db.fts import (
    ALIAS_SEPARATOR,
    CONCEPT_SEARCH_TOKENS_TABLE,
    CONCEPT_TRIGRAM_TABLE,
    SEARCH_WEIGHTS,
    TABLE_KINDS,
    TOKEN_KEY_CHARS,
    TRIGRAM_SLOTS,
    ConceptSearchUnavailableError,
    missing_table_message,
)
from teacher.db.search.base import ConceptSearchBackend, TrigramRow, WordIndexRows, WordMatch
from teacher.db.search.unicode61 import tokenize, tokens
from teacher.managers.errors import IntegrityCheckError

# FTS5's bm25 constants (fts5_aux.c).
BM25_K1 = 1.2
BM25_B = 0.75
BM25_MIN_IDF = 1e-6

COL_NAME, COL_ALIASES, COL_SUMMARY = 0, 1, 2

#: The indexes the search needs: name -> (its table, how to make it, as migration 0005 does).
INDEXES: dict[str, tuple[str, str]] = {
    "ix_concept_search_tokens_key": (
        CONCEPT_SEARCH_TOKENS_TABLE,
        f"CREATE INDEX ix_concept_search_tokens_key ON {CONCEPT_SEARCH_TOKENS_TABLE} "
        f"(substr(token, 1, {TOKEN_KEY_CHARS}))",
    ),
    "ix_concept_trigram_text": (
        CONCEPT_TRIGRAM_TABLE,
        f"CREATE INDEX ix_concept_trigram_text ON {CONCEPT_TRIGRAM_TABLE} "
        "USING gin (text gin_trgm_ops)",
    ),
    "ix_concept_trigram_compact": (
        CONCEPT_TRIGRAM_TABLE,
        f"CREATE INDEX ix_concept_trigram_compact ON {CONCEPT_TRIGRAM_TABLE} "
        "USING gin (compact gin_trgm_ops)",
    ),
}

_UNDEFINED_TABLE = "42P01"
_RELATION = re.compile(r'relation "([^"]+)" does not exist')


def _like_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def column_tokens(word_row: tuple[str, str, str]) -> list[tuple[int, int, str]]:
    """The (col, pos, token) rows of a concept_search row (name, aliases, summary)."""
    return [
        (col, pos, token)
        for col, value in enumerate(word_row)
        for pos, token in enumerate(tokens(value))
    ]


_SPLITTER = 134217729.0  # 2**27 + 1, Veltkamp's splitting constant for doubles


def _split(a: float) -> tuple[float, float]:
    t = _SPLITTER * a
    high = t - (t - a)
    return high, a - high


def fma(a: float, b: float, c: float) -> float:
    """a * b + c rounded once, as a fused multiply-add (math.fma needs Python 3.13): a * b is
    split exactly into p + e (Dekker's product), and fsum rounds p + e + c correctly."""
    p = a * b
    a_high, a_low = _split(a)
    b_high, b_low = _split(b)
    e = ((a_high * b_high - p) + a_high * b_low + a_low * b_high) + a_low * b_low
    return math.fsum((p, e, c))


def bm25(
    frequencies: Sequence[float], idfs: Sequence[float], row_tokens: int, avgdl: float
) -> float:
    """FTS5's bm25() of one row: `frequencies[i]` is phrase i's weighted instance count in
    the row, `idfs[i]` its IDF; negative, lower is better. fts5_aux.c computes
    `score += idf * ((f * (k1 + 1.0)) / (f + k1 * (1 - b + b * D / avgdl)))`, its compiler
    fusing the two multiply-adds (see the module docstring)."""
    score = 0.0
    for frequency, idf in zip(frequencies, idfs, strict=True):
        length = 1 - BM25_B + BM25_B * row_tokens / avgdl
        term = (frequency * (BM25_K1 + 1.0)) / fma(BM25_K1, length, frequency)
        score = fma(idf, term, score)
    return -1.0 * score


def idf(rows: int, hits: int) -> float:
    """FTS5's IDF of a phrase found in `hits` of `rows` rows."""
    value = math.log((rows - hits + 0.5) / (hits + 0.5))
    return value if value > 0.0 else BM25_MIN_IDF


class PostgresConceptSearch(ConceptSearchBackend):
    dialect = "postgresql"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._indexes_checked = False

    # -- errors and availability --------------------------------------------------------------

    def errors(self) -> AbstractContextManager[None]:
        return self._errors()

    @contextmanager
    def _errors(self) -> Iterator[None]:
        try:
            yield
        except ProgrammingError as exc:
            if getattr(exc.orig, "sqlstate", None) == _UNDEFINED_TABLE:
                found = _RELATION.search(str(exc.orig))
                if found and found.group(1) in TABLE_KINDS:
                    raise ConceptSearchUnavailableError(
                        missing_table_message(found.group(1))
                    ) from exc
            raise

    def _require_indexes(self, session: Session) -> None:
        """ConceptSearchUnavailableError unless the indexes of the search tables exist and are
        valid. Checked once per backend (per database) once all are there; a missing table is
        left to the query that uses it, whose error `errors()` reports."""
        if self._indexes_checked:
            return
        tables = {table for table, _ in INDEXES.values()}
        present = set(
            session.scalars(
                text(
                    "SELECT c.relname FROM pg_class c LEFT JOIN pg_index i "
                    "ON i.indexrelid = c.oid WHERE c.relname = ANY(CAST(:names AS text[])) "
                    "AND i.indisvalid IS NOT FALSE AND pg_table_is_visible(c.oid)"
                ),
                {"names": [*INDEXES, *tables]},
            )
        )
        missing = [
            name for name, (table, _) in INDEXES.items() if table in present and name not in present
        ]
        if missing:
            ddl = "; ".join(INDEXES[name][1] for name in missing)
            raise ConceptSearchUnavailableError(
                f"the concept search index(es) {', '.join(missing)} are missing or invalid, so "
                "every search would scan whole tables; the trigram indexes need the pg_trgm "
                "extension (on Supabase: CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA "
                f"extensions). Re-create them: {ddl}"
            )
        if tables <= present:
            with self._lock:
                self._indexes_checked = True

    # -- word matches --------------------------------------------------------------------------

    def word_matches(
        self, session: Session, terms: Sequence[str], *, prefix: bool, limit: int
    ) -> list[WordMatch]:
        self._require_indexes(session)
        phrases = [(list(tokens(term)), star) for term, star in word_phrases(terms, prefix=prefix)]
        live = [(toks, star) for toks, star in phrases if toks]
        if not live:
            return []  # FTS5: phrases without tokens match nothing on their own
        params: dict[str, Any] = {}
        selects = []
        for k, (toks, star) in enumerate(live):
            aliases = [f"p{k}t{j}" for j in range(len(toks))]
            joins = "".join(
                f" JOIN concept_search_tokens {a} ON {a}.rowid = {aliases[0]}.rowid "
                f"AND {a}.col = {aliases[0]}.col AND {a}.pos = {aliases[0]}.pos + {j}"
                for j, a in enumerate(aliases)
                if j
            )
            conditions = []
            for j, (a, token) in enumerate(zip(aliases, toks, strict=True)):
                key = token[:TOKEN_KEY_CHARS]
                params[f"{a}k"] = key
                if star and j == len(toks) - 1:
                    params[f"{a}p"] = _like_escape(token) + "%"
                    if len(token) < TOKEN_KEY_CHARS:
                        conditions.append(
                            f"substr({a}.token, 1, {TOKEN_KEY_CHARS}) LIKE :{a}p ESCAPE '\\'"
                        )
                    else:
                        conditions.append(
                            f"substr({a}.token, 1, {TOKEN_KEY_CHARS}) = :{a}k "
                            f"AND {a}.token LIKE :{a}p ESCAPE '\\'"
                        )
                else:
                    params[f"{a}t"] = token
                    conditions.append(
                        f"substr({a}.token, 1, {TOKEN_KEY_CHARS}) = :{a}k AND {a}.token = :{a}t"
                    )
            first = aliases[0]
            selects.append(
                f"SELECT {k} AS phrase, {first}.rowid, {first}.col, {first}.pos "
                f"FROM concept_search_tokens {first}{joins} WHERE {' AND '.join(conditions)}"
            )
        rows = session.execute(
            text(
                "SELECT i.phrase, i.rowid, i.col, i.pos, "
                "(SELECT count(*) FROM concept_search_tokens d WHERE d.rowid = i.rowid), "
                "(SELECT count(*) FROM concept_search), "
                "(SELECT count(*) FROM concept_search_tokens) "
                f"FROM ({' UNION ALL '.join(selects)}) AS i"
            ),
            params,
        ).all()
        if not rows:
            return []
        n_rows, n_tokens = rows[0][5], rows[0][6]
        found: list[set[int]] = [set() for _ in live]
        frequency: dict[int, list[float]] = {}
        instances: dict[int, list[tuple[int, int, int]]] = {}
        sizes: dict[int, int] = {}
        for phrase, rowid, col, pos, size, _, _ in rows:
            found[phrase].add(rowid)
            frequency.setdefault(rowid, [0.0] * len(live))[phrase] += SEARCH_WEIGHTS[col]
            instances.setdefault(rowid, []).append((phrase, col, pos))
            sizes[rowid] = size
        matched = set.intersection(*found)
        if not matched:
            return []
        idfs = [idf(n_rows, len(rowids)) for rowids in found]
        avgdl = float(n_tokens) / float(n_rows)
        ranked = sorted(
            (bm25(frequency[rowid], idfs, sizes[rowid], avgdl), rowid) for rowid in matched
        )[:limit]
        return self._with_fields(session, ranked, instances, [len(t) for t, _ in live])

    def _with_fields(
        self,
        session: Session,
        ranked: list[tuple[float, int]],
        instances: dict[int, list[tuple[int, int, int]]],
        lengths: list[int],
    ) -> list[WordMatch]:
        """The hits with the column (and alias) each matched in."""
        texts = {
            rowid: (name, aliases)
            for rowid, name, aliases in session.execute(
                text(
                    "SELECT rowid, name, aliases FROM concept_search "
                    "WHERE rowid = ANY(CAST(:ids AS bigint[]))"
                ),
                {"ids": [rowid for _, rowid in ranked]},
            )
        }
        hits = []
        for rank, rowid in ranked:
            if rowid not in texts:
                raise IntegrityCheckError(
                    f"the {CONCEPT_SEARCH_TOKENS_TABLE} index has words of row {rowid}, which "
                    "has no concept_search row; run ConceptManager.rebuild_search_index()"
                )
            name, aliases = texts[rowid]
            cols = {col for _, col, _ in instances[rowid]}
            if COL_NAME in cols:
                hits.append(WordMatch(rowid, rank, FIELD_NAME, name))
            elif COL_ALIASES in cols:
                starts = [start for _, start in tokenize(aliases)]
                segment = None
                for phrase, col, pos in instances[rowid]:
                    if col != COL_ALIASES:
                        continue
                    if pos + lengths[phrase] > len(starts):
                        raise IntegrityCheckError(
                            f"the {CONCEPT_SEARCH_TOKENS_TABLE} rows of row {rowid} do not match "
                            "its aliases; run ConceptManager.rebuild_search_index()"
                        )
                    index = aliases.count(ALIAS_SEPARATOR, 0, starts[pos])
                    segment = index if segment is None else min(segment, index)
                assert segment is not None
                alias = aliases.split(ALIAS_SEPARATOR)[segment]
                hits.append(WordMatch(rowid, rank, FIELD_ALIAS, alias))
            else:
                hits.append(WordMatch(rowid, rank, FIELD_SUMMARY, None))
        return hits

    # -- trigram rows --------------------------------------------------------------------------

    def _trigram_query(
        self, session: Session, groups: Sequence[Sequence[str]], *, all_groups: bool
    ) -> list[TrigramRow]:
        """Rows where, for every group (`all_groups`) or for some group, one of its strings
        occurs in `text` or `compact`."""
        self._require_indexes(session)
        params: dict[str, Any] = {}
        clauses = []
        for g, strings in enumerate(groups):
            options = []
            for s, value in enumerate(strings):
                name = f"g{g}s{s}"
                params[name] = "%" + _like_escape(value) + "%"
                options.append(f"text LIKE :{name} ESCAPE '\\'")
                options.append(f"compact LIKE :{name} ESCAPE '\\'")
            clauses.append(f"({' OR '.join(options)})")
        where = (" AND " if all_groups else " OR ").join(clauses)
        return [
            tuple(row)  # type: ignore[misc]
            for row in session.execute(
                text(
                    f"SELECT rowid, text, compact, label FROM concept_trigram WHERE {where} "
                    "ORDER BY rowid"
                ),
                params,
            )
        ]

    def substring_rows(self, session: Session, clauses: Sequence[Sequence[str]]) -> list[TrigramRow]:
        return self._trigram_query(session, clauses, all_groups=True)

    def fuzzy_rows(self, session: Session, grams: Sequence[str]) -> list[TrigramRow]:
        return self._trigram_query(session, [[gram] for gram in grams], all_groups=False)

    # -- the index as stored -------------------------------------------------------------------

    def word_rows(self, session: Session) -> WordIndexRows:
        rows = {
            rowid: (name, aliases, summary)
            for rowid, name, aliases, summary in session.execute(
                text("SELECT rowid, name, aliases, summary FROM concept_search")
            )
        }
        stored: dict[int, list[tuple[int, int, str]]] = {}
        for rowid, col, pos, token in session.execute(
            text(
                "SELECT rowid, col, pos, token FROM concept_search_tokens "
                "ORDER BY rowid, col, pos"
            )
        ):
            stored.setdefault(rowid, []).append((col, pos, token))
        inconsistent = {rowid for rowid in stored if rowid not in rows}
        inconsistent.update(
            rowid for rowid, values in rows.items() if stored.get(rowid, []) != column_tokens(values)
        )
        return WordIndexRows(rows, frozenset(inconsistent))

    def trigram_rows(self, session: Session) -> list[TrigramRow]:
        return [
            tuple(row)  # type: ignore[misc]
            for row in session.execute(
                text("SELECT rowid, text, compact, label FROM concept_trigram ORDER BY rowid")
            )
        ]

    # -- writes --------------------------------------------------------------------------------

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
        words = column_tokens(word_row)
        if words:
            session.execute(
                text(
                    "INSERT INTO concept_search_tokens (rowid, col, pos, token) "
                    "SELECT :id, w.col, w.pos, w.token FROM unnest(CAST(:cols AS smallint[]), "
                    "CAST(:positions AS integer[]), CAST(:tokens AS text[])) AS w(col, pos, token)"
                ),
                {
                    "id": concept_id,
                    "cols": [col for col, _, _ in words],
                    "positions": [pos for _, pos, _ in words],
                    "tokens": [token for _, _, token in words],
                },
            )
        session.execute(
            text(
                "INSERT INTO concept_trigram (rowid, text, compact, label) "
                "SELECT * FROM unnest(CAST(:rowids AS bigint[]), CAST(:texts AS text[]), "
                "CAST(:compacts AS text[]), CAST(:labels AS text[]))"
            ),
            {
                "rowids": [row[0] for row in trigram_rows],
                "texts": [row[1] for row in trigram_rows],
                "compacts": [row[2] for row in trigram_rows],
                "labels": [row[3] for row in trigram_rows],
            },
        )

    def delete(self, session: Session, concept_id: int) -> None:
        session.execute(
            text("DELETE FROM concept_search_tokens WHERE rowid = :id"), {"id": concept_id}
        )
        session.execute(text("DELETE FROM concept_search WHERE rowid = :id"), {"id": concept_id})
        session.execute(
            text("DELETE FROM concept_trigram WHERE rowid >= :low AND rowid < :high"),
            {"low": concept_id * TRIGRAM_SLOTS, "high": (concept_id + 1) * TRIGRAM_SLOTS},
        )

    def clear(self, session: Session) -> None:
        session.execute(text("DELETE FROM concept_search_tokens"))
        session.execute(text("DELETE FROM concept_search"))
        session.execute(text("DELETE FROM concept_trigram"))
