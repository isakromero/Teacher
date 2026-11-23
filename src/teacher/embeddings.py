"""Text embeddings for the concept graph.

- `Embedder`: the protocol the concept manager uses (`model`, `embed(texts)`); tests use a fake.
- `OpenAIEmbedder`: the OpenAI embeddings endpoint through the official `openai` SDK. The client
  is created lazily from `$OPENAI_API_KEY`, so the app starts without a key; a missing key is an
  `EmbeddingUnavailableError` the first time an embedding is requested (or when
  `check_available()` is called, which the generation preflight does).
- `concept_text(name, aliases, summary)`: the exact text embedded for a concept; its SHA-256
  (`text_sha256`) is stored with the vector to tell when the embedding is stale.
- `check_vectors(...)`: the response checks (one finite, non-empty vector per text, all the same
  length) applied to every embedder's output.
"""

from __future__ import annotations

import hashlib
import math
import numbers
import os
from collections.abc import Iterable, Sequence
from typing import Any, Protocol, runtime_checkable

import openai

from teacher.concepts import normalize_name
from teacher.config import DEFAULT_EMBEDDING_MODEL

ENV_OPENAI_API_KEY = "OPENAI_API_KEY"
DEFAULT_BATCH_SIZE = 256  # the API allows 2048 inputs (and 300k tokens) per request
DEFAULT_TIMEOUT_SECONDS = 60.0

NO_KEY_REASON = (
    f"no OpenAI API key was found. Set the {ENV_OPENAI_API_KEY} environment variable for the "
    "Teacher server, then try again."
)


class EmbeddingError(Exception):
    """Embeddings could not be made. The message says why and never contains a secret."""


class EmbeddingUnavailableError(EmbeddingError):
    """The embedding API could not be used: no key, or the request failed."""


class EmbeddingResponseError(EmbeddingError):
    """The embedding API answered, but the answer is unusable (wrong count, ragged or
    non-finite vectors, ...)."""


@runtime_checkable
class Embedder(Protocol):
    """Turns texts into vectors with one fixed model (`model` is stored with each vector)."""

    model: str

    def embed(self, texts: list[str]) -> list[list[float]]:
        """One vector per text, in order, all of the same length."""
        ...


# -- the text that is embedded --------------------------------------------------------------


def concept_text(name: str, aliases: Iterable[str], summary: str) -> str:
    """The text embedded for a concept: its name, its aliases sorted (by normalised form, then
    as written) on an "Also known as" line if there are any, and its summary, one per line."""
    ordered = sorted(aliases, key=lambda alias: (normalize_name(alias, what="alias"), alias))
    lines = [name]
    if ordered:
        lines.append("Also known as: " + "; ".join(ordered))
    lines.append(summary)
    return "\n".join(lines)


def text_sha256(text: str) -> str:
    """Hex SHA-256 of `text` encoded as UTF-8."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# -- response checks ------------------------------------------------------------------------


def check_texts(texts: object) -> list[str]:
    """`texts` as a list of non-blank strings (the API rejects empty input)."""
    if not isinstance(texts, list):
        raise TypeError(f"texts must be a list of strings, got {texts.__class__.__name__}")
    for number, text in enumerate(texts, start=1):
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f"text {number} to embed must be a non-blank string")
    return texts


def check_vectors(
    vectors: object, *, count: int, model: str, dimensions: int | None = None
) -> list[list[float]]:
    """`vectors` as `count` lists of finite floats of one length (`dimensions` if given).

    Raises EmbeddingResponseError naming the model and the first problem found.
    """
    if not isinstance(vectors, list | tuple):
        raise EmbeddingResponseError(
            f"embedding model {model!r} returned {vectors.__class__.__name__}, "
            "not a list of vectors"
        )
    if len(vectors) != count:
        raise EmbeddingResponseError(
            f"embedding model {model!r} returned {len(vectors)} vectors for {count} texts"
        )
    checked: list[list[float]] = []
    for number, vector in enumerate(vectors, start=1):
        if not isinstance(vector, list | tuple) or not vector:
            raise EmbeddingResponseError(
                f"embedding model {model!r}: vector {number} is empty or not a list"
            )
        values: list[float] = []
        for value in vector:
            if isinstance(value, bool) or not isinstance(value, numbers.Real):
                raise EmbeddingResponseError(
                    f"embedding model {model!r}: vector {number} contains a non-number "
                    f"({value.__class__.__name__})"
                )
            if not math.isfinite(value):
                raise EmbeddingResponseError(
                    f"embedding model {model!r}: vector {number} contains {value!r}"
                )
            values.append(float(value))
        if dimensions is None:
            dimensions = len(values)
        elif len(values) != dimensions:
            raise EmbeddingResponseError(
                f"embedding model {model!r}: vector {number} has {len(values)} dimensions, "
                f"expected {dimensions} like the others"
            )
        checked.append(values)
    return checked


# -- OpenAI ---------------------------------------------------------------------------------


def describe_openai_error(exc: Exception, *, model: str, timeout: float) -> str:
    """A key-free, plain-language reason for an exception from the `openai` SDK."""
    if isinstance(exc, openai.APIStatusError):
        detail = f"HTTP {exc.status_code}"
        if exc.request_id:
            detail += f", request id {exc.request_id}"
        if isinstance(exc, openai.AuthenticationError):
            return f"the OpenAI API rejected the key ({detail}). Check {ENV_OPENAI_API_KEY}."
        if isinstance(exc, openai.PermissionDeniedError):
            return f"the OpenAI key is not allowed to use model {model!r} ({detail})."
        if isinstance(exc, openai.NotFoundError):
            return f"the OpenAI API does not know embedding model {model!r} ({detail})."
        if isinstance(exc, openai.RateLimitError):
            return f"the OpenAI API rate limit or quota was reached ({detail}). Try again later."
        if isinstance(exc, openai.InternalServerError):
            return f"the OpenAI API had a server error ({detail}). Try again later."
        return f"the OpenAI API returned an error ({detail})."
    if isinstance(exc, openai.APITimeoutError):  # before APIConnectionError, its base class
        return f"the OpenAI API did not answer within {timeout:g} seconds. Try again."
    if isinstance(exc, openai.APIConnectionError):
        return "could not connect to the OpenAI API. Check the network connection."
    return f"the OpenAI API call failed ({exc.__class__.__name__})."


class OpenAIEmbedder:
    """Embeddings from the OpenAI API (`client.embeddings.create`), `batch_size` texts per
    request. Pass `client` to use a ready client (tests pass a fake); otherwise one is created
    on first use from `$OPENAI_API_KEY`."""

    def __init__(
        self,
        model: str = DEFAULT_EMBEDDING_MODEL,
        *,
        client: Any = None,
        batch_size: int = DEFAULT_BATCH_SIZE,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model must be a non-empty string")
        if isinstance(batch_size, bool) or not isinstance(batch_size, int) or not (
            1 <= batch_size <= 2048
        ):
            raise ValueError("batch_size must be an integer from 1 to 2048")
        self.model = model
        self.batch_size = batch_size
        self.timeout = timeout
        self._client = client

    def _get_client(self) -> Any:
        if self._client is None:
            key = os.environ.get(ENV_OPENAI_API_KEY)
            if key is None or not key.strip():
                raise EmbeddingUnavailableError(f"Embedding is unavailable: {NO_KEY_REASON}")
            self._client = openai.OpenAI(api_key=key.strip(), timeout=self.timeout)
        return self._client

    def check_available(self) -> None:
        """EmbeddingUnavailableError now if no client can be created (no `$OPENAI_API_KEY`).
        Makes no request; a key the API would reject is only found on the first embedding."""
        self._get_client()

    def embed(self, texts: list[str]) -> list[list[float]]:
        """One vector per text, in order. No request is made for an empty list."""
        check_texts(texts)
        if not texts:
            return []
        client = self._get_client()
        vectors: list[list[float]] = []
        dimensions: int | None = None
        for start in range(0, len(texts), self.batch_size):
            batch = texts[start : start + self.batch_size]
            try:
                response = client.embeddings.create(
                    model=self.model, input=batch, encoding_format="float"
                )
            except openai.OpenAIError as exc:
                reason = describe_openai_error(exc, model=self.model, timeout=self.timeout)
                raise EmbeddingUnavailableError(f"Embedding failed: {reason}") from exc
            checked = check_vectors(
                _ordered_embeddings(response, len(batch), self.model),
                count=len(batch),
                model=self.model,
                dimensions=dimensions,
            )
            dimensions = len(checked[0])
            vectors.extend(checked)
        return vectors


def _ordered_embeddings(response: Any, count: int, model: str) -> list[Any]:
    """The response's vectors ordered by their `index`, which must be exactly 0..count-1."""
    data: Sequence[Any] | None = getattr(response, "data", None)
    if not isinstance(data, list | tuple):
        raise EmbeddingResponseError(f"embedding model {model!r} returned no data list")
    if len(data) != count:
        raise EmbeddingResponseError(
            f"embedding model {model!r} returned {len(data)} vectors for {count} texts"
        )
    indexes = [getattr(item, "index", None) for item in data]
    if any(isinstance(i, bool) or not isinstance(i, int) for i in indexes) or sorted(
        indexes  # type: ignore[type-var]
    ) != list(range(count)):
        raise EmbeddingResponseError(
            f"embedding model {model!r} returned indexes {indexes}, expected 0..{count - 1}"
        )
    by_index = {item.index: item for item in data}
    return [getattr(by_index[i], "embedding", None) for i in range(count)]


__all__ = [
    "DEFAULT_BATCH_SIZE",
    "ENV_OPENAI_API_KEY",
    "NO_KEY_REASON",
    "Embedder",
    "EmbeddingError",
    "EmbeddingResponseError",
    "EmbeddingUnavailableError",
    "OpenAIEmbedder",
    "check_texts",
    "check_vectors",
    "concept_text",
    "describe_openai_error",
    "text_sha256",
]
