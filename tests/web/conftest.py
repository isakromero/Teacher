"""Web fixtures: an `app` on the migrated tmp `settings`, a `client`, and `upload_source`.

`app` uses the offline `StubGenerator` and `StubLabGenerator` (without a runner), passed
explicitly. `client` is a `TestClient` used as a context manager, so the app's lifespan (migrations and the
stale-`generating` sweep) runs. `upload_source(**overrides)` posts a small text file to
`POST /api/sources` and returns the response JSON (it asserts a 201); an explicit client may be
passed first: `upload_source(client, **overrides)`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from teacher.config import Settings
from teacher.generation import StubGenerator
from teacher.lab_generator import StubLabGenerator
from teacher.web.app import create_app

UploadSource = Callable[..., dict[str, Any]]


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    """The app with the offline `StubGenerator` and `StubLabGenerator`, explicitly (the default
    generators call Claude)."""
    return create_app(settings, generator=StubGenerator(), lab_generator=StubLabGenerator())


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


def send_source(client: TestClient, **overrides: Any):
    """POST a source upload and return the raw response (no status assertion).

    Overrides: `content` (bytes or str; defaults to text derived from title and slug), `filename`, `content_type` and any form field
    (`kind`, `title`, `slug`, `creators`, `identifiers`, ...). A form field set to None is omitted.
    """
    content = overrides.pop("content", None)
    filename = overrides.pop("filename", "notes.txt")
    content_type = overrides.pop("content_type", "text/plain")
    data: dict[str, Any] = {"kind": "note", "title": "Test Notes"}
    data.update(overrides)
    data = {k: v for k, v in data.items() if v is not None}
    if content is None:  # default differs per title/slug: identical content is a 409
        content = f"Study text for {data.get('title')} ({data.get('slug')}).\n"
    if isinstance(content, str):
        content = content.encode("utf-8")
    return client.post(
        "/api/sources", data=data, files={"file": (filename, content, content_type)}
    )


@pytest.fixture
def upload_source(client: TestClient) -> UploadSource:
    """Post a source (see `send_source`), assert 201, return the JSON.

    Call as `upload_source(**overrides)` or `upload_source(some_client, **overrides)`; the
    first form uses the `client` fixture.
    """

    def upload(target: TestClient | None = None, /, **overrides: Any) -> dict[str, Any]:
        response = send_source(target or client, **overrides)
        assert response.status_code == 201, response.text
        return response.json()

    return upload


@pytest.fixture
def post_source(client: TestClient) -> Callable[..., Any]:
    """`post_source(**overrides) -> Response`: like `upload_source` but returns the raw response."""

    def post(target: TestClient | None = None, /, **overrides: Any):
        return send_source(target or client, **overrides)

    return post
