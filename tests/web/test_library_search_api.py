"""`GET /api/search`: the home page's search across study pages, labs and sources (the rules
are tested in tests/test_search.py): the response shape, the 422s, gated text, failing loudly
on an unreadable block file, and that searching logs nothing."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from teacher.config import Settings
from teacher.db.engine import session_scope
from teacher.db.models import StudyEvent
from teacher.managers import ArtifactManager
from teacher.search import MAX_LIMIT

Json = dict[str, Any]
URL = "/api/search"


@pytest.fixture
def page(client: TestClient, upload_source: Callable[..., Json]) -> Json:
    """A ready stub study page from the source "Deep Learning" by Ada Lovelace."""
    upload_source(title="Deep Learning", slug="deep-learning", kind="paper",
                  creators=["Ada Lovelace"], originated_on="2015")
    created = client.post("/api/artifacts", json={"sources": ["deep-learning"]})
    assert created.status_code == 202, created.text
    return client.get(f"/api/artifacts/{created.json()['slug']}").json()


def get(client: TestClient, **params: Any) -> Json:
    response = client.get(URL, params=params)
    assert response.status_code == 200, response.text
    return response.json()


def text_of(segs: list[Json]) -> str:
    return "".join(f"[{s['text']}]" if s["mark"] else s["text"] for s in segs)


@pytest.mark.parametrize(
    ("params", "detail"),
    [
        ({}, "q: Field required"),
        ({"q": ""}, "Type something to search for."),
        ({"q": "   "}, "Type something to search for."),
        ({"q": "a"}, "Type at least 2 letters or digits to search."),
        ({"q": "?!"}, "Search for words: the query has no letters or digits."),
        ({"q": "deep", "limit": 0}, "limit"),
        ({"q": "deep", "limit": MAX_LIMIT + 1}, "limit"),
    ],
)
def test_unsearchable_requests_are_422s(client: TestClient, params: Json, detail: str) -> None:
    response = client.get(URL, params=params)
    assert response.status_code == 422, response.text
    assert detail in response.json()["detail"]


def test_search_response(client: TestClient, page: Json) -> None:
    data = get(client, q="placeholder")
    assert {k: data[k] for k in ("query", "words", "prefix", "total", "limit")} == {
        "query": "placeholder", "words": ["placeholder"], "prefix": True, "total": 1, "limit": 20,
    }
    (r,) = data["results"]
    assert set(r) == {
        "kind", "slug", "title", "title_segments", "url", "authors", "year", "status",
        "superseded", "score", "fields", "match", "snippet", "study",
    }
    assert (r["kind"], r["slug"], r["url"]) == ("study_page", page["slug"], f"/artifacts/{page['slug']}")
    assert r["authors"] == ["Ada Lovelace"] and r["year"] == "2015" and r["status"] == "ready"
    assert r["match"] == {
        "field": "body", "section": "Deep Learning", "block": "b01", "term": None,
        "concept": None, "locked": False, "href": f"/artifacts/{page['slug']}#block-b01",
    }
    assert "[placeholder]" in text_of(r["snippet"])
    assert r["study"] == {
        "state": "not_opened", "active_seconds": 0.0, "last_studied_at": None,
        "gates_total": page["progress"]["gates_total"], "gates_cleared": 0,
    }
    assert page["progress"]["gates_total"] >= 1


def test_search_finds_sources_and_pages(client: TestClient, page: Json) -> None:
    data = get(client, q="lovelace")
    assert sorted(r["kind"] for r in data["results"]) == ["source", "study_page"]
    source = next(r for r in data["results"] if r["kind"] == "source")
    assert source["url"] == "/sources/deep-learning" and source["fields"] == ["authors"]
    assert source["study"] == {"state": "not_opened", "active_seconds": 0.0,
                               "last_studied_at": None, "study_pages": 1}
    assert get(client, q="lovelace", limit=1)["results"][0]["kind"] == "study_page"


def test_locked_text_is_found_but_not_quoted(client: TestClient, page: Json) -> None:
    (r,) = get(client, q="reflect")["results"]  # the heading of b03, behind the gate b02
    assert r["match"]["locked"] is True and r["match"]["href"] is None
    assert r["snippet"] == []


def test_query_text_comes_back_as_text(client: TestClient, upload_source: Callable[..., Json]) -> None:
    upload_source(title="Tags like <script>alert(1)</script> & more", slug="tags")
    (r,) = get(client, q="script alert")["results"]
    assert text_of(r["title_segments"]) == "Tags like <[script]>[alert](1)</[script]> & more"


def test_searching_logs_nothing(client: TestClient, page: Json, settings: Settings) -> None:
    def count() -> int:
        with session_scope(settings) as session:
            return session.scalar(select(func.count(StudyEvent.id))) or 0

    before = count()
    get(client, q="deep learning")
    assert count() == before


def test_an_unreadable_block_file_is_an_error(client: TestClient, page: Json, settings: Settings) -> None:
    with session_scope(settings) as session:
        artifacts = ArtifactManager(session, settings)
        path = artifacts.dir(artifacts.get_by_slug(page["slug"])) / "blocks" / "b01.json"
    path.unlink()
    response = client.get(URL, params={"q": "deep"})
    assert response.status_code == 422
    assert "b01" in response.json()["detail"] and "missing" in response.json()["detail"]
