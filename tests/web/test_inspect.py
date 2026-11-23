"""POST /api/sources/inspect: detect a paper's details from its PDF, storing nothing."""

from __future__ import annotations

import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import anthropic
import pytest
from fastapi.testclient import TestClient

from teacher.config import Settings
from teacher.generation import StubGenerator
from teacher.metadata import MetadataExtractor
from teacher.web.app import create_app

INSPECT_KEYS = {"title", "authors", "year", "arxiv_id", "doi", "summary", "model", "duplicate_of"}


@pytest.fixture
def private_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the temp-file module at an empty directory, so leftovers can be seen."""
    directory = tmp_path / "tmp"
    directory.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(directory))
    return directory


@pytest.fixture
def inspect_client(settings: Settings, fake_anthropic) -> Iterator[Callable[..., Any]]:
    """`inspect_client(*outcomes) -> (client, fake)`: an app whose extractor uses a FakeAnthropic."""
    clients: list[TestClient] = []

    def make(*outcomes: Any) -> tuple[TestClient, Any]:
        fake = fake_anthropic(*outcomes)
        app = create_app(
            settings, generator=StubGenerator(), metadata_extractor=MetadataExtractor(fake)
        )
        client = TestClient(app)
        client.__enter__()
        clients.append(client)
        return client, fake

    yield make
    for client in clients:
        client.__exit__(None, None, None)


def inspect(
    client: TestClient, content: bytes, filename: str = "paper.pdf", ctype: str = "application/pdf"
):
    return client.post("/api/sources/inspect", files={"file": (filename, content, ctype)})


def test_inspect_detects_details(inspect_client, fake_anthropic, make_pdf) -> None:
    client, fake = inspect_client(
        fake_anthropic.metadata_reply(arxiv_id="arXiv:1706.03762v7", doi="doi:10.1145/3295222")
    )
    response = inspect(client, make_pdf(4))
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == INSPECT_KEYS
    assert body == {
        "title": "Attention Is All You Need",
        "authors": ["Ashish Vaswani", "Noam Shazeer"],
        "year": 2017,
        "arxiv_id": "1706.03762",
        "doi": "10.1145/3295222",
        "summary": "The paper proposes the Transformer. It relies on attention only.",
        "model": "claude-haiku-5-5",
        "duplicate_of": None,
    }
    assert len(fake.calls) == 1
    assert fake.calls[0]["messages"][0]["content"][0]["type"] == "document"


def test_inspect_nulls_are_returned_as_null(inspect_client, fake_anthropic, make_pdf) -> None:
    client, _ = inspect_client(
        fake_anthropic.metadata_reply(
            title=None, authors=[], year=None, arxiv_id=None, doi=None, summary=None
        )
    )
    response = inspect(client, make_pdf(1))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["authors"] == []
    assert all(body[k] is None for k in ("title", "year", "arxiv_id", "doi", "summary"))


def test_inspect_stores_nothing(
    inspect_client, fake_anthropic, make_pdf, settings: Settings, private_tmp: Path
) -> None:
    client, _ = inspect_client(fake_anthropic.metadata_reply())
    assert inspect(client, make_pdf(2)).status_code == 200
    assert client.get("/api/sources").json() == []
    sources_dir = settings.sources_dir
    assert not sources_dir.exists() or not any(sources_dir.iterdir())
    assert list(private_tmp.iterdir()) == []  # the temporary copy was deleted


def test_inspect_deletes_temp_file_on_failure(
    inspect_client, fake_anthropic, make_pdf, private_tmp: Path
) -> None:
    client, _ = inspect_client(fake_anthropic.timeout())
    assert inspect(client, make_pdf(2)).status_code == 503
    assert list(private_tmp.iterdir()) == []


def test_inspect_reports_duplicate_without_calling_claude(
    inspect_client, make_pdf, upload_source
) -> None:
    client, fake = inspect_client()  # no outcome queued: any API call fails the test
    content = make_pdf(3)
    stored = upload_source(
        client,
        kind="paper",
        title="Attention",
        slug="attention",
        filename="paper.pdf",
        content_type="application/pdf",
        content=content,
    )
    response = inspect(client, content, filename="other-name.pdf")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["duplicate_of"] == stored["slug"] == "attention"
    assert body["model"] is None and body["title"] is None and body["authors"] == []
    assert fake.calls == []
    assert len(client.get("/api/sources").json()) == 1


def test_inspect_then_upload_with_detected_details(
    inspect_client, fake_anthropic, make_pdf
) -> None:
    client, _ = inspect_client(fake_anthropic.metadata_reply(doi="10.1145/3295222"))
    content = make_pdf(3)
    found = inspect(client, content).json()
    assert found["duplicate_of"] is None
    response = client.post(
        "/api/sources",
        data={
            "kind": "paper",
            "title": found["title"],
            "creators": found["authors"],
            "identifiers": [f"arxiv:{found['arxiv_id']}", f"doi:{found['doi']}"],
            "originated_on": str(found["year"]),
            "summary": found["summary"],
        },
        files={"file": ("paper.pdf", content, "application/pdf")},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["identifiers"] == [
        {"scheme": "arxiv", "value": "1706.03762"},
        {"scheme": "doi", "value": "10.1145/3295222"},
    ]
    assert body["summary"] == found["summary"]
    assert body["originated_on"] == "2017"
    assert body["creators"] == ["Ashish Vaswani", "Noam Shazeer"]
    # The same file again is now reported as a duplicate.
    again = inspect(client, content).json()
    assert again["duplicate_of"] == body["slug"]


FAILURES = [
    ("refusal", lambda f: f.metadata_reply(stop_reason="refusal"), "declined"),
    ("truncated", lambda f: f.metadata_reply(stop_reason="max_tokens"), "cut off"),
    ("invalid", lambda f: f.metadata_reply(text="{}"), "not in the expected format"),
    ("bad year", lambda f: f.metadata_reply(year=3000), "publication year"),
    (
        "auth",
        lambda f: f.status_error(anthropic.AuthenticationError, 401, "authentication_error"),
        "rejected the credentials",
    ),
    (
        "rate limit",
        lambda f: f.status_error(anthropic.RateLimitError, 429, "rate_limit_error"),
        "rate limit",
    ),
    (
        "overloaded",
        lambda f: f.status_error(anthropic.InternalServerError, 529, "overloaded_error"),
        "overloaded",
    ),
    ("timeout", lambda f: f.timeout(), "did not answer"),
    ("connection", lambda f: f.connection_error(), "could not connect"),
]


@pytest.mark.parametrize(("name", "outcome", "message"), FAILURES, ids=[f[0] for f in FAILURES])
def test_inspect_failures_are_503(
    inspect_client, fake_anthropic, make_pdf, name: str, outcome, message: str
) -> None:
    client, _ = inspect_client(outcome(fake_anthropic))
    response = inspect(client, make_pdf(1))
    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail.startswith("Could not detect the paper's details: ")
    assert message in detail
    assert set(response.json()) == {"detail"}


def test_inspect_without_credentials_is_503(
    settings: Settings, make_pdf, forbid_real_messages_api: None
) -> None:
    app = create_app(settings, generator=StubGenerator())  # default extractor, no credentials
    assert isinstance(app.state.metadata_extractor, MetadataExtractor)
    with TestClient(app) as client:
        response = inspect(client, make_pdf(1))
    assert response.status_code == 503
    assert "ANTHROPIC_API_KEY" in response.json()["detail"]


@pytest.mark.parametrize(
    ("filename", "ctype"),
    [("notes.txt", "text/plain"), ("figure.png", "image/png"), ("paper", "")],
)
def test_inspect_non_pdf_is_422(inspect_client, filename: str, ctype: str) -> None:
    client, fake = inspect_client()
    response = inspect(client, b"some content", filename=filename, ctype=ctype)
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "PDF files only" in detail and "by hand" in detail
    assert fake.calls == []


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (b"", "empty"),
        (b"just some text pretending", "not a PDF"),
        (b"%PDF-1.4\n% tiny test file\n", "could not be read"),
    ],
)
def test_inspect_bad_pdf_is_422(inspect_client, content: bytes, message: str) -> None:
    client, fake = inspect_client()
    response = inspect(client, content)
    assert response.status_code == 422
    assert message in response.json()["detail"]
    assert fake.calls == []


def test_inspect_without_file_is_422(inspect_client) -> None:
    client, _ = inspect_client()
    response = client.post("/api/sources/inspect", data={})
    assert response.status_code == 422
    assert "file" in response.json()["detail"]
