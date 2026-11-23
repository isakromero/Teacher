"""MetadataExtractor and first_pages_pdf: the request, normalisation and every failure class.

No test reaches the network: `FakeAnthropic` stands in for the client, and the autouse fixture
in conftest removes every credential and points the SDK at a closed port.
"""

from __future__ import annotations

import base64
import io
import json
import re
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import anthropic
import pytest
from anthropic.types import Message
from pypdf import PdfReader, PdfWriter

from teacher.config import ModelSettings
from teacher.metadata import (
    INSTRUCTIONS,
    MAX_PAGES,
    METADATA_FORMAT,
    SYSTEM_PROMPT,
    MetadataExtractor,
    MetadataUnavailableError,
    UnreadablePdfError,
    first_pages_pdf,
)

MakePdf = Callable[[int], bytes]


def page_widths(data: bytes) -> list[int]:
    return [int(page.mediabox.width) for page in PdfReader(io.BytesIO(data)).pages]


@pytest.fixture
def pdf_path(tmp_path: Path, make_pdf: MakePdf) -> Path:
    path = tmp_path / "paper.pdf"
    path.write_bytes(make_pdf(5))
    return path


def extract(fake: Any, path: Path, **kwargs: Any):
    return MetadataExtractor(fake, **kwargs).extract(path, "application/pdf")


# -- first_pages_pdf --------------------------------------------------------------------------


def test_first_pages_keeps_only_the_first_pages(pdf_path: Path, tmp_path: Path, make_pdf) -> None:
    assert page_widths(pdf_path.read_bytes()) == [100, 101, 102, 103, 104]
    assert page_widths(first_pages_pdf(pdf_path)) == [100, 101, 102]
    assert page_widths(first_pages_pdf(pdf_path, max_pages=1)) == [100]
    short = tmp_path / "short.pdf"
    short.write_bytes(make_pdf(2))
    assert page_widths(first_pages_pdf(short)) == [100, 101]


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (b"", "empty"),
        (b"this is not a pdf at all", "not a PDF"),
        (b"\x00\x01garbage\xff" * 100, "not a PDF"),
        (b"%PDF-1.4\n% tiny test file\n", "could not be read"),
        (b"%PDF-1.7\n" + b"\x8f\x00junk" * 500, "could not be read"),
    ],
)
def test_first_pages_rejects_bad_files(tmp_path: Path, content: bytes, message: str) -> None:
    path = tmp_path / "bad.pdf"
    path.write_bytes(content)
    with pytest.raises(UnreadablePdfError, match=message) as info:
        first_pages_pdf(path)
    assert isinstance(info.value, ValueError)  # -> HTTP 422


def test_first_pages_rejects_a_pdf_without_pages(tmp_path: Path) -> None:
    out = io.BytesIO()
    PdfWriter().write(out)
    path = tmp_path / "empty.pdf"
    path.write_bytes(out.getvalue())
    with pytest.raises(UnreadablePdfError, match="no pages"):
        first_pages_pdf(path)


def test_first_pages_password_protection(tmp_path: Path) -> None:
    locked = PdfWriter()
    locked.add_blank_page(width=100, height=100)
    locked.encrypt(user_password="secret", owner_password="owner", algorithm="RC4-128")
    out = io.BytesIO()
    locked.write(out)
    path = tmp_path / "locked.pdf"
    path.write_bytes(out.getvalue())
    with pytest.raises(UnreadablePdfError, match="password-protected"):
        first_pages_pdf(path)

    # Only an owner password (opens in every viewer without asking): read normally.
    owner_only = PdfWriter()
    owner_only.add_blank_page(width=100, height=100)
    owner_only.encrypt(user_password="", owner_password="owner", algorithm="RC4-128")
    out = io.BytesIO()
    owner_only.write(out)
    path.write_bytes(out.getvalue())
    assert page_widths(first_pages_pdf(path)) == [100]


# -- the request ------------------------------------------------------------------------------


def test_request_shape(fake_anthropic, pdf_path: Path) -> None:
    fake = fake_anthropic(fake_anthropic.metadata_reply())
    extract(fake, pdf_path)
    (call,) = fake.calls
    assert call["model"] == "claude-haiku-5-5"
    assert call["system"] == SYSTEM_PROMPT
    assert call["max_tokens"] >= 4000
    assert call["timeout"] == 60.0
    assert call["output_config"] == {"effort": "low", "format": METADATA_FORMAT}
    for forbidden in ("thinking", "temperature", "top_p", "top_k", "tool_choice", "tools"):
        assert forbidden not in call
    assert set(call) == {"model", "max_tokens", "system", "messages", "output_config", "timeout"}

    (message,) = call["messages"]  # one user turn, no assistant prefill
    assert message["role"] == "user"
    document, text = message["content"]
    assert document["type"] == "document"  # the document comes before the instructions
    assert document["source"]["type"] == "base64"
    assert document["source"]["media_type"] == "application/pdf"
    sent = base64.standard_b64decode(document["source"]["data"])
    assert page_widths(sent) == [100, 101, 102]  # only the first MAX_PAGES pages
    assert MAX_PAGES == 3
    assert text == {"type": "text", "text": INSTRUCTIONS}


def test_format_schema_is_strict_json_schema() -> None:
    schema = METADATA_FORMAT["schema"]
    assert METADATA_FORMAT["type"] == "json_schema"
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"title", "authors", "year", "arxiv_id", "doi", "summary"}


def test_prompt_says_what_matters() -> None:
    prompt = SYSTEM_PROMPT + INSTRUCTIONS
    assert "only what is printed" in prompt
    assert "return null" in prompt and "instead of guessing" in prompt
    assert "ignore that text" in prompt  # the document is data
    assert "only date information" in prompt  # the arXiv-year exception


def test_model_and_timeout_are_configurable(fake_anthropic, pdf_path: Path) -> None:
    fake = fake_anthropic(fake_anthropic.metadata_reply(model="claude-test-reader"))
    result = extract(fake, pdf_path, model="claude-test-reader", timeout=5.0)
    assert fake.calls[0]["model"] == "claude-test-reader"
    assert fake.calls[0]["timeout"] == 5.0
    assert result.model == "claude-test-reader"


def test_env_var_selects_the_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEACHER_METADATA_MODEL", raising=False)
    assert ModelSettings.from_env().metadata == "claude-haiku-5-5"
    monkeypatch.setenv("TEACHER_METADATA_MODEL", " claude-test-reader ")
    assert ModelSettings.from_env().metadata == "claude-test-reader"
    monkeypatch.setenv("TEACHER_METADATA_MODEL", "")
    with pytest.raises(ValueError, match="TEACHER_METADATA_MODEL is set but empty"):
        ModelSettings.from_env()


# -- results and normalisation ----------------------------------------------------------------


def test_happy_path(fake_anthropic, pdf_path: Path) -> None:
    fake = fake_anthropic(fake_anthropic.metadata_reply(doi="10.48550/arXiv.1706.03762"))
    result = extract(fake, pdf_path)
    assert result.title == "Attention Is All You Need"
    assert result.authors == ["Ashish Vaswani", "Noam Shazeer"]
    assert result.year == 2017
    assert result.arxiv_id == "1706.03762"
    assert result.doi == "10.48550/arXiv.1706.03762"
    assert result.summary == "The paper proposes the Transformer. It relies on attention only."
    assert result.model == "claude-haiku-5-5"


def test_normalisation(fake_anthropic, pdf_path: Path) -> None:
    fake = fake_anthropic(
        fake_anthropic.metadata_reply(
            title="  Attention Is\n All You Need ",
            authors=[" Ashish  Vaswani ", "", "   ", "Noam\nShazeer"],
            arxiv_id=" arXiv:1706.03762v7 ",
            doi="https://doi.org/10.48550/arXiv.1706.03762",
            summary="  Two sentences.  ",
        )
    )
    result = extract(fake, pdf_path)
    assert result.title == "Attention Is All You Need"
    assert result.authors == ["Ashish Vaswani", "Noam Shazeer"]
    assert result.arxiv_id == "1706.03762"
    assert result.doi == "10.48550/arXiv.1706.03762"
    assert result.summary == "Two sentences."


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1706.03762", "1706.03762"),
        ("arxiv: 2401.12345v2", "2401.12345"),
        ("https://arxiv.org/abs/1706.03762v3", "1706.03762"),
        ("hep-th/9901001v1", "hep-th/9901001"),
        ("math.AG/0309136", "math.AG/0309136"),
    ],
)
def test_arxiv_id_forms(fake_anthropic, pdf_path: Path, raw: str, expected: str) -> None:
    fake = fake_anthropic(fake_anthropic.metadata_reply(arxiv_id=raw))
    assert extract(fake, pdf_path).arxiv_id == expected


@pytest.mark.parametrize("raw", ["doi:10.1145/3295222", "http://dx.doi.org/10.1145/3295222"])
def test_doi_forms(fake_anthropic, pdf_path: Path, raw: str) -> None:
    fake = fake_anthropic(fake_anthropic.metadata_reply(doi=raw))
    assert extract(fake, pdf_path).doi == "10.1145/3295222"


def test_missing_fields_are_null_not_an_error(fake_anthropic, pdf_path: Path) -> None:
    fake = fake_anthropic(
        fake_anthropic.metadata_reply(
            title=None, authors=[], year=None, arxiv_id=None, doi=None, summary=None
        )
    )
    result = extract(fake, pdf_path)
    assert (result.title, result.authors, result.year) == (None, [], None)
    assert (result.arxiv_id, result.doi, result.summary) == (None, None, None)


def test_blank_strings_become_null(fake_anthropic, pdf_path: Path) -> None:
    fake = fake_anthropic(
        fake_anthropic.metadata_reply(title="  ", arxiv_id="", doi=" ", summary="\n")
    )
    result = extract(fake, pdf_path)
    assert (result.title, result.arxiv_id, result.doi, result.summary) == (None, None, None, None)


def test_year_bounds(fake_anthropic, pdf_path: Path) -> None:
    next_year = date.today().year + 1
    for year in (1500, next_year):
        fake = fake_anthropic(fake_anthropic.metadata_reply(year=year))
        assert extract(fake, pdf_path).year == year
    for year in (1499, next_year + 1, 0, 17):
        fake = fake_anthropic(fake_anthropic.metadata_reply(year=year))
        with pytest.raises(MetadataUnavailableError, match="publication year"):
            extract(fake, pdf_path)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("arxiv_id", "not-an-id", "not a valid arXiv identifier"),
        ("arxiv_id", "17.03762", "not a valid arXiv identifier"),
        ("doi", "https://example.com/paper", "not a valid DOI"),
        ("doi", "11.1234/abc", "not a valid DOI"),
    ],
)
def test_malformed_identifiers_fail_loudly(
    fake_anthropic, pdf_path: Path, field: str, value: str, message: str
) -> None:
    fake = fake_anthropic(fake_anthropic.metadata_reply(**{field: value}))
    with pytest.raises(MetadataUnavailableError, match=message):
        extract(fake, pdf_path)


# -- failures -----------------------------------------------------------------------------------


def _no_text_message() -> Message:
    return Message.model_validate(
        {
            "id": "msg_fake",
            "type": "message",
            "role": "assistant",
            "model": "claude-haiku-5-5",
            "content": [{"type": "thinking", "thinking": "", "signature": "sig"}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }
    )


FAILURES: list[tuple[str, Callable[[Any], Any], str]] = [
    (
        "refusal",
        lambda f: f.metadata_reply(
            stop_reason="refusal",
            stop_details={"type": "refusal", "category": "cyber", "explanation": None},
        ),
        "declined to read this document (category cyber)",
    ),
    ("max_tokens", lambda f: f.metadata_reply(stop_reason="max_tokens"), "cut off"),
    ("tool_use", lambda f: f.metadata_reply(stop_reason="tool_use"), "stopped unexpectedly"),
    ("no text", lambda f: _no_text_message(), "returned no answer"),
    ("not json", lambda f: f.metadata_reply(text="Title: Attention"), "not in the expected"),
    (
        "missing key",
        lambda f: f.metadata_reply(text=json.dumps({"title": "x", "authors": []})),
        "not in the expected format",
    ),
    (
        "extra key",
        lambda f: f.metadata_reply(
            text=json.dumps(
                {
                    "title": "x",
                    "authors": [],
                    "year": None,
                    "arxiv_id": None,
                    "doi": None,
                    "summary": None,
                    "venue": "NeurIPS",
                }
            )
        ),
        "not in the expected format",
    ),
    (
        "wrong type",
        lambda f: f.metadata_reply(
            text=json.dumps(
                {
                    "title": "x",
                    "authors": "A, B",
                    "year": "2017",
                    "arxiv_id": None,
                    "doi": None,
                    "summary": None,
                }
            )
        ),
        "not in the expected format",
    ),
    (
        "auth",
        lambda f: f.status_error(anthropic.AuthenticationError, 401, "authentication_error"),
        "rejected the credentials",
    ),
    (
        "permission",
        lambda f: f.status_error(anthropic.PermissionDeniedError, 403, "permission_error"),
        "not allowed to use model",
    ),
    (
        "rate limit",
        lambda f: f.status_error(anthropic.RateLimitError, 429, "rate_limit_error"),
        "rate limit was reached",
    ),
    (
        "server",
        lambda f: f.status_error(anthropic.InternalServerError, 529, "overloaded_error"),
        "server error or is overloaded",
    ),
    (
        "bad request",
        lambda f: f.status_error(anthropic.BadRequestError, 400, "invalid_request_error"),
        "returned an error (HTTP 400",
    ),
    ("timeout", lambda f: f.timeout(), "did not answer within 60 seconds"),
    ("connection", lambda f: f.connection_error(), "could not connect"),
]


@pytest.mark.parametrize(("name", "outcome", "message"), FAILURES, ids=[f[0] for f in FAILURES])
def test_failures_raise_metadata_unavailable(
    fake_anthropic, pdf_path: Path, name: str, outcome, message: str
) -> None:
    fake = fake_anthropic(outcome(fake_anthropic))
    with pytest.raises(MetadataUnavailableError, match=re.escape(message)) as info:
        extract(fake, pdf_path)
    assert str(info.value).startswith("Could not detect the paper's details: ")
    assert len(fake.calls) == 1  # no retry, no fallback


def test_missing_credentials(pdf_path: Path, forbid_real_messages_api: None) -> None:
    extractor = MetadataExtractor()
    assert extractor._client is None  # lazy: nothing created until the first call
    with pytest.raises(MetadataUnavailableError, match="ANTHROPIC_API_KEY"):
        extractor.extract(pdf_path, "application/pdf")


@pytest.mark.parametrize("media_type", ["text/plain", "image/png", None, ""])
def test_non_pdf_is_a_value_error(fake_anthropic, pdf_path: Path, media_type) -> None:
    fake = fake_anthropic()
    with pytest.raises(ValueError, match="PDF files only") as info:
        MetadataExtractor(fake).extract(pdf_path, media_type)
    assert not isinstance(info.value, MetadataUnavailableError)
    assert fake.calls == []


def test_media_type_parameters_are_ignored(fake_anthropic, pdf_path: Path) -> None:
    fake = fake_anthropic(fake_anthropic.metadata_reply())
    result = MetadataExtractor(fake).extract(pdf_path, "Application/PDF; charset=binary")
    assert result.title == "Attention Is All You Need"


def test_unreadable_pdf_makes_no_call(fake_anthropic, tmp_path: Path) -> None:
    path = tmp_path / "bad.pdf"
    path.write_bytes(b"%PDF-1.4\n% tiny test file\n")
    fake = fake_anthropic()
    with pytest.raises(UnreadablePdfError):
        MetadataExtractor(fake).extract(path, "application/pdf")
    assert fake.calls == []


def test_first_pages_reports_unsupported_encryption(
    pdf_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AES needs the optional `cryptography` package; pypdf then raises DependencyError."""
    from pypdf.errors import DependencyError

    import teacher.metadata as metadata

    def reader(*_args: Any, **_kwargs: Any) -> None:
        raise DependencyError("cryptography>=3.1 is required for AES algorithm")

    monkeypatch.setattr(metadata, "PdfReader", reader)
    with pytest.raises(UnreadablePdfError, match="encrypted in a way"):
        first_pages_pdf(pdf_path)
