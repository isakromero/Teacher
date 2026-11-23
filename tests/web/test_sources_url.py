"""Sources from a web address: `POST /api/sources/inspect-url` and `POST /api/sources/from-url`."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import anthropic
import httpx2
import pytest
from fastapi.testclient import TestClient

from teacher.config import Settings
from teacher.generation import StubGenerator
from teacher.metadata import MetadataExtractor
from teacher.url_sources import MIN_WORDS, UrlFetcher
from teacher.web.app import create_app

WORDS = " ".join(f"word{n}" for n in range(MIN_WORDS))
HEAD = (
    "<title>Tab</title><meta property='og:title' content='How attention works'>"
    "<meta name='author' content='Ada L'>"
    "<meta property='article:published_time' content='2024-03-05'>"
    "<meta name='description' content='An introduction.'>"
    "<meta property='og:site_name' content='The Blog'>"
)
PAGE = f"<html><head>{HEAD}</head><body><nav>MENU</nav><main><h1>Post</h1><p>{WORDS}</p></main></body></html>"
INSPECT_URL_KEYS = {
    "title", "authors", "year", "arxiv_id", "doi", "summary", "model", "duplicate_of",
    "url", "kind", "words", "detection_error",
}  # fmt: skip

Routes = dict[str, tuple[int, bytes, str]]


@pytest.fixture
def url_client(settings: Settings, fake_anthropic) -> Iterator[Callable[..., Any]]:
    """`url_client(routes, *outcomes) -> (client, fake, requests)`: an app whose fetcher serves
    `routes` (path -> (status, body, content type); a missing path is a 404), whose hosts all
    resolve to a public address except `private.test`, and whose metadata extractor uses a
    FakeAnthropic with `outcomes`."""
    clients: list[TestClient] = []

    def make(routes: Routes, *outcomes: Any) -> tuple[TestClient, Any, list[str]]:
        requests: list[str] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            requests.append(str(request.url))
            if request.url.path == "/moved":
                return httpx2.Response(301, headers={"location": "/post"})
            status, body, content_type = routes.get(request.url.path, (404, b"", "text/html"))
            return httpx2.Response(status, content=body, headers={"content-type": content_type})

        fetcher = UrlFetcher(
            httpx2.Client(transport=httpx2.MockTransport(handler)),
            resolve=lambda host: ["10.0.0.1"] if host == "private.test" else ["93.184.216.34"],
        )
        fake = fake_anthropic(*outcomes)
        app = create_app(
            settings,
            generator=StubGenerator(),
            metadata_extractor=MetadataExtractor(fake),
            url_fetcher=fetcher,
        )
        client = TestClient(app)
        client.__enter__()
        clients.append(client)
        return client, fake, requests

    yield make
    for client in clients:
        client.__exit__(None, None, None)


HTML_ROUTES: Routes = {"/post": (200, PAGE.encode(), "text/html; charset=utf-8")}


def test_inspect_url_reads_a_web_page_without_a_model(url_client) -> None:
    client, fake, requests = url_client(HTML_ROUTES)
    response = client.post("/api/sources/inspect-url", json={"url": "https://blog.test/moved"})
    assert response.status_code == 200, response.text
    assert response.json() == {
        "title": "How attention works",
        "authors": ["Ada L"],
        "year": 2024,
        "arxiv_id": None,
        "doi": None,
        "summary": "An introduction.",
        "model": None,
        "duplicate_of": None,
        "url": "https://blog.test/post",
        "kind": "web_page",
        "words": MIN_WORDS + 2,
        "detection_error": None,
    }
    assert fake.calls == []
    assert requests == ["https://blog.test/moved", "https://blog.test/post"]
    assert client.get("/api/sources").json() == []  # nothing is stored


def test_inspect_url_detects_a_pdf_like_an_upload(url_client, fake_anthropic, make_pdf) -> None:
    routes: Routes = {"/paper.pdf": (200, make_pdf(4), "application/pdf")}
    client, fake, _ = url_client(routes, fake_anthropic.metadata_reply())
    response = client.post("/api/sources/inspect-url", json={"url": "https://arxiv.test/paper.pdf"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == INSPECT_URL_KEYS
    assert (body["kind"], body["words"], body["detection_error"]) == ("paper", None, None)
    assert (body["title"], body["year"]) == ("Attention Is All You Need", 2017)
    assert body["model"] == "claude-haiku-5-5"
    assert len(fake.calls) == 1


def test_inspect_url_reports_failed_detection_and_still_describes_the_pdf(
    url_client, fake_anthropic, make_pdf
) -> None:
    routes: Routes = {"/paper.pdf": (200, make_pdf(1), "application/pdf")}
    error = fake_anthropic.status_error(anthropic.RateLimitError, 429, "rate_limit_error")
    client, _, _ = url_client(routes, error)
    response = client.post("/api/sources/inspect-url", json={"url": "https://arxiv.test/paper.pdf"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["kind"], body["url"]) == ("paper", "https://arxiv.test/paper.pdf")
    assert body["title"] is None and body["authors"] == [] and body["model"] is None
    assert "rate limit" in body["detection_error"]


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("https://blog.test/missing", "HTTP 404"),
        ("https://private.test/post", "private or local address"),
        ("ftp://blog.test/post", "must start with http"),
        ("https://blog.test/thin", "too little readable text"),
        ("https://blog.test/image", "image/png, which Teacher cannot read"),
    ],
)
def test_url_endpoints_fail_loudly(url_client, url: str, message: str) -> None:
    routes: Routes = {
        **HTML_ROUTES,
        "/thin": (200, b"<html><body><p>Too short.</p></body></html>", "text/html"),
        "/image": (200, b"\x89PNG", "image/png"),
    }
    client, fake, _ = url_client(routes)
    for path, body in (
        ("/api/sources/inspect-url", {"url": url}),
        ("/api/sources/from-url", {"url": url, "title": "A title"}),
    ):
        response = client.post(path, json=body)
        assert response.status_code == 422, response.text
        assert message in response.json()["detail"]
    assert fake.calls == []
    assert client.get("/api/sources").json() == []


def test_from_url_stores_a_web_page_as_markdown(url_client, settings: Settings) -> None:
    client, _, _ = url_client(HTML_ROUTES)
    response = client.post(
        "/api/sources/from-url",
        json={
            "url": "https://blog.test/moved",
            "title": "How attention works",
            "creators": ["Ada L", " "],
            "identifiers": ["doi:10.1/x"],
            "originated_on": "2024",
            "summary": "An introduction.",
        },
    )
    assert response.status_code == 201, response.text
    source = response.json()
    assert source["slug"] == "how-attention-works"
    assert (source["kind"], source["media_type"]) == ("web_page", "text/markdown")
    assert source["origin_url"] == "https://blog.test/post"
    assert source["creators"] == ["Ada L"] and source["originated_on"] == "2024"
    assert source["identifiers"] == [{"scheme": "doi", "value": "10.1/x"}]
    metadata = source["metadata"]
    assert metadata["requested_url"] == "https://blog.test/moved"
    assert metadata["original_media_type"] == "text/html"
    assert metadata["words"] == MIN_WORDS + 2 and metadata["site_name"] == "The Blog"
    assert metadata["fetched_at"].endswith("+00:00")

    stored = client.get("/api/sources/how-attention-works/file")
    assert stored.status_code == 200
    assert stored.text == f"# Post\n\n{WORDS}\n"
    assert "MENU" not in stored.text

    # The same page again: found by its content, by inspect and by a second add.
    again = client.post("/api/sources/inspect-url", json={"url": "https://blog.test/post"})
    assert again.json()["duplicate_of"] == "how-attention-works"
    assert again.json()["title"] is None
    second = client.post("/api/sources/from-url", json={"url": "https://blog.test/post", "title": "Other"})
    assert second.status_code == 409, second.text


def test_from_url_stores_a_pdf_as_a_paper(url_client, make_pdf) -> None:
    pdf = make_pdf(3)
    client, fake, _ = url_client({"/p": (200, pdf, "application/octet-stream")})
    response = client.post("/api/sources/from-url", json={"url": "https://arxiv.test/p", "title": "A Paper"})
    assert response.status_code == 201, response.text
    source = response.json()
    assert (source["kind"], source["media_type"]) == ("paper", "application/pdf")
    assert source["origin_url"] == "https://arxiv.test/p"
    assert "words" not in source["metadata"]
    assert client.get("/api/sources/a-paper/file").content == pdf
    assert fake.calls == []  # adding never calls the model


def test_from_url_can_start_a_study_page(url_client) -> None:
    client, _, _ = url_client(HTML_ROUTES)
    source = client.post("/api/sources/from-url", json={"url": "https://blog.test/post", "title": "Post"})
    assert source.status_code == 201, source.text
    artifact = client.post("/api/artifacts", json={"sources": ["post"]})
    assert artifact.status_code == 202, artifact.text
    assert client.get(f"/api/artifacts/{artifact.json()['slug']}").json()["status"] == "ready"


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"url": "https://blog.test/post"},
        {"url": "https://blog.test/post", "title": "T", "kind": "paper"},
        {"url": "https://blog.test/post", "title": "T", "originated_on": "March 2024"},
        {"url": "https://blog.test/post", "title": "T", "identifiers": ["no-scheme"]},
        {"url": "https://blog.test/post", "title": "  "},
    ],
)
def test_from_url_rejects_bad_bodies(url_client, body: dict[str, Any]) -> None:
    client, _, _ = url_client(HTML_ROUTES)
    response = client.post("/api/sources/from-url", json=body)
    assert response.status_code == 422, response.text
    assert client.get("/api/sources").json() == []
