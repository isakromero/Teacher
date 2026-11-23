"""ReferenceExtractor with a fake Claude client (no network): the request, the reply's checks,
every way a call can fail, the stub, and running an extraction end to end (claim, model call,
store and match, or a recorded failure that keeps the source and the earlier references)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import anthropic
import pytest

from teacher.config import DEFAULT_METADATA_MODEL, Settings
from teacher.db.engine import session_scope
from teacher.managers import KnowledgeSourceManager, SourceReferenceManager
from teacher.managers.source_references import ExtractionBusyError
from teacher.reference_extraction import (
    EFFORT,
    ERROR_PREFIX,
    INSTRUCTIONS,
    MAX_REFERENCES,
    MAX_TOKENS,
    REFERENCES_FORMAT,
    SYSTEM_PROMPT,
    ReferenceExtractor,
    ReferenceInputError,
    ReferenceOutputError,
    ReferencesUnavailableError,
    SourceDocument,
    StubReferenceExtractor,
    WireReferences,
    check_references,
    claim_extraction,
    extract_references,
    run_claimed,
)

MODEL = "claude-test-references"


def entry(text: str = "Vaswani et al. Attention is all you need. 2017.", **kw: Any) -> dict[str, Any]:
    fields = {"text": text, "title": None, "authors": [], "year": None, "venue": None,
              "doi": None, "arxiv_id": None, "url": None}  # fmt: skip
    fields.update(kw)
    return fields


def reply(fake: Any, references: list[dict[str, Any]], found_in: str = "reference_list", **kw: Any):
    kw.setdefault("model", MODEL)
    return fake.reply(
        text=json.dumps({"found_in": found_in, "references": references}), **kw
    )


@pytest.fixture
def document(tmp_path: Path) -> SourceDocument:
    path = tmp_path / "source.md"
    path.write_text("# Paper\n\nText.\n\n## References\n\n1. A cited work.\n", encoding="utf-8")
    return SourceDocument(
        source_id=1, slug="paper", title="Paper", kind="paper", path=path,
        media_type="text/markdown",
    )  # fmt: skip


# -- the request --------------------------------------------------------------------------------


def test_request_shape(fake_anthropic, document: SourceDocument) -> None:
    fake = fake_anthropic(reply(fake_anthropic, [entry()]))
    ReferenceExtractor(fake, model=MODEL).extract(document)
    (call,) = fake.calls
    assert call["model"] == MODEL
    assert call["max_tokens"] == MAX_TOKENS
    assert call["system"] == SYSTEM_PROMPT
    assert call["output_config"] == {"effort": EFFORT, "format": REFERENCES_FORMAT}
    assert "temperature" not in call and "thinking" not in call and "tool_choice" not in call
    (message,) = call["messages"]
    block, instructions = message["content"]
    assert block["type"] == "document" and "cache_control" not in block
    assert block["source"] == {
        "type": "text", "media_type": "text/plain", "data": document.path.read_text()
    }
    assert instructions == {"type": "text", "text": INSTRUCTIONS}
    assert len(fake.streams) == 1 and fake.streams[0].closed  # streamed, then closed


def test_pdf_is_sent_as_a_base64_document(fake_anthropic, tmp_path: Path, make_pdf) -> None:
    path = tmp_path / "paper.pdf"
    path.write_bytes(make_pdf(2))
    fake = fake_anthropic(reply(fake_anthropic, [], found_in="none"))
    doc = SourceDocument(1, "p", "P", "paper", path, "application/pdf")
    ReferenceExtractor(fake).extract(doc)
    block = fake.calls[0]["messages"][0]["content"][0]
    assert block["source"]["type"] == "base64" and block["source"]["media_type"] == "application/pdf"
    assert fake.calls[0]["model"] == DEFAULT_METADATA_MODEL  # the metadata model by default


def test_the_default_client_is_lazy_and_needs_credentials(
    document: SourceDocument, forbid_real_messages_api: None
) -> None:
    extractor = ReferenceExtractor()  # no client is created yet
    with pytest.raises(ReferencesUnavailableError, match="credentials"):
        extractor.check_available()
    with pytest.raises(ReferencesUnavailableError, match=f"^{ERROR_PREFIX}"):
        extractor.extract(document)


# -- the reply ----------------------------------------------------------------------------------


def test_a_reference_list_is_normalised(fake_anthropic, document: SourceDocument) -> None:
    fake = fake_anthropic(
        reply(fake_anthropic, [
            entry("  Vaswani,  A. Attention\nis all you need.  ", title=" Attention is\nall you need ",
                  authors=["Ashish Vaswani", " ", "Noam Shazeer"], year=2017, venue="NeurIPS",
                  arxiv_id="arXiv:1706.03762v5"),
            entry("Krizhevsky. ImageNet.", doi="https://doi.org/10.1145/3065386.",
                  url="https://dl.acm.org/doi/10.1145/3065386"),
        ], input_tokens=900, output_tokens=120)
    )  # fmt: skip
    result = ReferenceExtractor(fake, model=MODEL).extract(document)
    assert result.found_in == "reference_list"
    assert (result.model, result.input_tokens, result.output_tokens) == (MODEL, 900, 120)
    first, second = result.references
    assert first.raw_text == "Vaswani, A. Attention is all you need."
    assert first.title == "Attention is all you need"
    assert first.authors == ("Ashish Vaswani", "Noam Shazeer")
    assert (first.year, first.venue, first.arxiv_id, first.doi) == (2017, "NeurIPS", "1706.03762", None)
    assert second.doi == "10.1145/3065386" and second.url == "https://dl.acm.org/doi/10.1145/3065386"


def test_no_references_is_a_result_not_an_error(fake_anthropic, document: SourceDocument) -> None:
    fake = fake_anthropic(reply(fake_anthropic, [], found_in="none"))
    result = ReferenceExtractor(fake).extract(document)
    assert (result.found_in, result.references) == ("none", ())


@pytest.mark.parametrize(
    ("found_in", "references", "message"),
    [
        ("none", [entry()], "found_in is 'none' but 1 reference"),
        ("reference_list", [], "found_in is 'reference_list' but no references"),
        ("reference_list", [entry("  ")], "reference 1 has no text"),
        ("reference_list", [entry(doi="1706.03762")], "is not a valid DOI"),
        ("reference_list", [entry(arxiv_id="attention")], "not a valid arXiv identifier"),
        ("reference_list", [entry(url="www.example.org")], "is not an http\\(s\\) address"),
        ("inline_citations", [entry(year=3000)], "the year 3000 is outside"),
        ("reference_list", [entry("x" * 2001)], "text is longer than 2000"),
        ("reference_list", [entry(title="t" * 501)], "title is longer than 500"),
        ("reference_list", [entry()] * (MAX_REFERENCES + 1), f"at most {MAX_REFERENCES}"),
    ],
)
def test_reply_rules(
    fake_anthropic, document: SourceDocument, found_in: str, references: list, message: str
) -> None:
    fake = fake_anthropic(reply(fake_anthropic, references, found_in=found_in, input_tokens=7))
    with pytest.raises(ReferenceOutputError, match=message) as info:
        ReferenceExtractor(fake).extract(document)
    assert str(info.value).startswith(ERROR_PREFIX)
    assert info.value.tokens == (7, 54)  # the reply's tokens are kept for the record


def test_every_problem_is_counted() -> None:
    wire = WireReferences.model_validate(
        {"found_in": "reference_list", "references": [entry(doi="x"), entry(arxiv_id="y")] * 4}
    )
    with pytest.raises(ReferenceOutputError, match=r"breaks 8 rule\(s\).*\(and 3 more\)"):
        check_references(wire)


@pytest.mark.parametrize(
    ("outcome", "error", "message"),
    [
        ("refusal", ReferencesUnavailableError, "declined"),
        ("max_tokens", ReferencesUnavailableError, "cut off"),
        ("not json", ReferenceOutputError, "not a valid WireReferences"),
        ("extra field", ReferenceOutputError, "not a valid WireReferences"),
    ],
)
def test_unusable_replies(
    fake_anthropic, document: SourceDocument, outcome: str, error: type, message: str
) -> None:
    if outcome == "refusal":
        message_obj = fake_anthropic.reply(text="", stop_reason="refusal")
    elif outcome == "max_tokens":
        message_obj = reply(fake_anthropic, [entry()], stop_reason="max_tokens")
    elif outcome == "not json":
        message_obj = fake_anthropic.reply(text="Here are the references: ...")
    else:
        message_obj = fake_anthropic.reply(
            text=json.dumps({"found_in": "none", "references": [], "note": "x"})
        )
    with pytest.raises(error, match=message):
        ReferenceExtractor(fake_anthropic(message_obj)).extract(document)


@pytest.mark.parametrize(
    ("raised", "message"),
    [
        ("rate", "rate limit was reached"),
        ("auth", "rejected the credentials"),
        ("timeout", "did not answer within 300 seconds"),
        ("connection", "could not connect"),
    ],
)
def test_api_errors(fake_anthropic, document: SourceDocument, raised: str, message: str) -> None:
    exc = {
        "rate": lambda: fake_anthropic.status_error(anthropic.RateLimitError, 429, "rate_limit_error"),
        "auth": lambda: fake_anthropic.status_error(anthropic.AuthenticationError, 401),
        "timeout": fake_anthropic.timeout,
        "connection": fake_anthropic.connection_error,
    }[raised]()
    with pytest.raises(ReferencesUnavailableError, match=message) as info:
        ReferenceExtractor(fake_anthropic(exc)).extract(document)
    assert str(info.value).startswith(f"{ERROR_PREFIX}the references call to Claude failed: ")


@pytest.mark.parametrize(
    ("media_type", "content", "message"),
    [
        ("text/markdown", b"", "is empty"),
        ("application/pdf", b"not a pdf", "not a PDF file"),
        ("image/png", b"\x89PNG", "which Teacher cannot read"),
    ],
)
def test_unusable_sources(
    fake_anthropic, tmp_path: Path, media_type: str, content: bytes, message: str
) -> None:
    path = tmp_path / "file"
    path.write_bytes(content)
    fake = fake_anthropic()
    with pytest.raises(ReferenceInputError, match=message):
        ReferenceExtractor(fake).extract(SourceDocument(1, "s", "S", "paper", path, media_type))
    assert fake.calls == []


def test_model_must_be_given() -> None:
    with pytest.raises(ValueError, match="model"):
        ReferenceExtractor(model=" ")


# -- the stub ---------------------------------------------------------------------------------


def test_stub_reads_a_references_section(tmp_path: Path) -> None:
    path = tmp_path / "page.md"
    path.write_text(
        "# A page\n\nBody text with https://example.org/inline.\n\n## References\n\n"
        '1. Vaswani et al. "Attention Is All You Need". NeurIPS 2017. arXiv:1706.03762v5.\n'
        "- Krizhevsky. ImageNet. doi:10.1145/3065386.\n"
        "[3] A blog post, https://example.org/post.\n\n"
        "## Appendix\n\nNot a reference.\n",
        encoding="utf-8",
    )
    result = StubReferenceExtractor().extract(
        SourceDocument(1, "page", "A page", "web_page", path, "text/markdown")
    )
    assert result.found_in == "reference_list" and result.model == "stub"
    first, second, third = result.references
    assert (first.title, first.year, first.arxiv_id) == ("Attention Is All You Need", 2017, "1706.03762")
    assert second.doi == "10.1145/3065386"
    assert third.raw_text == "A blog post, https://example.org/post."
    assert third.url == "https://example.org/post"


def test_stub_without_a_references_section_finds_none(tmp_path: Path, make_pdf) -> None:
    md = tmp_path / "plain.md"
    md.write_text("# Plain\n\nNo citations here.\n", encoding="utf-8")
    pdf = tmp_path / "blank.pdf"
    pdf.write_bytes(make_pdf(1))
    stub = StubReferenceExtractor()
    for path, media_type in ((md, "text/markdown"), (pdf, "application/pdf")):
        result = stub.extract(SourceDocument(1, "s", "S", "paper", path, media_type))
        assert (result.found_in, result.references) == ("none", ())


# -- running an extraction ----------------------------------------------------------------------


def add(settings: Settings, tmp_path: Path, slug: str, text: str, **kw: Any) -> None:
    path = tmp_path / f"{slug}.md"
    path.write_text(text, encoding="utf-8")
    with session_scope(settings) as session:
        KnowledgeSourceManager(session, settings).add(
            path, slug=slug, kind="paper", title=kw.pop("title", f"The {slug} source"), **kw
        )


def state(settings: Settings, slug: str):
    with session_scope(settings) as session:
        source = KnowledgeSourceManager(session, settings).get_by_slug(slug)
        manager = SourceReferenceManager(session, settings)
        return manager.extraction(source), [
            (r.raw_text, r.matched_source_id, r.match_method) for r in manager.references(source)
        ]


def test_extract_references_stores_and_matches(
    settings: Settings, tmp_path: Path, fake_anthropic
) -> None:
    add(settings, tmp_path, "citing", "# Citing\n\nText.\n")
    add(settings, tmp_path, "attention", "# Attention\n", title="Attention Is All You Need",
        identifiers={"arxiv": "1706.03762"})  # fmt: skip
    fake = fake_anthropic(reply(fake_anthropic, [
        entry("Vaswani. Attention.", arxiv_id="1706.03762"), entry("Someone else. 2020."),
    ], input_tokens=500, output_tokens=60))  # fmt: skip
    outcome = extract_references(settings, "citing", ReferenceExtractor(fake, model=MODEL))
    assert (outcome.reference_count, outcome.matched, outcome.found_in) == (2, 1, "reference_list")
    assert outcome.summary_lines()[0] == (
        "citing: 2 reference(s) from its reference list; 1 in the library, 1 not."
    )
    row, refs = state(settings, "citing")
    assert (row.status, row.trigger, row.extractor, row.model) == (
        "succeeded", "cli", "claude-references-v1", MODEL
    )
    assert (row.input_tokens, row.output_tokens, row.reference_count) == (500, 60, 2)
    assert [(r[0], r[2]) for r in refs] == [("Vaswani. Attention.", "arxiv"), ("Someone else. 2020.", None)]


def test_a_failure_is_recorded_and_keeps_the_source_and_earlier_references(
    settings: Settings, tmp_path: Path, fake_anthropic
) -> None:
    add(settings, tmp_path, "citing", "# Citing\n")
    ok = fake_anthropic(reply(fake_anthropic, [entry("Kept reference.")]))
    extract_references(settings, "citing", ReferenceExtractor(ok))
    bad = fake_anthropic(reply(fake_anthropic, [entry(doi="nonsense")], input_tokens=11))
    with pytest.raises(ReferenceOutputError):
        extract_references(settings, "citing", ReferenceExtractor(bad), trigger="on_demand")
    row, refs = state(settings, "citing")
    assert row.status == "failed" and row.trigger == "on_demand" and row.run_number == 2
    assert row.error.startswith(ERROR_PREFIX) and "is not a valid DOI" in row.error
    assert (row.input_tokens, row.output_tokens) == (11, 54)
    assert row.reference_count == 1 and refs == [("Kept reference.", None, None)]
    with session_scope(settings) as session:  # the source is untouched
        assert KnowledgeSourceManager(session, settings).get_by_slug("citing").title


def test_an_unexpected_error_is_recorded_with_its_class(settings: Settings, tmp_path: Path) -> None:
    add(settings, tmp_path, "citing", "# Citing\n")

    class Broken(StubReferenceExtractor):
        def extract(self, document):  # type: ignore[no-untyped-def]
            raise RuntimeError("disk on fire")

    with pytest.raises(RuntimeError):
        extract_references(settings, "citing", Broken())
    row, _ = state(settings, "citing")
    assert row.error == f"{ERROR_PREFIX}RuntimeError: disk on fire"


def test_missing_credentials_are_recorded(
    settings: Settings, tmp_path: Path, forbid_real_messages_api: None
) -> None:
    add(settings, tmp_path, "citing", "# Citing\n")
    with pytest.raises(ReferencesUnavailableError):
        extract_references(settings, "citing", ReferenceExtractor())
    row, _ = state(settings, "citing")
    assert row.status == "failed" and "credentials" in row.error


def test_a_running_extraction_blocks_another(settings: Settings, tmp_path: Path) -> None:
    add(settings, tmp_path, "citing", "# Citing\n\n## References\n\n- One.\n")
    stub = StubReferenceExtractor()
    with session_scope(settings) as session:
        source = KnowledgeSourceManager(session, settings).get_by_slug("citing")
        claim = claim_extraction(session, settings, source, stub, trigger="on_demand")
    with pytest.raises(ExtractionBusyError):
        extract_references(settings, "citing", stub)
    outcome = run_claimed(settings, claim, stub)
    assert outcome.reference_count == 1
    with session_scope(settings) as session:
        source = KnowledgeSourceManager(session, settings).get_by_slug("citing")
        with pytest.raises(ValueError, match="trigger"):
            claim_extraction(session, settings, source, stub, trigger="whenever")
