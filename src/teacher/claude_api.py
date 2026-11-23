"""Shared Anthropic SDK plumbing for the LLM judge, the study-page generator and the tutor.

The callers create their client lazily (so the app starts without credentials) and turn every
SDK failure into their own loud, key-free error. This module holds the two pieces they share:

- `default_client(error)`: create `anthropic.Anthropic()` from the environment, or raise
  `error(<reason>)` when no credentials are configured or they cannot be loaded.
- `describe_api_error(exc, ...)`: a plain-language reason for an SDK exception, most specific
  class first (auth, permission, rate limit, server error, other status, timeout, connection,
  credentials).

The reasons are sentence fragments ("the Anthropic API rate limit was reached (...)") that the
caller prefixes with what failed ("Grading is unavailable: ", "Generation failed: ").

- `source_document_block(...)`: the `document` content block for a knowledge source's file (a
  base64 PDF or a plain-text document), checked against the 32 MB request limit; the generator
  and the tutor send a paper this way.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import anthropic

# The Messages API rejects request bodies over 32 MB. A document is checked against this before
# any call, with room left for the prompts (and, for the generator, the outline).
MAX_REQUEST_BYTES = 32_000_000
REQUEST_OVERHEAD_BYTES = 256_000
PDF_MEDIA_TYPE = "application/pdf"

NO_CREDENTIALS_REASON = (
    "no Anthropic API credentials were found. "
    "Set the ANTHROPIC_API_KEY environment variable for the Teacher server "
    "(or log in with `ant auth login`), then try again."
)


def default_client(error: Callable[[str], Exception]) -> anthropic.Anthropic:
    """`anthropic.Anthropic()` from the environment; raises `error(reason)` without credentials.

    The SDK raises `CredentialsError` for a profile it cannot load; when nothing is configured at
    all it builds a client without credentials, which is checked here so the failure is reported
    before any request is attempted.
    """
    try:
        client = anthropic.Anthropic()
    except anthropic.CredentialsError as exc:
        raise error(
            f"Anthropic credentials could not be loaded ({exc}). "
            "Fix the Anthropic profile or set ANTHROPIC_API_KEY, then try again."
        ) from exc
    if client.api_key is None and client.auth_token is None and client.credentials is None:
        raise error(NO_CREDENTIALS_REASON)
    return client


def status_detail(exc: anthropic.APIStatusError, *, with_api_message: bool = False) -> str:
    """`HTTP 429, rate_limit_error, request id req_...` (plus the API's own message if asked)."""
    parts = [f"HTTP {exc.status_code}"]
    if exc.type:
        parts.append(str(exc.type))
    if exc.request_id:
        parts.append(f"request id {exc.request_id}")
    if with_api_message and exc.api_message:
        parts.append(f"message: {exc.api_message}")
    return ", ".join(parts)


def describe_api_error(
    exc: anthropic.AnthropicError,
    *,
    model: str,
    timeout: float,
    with_api_message: bool = False,
) -> str:
    """A key-free, plain-language reason for an SDK exception raised by a Messages API call.

    `with_api_message` appends the API's own error message to status errors (useful for a
    400 such as "prompt is too long"; the judge leaves it out because its errors reach learners).
    """
    if isinstance(exc, anthropic.APIStatusError):
        detail = status_detail(exc, with_api_message=with_api_message)
        if isinstance(exc, anthropic.AuthenticationError):
            return f"the Anthropic API rejected the credentials ({detail}). Check ANTHROPIC_API_KEY."
        if isinstance(exc, anthropic.PermissionDeniedError):
            return (
                f"the Anthropic credentials are not allowed to use model {model!r} ({detail})."
            )
        if isinstance(exc, anthropic.RateLimitError):
            return (
                f"the Anthropic API rate limit was reached ({detail}). "
                "Wait a moment and try again."
            )
        if isinstance(exc, anthropic.InternalServerError):
            return (
                "the Anthropic API had a server error or is overloaded "
                f"({detail}). Try again later."
            )
        return f"the Anthropic API returned an error ({detail})."
    if isinstance(exc, anthropic.APITimeoutError):  # before APIConnectionError, its base class
        return f"the Anthropic API did not answer within {timeout:g} seconds. Try again."
    if isinstance(exc, anthropic.APIConnectionError):
        return "could not connect to the Anthropic API. Check the network connection."
    if isinstance(exc, anthropic.CredentialsError):
        return f"Anthropic credentials could not be used ({exc})."
    return f"the Anthropic API call failed ({exc.__class__.__name__})."


# -- the source document --------------------------------------------------------------------


def _base_media_type(media_type: str) -> str:
    return media_type.split(";", 1)[0].strip().lower()


def mb(size: int) -> str:
    """`size` bytes as `"12.3 MB"`."""
    return f"{size / 1_000_000:.1f} MB"


def source_document_block(
    path: Path,
    *,
    slug: str,
    title: str,
    media_type: str,
    cache_control: Mapping[str, str],
    error: Callable[[str], Exception],
) -> tuple[dict[str, Any], bool]:
    """The `document` content block for the source file at `path`, and whether it is a PDF.

    PDFs are sent as base64 documents, `text/*` files as plain-text documents; the block carries
    `title` and a copy of `cache_control`. Raises `error(reason)` for an unreadable, empty,
    mislabelled, non-UTF-8, unsupported or oversized file (nothing is truncated).
    """
    base = _base_media_type(media_type)
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise error(
            f"could not read the file of source {slug!r} ({exc.__class__.__name__})"
        ) from exc
    if not data:
        raise error(f"the file of source {slug!r} is empty")

    def check_size(encoded_size: int, what: str) -> None:
        if encoded_size + REQUEST_OVERHEAD_BYTES > MAX_REQUEST_BYTES:
            raise error(
                f"source {slug!r} is too large to send: {what} is {mb(encoded_size)}, and "
                f"with the prompts the request would exceed the Messages API's 32 MB request "
                "limit. Use a smaller file (for example, split the document); it was not "
                "truncated."
            )

    if base == PDF_MEDIA_TYPE:
        if not data.startswith(b"%PDF-"):
            raise error(f"source {slug!r} is labelled application/pdf but is not a PDF file")
        encoded_size = 4 * ((len(data) + 2) // 3)  # base64 length
        check_size(encoded_size, f"the PDF ({mb(len(data))}) encoded as base64")
        payload: dict[str, Any] = {
            "type": "base64",
            "media_type": PDF_MEDIA_TYPE,
            "data": base64.standard_b64encode(data).decode("ascii"),
        }
        is_pdf = True
    elif base.startswith("text/"):
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise error(f"source {slug!r} ({base}) is not valid UTF-8 text") from exc
        if not text.strip():
            raise error(f"the file of source {slug!r} contains only whitespace")
        check_size(len(json.dumps(text)), "the text")
        payload = {"type": "text", "media_type": "text/plain", "data": text}
        is_pdf = False
    else:
        raise error(
            f"source {slug!r} has media type {base!r}, which Teacher cannot read; supported "
            "are application/pdf and text/* (plain text, Markdown)"
        )
    block = {
        "type": "document",
        "source": payload,
        "title": title,
        "cache_control": dict(cache_control),
    }
    return block, is_pdf


__all__ = [
    "MAX_REQUEST_BYTES",
    "NO_CREDENTIALS_REASON",
    "PDF_MEDIA_TYPE",
    "REQUEST_OVERHEAD_BYTES",
    "default_client",
    "describe_api_error",
    "mb",
    "source_document_block",
    "status_detail",
]
