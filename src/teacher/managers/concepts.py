"""ConceptManager: concepts, their aliases, the search indexes, and merging two concepts.

Names and aliases are compared by `teacher.concepts.normalize_name`. An alias is unique per
concept (not across concepts: acronyms collide between fields) and never equal to the concept's
own name. Any content edit (name, summary, an alias added or removed) clears `reviewed_at`;
`mark_reviewed` is the only way to set it.

The two search indexes (see `teacher.db.fts`) - the word index `concept_search` (one row per
concept) and the trigram index `concept_trigram` (one row per name or alias) - are rewritten for
a concept by every write here, inside the same savepoint as the change, so they cannot drift
from the rows written through this manager; `check()` reports any drift and
`rebuild_search_index()` repairs it. `search_hits` (and `search`, its concepts only) combines
them with the exact lookup; the matching rules are in `search_hits` and
`teacher.concept_search`. The SQL is the database's concept-search backend's
(`teacher.db.search`: FTS5 on SQLite, plain tables with pg_trgm on Postgres), which the
database provider names; both give the same hits in the same order with the same scores.
"""

from __future__ import annotations

import builtins
from collections.abc import Collection, Iterable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from teacher.concept_search import (
    EXACT,
    FIELD_ALIAS,
    FIELD_NAME,
    FUZZY,
    KINDS,
    PREFIX,
    SUBSTRING,
    WORD,
    ParsedQuery,
    compact,
    fold,
    fuzzy_score,
    parse_query,
    substring_clauses,
    substring_matches,
    substring_score,
    trigrams,
    word_expression,
)
from teacher.concepts import (
    BUILDS_ON,
    SYMMETRIC_RELATION_TYPES,
    normalize_name,
    stronger_role,
    validate_origin,
)
from teacher.config import Settings
from teacher.db.fts import (
    ALIAS_SEPARATOR,
    CONCEPT_SEARCH_TABLE,
    CONCEPT_TRIGRAM_TABLE,
    SEARCH_WEIGHTS,
    TRIGRAM_SLOTS,
)
from teacher.db.models import (
    BlockConcept,
    Concept,
    ConceptAlias,
    ConceptRelation,
    KnowledgeSource,
    SourceConcept,
    utcnow,
)
from teacher.db.providers import create_db_provider
from teacher.db.search import ConceptSearchBackend, TrigramRow
from teacher.files import slugify, validate_slug
from teacher.managers.artifacts import _is_unique_violation, _require_persistent, _validate_text
from teacher.managers.block_concepts import stronger_block_role
from teacher.managers.concept_judgements import ConceptJudgementManager
from teacher.managers.concept_relations import (
    builds_on_edges,
    describe,
    raise_if_cyclic,
    set_endpoints,
    slugs_by_id,
    touching,
)
from teacher.managers.errors import (
    CycleError,
    DuplicateError,
    IntegrityCheckError,
    Issue,
    NotFoundError,
)

DEFAULT_SEARCH_LIMIT = 20
# SEARCH_WEIGHTS (bm25 column weights for name, aliases, summary) and ALIAS_SEPARATOR live in
# teacher.db.fts, with the indexes; they are re-exported here.


# -- search results -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SearchHit:
    """One search result.

    - `kind`: how the concept matched, one of `KINDS` ('exact', 'word', 'prefix', 'substring',
      'fuzzy'), best first; a concept is reported once, at its best kind.
    - `score`: higher is better, but only comparable between hits of the same kind (exact: 1.0;
      word/prefix: -bm25; substring: the share of the name/alias the query covers, 0..1;
      fuzzy: the similarity, 0..1).
    - `field`: 'name', 'alias', or (word/prefix only) 'summary' when neither the name nor an
      alias contains a matched word.
    - `matched`: the name or alias that matched, as stored (e.g. "PG" for "matched alias: PG",
      or the name a fuzzy hit corrects to for "did you mean"); None for a summary match.
    """

    concept: Concept
    kind: str
    score: float
    field: str
    matched: str | None


@dataclass(frozen=True, slots=True)
class _Found:
    concept_id: int
    kind: str
    score: float
    field: str
    matched: str | None
    index: str | None  # the index the hit came from (None: the concept tables themselves)


# -- merge results --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RetargetedRelation:
    """A relation of the merged-away concept, now pointing at `into` (`relation`), and its
    endpoints before the merge."""

    relation: ConceptRelation
    old_from_slug: str
    old_to_slug: str


@dataclass(frozen=True, slots=True)
class DroppedRelation:
    """A relation the merge deleted. `code` is 'self_relation' (it would have related `into`
    to itself) or 'conflict' (it lost to relation `kept_id` on the same pair); `reason` says
    why in words."""

    id: int
    type: str
    from_slug: str
    to_slug: str
    rationale: str
    code: str
    reason: str
    kept_id: int | None


@dataclass(frozen=True, slots=True)
class MergeResult:
    """What `ConceptManager.merge` did. `moved_aliases` are the aliases (the source's name
    first) added to `into`; `skipped_aliases` duplicated `into`'s name or an alias it had.
    `moved_links` are the knowledge sources (slugs) whose link to the merged concept now
    points at `into`; `combined_links` those that were linked to both, whose two links became
    `into`'s one link (with the stronger role). `dropped_judgements` counts the relation
    judge's memo rows (`concept_pair_judgements`) of the merged-away concept, which were
    deleted: they judged its old text, so the pairs are judged again with `into`'s.
    `moved_block_tags` counts the study-page block tags (`block_concepts`) now on `into`;
    `combined_block_tags` the blocks tagged with both, whose two tags became `into`'s one."""

    into: Concept
    source_slug: str
    moved_aliases: tuple[str, ...]
    skipped_aliases: tuple[str, ...]
    retargeted: tuple[RetargetedRelation, ...]
    dropped: tuple[DroppedRelation, ...]
    moved_links: tuple[str, ...]
    combined_links: tuple[str, ...]
    dropped_judgements: int = 0
    moved_block_tags: int = 0
    combined_block_tags: int = 0


# -- helpers --------------------------------------------------------------------------------


def _clean(value: object, name: str) -> str:
    """A non-empty string, stripped of outer whitespace."""
    return _validate_text(value, name).strip()


def search_expression(query: object, *, prefix: bool = False) -> str:
    """`query` as a safe FTS5 MATCH expression for the word index: every whitespace-separated
    term that has a letter or digit becomes one quoted string (inner quotes doubled), so FTS
    syntax in the text (quotes, `*`, `-`, `:`, `AND`, `NEAR(...)`) is matched as text, never as
    operators. Terms are ANDed. With `prefix`, a term whose last word has at least 2 characters
    also matches as a prefix (`"term"*`). ValueError for an empty query or one with no letters
    or digits (a query of only symbols, such as "∇", can still be searched with `search_hits`:
    it has no words, so only the exact and substring tiers apply)."""
    parsed = parse_query(query)
    if not parsed.word_terms:
        raise ValueError(f"search query {query!r} has no letters or digits to search for")
    return word_expression(parsed.word_terms, prefix=prefix)


def _kinds(kinds: object) -> frozenset[str]:
    if isinstance(kinds, str) or not isinstance(kinds, Collection):
        raise TypeError(f"kinds must be a collection of match kinds, got {kinds!r}")
    chosen = frozenset(kinds)
    unknown = sorted(str(k) for k in chosen - set(KINDS))
    if unknown:
        raise ValueError(f"unknown match kind(s) {unknown}: expected some of {KINDS}")
    if not chosen:
        raise ValueError(f"kinds must name at least one of {KINDS}")
    return chosen


def _positive(limit: object, name: str = "limit") -> int:
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError(f"{name} must be a positive integer, got {limit!r}")
    return limit


def _best_per_concept(scored: Iterable[tuple[tuple[float, ...], _Found]]) -> list[_Found]:
    """Each concept's best hit (smallest key), best first; ties by concept id."""
    best: dict[int, tuple[tuple[float, ...], _Found]] = {}
    for key, hit in scored:
        if hit.concept_id not in best or key < best[hit.concept_id][0]:
            best[hit.concept_id] = (key, hit)
    ranked = sorted(best.values(), key=lambda pair: (pair[0], pair[1].concept_id))
    return [hit for _, hit in ranked]


_RANK = {BUILDS_ON: 2, "different_version_of": 1, "relates_to": 0}


def _resolve(
    kept_by_into: ConceptRelation, incoming: ConceptRelation
) -> tuple[ConceptRelation, ConceptRelation, str]:
    """Which of two relations on the same pair survives a merge: `(winner, loser, why)`.

    builds_on beats the other types, different_version_of beats relates_to; for the same type
    the reviewed one wins, else the one `into` already had.
    """
    a, b = kept_by_into, incoming
    if a.type != b.type:
        winner, loser = (a, b) if _RANK[a.type] > _RANK[b.type] else (b, a)
        return winner, loser, f"{winner.type} beats {loser.type}"
    if b.reviewed_at is not None and a.reviewed_at is None:
        return b, a, "the other one is reviewed"
    if a.reviewed_at is not None and b.reviewed_at is None:
        return a, b, "the other one is reviewed"
    return a, b, "same type; the merge target's own relation is kept"


class ConceptManager:
    """Create, edit, review, look up, search, merge and delete concepts."""

    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings

    # -- creation ---------------------------------------------------------------------------

    def create(
        self,
        name: str,
        summary: str,
        origin: str,
        *,
        aliases: Iterable[str] = (),
        slug: str | None = None,
    ) -> Concept:
        """A new, unreviewed concept. `slug` defaults to `slugify(name)`.

        ValueError for a blank name or summary, an unknown origin, an invalid slug or an alias
        equal to the name; DuplicateError for a taken slug (no automatic suffix) or two aliases
        with the same normalised form. Nothing changes on error.
        """
        name = _clean(name, "name")
        summary = _clean(summary, "summary")
        validate_origin(origin)
        name_norm = normalize_name(name)
        slug = validate_slug(slug) if slug is not None else slugify(name)
        if isinstance(aliases, str):
            raise TypeError("aliases must be an iterable of strings, not a string")
        rows: list[ConceptAlias] = []
        seen: dict[str, str] = {}
        for alias in aliases:
            alias, alias_norm = self._checked_alias(alias, name, name_norm)
            if alias_norm in seen:
                raise DuplicateError(
                    f"alias {alias!r} repeats alias {seen[alias_norm]!r} of the new concept "
                    f"{slug!r} (aliases are compared normalised)"
                )
            seen[alias_norm] = alias
            rows.append(ConceptAlias(alias=alias, alias_norm=alias_norm))
        self._require_free_slug(slug)
        concept = Concept(
            slug=slug, name=name, name_norm=name_norm, summary=summary, origin=origin, aliases=rows
        )
        try:
            with self.session.begin_nested():
                self.session.add(concept)
                self.session.flush()
                self._sync_search(concept)
        except IntegrityError as err:
            if _is_unique_violation(err):
                raise DuplicateError(f"concept slug {slug!r} already exists") from err
            raise
        self.session.expire(concept, ["aliases"])  # reload in alias order
        return concept

    # -- lookups ----------------------------------------------------------------------------

    def get(self, id: int) -> Concept:
        """The concept with `id`; NotFoundError if there is none."""
        concept = self.session.get(Concept, id)
        if concept is None:
            raise NotFoundError(f"no concept with id {id}")
        return concept

    def get_by_slug(self, slug: str) -> Concept:
        """The concept with `slug`; NotFoundError if there is none."""
        concept = self.session.scalar(select(Concept).where(Concept.slug == slug))
        if concept is None:
            raise NotFoundError(f"no concept with slug {slug!r}")
        return concept

    def list(self) -> builtins.list[Concept]:
        """All concepts, by slug."""
        return list(self.session.scalars(select(Concept).order_by(Concept.slug)))

    def find_by_name(self, name: str) -> builtins.list[Concept]:
        """Every concept whose name or an alias equals `name` once both are normalised (exact
        lookup; several concepts can share an alias, or even a name). By slug."""
        norm = normalize_name(name)
        by_alias = select(ConceptAlias.concept_id).where(ConceptAlias.alias_norm == norm)
        return list(
            self.session.scalars(
                select(Concept)
                .where((Concept.name_norm == norm) | Concept.id.in_(by_alias))
                .order_by(Concept.slug)
            )
        )

    def search(
        self,
        query: str,
        limit: int = DEFAULT_SEARCH_LIMIT,
        *,
        kinds: Collection[str] = KINDS,
    ) -> builtins.list[Concept]:
        """The concepts of `search_hits(query, limit, kinds=kinds)`, best first."""
        return [hit.concept for hit in self.search_hits(query, limit, kinds=kinds)]

    def search_hits(
        self,
        query: str,
        limit: int = DEFAULT_SEARCH_LIMIT,
        *,
        kinds: Collection[str] = KINDS,
    ) -> builtins.list[SearchHit]:
        """Search names, aliases and summaries; at most `limit` hits, best first.

        Up to five kinds of match run in this order, and their hits are listed in this order
        (each concept once, at the first kind that finds it); `kinds` restricts which run, e.g.
        `kinds={"exact", "word", "prefix", "substring"}` for no typo tolerance.

        1. exact - the query equals the name or an alias once both are normalised
           (`normalize_name`, as `find_by_name`). Names before aliases, then by id.
        2. word - every query term matches whole words of the name, aliases or summary (word
           index; a term like "k-NN" is the phrase "k NN"; case and diacritics are ignored).
           By bm25 with weights name 10, aliases 5, summary 1, then id.
        3. prefix - as word, but each term's last word may be the start of a word ("grad" finds
           "gradient"), if it has at least 2 characters. Ordered as word, after all word hits.
        4. substring - every term occurs anywhere inside one name or alias ("dient" finds
           "Gradient", "former" finds "Transformer"), compared folded (`fold`: no case, no
           diacritics) and also with separators removed ("knn" finds "k-NN"). Uses the trigram
           index, so it only runs when some term has at least 3 characters; shorter terms of
           the same query are then checked on the rows found. Ordered by how much of the
           name/alias the terms cover, then name before alias, then id.
        5. fuzzy - typo tolerance on names and aliases. Candidates: every trigram-index row
           sharing a 3-character piece with a query word (all are scored: a typo may leave
           only one common piece, e.g. "entorpy"/"entropy" share just "ent", so taking the
           best-bm25 few would lose real matches). Each query word must be within an edit
           budget of a word of the name or alias (Damerau/OSA distance; 1 edit for words of
           4-8 characters, 2 from 9; words under 4 characters or containing a digit must match
           exactly; the first letter is never corrected), or the whole query without
           separators within the budget of the name/alias without separators. Ordered by
           similarity, then fewer unmatched words of the name/alias, then name before alias,
           then id. A typo the trigram index cannot see (no 3-character piece in common,
           e.g. "lsmt" for "LSTM") is not found.

        Terms shorter than 3 characters can only match through tiers 1-3. The query text is
        never parsed as FTS5 syntax (see `teacher.concept_search`).

        TypeError for a non-string query; ValueError for an empty query, one of only
        punctuation (a letter, digit or symbol such as "ε" or "∇" is enough), a bad limit or
        bad `kinds`; IntegrityCheckError if an index points at a concept that no longer
        exists; ConceptSearchUnavailableError if the database (or SQLite build) lacks what
        the indexes need.
        """
        parsed = parse_query(query)
        _positive(limit)
        chosen = _kinds(kinds)
        tiers = {
            EXACT: self._exact_hits,
            WORD: self._word_hits,
            PREFIX: self._prefix_hits,
            SUBSTRING: self._substring_hits,
            FUZZY: self._fuzzy_hits,
        }
        found: dict[int, _Found] = {}
        for kind in KINDS:
            if kind not in chosen or len(found) >= limit:
                continue
            for hit in tiers[kind](parsed, limit, chosen):
                if hit.concept_id not in found:
                    found[hit.concept_id] = hit
                    if len(found) >= limit:
                        break
        concepts = {
            concept.id: concept
            for concept in self.session.scalars(select(Concept).where(Concept.id.in_(found)))
        }
        missing: dict[str, list[int]] = {}
        for hit in found.values():
            if hit.concept_id not in concepts:
                missing.setdefault(hit.index or "concepts", []).append(hit.concept_id)
        if missing:
            raise IntegrityCheckError(
                "; ".join(
                    f"the {index} index has rows for concepts that do not exist (ids {ids})"
                    for index, ids in missing.items()
                )
                + "; run ConceptManager.rebuild_search_index()"
            )
        return [
            SearchHit(concepts[h.concept_id], h.kind, h.score, h.field, h.matched)
            for h in found.values()
        ]

    # -- the search tiers (each yields hits best first; search_hits drops repeats) ---------

    def _exact_hits(
        self, parsed: ParsedQuery, limit: int, kinds: frozenset[str]
    ) -> builtins.list[_Found]:
        hits = [
            _Found(concept_id, EXACT, 1.0, FIELD_NAME, name, None)
            for concept_id, name in self.session.execute(
                select(Concept.id, Concept.name)
                .where(Concept.name_norm == parsed.norm)
                .order_by(Concept.id)
            )
        ]
        hits += [
            _Found(concept_id, EXACT, 1.0, FIELD_ALIAS, alias, None)
            for concept_id, alias in self.session.execute(
                select(ConceptAlias.concept_id, ConceptAlias.alias)
                .where(ConceptAlias.alias_norm == parsed.norm)
                .order_by(ConceptAlias.concept_id)
            )
        ]
        return hits

    def _word_hits(
        self, parsed: ParsedQuery, limit: int, kinds: frozenset[str]
    ) -> builtins.list[_Found]:
        if not parsed.word_terms:
            return []
        return self._word_index(parsed.word_terms, False, WORD, limit)

    def _prefix_hits(
        self, parsed: ParsedQuery, limit: int, kinds: frozenset[str]
    ) -> builtins.list[_Found]:
        if not parsed.word_terms:
            return []
        expression = word_expression(parsed.word_terms, prefix=True)
        if WORD in kinds and expression == word_expression(parsed.word_terms):
            return []  # no term long enough to be a prefix: the word tier found all of it
        # Whole-word hits come back too; search_hits already has them from the word tier
        # (which found them all, or filled the limit), so only prefix-only hits are new.
        return self._word_index(parsed.word_terms, True, PREFIX, limit)

    def _word_index(
        self, terms: tuple[str, ...], prefix: bool, kind: str, limit: int
    ) -> builtins.list[_Found]:
        """Hits from the word index for `terms` (as `word_expression(terms, prefix=prefix)`),
        best first. `limit` rows are enough: at most `limit` hits are kept, and earlier tiers
        can only have found fewer than that."""
        backend = self._search
        with backend.errors():
            matches = backend.word_matches(self.session, terms, prefix=prefix, limit=limit)
        return [
            _Found(m.concept_id, kind, -m.rank, m.field, m.matched, CONCEPT_SEARCH_TABLE)
            for m in matches
        ]

    @property
    def _search(self) -> ConceptSearchBackend:
        """The database's concept-search backend (its provider names it); an error if the
        session is bound to another kind of database than `settings` names."""
        backend = create_db_provider(self.settings).concept_search
        dialect = self.session.get_bind().dialect.name
        if dialect != backend.dialect:
            raise RuntimeError(
                f"the session is on a {dialect} database, but the settings name a "
                f"{backend.dialect} one ({self.settings.db}); use a session of those settings"
            )
        return backend

    @staticmethod
    def _trigram_hit(rowid: int, kind: str, score: float, label: str) -> _Found:
        concept_id, slot = divmod(rowid, TRIGRAM_SLOTS)
        field = FIELD_NAME if slot == 0 else FIELD_ALIAS
        return _Found(concept_id, kind, score, field, label, CONCEPT_TRIGRAM_TABLE)

    def _substring_hits(
        self, parsed: ParsedQuery, limit: int, kinds: frozenset[str]
    ) -> builtins.list[_Found]:
        clauses = substring_clauses(parsed.terms)
        if not clauses:
            return []
        backend = self._search
        with backend.errors():
            rows = backend.substring_rows(self.session, clauses)
        scored = []
        for rowid, row_text, row_compact, label in rows:
            if substring_matches(parsed.terms, row_text, row_compact):
                score = substring_score(parsed.terms, row_compact)
                hit = self._trigram_hit(rowid, SUBSTRING, score, label)
                scored.append(((-score, rowid % TRIGRAM_SLOTS), hit))
        return _best_per_concept(scored)

    def _fuzzy_hits(
        self, parsed: ParsedQuery, limit: int, kinds: frozenset[str]
    ) -> builtins.list[_Found]:
        grams = trigrams(parsed.words)
        if not grams:
            return []
        backend = self._search
        with backend.errors():
            rows = backend.fuzzy_rows(self.session, grams)
        scored = []
        for rowid, row_text, _row_compact, label in rows:
            closeness = fuzzy_score(parsed.words, row_text)
            if closeness is not None:
                hit = self._trigram_hit(rowid, FUZZY, closeness.score, label)
                key = (-closeness.score, closeness.extra, rowid % TRIGRAM_SLOTS)
                scored.append((key, hit))
        return _best_per_concept(scored)

    # -- edits ------------------------------------------------------------------------------

    def update(
        self, concept: Concept, *, name: str | None = None, summary: str | None = None
    ) -> Concept:
        """Change the name and/or summary (the slug never changes). An actual change clears
        `reviewed_at`; passing the current values changes nothing. ValueError if nothing is
        given, a value is blank, or the new name equals one of the concept's aliases."""
        _require_persistent(concept, "concept")
        if name is None and summary is None:
            raise ValueError("nothing to update: give name and/or summary")
        new_name = _clean(name, "name") if name is not None else concept.name
        new_summary = _clean(summary, "summary") if summary is not None else concept.summary
        new_norm = normalize_name(new_name)
        if new_norm != concept.name_norm:
            for alias in concept.aliases:
                if alias.alias_norm == new_norm:
                    raise ValueError(
                        f"concept {concept.slug!r}: the new name {new_name!r} equals its alias "
                        f"{alias.alias!r}; remove the alias first"
                    )
        if (new_name, new_summary) == (concept.name, concept.summary):
            return concept
        with self.session.begin_nested():
            concept.name, concept.name_norm, concept.summary = new_name, new_norm, new_summary
            self._content_changed(concept)
            self.session.flush()
            self._sync_search(concept)
        return concept

    def add_alias(self, concept: Concept, alias: str) -> ConceptAlias:
        """Add another name for the concept (stored stripped; compared normalised). Clears
        `reviewed_at`. ValueError if it equals the name; DuplicateError if the concept has it."""
        _require_persistent(concept, "concept")
        alias, alias_norm = self._checked_alias(alias, concept.name, concept.name_norm)
        for existing in concept.aliases:
            if existing.alias_norm == alias_norm:
                raise DuplicateError(
                    f"concept {concept.slug!r} already has alias {existing.alias!r} "
                    f"(same as {alias!r} once normalised)"
                )
        row = ConceptAlias(alias=alias, alias_norm=alias_norm)
        try:
            with self.session.begin_nested():
                concept.aliases.append(row)
                self._content_changed(concept)
                self.session.flush()
                self._sync_search(concept)
        except IntegrityError as err:
            if _is_unique_violation(err):
                raise DuplicateError(
                    f"concept {concept.slug!r} already has alias {alias!r}"
                ) from err
            raise
        self.session.expire(concept, ["aliases"])  # reload in alias order
        return row

    def remove_alias(self, concept: Concept, alias: str) -> None:
        """Remove the alias equal to `alias` once normalised. Clears `reviewed_at`.
        NotFoundError if the concept has no such alias."""
        _require_persistent(concept, "concept")
        alias_norm = normalize_name(alias, what="alias")
        row = next((a for a in concept.aliases if a.alias_norm == alias_norm), None)
        if row is None:
            raise NotFoundError(f"concept {concept.slug!r} has no alias {alias!r}")
        with self.session.begin_nested():
            concept.aliases.remove(row)
            self._content_changed(concept)
            self.session.flush()
            self._sync_search(concept)

    def mark_reviewed(self, concept: Concept) -> Concept:
        """Record that a person checked the concept (reviewed_at = now)."""
        _require_persistent(concept, "concept")
        concept.reviewed_at = utcnow()
        self.session.flush()
        return concept

    def delete(self, concept: Concept) -> None:
        """Delete the concept with its aliases, relations, embedding and search row; the
        database deletes its source links (`source_concepts`) and the relation judge's memo
        rows of its pairs (`concept_pair_judgements`), both ON DELETE CASCADE."""
        _require_persistent(concept, "concept")
        with self.session.begin_nested():
            for relation in touching(self.session, concept.id):
                self.session.delete(relation)
            self._delete_search_row(concept.id)
            _ = concept.embedding  # loaded, so the ORM deletes it too (not only the database)
            self.session.delete(concept)
            self.session.flush()

    # -- merge ------------------------------------------------------------------------------

    def merge(self, source: Concept, into: Concept) -> MergeResult:
        """Merge `source` into `into` and delete `source`, all or nothing.

        1. `source`'s name and aliases become aliases of `into`, except ones equal (normalised)
           to `into`'s name or an alias it already has.
        2. Every relation of `source` is retargeted to `into`.
        3. A relation that would join `into` to itself is dropped. If `into` and `source` were
           both related to the same concept, one relation survives: builds_on beats the other
           types, different_version_of beats relates_to; for the same type the reviewed one,
           else `into`'s own. Opposite builds_on edges on one pair, or a retargeted builds_on
           that closes a cycle, raise CycleError and change nothing.
        4. Source links (`source_concepts`): a knowledge source linked only to `source` is now
           linked to `into` (the link is kept as it is); one linked to both keeps `into`'s link,
           whose role becomes the stronger of the two (introduces > uses > assumes; when
           `source`'s role wins, its evidence comes with it), and `source`'s link is deleted.
        5. `source` (with its embedding) is deleted. `into` keeps `reviewed_at`; its search row
           is refreshed, and its embedding goes stale once its alias text changed.
        6. The relation judge's memo rows of `source` are deleted (not moved: they judged
           `source`'s text, and a moved row could collide with `into`'s own row for the same
           pair). `into`'s own rows stay; they go stale by themselves once its text changed.
        7. Block tags (`block_concepts`): a block tagged only with `source` is now tagged with
           `into` (the tag is kept as it is); a block tagged with both keeps `into`'s tag,
           whose role becomes the stronger of the two (main > incidental; when `source`'s role
           wins, its origin, model and rationale come with it), and `source`'s tag is deleted.

        ValueError when merging a concept into itself.
        """
        _require_persistent(source, "source")
        _require_persistent(into, "into")
        if source.id == into.id:
            raise ValueError(f"cannot merge concept {source.slug!r} into itself")
        src, dst = source.id, into.id
        what = f"merging {source.slug!r} into {into.slug!r}"

        # 1. aliases
        taken = {into.name_norm, *(a.alias_norm for a in into.aliases)}
        moved: list[tuple[str, str]] = []
        skipped: list[str] = []
        for alias in [source.name, *(a.alias for a in source.aliases)]:
            norm = normalize_name(alias, what="alias")
            if norm in taken:
                skipped.append(alias)
            else:
                taken.add(norm)
                moved.append((alias, norm))

        # 2./3. relations: plan everything before changing anything
        source_relations = touching(self.session, src)
        into_by_other: dict[int, ConceptRelation] = {}
        for relation in touching(self.session, dst):
            other = relation.to_id if relation.from_id == dst else relation.from_id
            into_by_other[other] = relation
        involved = {src, dst}
        for relation in source_relations:
            involved.update((relation.from_id, relation.to_id))
        slugs = slugs_by_id(self.session, involved | set(into_by_other))

        retarget: list[tuple[ConceptRelation, int, int]] = []
        dropped: list[DroppedRelation] = []

        def drop(relation: ConceptRelation, code: str, reason: str, kept: int | None) -> None:
            dropped.append(
                DroppedRelation(
                    id=relation.id,
                    type=relation.type,
                    from_slug=slugs[relation.from_id],
                    to_slug=slugs[relation.to_id],
                    rationale=relation.rationale,
                    code=code,
                    reason=reason,
                    kept_id=kept,
                )
            )

        for relation in source_relations:
            other = relation.to_id if relation.from_id == src else relation.from_id
            if other == dst:
                drop(
                    relation,
                    "self_relation",
                    f"it related {source.slug!r} and {into.slug!r}, which are now one concept",
                    None,
                )
                continue
            new_from = dst if relation.from_id == src else relation.from_id
            new_to = dst if relation.to_id == src else relation.to_id
            if relation.type in SYMMETRIC_RELATION_TYPES:
                new_from, new_to = min(new_from, new_to), max(new_from, new_to)
            existing = into_by_other.get(other)
            if existing is None:
                retarget.append((relation, new_from, new_to))
                continue
            if (
                relation.type == existing.type == BUILDS_ON
                and (new_from, new_to) != (existing.from_id, existing.to_id)
            ):
                cycle = (slugs[existing.from_id], slugs[existing.to_id], slugs[existing.from_id])
                raise CycleError(
                    f"{what} would make builds_on cyclic: {' -> '.join(cycle)} (relations "
                    f"{describe(existing, slugs)} and {describe(relation, slugs)} point in "
                    "opposite directions)",
                    cycle,
                )
            winner, loser, why = _resolve(existing, relation)
            drop(
                loser,
                "conflict",
                f"{slugs[other]!r} was related to both concepts; relation #{winner.id} "
                f"({winner.type}) was kept: {why}",
                winner.id,
            )
            if winner is relation:
                retarget.append((relation, new_from, new_to))

        dropped_ids = {d.id for d in dropped}
        retargeted_ids = {relation.id for relation, _, _ in retarget}
        edges = builds_on_edges(self.session, exclude=dropped_ids | retargeted_ids)
        for relation, new_from, new_to in retarget:
            if relation.type == BUILDS_ON:
                edges.setdefault(new_from, set()).add(new_to)
        raise_if_cyclic(self.session, edges, what)

        # 4. source links: plan
        source_links = list(
            self.session.scalars(select(SourceConcept).where(SourceConcept.concept_id == src))
        )
        into_links = {
            link.source_id: link
            for link in self.session.scalars(
                select(SourceConcept).where(SourceConcept.concept_id == dst)
            )
        }
        source_slugs = dict(
            self.session.execute(
                select(KnowledgeSource.id, KnowledgeSource.slug).where(
                    KnowledgeSource.id.in_({link.source_id for link in source_links})
                )
            ).all()
        )
        moved_links = sorted(
            source_slugs[link.source_id] for link in source_links if link.source_id not in into_links
        )
        combined_links = sorted(
            source_slugs[link.source_id] for link in source_links if link.source_id in into_links
        )

        # 7. block tags: plan
        source_tags = list(
            self.session.scalars(select(BlockConcept).where(BlockConcept.concept_id == src))
        )
        into_tags = {
            tag.block_id: tag
            for tag in self.session.scalars(
                select(BlockConcept).where(BlockConcept.concept_id == dst)
            )
        }
        combined_tags = sum(1 for tag in source_tags if tag.block_id in into_tags)

        # 5. apply
        retargeted = [
            RetargetedRelation(relation, slugs[relation.from_id], slugs[relation.to_id])
            for relation, _, _ in retarget
        ]
        source_slug = source.slug
        with self.session.begin_nested():
            for d in dropped:
                self.session.delete(self.session.get(ConceptRelation, d.id))
            self.session.flush()  # free the pairs before relations move onto them
            for relation, new_from, new_to in retarget:
                set_endpoints(relation, new_from, new_to)
            self.session.flush()
            for alias, norm in moved:
                into.aliases.append(ConceptAlias(alias=alias, alias_norm=norm))
            for link in source_links:
                kept = into_links.get(link.source_id)
                if kept is None:
                    continue
                role = stronger_role(kept.role, link.role)
                if role != kept.role:
                    kept.role, kept.evidence = role, link.evidence
                self.session.delete(link)
            self.session.flush()  # free (source, into) before the other links move onto it
            for link in source_links:
                if link.source_id not in into_links:
                    link.concept_id = dst
            for tag in source_tags:
                kept_tag = into_tags.get(tag.block_id)
                if kept_tag is None:
                    continue
                role = stronger_block_role(kept_tag.role, tag.role)
                if role != kept_tag.role:
                    kept_tag.role, kept_tag.origin = role, tag.origin
                    kept_tag.model, kept_tag.rationale = tag.model, tag.rationale
                self.session.delete(tag)
            self.session.flush()  # free (block, into) before the other tags move onto it
            for tag in source_tags:
                if tag.block_id not in into_tags:
                    tag.concept_id = dst
            self.session.flush()  # moved before `source` goes, or its cascade would take them
            dropped_judgements = ConceptJudgementManager(
                self.session, self.settings
            ).delete_for_concept(src)
            self._delete_search_row(src)
            _ = source.embedding  # loaded, so the ORM deletes it too (not only the database)
            self.session.delete(source)
            into.updated_at = utcnow()
            self.session.flush()
            self._sync_search(into)
        for relation, _, _ in retarget:
            self.session.expire(relation, ["from_concept", "to_concept"])
        self.session.expire(into, ["aliases"])  # reload in alias order
        return MergeResult(
            into=into,
            source_slug=source_slug,
            moved_aliases=tuple(alias for alias, _ in moved),
            skipped_aliases=tuple(skipped),
            retargeted=tuple(retargeted),
            dropped=tuple(dropped),
            moved_links=tuple(moved_links),
            combined_links=tuple(combined_links),
            dropped_judgements=dropped_judgements,
            moved_block_tags=len(source_tags) - combined_tags,
            combined_block_tags=combined_tags,
        )

    # -- the search index -------------------------------------------------------------------

    def check(self) -> builtins.list[Issue]:
        """Compare both search indexes with the concepts.

        Word index (`concept_search`): 'missing_search_row', 'stale_search_row' (content
        differs) and 'orphan_search_row' (no such concept; the Issue's slug is '#<rowid>').
        Trigram index (`concept_trigram`): 'missing_trigram_rows', 'stale_trigram_rows' (rows
        differ from the concept's name and aliases) and 'orphan_trigram_rows' (rows for a
        concept id that does not exist; the Issue's slug is '#<concept id>').
        On Postgres a word-index row whose words (`concept_search_tokens`) do not match its
        text is stale too, and words of a concept id with neither a row nor a concept orphan.
        """
        concepts = self.list()
        expected = {
            concept.id: (concept.slug, self._search_values(concept)) for concept in concepts
        }
        backend = self._search
        with backend.errors():
            word_index = backend.word_rows(self.session)
        actual = word_index.rows
        issues: list[Issue] = []
        for concept_id, (slug, values) in expected.items():
            if concept_id not in actual:
                issues.append(Issue("missing_search_row", slug, "no concept_search row"))
            elif actual[concept_id] != values or concept_id in word_index.inconsistent:
                issues.append(
                    Issue("stale_search_row", slug, "concept_search row differs from the concept")
                )
        for rowid in sorted((set(actual) | word_index.inconsistent) - set(expected)):
            issues.append(
                Issue(
                    "orphan_search_row", f"#{rowid}", f"concept_search row {rowid} has no concept"
                )
            )

        expected_trigrams = {
            concept.id: (concept.slug, self._trigram_values(concept)) for concept in concepts
        }
        actual_trigrams: dict[int, list[TrigramRow]] = {}
        with backend.errors():
            trigram_rows = backend.trigram_rows(self.session)
        for row in trigram_rows:
            actual_trigrams.setdefault(row[0] // TRIGRAM_SLOTS, []).append(row)
        for concept_id, (slug, rows) in expected_trigrams.items():
            if concept_id not in actual_trigrams:
                issues.append(Issue("missing_trigram_rows", slug, "no concept_trigram rows"))
            elif actual_trigrams[concept_id] != rows:
                issues.append(
                    Issue(
                        "stale_trigram_rows",
                        slug,
                        "concept_trigram rows differ from the concept's name and aliases",
                    )
                )
        for concept_id in sorted(set(actual_trigrams) - set(expected_trigrams)):
            issues.append(
                Issue(
                    "orphan_trigram_rows",
                    f"#{concept_id}",
                    f"concept_trigram has rows for concept id {concept_id}, which does not exist",
                )
            )
        return issues

    def rebuild_search_index(self) -> int:
        """Rewrite both search indexes from the concepts; returns the number of concepts."""
        concepts = self.list()
        backend = self._search
        with self.session.begin_nested(), backend.errors():
            backend.clear(self.session)
            for concept in concepts:
                self._insert_search_row(concept)
        return len(concepts)

    # -- internals --------------------------------------------------------------------------

    def _checked_alias(self, alias: object, name: str, name_norm: str) -> tuple[str, str]:
        if not isinstance(alias, str):
            raise TypeError(f"alias must be a string, got {alias.__class__.__name__}")
        alias_norm = normalize_name(alias, what="alias")
        if alias_norm == name_norm:
            raise ValueError(
                f"alias {alias!r} is the concept's own name {name!r} (compared normalised); "
                "a concept's name is not stored as an alias"
            )
        return alias.strip(), alias_norm

    def _require_free_slug(self, slug: str) -> None:
        taken = self.session.scalar(select(Concept).where(Concept.slug == slug))
        if taken is not None:
            raise DuplicateError(
                f"concept slug {slug!r} already exists (concept {taken.name!r}); pass another "
                "slug"
            )

    @staticmethod
    def _content_changed(concept: Concept) -> None:
        concept.reviewed_at = None
        concept.updated_at = utcnow()

    def _aliases(self, concept: Concept) -> builtins.list[str]:
        """The concept's aliases as stored, in alias order: by normalised alias, then alias,
        compared by code point (read from the database, so the order is the same whether or
        not the relationship was reloaded; sorted here, so it is the same on SQLite and on
        Postgres, whose ORDER BY would follow the database's collation)."""
        rows = self.session.execute(
            select(ConceptAlias.alias_norm, ConceptAlias.alias).where(
                ConceptAlias.concept_id == concept.id
            )
        )
        return [alias for _, alias in sorted(tuple(row) for row in rows)]

    def _search_values(self, concept: Concept) -> tuple[str, str, str]:
        """The concept's concept_search row: (name, aliases newline-separated, summary)."""
        return concept.name, ALIAS_SEPARATOR.join(self._aliases(concept)), concept.summary

    def _trigram_values(self, concept: Concept) -> builtins.list[TrigramRow]:
        """The concept's concept_trigram rows: (rowid, folded text, compact text, label) for
        the name (slot 0) and each alias (slots 1.., in alias order)."""
        labels = [concept.name, *self._aliases(concept)]
        if len(labels) > TRIGRAM_SLOTS:
            raise ValueError(
                f"concept {concept.slug!r} has {len(labels) - 1} aliases; the trigram index "
                f"holds at most {TRIGRAM_SLOTS - 1} per concept"
            )
        rows = []
        for slot, label in enumerate(labels):
            folded = fold(label)
            rows.append((concept.id * TRIGRAM_SLOTS + slot, folded, compact(folded), label))
        return rows

    def _insert_search_row(self, concept: Concept) -> None:
        self._search.insert(
            self.session, concept.id, self._search_values(concept), self._trigram_values(concept)
        )

    def _delete_search_row(self, concept_id: int) -> None:
        """Delete the concept's rows from both indexes."""
        backend = self._search
        with backend.errors():
            backend.delete(self.session, concept_id)

    def _sync_search(self, concept: Concept) -> None:
        """Rewrite the concept's rows in both indexes (call after flushing the change)."""
        self._delete_search_row(concept.id)
        with self._search.errors():
            self._insert_search_row(concept)


__all__ = [
    "ALIAS_SEPARATOR",
    "DEFAULT_SEARCH_LIMIT",
    "SEARCH_WEIGHTS",
    "ConceptManager",
    "DroppedRelation",
    "MergeResult",
    "RetargetedRelation",
    "SearchHit",
    "search_expression",
]
