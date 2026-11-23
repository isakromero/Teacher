"""Hybrid concept search (`teacher.concept_hybrid_search`) with a fake embedder (no network): the
merge order (pure), lexical-only, semantic-only and both, the floor, the limit, every semantic
status with the lexical results still returned, one embedding request per search and none for
an empty query, and that a search writes nothing to the database."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text

from teacher.concept_hybrid_search import (
    MAX_SEMANTIC_QUERY_CHARS,
    REINDEX_COMMAND,
    SIMILARITY_FLOOR,
    HybridHit,
    hybrid_search,
    merge_hits,
)
from teacher.config import Settings
from teacher.db.engine import session_scope
from teacher.db.models import Concept
from teacher.managers import ConceptEmbeddingManager, ConceptManager, VectorIndexError
from teacher.managers.concepts import SearchHit

# FakeEmbedder vectors, keyed by the first line of a text: a concept's name, or a query.
# Concepts: attention e1, softmax e2, gradient descent e3, stochastic gradient descent
# (0.8 e3 + 0.6 e4), momentum e4. Queries are unit vectors with the cosines in the comments.
TABLE = {
    "attention": [1.0],
    "softmax": [0.0, 1.0],
    "gradient descent": [0.0, 0.0, 1.0],
    "stochastic gradient descent": [0.0, 0.0, 0.8, 0.6],
    "momentum": [0.0, 0.0, 0.0, 1.0],
    # attention 0.9, the rest 0
    "how do models focus on their inputs": [0.9, 0.0, 0.0, 0.0, 0.0, 0.43589],
    # gradient descent 0.5, stochastic gradient descent 0.58, momentum 0.3
    "learning by small steps": [0.0, 0.0, 0.5, 0.3, 0.0, 0.81240],
    # momentum 0.954, stochastic gradient descent 0.812, gradient descent 0.3
    "radient": [0.0, 0.0, 0.3, 0.95394],
    # attention 0.24 (just below the floor), gradient descent 0.26 (just above), SGD 0.208
    "barely related": [0.24, 0.0, 0.26, 0.0, 0.0, 0.93531],
}
NAMES = ("attention", "softmax", "gradient descent", "stochastic gradient descent", "momentum")


@pytest.fixture
def embedder(fake_embedder: type) -> Any:
    return fake_embedder(TABLE)


@pytest.fixture
def seeded(settings: Settings, embedder: Any) -> dict[str, int]:
    """The concepts in NAMES, embedded (the embedder's calls are then cleared)."""
    with session_scope(settings) as session:
        concepts = ConceptManager(session, settings)
        ids = {c.slug: c.id for c in (concepts.create(n, f"About {n}.", "user") for n in NAMES)}
        ConceptEmbeddingManager(session, settings).embed_stale(embedder)
    embedder.calls.clear()
    return ids


def search(settings: Settings, embedder: Any, query: str, limit: int = 20) -> Any:
    with session_scope(settings) as session:
        result = hybrid_search(session, settings, query, limit, embedder)
        # plain values, so nothing is read from a closed session
        hits = [
            (h.concept.slug, h.lexical.kind if h.lexical else None,
             None if h.similarity is None else round(h.similarity, 3))
            for h in result.hits
        ]
        return hits, result.semantic


def tables(settings: Settings) -> dict[str, list]:
    with session_scope(settings) as session:
        return {
            table: sorted(session.execute(text(f"SELECT * FROM {table}")).all())
            for table in ("concepts", "concept_embeddings", "concept_relations",
                          "concept_pair_judgements")
        }


# -- the merge (pure) ------------------------------------------------------------------------------


def concept(id: int, slug: str) -> Concept:
    return Concept(id=id, slug=slug, name=slug, summary="S.", origin="user")


C = {slug: concept(i, slug) for i, slug in enumerate("abcdefghi", start=1)}


def hit(slug: str, kind: str) -> SearchHit:
    return SearchHit(C[slug], kind, 1.0, "name", slug)


def summary(hits: list[HybridHit]) -> list[tuple[str, str | None, float | None]]:
    return [(h.concept.slug, h.lexical.kind if h.lexical else None, h.similarity) for h in hits]


LEXICAL = [hit("a", "exact"), hit("b", "word"), hit("e", "prefix"), hit("c", "substring"),
           hit("d", "fuzzy"), hit("f", "substring")]
SEMANTIC = [(C["g"], 0.9), (C["c"], 0.7), (C["b"], 0.6), (C["h"], 0.5), (C["i"], 0.5)]


def test_merge_orders_strong_lexical_then_by_similarity_then_weak_lexical() -> None:
    assert summary(merge_hits(LEXICAL, SEMANTIC, 8)) == [
        # exact, word, prefix in their lexical order (b also carries its similarity)
        ("a", "exact", None), ("b", "word", 0.6), ("e", "prefix", None),
        # by similarity: meaning-only g, substring c, meaning-only h (i: no room left)
        ("g", None, 0.9), ("c", "substring", 0.7), ("h", None, 0.5),
        # substring/fuzzy hits without a similarity, in their lexical order
        ("d", "fuzzy", None), ("f", "substring", None),
    ]


def test_merge_never_displaces_a_lexical_hit() -> None:
    # The lexical hits fill the limit: nothing found only by meaning is added, but the
    # similarities of the lexical hits are still shown.
    assert summary(merge_hits(LEXICAL, SEMANTIC, 6)) == [
        ("a", "exact", None), ("b", "word", 0.6), ("e", "prefix", None),
        ("c", "substring", 0.7), ("d", "fuzzy", None), ("f", "substring", None),
    ]
    # Room for exactly the best meaning-only concept.
    assert [s for s, _k, _v in summary(merge_hits(LEXICAL, SEMANTIC, 7))] == [
        "a", "b", "e", "g", "c", "d", "f"
    ]


def test_merge_without_semantic_neighbours_is_the_lexical_order() -> None:
    assert summary(merge_hits(LEXICAL, [], 20)) == [
        (h.concept.slug, h.kind, None) for h in LEXICAL
    ]
    assert summary(merge_hits([], SEMANTIC, 3)) == [("g", None, 0.9), ("c", None, 0.7),
                                                    ("b", None, 0.6)]
    assert merge_hits([], [], 5) == []


def test_merge_breaks_similarity_ties_by_slug_and_lists_each_concept_once() -> None:
    lexical = [hit("f", "substring"), hit("d", "fuzzy")]
    semantic = [(C["d"], 0.5), (C["f"], 0.5), (C["b"], 0.5)]
    merged = summary(merge_hits(lexical, semantic, 10))
    assert merged == [("b", None, 0.5), ("d", "fuzzy", 0.5), ("f", "substring", 0.5)]
    assert len({slug for slug, _k, _v in merged}) == len(merged)


# -- searches --------------------------------------------------------------------------------------


def test_lexical_only(settings: Settings, embedder, seeded) -> None:
    # "gradient" gets a free axis from the fake: orthogonal to every concept, so nothing is
    # found by meaning although meaning was searched.
    hits, semantic = search(settings, embedder, "gradient")
    assert {(s, k, v) for s, k, v in hits} == {
        ("gradient-descent", "word", None), ("stochastic-gradient-descent", "word", None),
    }
    assert semantic.status == "ok" and semantic.reason is None
    assert embedder.calls == [["gradient"]]  # one embedding request per search


def test_semantic_only(settings: Settings, embedder, seeded) -> None:
    hits, semantic = search(settings, embedder, "how do models focus on their inputs")
    assert hits == [("attention", None, 0.9)]
    assert semantic.status == "ok"
    hits, _ = search(settings, embedder, "learning by small steps")
    assert hits == [("stochastic-gradient-descent", None, 0.58),
                    ("gradient-descent", None, 0.5), ("momentum", None, 0.3)]


def test_found_both_ways_appears_once_with_both_evidences(settings, embedder, seeded) -> None:
    hits, _ = search(settings, embedder, "gradient descent")
    assert hits == [
        ("gradient-descent", "exact", 1.0),
        ("stochastic-gradient-descent", "word", 0.8),
    ]
    # Substring hits ranked by meaning, with a concept found only by meaning between them.
    hits, _ = search(settings, embedder, "radient")
    assert hits == [
        ("momentum", None, 0.954),
        ("stochastic-gradient-descent", "substring", 0.812),
        ("gradient-descent", "substring", 0.3),
    ]


def test_the_floor(settings: Settings, embedder, seeded) -> None:
    assert SIMILARITY_FLOOR == 0.25
    hits, _ = search(settings, embedder, "barely related")
    assert hits == [("gradient-descent", None, 0.26)]  # attention (0.24), SGD (0.208) are below


def test_the_limit(settings: Settings, embedder, seeded) -> None:
    hits, _ = search(settings, embedder, "learning by small steps", limit=1)
    assert hits == [("stochastic-gradient-descent", None, 0.58)]
    hits, _ = search(settings, embedder, "radient", limit=2)  # both lexical hits stay
    assert [(s, k) for s, k, _v in hits] == [("stochastic-gradient-descent", "substring"),
                                             ("gradient-descent", "substring")]
    with pytest.raises(ValueError, match="limit must be a positive integer"):
        search(settings, embedder, "radient", limit=0)
    assert embedder.calls == [["learning by small steps"], ["radient"]]


def test_the_query_is_embedded_stripped(settings, embedder, seeded) -> None:
    hits, _ = search(settings, embedder, "   learning by small steps \n")
    assert hits[0][0] == "stochastic-gradient-descent"
    assert embedder.calls == [["learning by small steps"]]


@pytest.mark.parametrize("query", ["", "   ", "(*"])
def test_a_bad_query_fails_before_anything_is_embedded(settings, embedder, seeded, query) -> None:
    with pytest.raises(ValueError):
        search(settings, embedder, query)
    assert embedder.calls == []


def test_an_empty_graph(settings: Settings, embedder) -> None:
    hits, semantic = search(settings, embedder, "anything")
    assert hits == [] and semantic.status == "ok" and embedder.calls == []


# -- when meaning cannot be searched ---------------------------------------------------------------


def test_unavailable_embedder_returns_the_lexical_results(settings, embedder, seeded) -> None:
    embedder.available = False
    before = tables(settings)
    hits, semantic = search(settings, embedder, "gradient descent")
    assert hits == [("gradient-descent", "exact", None),
                    ("stochastic-gradient-descent", "word", None)]
    assert semantic.status == "unavailable" and "no OpenAI API key (fake)" in semantic.reason
    assert semantic.model == "fake-embed" and semantic.floor == SIMILARITY_FLOOR
    assert embedder.calls == [] and tables(settings) == before


def test_stale_embeddings_are_reported_not_embedded(settings, embedder, seeded) -> None:
    with session_scope(settings) as session:
        concepts = ConceptManager(session, settings)
        concepts.create("dropout", "Randomly zeroing units.", "user")  # never embedded
        concepts.update(concepts.get_by_slug("softmax"), summary="Changed.")  # out of date
    before = tables(settings)
    hits, semantic = search(settings, embedder, "gradient descent")
    assert [s for s, _k, _v in hits] == ["gradient-descent", "stochastic-gradient-descent"]
    assert all(v is None for _s, _k, v in hits)
    assert (semantic.status, semantic.stale_count, semantic.concept_count, semantic.fix) == (
        "stale", 2, 6, REINDEX_COMMAND)
    assert semantic.reason == ("2 of 6 concepts have no up-to-date embedding for 'fake-embed', so "
                               "a search by meaning would miss them")
    assert embedder.calls == []  # neither the query nor the stale concepts were embedded
    assert tables(settings) == before  # a search never writes


def test_a_failed_embedding_request_returns_the_lexical_results(settings, embedder, seeded) -> None:
    embedder.fail_on = {1}
    before = tables(settings)
    hits, semantic = search(settings, embedder, "gradient descent")
    assert [k for _s, k, _v in hits] == ["exact", "word"]
    assert semantic.status == "failed" and "fake failure on call 1" in semantic.reason
    assert len(embedder.calls) == 1 and tables(settings) == before


def test_a_bad_embedding_answer_or_index_is_a_failure(
    settings, embedder, seeded, monkeypatch: pytest.MonkeyPatch
) -> None:
    embedder.table["gradient descent"] = [0.0, 0.0, 1.0] + [0.0] * 3  # query vector: fine
    original = embedder.embed
    embedder.embed = lambda texts: [[1.0, 2.0]]  # wrong dimensions for the stored vectors
    _hits, semantic = search(settings, embedder, "gradient descent")
    assert semantic.status == "failed" and "2-dimensional vector for the query" in semantic.reason
    embedder.embed = original

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise VectorIndexError("the index file is corrupt. Rebuild it with `x`")

    monkeypatch.setattr(ConceptEmbeddingManager, "similar_to_text", broken)
    hits, semantic = search(settings, embedder, "gradient descent")
    assert semantic.status == "failed" and "Rebuild it" in semantic.reason
    assert [k for _s, k, _v in hits] == ["exact", "word"]


def test_a_very_long_query_is_searched_by_name_only(settings, embedder, seeded) -> None:
    query = "gradient " + "x" * MAX_SEMANTIC_QUERY_CHARS
    hits, semantic = search(settings, embedder, query)
    assert semantic.status == "skipped" and f"at most {MAX_SEMANTIC_QUERY_CHARS}" in semantic.reason
    assert embedder.calls == []
    assert hits == []  # no concept has that long a word; the lexical search still ran
    _hits, semantic = search(settings, embedder, "x" * MAX_SEMANTIC_QUERY_CHARS)
    assert semantic.status == "ok"  # exactly the maximum is embedded
