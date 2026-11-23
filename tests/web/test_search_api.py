"""`GET /api/graph/search`: hybrid search through the app with a fake embedder (no network): the
response shape, every semantic status with the lexical results still returned, no write and one
embedding request per search, the search embedder's wiring in `create_app`."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from teacher.concept_hybrid_search import MAX_SEMANTIC_QUERY_CHARS, SIMILARITY_FLOOR
from teacher.concept_relating import ConceptRelater, StubEmbedder
from teacher.config import Settings
from teacher.db.engine import session_scope
from teacher.embeddings import OpenAIEmbedder
from teacher.generation import StubGenerator
from teacher.managers import ConceptEmbeddingManager, ConceptManager
from teacher.web.app import create_app

TABLE = {
    "attention": [1.0],
    "gradient descent": [0.0, 1.0],
    "stochastic gradient descent": [0.0, 0.8, 0.6],
    "how do models focus on their inputs": [0.9, 0.0, 0.0, 0.43589],
}
URL = "/api/graph/search"


@pytest.fixture
def embedder(fake_embedder: type) -> Any:
    return fake_embedder(TABLE)


@pytest.fixture
def seeded(settings: Settings, embedder: Any) -> None:
    with session_scope(settings) as session:
        concepts = ConceptManager(session, settings)
        for name in TABLE:
            if not name.startswith("how"):
                concepts.create(name, f"About {name}.", "paper", aliases=["GD"] if name ==
                                "gradient descent" else [])
        ConceptEmbeddingManager(session, settings).embed_stale(embedder)
    embedder.calls.clear()


@pytest.fixture
def client(settings: Settings, embedder: Any) -> Iterator[TestClient]:
    relater = ConceptRelater(embedder=embedder, model="claude-test-relations")
    with TestClient(create_app(settings, generator=StubGenerator(),
                               concept_relater=relater)) as test_client:
        yield test_client


def get(client: TestClient, query: str, status: int = 200) -> Any:
    response = client.get(f"{URL}?{query}")
    assert response.status_code == status, response.text
    return response.json()


def tables(settings: Settings) -> dict[str, list]:
    with session_scope(settings) as session:
        return {
            table: sorted(session.execute(text(f"SELECT * FROM {table}")).all())
            for table in ("concepts", "concept_embeddings", "concept_relations",
                          "concept_pair_judgements", "concept_aliases")
        }


def test_the_response_shape(client: TestClient, seeded, embedder) -> None:
    data = get(client, "q=gradient%20descent")
    assert set(data) == {"query", "limit", "results", "semantic"}
    assert data["semantic"] == {
        "status": "ok", "reason": None, "model": "fake-embed", "floor": SIMILARITY_FLOOR,
        "stale_count": None, "concept_count": None, "fix": None,
    }
    first, second = data["results"]
    assert set(first) == {"id", "slug", "name", "origin", "reviewed", "summary", "match_kind",
                          "match_field", "matched", "similarity"}
    assert (first["slug"], first["match_kind"], first["match_field"], first["similarity"]) == (
        "gradient-descent", "exact", "name", 1.0)
    assert (second["slug"], second["match_kind"], second["similarity"]) == (
        "stochastic-gradient-descent", "word", 0.8)
    # Found only by meaning: no lexical evidence, only the similarity.
    [only] = get(client, "q=how%20do%20models%20focus%20on%20their%20inputs")["results"]
    assert (only["slug"], only["match_kind"], only["match_field"], only["matched"],
            only["similarity"]) == ("attention", None, None, None, 0.9)
    assert embedder.calls == [["gradient descent"], ["how do models focus on their inputs"]]


def test_the_limit_and_the_alias_evidence(client: TestClient, seeded) -> None:
    data = get(client, "q=GD&limit=1")
    assert data["limit"] == 1
    [result] = data["results"]
    assert (result["slug"], result["match_kind"], result["match_field"], result["matched"]) == (
        "gradient-descent", "exact", "alias", "GD")


def test_a_search_writes_nothing(client: TestClient, settings: Settings, seeded) -> None:
    before = tables(settings)
    get(client, "q=gradient")
    get(client, "q=how%20do%20models%20focus%20on%20their%20inputs")
    assert tables(settings) == before


@pytest.mark.parametrize(
    ("query", "detail"),
    [("q=", "search query must not be empty"), ("q=%20%20", "search query must not be empty"),
     ("q=attention&limit=0", "limit must be a positive integer, got 0")],
)
def test_bad_queries_are_422_before_any_embedding(
    client: TestClient, seeded, embedder, query: str, detail: str
) -> None:
    assert get(client, query, status=422) == {"detail": detail}
    assert embedder.calls == []


def test_unavailable(client: TestClient, settings: Settings, seeded, embedder) -> None:
    embedder.available = False
    before = tables(settings)
    data = get(client, "q=gradient%20descent")
    assert [(r["slug"], r["match_kind"], r["similarity"]) for r in data["results"]] == [
        ("gradient-descent", "exact", None), ("stochastic-gradient-descent", "word", None)]
    assert data["semantic"]["status"] == "unavailable"
    assert "no OpenAI API key (fake)" in data["semantic"]["reason"]
    assert embedder.calls == [] and tables(settings) == before


def test_stale(client: TestClient, settings: Settings, seeded, embedder) -> None:
    with session_scope(settings) as session:
        ConceptManager(session, settings).create("dropout", "Randomly zeroing units.", "user")
    before = tables(settings)
    data = get(client, "q=gradient%20descent")
    assert [r["match_kind"] for r in data["results"]] == ["exact", "word"]
    semantic = data["semantic"]
    assert (semantic["status"], semantic["stale_count"], semantic["concept_count"],
            semantic["fix"]) == ("stale", 1, 4, "teacher concepts reindex")
    assert semantic["reason"].startswith("1 of 4 concepts have no up-to-date embedding")
    assert embedder.calls == [] and tables(settings) == before  # nothing embedded or written


def test_failed(client: TestClient, settings: Settings, seeded, embedder) -> None:
    embedder.fail_on = {1}
    data = get(client, "q=gradient%20descent")
    assert [r["match_kind"] for r in data["results"]] == ["exact", "word"]
    assert data["semantic"]["status"] == "failed"
    assert "fake failure on call 1" in data["semantic"]["reason"]


def test_skipped(client: TestClient, seeded, embedder) -> None:
    data = get(client, "q=" + "x" * (MAX_SEMANTIC_QUERY_CHARS + 1))
    assert data["results"] == [] and data["semantic"]["status"] == "skipped"
    assert embedder.calls == []


def test_the_search_index_missing_is_still_503(client: TestClient, settings: Settings,
                                                seeded, embedder) -> None:
    with session_scope(settings) as session:
        session.execute(text("DROP TABLE concept_search"))
    assert "concept_search full-text table is missing" in get(client, "q=attention",
                                                              status=503)["detail"]
    assert embedder.calls == []


# -- wiring ---------------------------------------------------------------------------------------


def test_the_default_app_searches_names_only_without_a_key(settings: Settings, seeded) -> None:
    app = create_app(settings, generator=StubGenerator())
    assert isinstance(app.state.search_embedder, OpenAIEmbedder)
    assert app.state.search_embedder is app.state.concept_relater.embedder  # one index, one model
    with TestClient(app) as client:
        data = get(client, "q=attention")
    assert [r["slug"] for r in data["results"]] == ["attention"]
    assert data["semantic"]["status"] == "unavailable"
    assert "no OpenAI API key was found" in data["semantic"]["reason"]


def test_the_search_embedder_wiring(settings: Settings, fake_embedder: type) -> None:
    relater = ConceptRelater(embedder=StubEmbedder(), model="m")
    app = create_app(settings, generator=StubGenerator(), concept_relater=relater)
    assert app.state.search_embedder is relater.embedder
    given = fake_embedder()
    app = create_app(settings, generator=StubGenerator(), concept_relater=relater,
                     search_embedder=given)
    assert app.state.search_embedder is given
    assert create_app(settings, generator=StubGenerator(),
                      search_embedder=given).state.search_embedder is given

    class NoEmbedder:
        model = "x"

    with pytest.raises(ValueError, match="pass search_embedder too"):
        create_app(settings, generator=StubGenerator(), concept_relater=NoEmbedder())
    assert create_app(settings, generator=StubGenerator(), concept_relater=NoEmbedder(),
                      search_embedder=given).state.search_embedder is given
    with pytest.raises(TypeError, match="search_embedder must have"):
        create_app(settings, generator=StubGenerator(), search_embedder=object())
