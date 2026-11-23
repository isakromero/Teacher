"""ConceptEmbeddingManager with a deterministic fake embedder: staleness, storage, similarity."""

from __future__ import annotations

import hashlib
import math
import struct

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from teacher.config import Settings
from teacher.db.models import Concept, ConceptEmbedding
from teacher.embeddings import Embedder, EmbeddingResponseError, concept_text
from teacher.managers import (
    ConceptEmbeddingManager,
    ConceptManager,
    EmbeddingIndexError,
    StaleEmbeddingsError,
)
from teacher.managers.concept_embeddings import text_of

# Concept name (the first line of its text) -> vector. Queries use the same table.
VECTORS = {
    "Attention": [1.0, 0.0, 0.0],
    "Self-attention": [0.9, 0.1, 0.0],
    "Softmax": [0.5, 0.5, 0.0],
    "Convolution": [0.0, 0.0, 1.0],
    "query: weights": [1.0, 0.05, 0.0],
}


class FakeEmbedder:
    """Looks each text's first line up in `table`; records every call."""

    def __init__(self, model: str = "fake-3", table: dict[str, list[float]] | None = None) -> None:
        self.model = model
        self.table = dict(VECTORS if table is None else table)
        self.calls: list[list[str]] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [list(self.table[t.split("\n")[0]]) for t in texts]


class BadEmbedder(FakeEmbedder):
    def __init__(self, result: object) -> None:
        super().__init__()
        self.result = result

    def embed(self, texts: list[str]) -> list[list[float]]:  # type: ignore[override]
        self.calls.append(list(texts))
        return self.result  # type: ignore[return-value]


@pytest.fixture
def concepts(session: Session, settings: Settings) -> ConceptManager:
    return ConceptManager(session, settings)


@pytest.fixture
def embeddings(session: Session, settings: Settings) -> ConceptEmbeddingManager:
    return ConceptEmbeddingManager(session, settings)


@pytest.fixture
def four(concepts: ConceptManager) -> dict[str, Concept]:
    names = ["Attention", "Self-attention", "Softmax", "Convolution"]
    return {name: concepts.create(name, f"All about {name.lower()}.", "paper") for name in names}


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    return dot / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


def test_fake_satisfies_the_protocol() -> None:
    assert isinstance(FakeEmbedder(), Embedder)


def test_numpy_scalars_are_accepted_as_vector_values(
    embeddings: ConceptEmbeddingManager, four: dict[str, Concept]
) -> None:
    import numpy as np

    table = {name: [np.float32(x) for x in vector] for name, vector in VECTORS.items()}
    assert embeddings.embed_stale(FakeEmbedder(table=table)) == 4  # type: ignore[arg-type]
    assert four["Softmax"].embedding.vector == struct.pack("<3f", 0.5, 0.5, 0.0)  # type: ignore[union-attr]


def test_embed_stale_embeds_exactly_the_stale_concepts(
    embeddings: ConceptEmbeddingManager, four: dict[str, Concept], session: Session
) -> None:
    fake = FakeEmbedder()
    assert embeddings.stale("fake-3") == sorted(four.values(), key=lambda c: c.slug)
    assert embeddings.embed_stale(fake) == 4
    session.commit()
    assert embeddings.stale("fake-3") == []
    assert embeddings.embed_stale(fake) == 0
    assert len(fake.calls) == 1  # nothing stale: the embedder is not called

    row = four["Softmax"].embedding
    assert row is not None
    assert (row.model, row.dimensions) == ("fake-3", 3)
    assert row.vector == struct.pack("<3f", 0.5, 0.5, 0.0)
    expected_text = "Softmax\nAll about softmax."
    assert text_of(four["Softmax"]) == expected_text
    assert row.text_hash == hashlib.sha256(expected_text.encode()).hexdigest()


def test_text_changes_make_only_that_concept_stale(
    embeddings: ConceptEmbeddingManager, concepts: ConceptManager, four: dict[str, Concept],
    session: Session,
) -> None:
    fake = FakeEmbedder()
    embeddings.embed_stale(fake)
    first = four["Softmax"].embedding.embedded_at  # type: ignore[union-attr]
    concepts.update(four["Softmax"], summary="Normalised exponentials.")
    concepts.add_alias(four["Attention"], "attn")
    assert [c.slug for c in embeddings.stale("fake-3")] == ["attention", "softmax"]
    assert embeddings.embed_stale(fake) == 2
    assert [t.split("\n")[0] for t in fake.calls[-1]] == ["Attention", "Softmax"]
    assert fake.calls[-1][0] == "Attention\nAlso known as: attn\nAll about attention."
    assert four["Softmax"].embedding.embedded_at >= first  # type: ignore[union-attr]
    # The stored vector is now of the new summary, so reverting the summary makes it stale again.
    concepts.update(four["Softmax"], summary="All about softmax.")
    assert [c.slug for c in embeddings.stale("fake-3")] == ["softmax"]
    session.commit()


def test_a_different_model_makes_everything_stale_and_replaces_the_vectors(
    embeddings: ConceptEmbeddingManager, four: dict[str, Concept], session: Session
) -> None:
    embeddings.embed_stale(FakeEmbedder("fake-3"))
    assert len(embeddings.stale("fake-4")) == 4
    with pytest.raises(StaleEmbeddingsError, match="4 of 4 concepts have no up-to-date embedding"):
        embeddings.similar_to_concept(four["Attention"], "fake-4")
    table = {name: [*vector, 1.0] for name, vector in VECTORS.items()}
    assert embeddings.embed_stale(FakeEmbedder("fake-4", table)) == 4
    session.commit()
    assert session.scalar(text("SELECT count(*) FROM concept_embeddings")) == 4  # one per concept
    assert session.scalar(text("SELECT DISTINCT model FROM concept_embeddings")) == "fake-4"
    assert len(embeddings.stale("fake-3")) == 4


def test_similarity_search_refuses_a_partial_index(
    embeddings: ConceptEmbeddingManager, concepts: ConceptManager, four: dict[str, Concept]
) -> None:
    fake = FakeEmbedder()
    embeddings.embed_stale(fake)
    concepts.create("Pooling", "Downsampling.", "paper")
    calls = len(fake.calls)
    with pytest.raises(
        StaleEmbeddingsError,
        match=r"1 of 5 concepts have no up-to-date embedding for model 'fake-3' \('pooling'\); "
        "run embed_stale first",
    ):
        embeddings.similar_to_text("query: weights", fake)
    assert len(fake.calls) == calls  # checked before the query text is embedded
    with pytest.raises(StaleEmbeddingsError):
        embeddings.similar_to_concept(four["Attention"], fake)


def test_stale_error_names_at_most_five_slugs(
    embeddings: ConceptEmbeddingManager, concepts: ConceptManager
) -> None:
    for name in "abcdefg":
        concepts.create(name, "x.", "user")
    with pytest.raises(
        StaleEmbeddingsError, match=r"7 of 7 .*\('a', 'b', 'c', 'd', 'e' and 2 more\)"
    ):
        embeddings.similar_to_text("anything", FakeEmbedder())


def test_similar_to_concept_ranks_by_cosine_and_excludes_itself(
    embeddings: ConceptEmbeddingManager, four: dict[str, Concept]
) -> None:
    fake = FakeEmbedder()
    embeddings.embed_stale(fake)
    ranked = embeddings.similar_to_concept(four["Attention"], fake)
    assert [c.name for c, _ in ranked] == ["Self-attention", "Softmax", "Convolution"]
    for concept, score in ranked:
        assert score == pytest.approx(cosine(VECTORS["Attention"], VECTORS[concept.name]), abs=1e-6)
    assert embeddings.similar_to_concept(four["Attention"], "fake-3", limit=1) == ranked[:1]
    assert len(fake.calls) == 1  # the concept's stored vector is the query


def test_similar_to_text_embeds_the_query_and_breaks_ties_by_slug(
    embeddings: ConceptEmbeddingManager, concepts: ConceptManager, four: dict[str, Concept]
) -> None:
    table = VECTORS | {"Attention twin": VECTORS["Attention"]}
    concepts.create("Attention twin", "Same vector.", "user", slug="a-twin")
    fake = FakeEmbedder(table=table)
    embeddings.embed_stale(fake)
    ranked = embeddings.similar_to_text("query: weights", fake, limit=3)
    assert fake.calls[-1] == ["query: weights"]
    assert [c.slug for c, _ in ranked] == ["a-twin", "attention", "self-attention"]
    assert ranked[0][1] == ranked[1][1]
    assert ranked[0][1] == pytest.approx(cosine(VECTORS["query: weights"], [1.0, 0.0, 0.0]))


def test_similar_to_text_with_no_concepts_embeds_nothing(
    embeddings: ConceptEmbeddingManager,
) -> None:
    fake = FakeEmbedder()
    assert embeddings.similar_to_text("query: weights", fake) == []
    assert fake.calls == []


@pytest.mark.parametrize(
    ("call", "error", "fragment"),
    [
        (lambda m, c, f: m.similar_to_text(" ", f), ValueError, "non-blank string"),
        (lambda m, c, f: m.similar_to_text("x", f, limit=0), ValueError, "limit must be"),
        (lambda m, c, f: m.similar_to_concept(c, "", 3), ValueError, "model must be"),
        (lambda m, c, f: m.stale(" "), ValueError, "model must be"),
    ],
)
def test_argument_errors(
    embeddings: ConceptEmbeddingManager, four: dict[str, Concept],
    call: object, error: type, fragment: str,
) -> None:
    with pytest.raises(error, match=fragment):
        call(embeddings, four["Attention"], FakeEmbedder())  # type: ignore[operator]


@pytest.mark.parametrize(
    ("result", "fragment"),
    [
        ([[1.0, 0.0]] * 3, "returned 3 vectors for 4 texts"),
        ([[1.0, 0.0]] * 3 + [[1.0]], "vector 4 has 1 dimensions, expected 2"),
        ([[1.0]] * 3 + [[float("nan")]], "vector 4 contains nan"),
        ([[1.0]] * 3 + [[]], "vector 4 is empty"),
        ([[1.0]] * 3 + [["1"]], "vector 4 contains a non-number"),
        ("nope", "returned str, not a list of vectors"),
    ],
)
def test_bad_embedder_output_stores_nothing(
    embeddings: ConceptEmbeddingManager, four: dict[str, Concept], session: Session,
    result: object, fragment: str,
) -> None:
    with pytest.raises(EmbeddingResponseError, match=fragment):
        embeddings.embed_stale(BadEmbedder(result))
    assert session.scalar(text("SELECT count(*) FROM concept_embeddings")) == 0


def test_new_vectors_must_match_the_stored_dimensions(
    embeddings: ConceptEmbeddingManager, concepts: ConceptManager, four: dict[str, Concept],
    session: Session,
) -> None:
    embeddings.embed_stale(FakeEmbedder())
    concepts.create("Pooling", "Downsampling.", "paper")
    wide = FakeEmbedder(table={"Pooling": [1.0, 2.0, 3.0, 4.0]})
    with pytest.raises(
        EmbeddingIndexError, match="returned 4-dimensional vectors, but the stored embedding of 'attention' has 3"
    ):
        embeddings.embed_stale(wide)
    assert session.scalar(text("SELECT count(*) FROM concept_embeddings")) == 4


def test_mixed_dimensions_in_the_database_raise(
    embeddings: ConceptEmbeddingManager, four: dict[str, Concept], session: Session
) -> None:
    embeddings.embed_stale(FakeEmbedder())
    row = four["Softmax"].embedding
    assert row is not None
    row.dimensions, row.vector = 2, struct.pack("<2f", 0.5, 0.5)
    session.flush()
    with pytest.raises(EmbeddingIndexError, match=r"mixed dimensions \[2, 3\]"):
        embeddings.similar_to_concept(four["Attention"], "fake-3")


def test_query_dimension_mismatch_and_zero_vectors_raise(
    embeddings: ConceptEmbeddingManager, four: dict[str, Concept], session: Session
) -> None:
    fake = FakeEmbedder(table=VECTORS | {"short": [1.0], "zero": [0.0, 0.0, 0.0]})
    embeddings.embed_stale(fake)
    with pytest.raises(EmbeddingIndexError, match="1-dimensional vector for the query, but the stored embeddings have 3"):
        embeddings.similar_to_text("short", fake)
    with pytest.raises(EmbeddingIndexError, match="zero vector for the query"):
        embeddings.similar_to_text("zero", fake)
    zero = FakeEmbedder("zero-model", table={n: [0.0, 0.0] for n in VECTORS})
    embeddings.embed_stale(zero)
    with pytest.raises(EmbeddingIndexError, match="has length 0.0, so it has no direction"):
        embeddings.similar_to_concept(four["Attention"], zero)


def test_vector_size_is_checked_by_the_database(
    four: dict[str, Concept], session: Session
) -> None:
    session.add(
        ConceptEmbedding(
            concept_id=four["Attention"].id, model="m", dimensions=3, vector=b"\x00" * 8,
            text_hash="h",
        )
    )
    with pytest.raises(IntegrityError, match="ck_concept_embeddings_vector_size"):
        session.flush()


def test_merge_deletes_the_source_embedding_and_makes_into_stale(
    embeddings: ConceptEmbeddingManager, concepts: ConceptManager, four: dict[str, Concept],
    session: Session,
) -> None:
    embeddings.embed_stale(FakeEmbedder())
    concepts.merge(four["Self-attention"], four["Attention"])
    session.commit()
    assert session.scalar(text("SELECT count(*) FROM concept_embeddings")) == 3
    assert [c.slug for c in embeddings.stale("fake-3")] == ["attention"]


def test_concept_text_is_deterministic() -> None:
    assert concept_text("GAN", ["b", "A", "c"], "Two nets.") == (
        "GAN\nAlso known as: A; b; c\nTwo nets."
    )
    assert concept_text("GAN", ["c", "b", "A"], "Two nets.") == concept_text(
        "GAN", ["A", "b", "c"], "Two nets."
    )
    assert concept_text("GAN", [], "Two nets.") == "GAN\nTwo nets."
