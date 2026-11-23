"""OpenAIEmbedder (fake client, and the real SDK on an in-process mock transport) and the
embedding model setting. No request leaves the process."""

from __future__ import annotations

import json
from typing import Any

import httpx2
import openai
import pytest
from openai.types import CreateEmbeddingResponse

from teacher.config import ModelSettings
from teacher.embeddings import (
    Embedder,
    EmbeddingResponseError,
    EmbeddingUnavailableError,
    OpenAIEmbedder,
)

KEY = "sk-test-not-a-real-key-123"


def response(vectors: list[list[float]], indexes: list[int] | None = None) -> CreateEmbeddingResponse:
    indexes = list(range(len(vectors))) if indexes is None else indexes
    return CreateEmbeddingResponse.model_validate(
        {
            "object": "list",
            "model": "text-embedding-3-small",
            "data": [
                {"object": "embedding", "index": i, "embedding": v}
                for i, v in zip(indexes, vectors, strict=True)
            ],
            "usage": {"prompt_tokens": 1, "total_tokens": 1},
        }
    )


def echo(dims: int = 2) -> Any:
    """An outcome answering each input text with [len(text), 1.0, ...]."""
    return lambda kwargs: response([[float(len(t))] + [1.0] * (dims - 1) for t in kwargs["input"]])


class FakeOpenAI:
    """Stands in for `openai.OpenAI`: `embeddings.create(**kwargs)` records the call and uses the
    next outcome (a response, a callable taking the kwargs, or an exception to raise)."""

    def __init__(self, *outcomes: Any) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []
        self.embeddings = self

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        assert self.outcomes, "FakeOpenAI: unexpected call"
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome(kwargs) if callable(outcome) else outcome


def test_is_an_embedder_with_the_configured_default_model() -> None:
    embedder = OpenAIEmbedder()
    assert isinstance(embedder, Embedder)
    assert embedder.model == "text-embedding-3-small"
    assert embedder._client is None  # nothing created until an embedding is requested


def test_missing_key_is_reported_only_when_embedding(monkeypatch: pytest.MonkeyPatch) -> None:
    embedder = OpenAIEmbedder("text-embedding-3-small")
    assert embedder.embed([]) == []  # nothing to embed: no client needed
    with pytest.raises(EmbeddingUnavailableError, match="Set the OPENAI_API_KEY environment variable"):
        embedder.embed(["attention"])
    monkeypatch.setenv("OPENAI_API_KEY", "   ")
    with pytest.raises(EmbeddingUnavailableError, match="no OpenAI API key was found"):
        embedder.embed(["attention"])
    assert embedder._client is None


def test_client_is_created_lazily_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    embedder = OpenAIEmbedder("text-embedding-3-small", timeout=12.0)
    client = embedder._get_client()
    assert isinstance(client, openai.OpenAI)
    assert client.api_key == KEY
    assert str(client.base_url).startswith("http://127.0.0.1:9")  # conftest: unreachable
    assert embedder._get_client() is client


def test_batches_requests_and_keeps_order() -> None:
    shuffled = response([[3.0, 0.0], [1.0, 0.0]], indexes=[1, 0])  # out of order on the wire
    fake = FakeOpenAI(echo(), shuffled, echo())
    embedder = OpenAIEmbedder("m-1", client=fake, batch_size=2)
    vectors = embedder.embed(["a", "bb", "ccc", "dddd", "eeeee"])
    assert [call["input"] for call in fake.calls] == [["a", "bb"], ["ccc", "dddd"], ["eeeee"]]
    assert all(call["model"] == "m-1" and call["encoding_format"] == "float" for call in fake.calls)
    assert vectors == [[1.0, 1.0], [2.0, 1.0], [1.0, 0.0], [3.0, 0.0], [5.0, 1.0]]


@pytest.mark.parametrize(
    ("outcomes", "fragment"),
    [
        ([response([[1.0, 0.0]])], "returned 1 vectors for 2 texts"),
        ([response([[1.0], [2.0]], indexes=[0, 0])], r"returned indexes \[0, 0\], expected 0..1"),
        ([response([[1.0], [2.0]], indexes=[1, 2])], r"returned indexes \[1, 2\]"),
        ([response([[1.0, 0.0], [1.0]])], "vector 2 has 1 dimensions, expected 2"),
        ([response([[1.0], [float("inf")]])], "vector 2 contains inf"),
        ([response([[1.0], []])], "vector 2 is empty"),
        ([echo(2), echo(3)], "vector 1 has 3 dimensions, expected 2"),  # across batches
        ([object()], "returned no data list"),
    ],
)
def test_bad_responses_fail_loudly(outcomes: list[Any], fragment: str) -> None:
    embedder = OpenAIEmbedder("m-1", client=FakeOpenAI(*outcomes), batch_size=2)
    texts = ["a", "b"] if len(outcomes) == 1 else ["a", "b", "c", "d"]
    with pytest.raises(EmbeddingResponseError, match=fragment):
        embedder.embed(texts)


@pytest.mark.parametrize(
    ("texts", "error", "fragment"),
    [
        ("attention", TypeError, "must be a list of strings"),
        (["ok", " "], ValueError, "text 2 to embed must be a non-blank string"),
        ([None], ValueError, "text 1 to embed"),
    ],
)
def test_invalid_input(texts: Any, error: type, fragment: str) -> None:
    fake = FakeOpenAI()
    with pytest.raises(error, match=fragment):
        OpenAIEmbedder("m-1", client=fake).embed(texts)
    assert fake.calls == []


@pytest.mark.parametrize(("model", "batch_size"), [("", 10), ("m", 0), ("m", 2049), ("m", True)])
def test_invalid_settings(model: str, batch_size: Any) -> None:
    with pytest.raises(ValueError):
        OpenAIEmbedder(model, batch_size=batch_size)


def _request() -> httpx2.Request:
    return httpx2.Request("POST", "https://api.openai.com/v1/embeddings")


def _status(cls: type[openai.APIStatusError], status: int) -> openai.APIStatusError:
    resp = httpx2.Response(status, request=_request(), headers={"x-request-id": "req_fake"})
    return cls("fake", response=resp, body=None)


@pytest.mark.parametrize(
    ("error", "fragment"),
    [
        (_status(openai.AuthenticationError, 401), "rejected the key (HTTP 401, request id req_fake)"),
        (_status(openai.PermissionDeniedError, 403), "not allowed to use model 'm-1'"),
        (_status(openai.NotFoundError, 404), "does not know embedding model 'm-1'"),
        (_status(openai.RateLimitError, 429), "rate limit or quota"),
        (_status(openai.InternalServerError, 500), "server error"),
        (_status(openai.BadRequestError, 400), "returned an error (HTTP 400"),
        (openai.APITimeoutError(request=_request()), "did not answer within 7 seconds"),
        (openai.APIConnectionError(request=_request()), "could not connect"),
    ],
)
def test_sdk_errors_become_key_free_unavailable_errors(error: Exception, fragment: str) -> None:
    embedder = OpenAIEmbedder("m-1", client=FakeOpenAI(error), timeout=7.0)
    with pytest.raises(EmbeddingUnavailableError, match="Embedding failed: ") as info:
        embedder.embed(["attention"])
    assert fragment in str(info.value)
    assert info.value.__cause__ is error


def test_the_real_sdk_on_a_mock_transport() -> None:
    """The SDK's own request building and response parsing, answered in-process."""
    seen: list[tuple[str, str, dict[str, Any]]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        seen.append((request.url.path, request.headers["authorization"], body))
        if body["input"] == ["unauthorised"]:
            return httpx2.Response(401, json={"error": {"message": "bad key", "type": "x"}})
        data = [
            {"object": "embedding", "index": i, "embedding": [float(len(t)), 0.5]}
            for i, t in enumerate(body["input"])
        ]
        return httpx2.Response(
            200,
            json={"object": "list", "model": body["model"], "data": data,
                  "usage": {"prompt_tokens": 3, "total_tokens": 3}},
        )

    client = openai.OpenAI(
        api_key=KEY,
        base_url="http://openai.test/v1",
        max_retries=0,
        http_client=httpx2.Client(transport=httpx2.MockTransport(handler)),
    )
    embedder = OpenAIEmbedder("text-embedding-3-small", client=client, batch_size=2)
    assert embedder.embed(["a", "bb", "ccc"]) == [[1.0, 0.5], [2.0, 0.5], [3.0, 0.5]]
    assert [(path, auth) for path, auth, _ in seen] == [
        ("/v1/embeddings", f"Bearer {KEY}"), ("/v1/embeddings", f"Bearer {KEY}"),
    ]
    assert seen[0][2] == {
        "model": "text-embedding-3-small", "input": ["a", "bb"], "encoding_format": "float",
    }
    with pytest.raises(EmbeddingUnavailableError, match="rejected the key") as info:
        embedder.embed(["unauthorised"])
    assert KEY not in str(info.value)


def test_embedding_model_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEACHER_EMBEDDING_MODEL", raising=False)
    assert ModelSettings.from_env().embedding == "text-embedding-3-small"
    monkeypatch.setenv("TEACHER_EMBEDDING_MODEL", " text-embedding-3-large ")
    assert ModelSettings.from_env().embedding == "text-embedding-3-large"
    monkeypatch.setenv("TEACHER_EMBEDDING_MODEL", " ")
    with pytest.raises(ValueError, match="TEACHER_EMBEDDING_MODEL is set but empty"):
        ModelSettings.from_env()


def test_check_available_reports_a_missing_key_without_a_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    embedder = OpenAIEmbedder()
    with pytest.raises(EmbeddingUnavailableError, match="no OpenAI API key was found"):
        embedder.check_available()
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-NOT-A-REAL-KEY")
    embedder.check_available()  # a client is made; nothing is sent (the base URL is closed)
    assert embedder._client is not None


def test_concept_model_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEACHER_CONCEPT_MODEL", raising=False)
    monkeypatch.delenv("TEACHER_GENERATION_MODEL", raising=False)
    assert ModelSettings.from_env().concept == "claude-sonnet-5-5"
    monkeypatch.setenv("TEACHER_GENERATION_MODEL", "claude-opus-5-5")  # independent of it
    assert ModelSettings.from_env().concept == "claude-sonnet-5-5"
    monkeypatch.setenv("TEACHER_CONCEPT_MODEL", " claude-test-concepts ")
    assert ModelSettings.from_env().concept == "claude-test-concepts"
    monkeypatch.setenv("TEACHER_CONCEPT_MODEL", "")
    with pytest.raises(ValueError, match="TEACHER_CONCEPT_MODEL is set but empty"):
        ModelSettings.from_env()
