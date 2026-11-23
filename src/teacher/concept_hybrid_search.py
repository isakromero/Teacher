"""Hybrid concept search: the inverted-index tiers and embedding similarity in one ranked list.

`hybrid_search(session, settings, query, limit, embedder)` is what the graph page's search bar
runs (`GET /api/graph/search`):

1. Lexical: `ConceptManager.search_hits(query, limit)`, unchanged (exact, word, prefix,
   substring, fuzzy; its errors, e.g. an empty query, propagate before anything is embedded).
2. Semantic: the query (stripped) is embedded in ONE request and its `limit` nearest concepts
   are taken with `ConceptEmbeddingManager.similar_to_text`; only those with cosine >=
   `SIMILARITY_FLOOR` count.
3. `merge_hits` makes one list (see its docstring for the order); a concept found both ways
   appears once with both pieces of evidence.

The semantic half never writes to the database and never spends silently on a GET: concepts
whose embedding is missing or out of date are NOT embedded here (that is `teacher concepts
reindex`, or a relating run). Whenever the semantic half cannot run, the lexical results are
still returned and `SemanticStatus` says why, so the page can say the results are names-only:

- `ok`: both halves ran.
- `unavailable`: the embedder cannot be used (no OpenAI API key); nothing was requested.
- `stale`: some concepts have no up-to-date embedding (`stale_count` of `concept_count`), so a
  similarity search would silently miss them; nothing was embedded. Fix: `REINDEX_COMMAND`.
- `failed`: the embedding request failed or the embedding index is unusable (the reason names
  the problem, and for a broken vector index the command that rebuilds it).
- `skipped`: the query is longer than `MAX_SEMANTIC_QUERY_CHARS`, the most embedded for a search.

The approximate-nearest-neighbour index (`teacher.vector_index`) is a derived cache that any
similarity search reconciles and may save to `derived/_vector_index/`; that file is the only
thing a search can write.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy.orm import Session

from teacher.concept_search import EXACT, PREFIX, WORD
from teacher.config import Settings
from teacher.db.models import Concept
from teacher.embeddings import Embedder, EmbeddingError
from teacher.managers import (
    ConceptEmbeddingManager,
    ConceptManager,
    EmbeddingIndexError,
    StaleEmbeddingsError,
)
from teacher.managers.concepts import SearchHit

# A user's search is a word, a phrase or a question, compared with a concept's text (name,
# aliases and a summary of a sentence or two). Such asymmetric pairs score lower than two concept
# texts: with OpenAI's text-embedding-3 models a short query and a passage about it typically
# reach about 0.25 to 0.5, while unrelated texts stay below about 0.2. So the floor sits below
# the relate pipeline's concept-vs-concept 0.3 (same field) and far below the extraction's
# 0.5 (same concept): 0.25 keeps a phrase's real matches and drops what is clearly unrelated.
# Borderline hits are acceptable because each result shows its similarity and lexical hits are
# never displaced by semantic ones (see `merge_hits`). A guess tied to the embedding model, to be
# tuned on real data (only fakes test it).
SIMILARITY_FLOOR = 0.25
# The longest query embedded (characters, after stripping). A search box query is short; this
# only stops pasted pages from being embedded on every keystroke-and-enter. Longer queries are
# still searched lexically, with status `skipped`.
MAX_SEMANTIC_QUERY_CHARS = 500
REINDEX_COMMAND = "teacher concepts reindex"

STRONG_KINDS = frozenset({EXACT, WORD, PREFIX})  # lexical kinds ranked before everything else

SemanticState = Literal["ok", "unavailable", "stale", "failed", "skipped"]
SEMANTIC_STATES: tuple[str, ...] = ("ok", "unavailable", "stale", "failed", "skipped")


@dataclass(frozen=True)
class SemanticStatus:
    """How the semantic half of one search went (see the module docstring). `reason` says why
    in words for every status but `ok`; `fix` is the command that repairs a `stale` index."""

    status: SemanticState
    model: str
    floor: float
    reason: str | None = None
    stale_count: int | None = None
    concept_count: int | None = None
    fix: str | None = None


@dataclass(frozen=True)
class HybridHit:
    """One merged result: `lexical` is its search hit (None if only found by meaning) and
    `similarity` the query's cosine to it (None if it was not among the nearest concepts at or
    above the floor, or the semantic half did not run). At least one of the two is set."""

    concept: Concept
    lexical: SearchHit | None
    similarity: float | None


@dataclass(frozen=True)
class HybridResult:
    hits: list[HybridHit]
    semantic: SemanticStatus


def merge_hits(
    lexical: Sequence[SearchHit],
    semantic: Sequence[tuple[Concept, float]],
    limit: int,
) -> list[HybridHit]:
    """One ranked list from the lexical hits (at most `limit`, in tier order) and the semantic
    neighbours (best first, already at or above the floor). Deterministic:

    1. exact, word and prefix hits, in their lexical order (the query's own words found them);
    2. then, by similarity (highest first, ties by slug), the substring and fuzzy hits that
       also have a similarity, together with the concepts found only by meaning;
    3. then the substring and fuzzy hits without a similarity, in their lexical order.

    Every lexical hit is kept: concepts found only by meaning fill the room the lexical hits
    leave under `limit` (the best of them first), so adding meaning never pushes out a match
    on the words typed. Without semantic neighbours the order is exactly the lexical order."""
    similarity = {concept.id: score for concept, score in semantic}
    lexical_ids = {hit.concept.id for hit in lexical}
    strong = [hit for hit in lexical if hit.kind in STRONG_KINDS]
    weak = [hit for hit in lexical if hit.kind not in STRONG_KINDS]
    room = max(0, limit - len(lexical))
    meaning_only = [(c, s) for c, s in semantic if c.id not in lexical_ids][:room]
    ranked = [
        HybridHit(hit.concept, hit, similarity[hit.concept.id])
        for hit in weak
        if hit.concept.id in similarity
    ] + [HybridHit(concept, None, score) for concept, score in meaning_only]
    ranked.sort(key=lambda h: (-(h.similarity or 0.0), h.concept.slug))
    return (
        [HybridHit(hit.concept, hit, similarity.get(hit.concept.id)) for hit in strong]
        + ranked
        + [HybridHit(hit.concept, hit, None) for hit in weak if hit.concept.id not in similarity]
    )


def semantic_neighbours(
    session: Session, settings: Settings, query: str, limit: int, embedder: Embedder
) -> tuple[list[tuple[Concept, float]], SemanticStatus]:
    """The query's nearest concepts at or above the floor, and the status (see the module
    docstring). At most one embedding request; none unless the status can be `ok`."""
    model = embedder.model

    def status(state: SemanticState, reason: str | None = None, **extra: Any) -> SemanticStatus:
        return SemanticStatus(state, model, SIMILARITY_FLOOR, reason, **extra)

    text = query.strip()
    if len(text) > MAX_SEMANTIC_QUERY_CHARS:
        return [], status(
            "skipped",
            f"the search is {len(text)} characters long; meaning is searched only for searches "
            f"of at most {MAX_SEMANTIC_QUERY_CHARS} characters",
        )
    try:
        embedder.check_available()
    except EmbeddingError as exc:
        return [], status("unavailable", str(exc))
    try:
        found = ConceptEmbeddingManager(session, settings).similar_to_text(text, embedder, limit)
    except StaleEmbeddingsError as exc:
        if exc.stale_count is None or exc.concept_count is None:
            raise  # every StaleEmbeddingsError from the manager is counted; never guess
        return [], status(
            "stale",
            f"{exc.stale_count} of {exc.concept_count} concepts have no up-to-date embedding "
            f"for {model!r}, so a search by meaning would miss them",
            stale_count=exc.stale_count,
            concept_count=exc.concept_count,
            fix=REINDEX_COMMAND,
        )
    except EmbeddingError as exc:  # the request failed, or its answer was unusable
        return [], status("failed", str(exc))
    except EmbeddingIndexError as exc:  # mixed dimensions, a corrupt vector, a broken index
        return [], status("failed", str(exc))
    return [(c, s) for c, s in found if s >= SIMILARITY_FLOOR], status("ok")


def hybrid_search(
    session: Session, settings: Settings, query: str, limit: int, embedder: Embedder
) -> HybridResult:
    """Search names, aliases and summaries, and meaning (see the module docstring). The lexical
    search runs first, so a bad query or limit fails (ValueError/TypeError) before any
    embedding request."""
    hits = ConceptManager(session, settings).search_hits(query, limit)
    neighbours, semantic = semantic_neighbours(session, settings, query, limit, embedder)
    return HybridResult(merge_hits(hits, neighbours, limit), semantic)


__all__ = [
    "MAX_SEMANTIC_QUERY_CHARS",
    "REINDEX_COMMAND",
    "SEMANTIC_STATES",
    "SIMILARITY_FLOOR",
    "STRONG_KINDS",
    "HybridHit",
    "HybridResult",
    "SemanticStatus",
    "hybrid_search",
    "merge_hits",
    "semantic_neighbours",
]
