"""POST /api/sources/discover (`teacher.web.routers.discovery`) with a finder of fakes, the Add
page's "Find sources" box, and its view model under node (tests/web/js/discover_test.mjs).
Nothing reaches the network and nothing is ingested."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from teacher.config import Settings
from teacher.db.engine import session_scope
from teacher.discovery import ProviderError, SourceFinder, TriageError
from teacher.generation import StubGenerator
from teacher.lab_generator import StubLabGenerator
from teacher.managers import ConceptManager, KnowledgeSourceManager
from teacher.web.app import create_app
from tests.discovery_fakes import FakeFetcher, FakeProvider, FakeTriager, hit, model_call, web_page

STATIC_JS = Path(__file__).resolve().parents[2] / "src" / "teacher" / "web" / "static" / "js"
JS_TESTS = Path(__file__).resolve().parent / "js"
URLS = ["https://example.org/dh", "https://arxiv.org/abs/2101.00001"]


def make_client(settings: Settings, finder: Any) -> TestClient:
    return TestClient(
        create_app(settings, generator=StubGenerator(), lab_generator=StubLabGenerator(),
                   source_finder=finder)  # fmt: skip
    )


@pytest.fixture
def web() -> FakeProvider:
    return FakeProvider(
        "web",
        [hit("web", 1, URLS[0], "DH explained"), hit("web", 2, URLS[1], "A survey")],
        calls=[model_call(cost=0.04, searches=2)],
    )


@pytest.fixture
def arxiv() -> FakeProvider:
    return FakeProvider("arxiv", error=ProviderError("arXiv answered with HTTP 503"))


@pytest.fixture
def finder(web: FakeProvider, arxiv: FakeProvider) -> SourceFinder:
    pages = {URLS[0]: web_page(URLS[0]), "https://arxiv.org/pdf/2101.00001": web_page(URLS[1], 50)}
    return SourceFinder([web, arxiv], fetcher=FakeFetcher(pages), triager=FakeTriager())


def test_discover_a_topic(settings: Settings, finder: SourceFinder, web: FakeProvider) -> None:
    with make_client(settings, finder) as client:
        response = client.post("/api/sources/discover", json={"topic": "Diffie-Hellman", "level": "intro"})
        assert response.status_code == 200, response.text
        data = response.json()
        assert [p["name"] for p in data["providers"]] == ["web", "arxiv"]
        assert data["providers"][1]["error"] == "arXiv answered with HTTP 503"
        assert [(c["rank"], c["url"], c["probe"]["verdict"]) for c in data["candidates"]] == [
            (1, URLS[0], "readable"),
            (2, "https://arxiv.org/pdf/2101.00001", "too_thin"),
        ]
        assert data["candidates"][1]["landing_url"] == URLS[1]
        assert data["usage"]["total_usd"] == pytest.approx(0.05)
        assert web.queries[0].level == "intro"
        # Nothing was ingested.
        assert client.get("/api/sources").json() == []


def test_the_search_calls_are_in_the_cost_ledger(settings: Settings, fake_anthropic: Any) -> None:
    """The web search (in a provider thread) and triage calls are recorded with purpose
    source_discovery, the web search with its search count, unpriced."""
    from sqlalchemy import select

    from teacher.db.models import ApiCall
    from teacher.discovery import Triager, WebSearchProvider
    from tests.discovery_fakes import score, triage_reply, web_reply

    sources = [{"url": URLS[0], "title": "DH explained", "reason": "A tutorial."}]
    client = fake_anthropic(web_reply(sources, searches=2), triage_reply([score("C1")]))
    finder = SourceFinder(
        [WebSearchProvider(client)],
        fetcher=FakeFetcher({URLS[0]: web_page(URLS[0])}),
        triager=Triager(client),
    )
    with make_client(settings, finder) as client_:
        response = client_.post("/api/sources/discover", json={"topic": "Diffie-Hellman"})
    assert response.status_code == 200, response.text
    with session_scope(settings) as session:
        rows = [(r.purpose, r.step, r.web_search_requests, r.exploration_id)
                for r in session.scalars(select(ApiCall).order_by(ApiCall.id))]  # fmt: skip
    assert rows == [("source_discovery", "web_search", 2, None), ("source_discovery", "triage", 0, None)]


def test_discover_marks_what_the_library_holds(settings: Settings, finder: SourceFinder, tmp_path: Path) -> None:
    path = tmp_path / "dh.md"
    path.write_text("# DH\n", encoding="utf-8")
    with session_scope(settings) as session:
        KnowledgeSourceManager(session, settings).add(
            path, slug="dh", kind="web_page", title="DH", origin_url=URLS[0]
        )
    with make_client(settings, finder) as client:
        data = client.post("/api/sources/discover", json={"topic": "Diffie-Hellman"}).json()
    first = data["candidates"][0]
    assert first["in_library"] is True
    assert first["library"] == [{"slug": "dh", "title": "DH", "method": "url"}]


def test_discover_a_concept(settings: Settings, finder: SourceFinder, web: FakeProvider) -> None:
    with session_scope(settings) as session:
        slug = ConceptManager(session, settings).create(
            "Diffie-Hellman key exchange", "Agreeing on a key in public.", "user", aliases=["DH"]
        ).slug
    with make_client(settings, finder) as client:
        assert client.post("/api/sources/discover", json={"concept": slug}).status_code == 200
        missing = client.post("/api/sources/discover", json={"concept": "nope"})
        assert missing.status_code == 404
    assert web.queries[0].aliases == ("DH",)
    assert web.queries[0].concept_slug == slug


@pytest.mark.parametrize(
    "body",
    [{}, {"topic": "  "}, {"topic": "x", "concept": "y"}, {"topic": "x", "level": "expert"},
     {"topic": "x" * 301}, {"topic": "x", "extra": 1}],
)  # fmt: skip
def test_bad_bodies_are_422(settings: Settings, finder: SourceFinder, body: dict[str, Any]) -> None:
    with make_client(settings, finder) as client:
        response = client.post("/api/sources/discover", json=body)
    assert response.status_code == 422, response.text
    assert isinstance(response.json()["detail"], str)


def test_without_a_finder_it_is_503(settings: Settings) -> None:
    with make_client(settings, None) as client:
        response = client.post("/api/sources/discover", json={"topic": "x"})
    assert response.status_code == 503
    assert "not available" in response.json()["detail"]


def test_every_provider_failing_is_502(settings: Settings, web: FakeProvider, arxiv: FakeProvider) -> None:
    web.error = ProviderError("the web search call failed: boom")
    finder = SourceFinder([web, arxiv], fetcher=FakeFetcher({}), triager=FakeTriager())
    with make_client(settings, finder) as client:
        response = client.post("/api/sources/discover", json={"topic": "x"})
    assert response.status_code == 502
    detail = response.json()["detail"]
    assert "web: the web search call failed: boom" in detail and "arxiv: arXiv answered" in detail


def test_a_triage_failure_is_502(settings: Settings, web: FakeProvider, arxiv: FakeProvider) -> None:
    triager = FakeTriager(error=TriageError("Could not rank the sources: no score for C1"))
    finder = SourceFinder([web, arxiv], fetcher=FakeFetcher({URLS[0]: web_page(URLS[0])}), triager=triager)
    with make_client(settings, finder) as client:
        response = client.post("/api/sources/discover", json={"topic": "x"})
    assert response.status_code == 502
    assert response.json()["detail"].startswith("Could not rank the sources: no score for C1")


# -- the Add page -------------------------------------------------------------------------------


def test_the_add_page_has_the_find_sources_box(client: TestClient) -> None:
    html = client.get("/add").text
    for marker in (
        'id="add-discover"', 'id="discover-form"', 'id="discover-topic"', 'id="discover-level"',
        'id="discover-submit"', 'id="discover-status"', 'id="discover-error"', 'id="discover-results"',
        "/static/js/add_discover.js", "/static/css/discover.css",
        '<option value="intro">Introductory</option>',
        '<option value="intermediate" selected>Intermediate</option>',
        '<option value="advanced">Advanced</option>',
    ):  # fmt: skip
        assert marker in html, marker
    discover_js = (STATIC_JS / "add_discover.js").read_text(encoding="utf-8")
    assert '"/api/sources/discover"' in discover_js
    # The Add button only fills and fetches the address through add.js: it stores nothing.
    assert '"teacher:prefill-url"' in discover_js and "fetch: true" in discover_js
    assert "/api/sources/from-url" not in discover_js
    add_js = (STATIC_JS / "add.js").read_text(encoding="utf-8")
    assert "event.detail.fetch" in add_js


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_discover_model_under_node() -> None:
    result = subprocess.run(
        ["node", str(JS_TESTS / "discover_test.mjs")], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stdout + result.stderr
    for name in ("discover_model.js", "add_discover.js", "add.js"):
        check = subprocess.run(
            ["node", "--check", str(STATIC_JS / name)], capture_output=True, text=True, timeout=60
        )
        assert check.returncode == 0, check.stderr
