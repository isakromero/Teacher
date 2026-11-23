"""teacher.search: query parsing, the all-words and prefix rules, every searched field, ranking,
grouping, snippets and their marks, gated and private text, study state, and failing loudly."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.orm import Session

from teacher.config import Settings
from teacher.db.models import Artifact, KnowledgeSource
from teacher.glossary import GlossaryEntry
from teacher.managers import (
    ArtifactBlockManager,
    ArtifactManager,
    BlockConceptManager,
    BlockResponseManager,
    ConceptManager,
    GlossaryManager,
    IntegrityCheckError,
    KnowledgeSourceManager,
    LinkSpec,
    SourceConceptManager,
    StudyHistoryManager,
)
from teacher.search import (
    MAX_LIMIT,
    MAX_QUERY_CHARS,
    MAX_QUERY_WORDS,
    QueryError,
    parse_query,
    search,
    segments,
    snippet,
)

MAX_ATTEMPTS = 2
GATE = {
    "question": "Which function turns scores into weights?",
    "options": ["softmax", "a residual connection"],
    "private": {"correct_index": 0, "explanation": "Only the zebra normalisation does that."},
}
INTRO = (
    "# Attention basics\n\n"
    "Scaled dot-product attention weighs the values by how well each key matches the query.\n\n"
    "## Multi-head attention\n\n"
    "Several heads run in parallel, each with its own projections."
)
LOCKED = "# Training the model\n\nLayer normalisation keeps the gradients of the unicorn stable."


def text_of(segs: list[dict[str, Any]]) -> str:
    """Segments as text, the marked parts in [brackets]."""
    return "".join(f"[{s['text']}]" if s["mark"] else s["text"] for s in segs)


@pytest.fixture
def source(session: Session, settings: Settings, tmp_path: Path) -> KnowledgeSource:
    path = tmp_path / "paper.pdf"
    path.write_bytes(b"%PDF-1.4\n% search test\n")
    return KnowledgeSourceManager(session, settings).add(
        path, slug="attention", kind="paper", title="Attention Is All You Need",
        creators=["Ashish Vaswani", "Noam Shazeer"], originated_on="2017-06-12",
        summary="A sequence model built from attention alone, without recurrence.",
    )


def make_page(
    session: Session, settings: Settings, source: KnowledgeSource, slug: str = "study-attention",
    *, title: str = "Study: the Transformer", blocks: list[tuple[str, dict[str, Any], bool]] | None = None,
    status: str = "ready", summary: str | None = "How a model can read a sentence all at once.",
    supersedes: Artifact | None = None,
) -> Artifact:
    artifacts = ArtifactManager(session, settings)
    page = artifacts.create(
        slug=slug, kind="study_page", title=title, generator="test", sources=[source],
        summary=summary, supersedes=supersedes,
    )
    manager = ArtifactBlockManager(session, settings)
    for type, content, gate in blocks if blocks is not None else [
        ("prose", {"markdown": INTRO}, False),
        ("multiple_choice", GATE, True),
        ("prose", {"markdown": LOCKED}, False),
    ]:
        manager.add(page, type=type, content=content, is_gate=gate)
    if status == "ready":
        artifacts.mark_ready(page)
    elif status == "failed":
        artifacts.mark_failed(page)
    return page


@pytest.fixture
def page(session: Session, settings: Settings, source: KnowledgeSource) -> Artifact:
    return make_page(session, settings, source)


def run(session: Session, settings: Settings, query: str, **kwargs: Any) -> dict[str, Any]:
    return search(session, settings, query, max_attempts=MAX_ATTEMPTS, **kwargs).record()


def result(data: dict[str, Any], kind: str, slug: str | None = None) -> dict[str, Any]:
    found = [r for r in data["results"] if r["kind"] == kind and (slug is None or r["slug"] == slug)]
    assert len(found) == 1, data["results"]
    return found[0]


# -- the query ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("query", "message"),
    [
        ("", "Type something"),
        ("   ", "Type something"),
        ("a", "at least 2"),
        ("!! ?", "no letters or digits"),
        ("x" * (MAX_QUERY_CHARS + 1), "too long"),
        (" ".join(f"w{i}" for i in range(MAX_QUERY_WORDS + 1)), "too many words"),
    ],
)
def test_unsearchable_queries_are_errors(query: str, message: str) -> None:
    with pytest.raises(QueryError, match=message):
        parse_query(query)


def test_query_words_are_folded_and_the_last_may_be_a_prefix() -> None:
    q = parse_query("  Bézier BEZIER curves")
    assert q.words == ("bezier", "curves") and q.prefix_last
    assert q.text == "Bézier BEZIER curves"
    assert not parse_query("attention ").prefix_last  # a finished word
    assert not parse_query("attention.").prefix_last
    assert parse_query("deepseek-r1").words == ("deepseek", "r1")
    assert QueryError.__mro__[1] is ValueError  # the API's 422


def test_limit_is_checked(session: Session, settings: Settings) -> None:
    for limit in (0, MAX_LIMIT + 1, True):
        with pytest.raises(QueryError, match="limit"):
            search(session, settings, "attention", max_attempts=MAX_ATTEMPTS, limit=limit)


def test_empty_library_has_no_results(session: Session, settings: Settings) -> None:
    data = run(session, settings, "attention")
    assert data["total"] == 0 and data["results"] == []
    assert data["query"] == "attention" and data["words"] == ["attention"]


# -- matching -------------------------------------------------------------------------------------


def test_every_word_must_match(session: Session, settings: Settings, page: Artifact) -> None:
    data = run(session, settings, "heads parallel")
    assert data["total"] == 1 and data["results"][0]["slug"] == page.slug
    assert run(session, settings, "heads submarine")["total"] == 0


def test_words_may_match_in_different_fields(session: Session, settings: Settings, page: Artifact) -> None:
    # "vaswani" is an author, "parallel" is in the text: together they find the page.
    data = run(session, settings, "vaswani parallel")
    assert [r["slug"] for r in data["results"]] == [page.slug]
    assert set(data["results"][0]["fields"]) >= {"authors", "body"}


def test_only_the_last_word_matches_as_a_prefix(session: Session, settings: Settings, page: Artifact) -> None:
    assert result(run(session, settings, "multi-head atten"), "study_page")
    assert run(session, settings, "atten heads")["total"] == 0  # "atten" is not the last word
    assert run(session, settings, "atten ")["total"] == 0  # finished: no prefix


def test_matching_ignores_case_and_accents(session: Session, settings: Settings, source: KnowledgeSource) -> None:
    make_page(session, settings, source, title="Les courbes de Bézier", blocks=[])
    assert run(session, settings, "BEZIER")["results"][0]["title"] == "Les courbes de Bézier"
    data = run(session, settings, "bézier courbes")
    assert text_of(data["results"][0]["title_segments"]) == "Les [courbes] de [Bézier]"


def test_one_result_per_item(session: Session, settings: Settings, page: Artifact) -> None:
    # "attention" is in the title of the source, the source summary, the page's headings and text.
    data = run(session, settings, "attention")
    assert sorted(r["kind"] for r in data["results"]) == ["source", "study_page"]
    assert data["total"] == 2


# -- each field -----------------------------------------------------------------------------------


def test_title_field(session: Session, settings: Settings, page: Artifact) -> None:
    r = result(run(session, settings, "transformer"), "study_page")
    assert r["fields"] == ["title"] and r["match"]["field"] == "title"
    assert text_of(r["title_segments"]) == "Study: the [Transformer]"
    # Only the title matched: the snippet is the start of the summary.
    assert text_of(r["snippet"]) == "How a model can read a sentence all at once."


def test_authors_field(session: Session, settings: Settings, page: Artifact) -> None:
    data = run(session, settings, "shazeer")
    for kind in ("source", "study_page"):  # a study page has its sources' authors
        r = result(data, kind)
        assert r["fields"] == ["authors"] and r["match"]["field"] == "authors"
        assert text_of(r["snippet"]) == "Noam [Shazeer]"
        assert r["authors"] == ["Ashish Vaswani", "Noam Shazeer"] and r["year"] == "2017"


def test_summary_field(session: Session, settings: Settings, page: Artifact) -> None:
    r = result(run(session, settings, "recurrence"), "source")
    assert r["fields"] == ["summary"]
    assert text_of(r["snippet"]) == "A sequence model built from attention alone, without [recurrence]."


def test_heading_field_links_to_its_block(session: Session, settings: Settings, page: Artifact) -> None:
    r = result(run(session, settings, "basics"), "study_page")
    assert r["fields"] == ["heading"]
    assert r["match"] == {
        "field": "heading", "section": "Attention basics", "block": "b01", "term": None,
        "concept": None, "locked": False, "href": f"/artifacts/{page.slug}#block-b01",
    }
    # A heading match quotes the start of its section.
    assert text_of(r["snippet"]).startswith("Scaled dot-product attention weighs")


def test_body_field_names_its_section(session: Session, settings: Settings, page: Artifact) -> None:
    r = result(run(session, settings, "projections"), "study_page")
    assert r["fields"] == ["body"]
    assert r["match"]["section"] == "Multi-head attention" and r["match"]["block"] == "b01"
    assert text_of(r["snippet"]) == "Several heads run in parallel, each with its own [projections]."


def test_question_text_and_options_are_searched(session: Session, settings: Settings, page: Artifact) -> None:
    r = result(run(session, settings, "residual"), "study_page")
    assert r["match"]["block"] == "b02" and r["match"]["section"] == "Multi-head attention"
    assert text_of(r["snippet"]) == "a [residual] connection"


def test_private_content_is_never_searched(session: Session, settings: Settings, page: Artifact) -> None:
    assert run(session, settings, "zebra")["total"] == 0  # only in the explanation


def test_locked_text_is_matched_but_not_quoted(
    session: Session, settings: Settings, page: Artifact
) -> None:
    r = result(run(session, settings, "unicorn"), "study_page")
    assert r["match"]["locked"] is True
    assert r["match"]["href"] is None and r["match"]["section"] is None and r["match"]["block"] is None
    assert r["snippet"] == []
    # Once the gate is cleared, the same text is quoted with its section and link.
    gate = ArtifactBlockManager(session, settings).get_by_key(page, "b02")
    BlockResponseManager(session, settings).record(gate, response={"choice": 0}, passed=True, grader="test")
    r = result(run(session, settings, "unicorn"), "study_page")
    assert r["match"]["locked"] is False and r["match"]["section"] == "Training the model"
    assert r["match"]["href"] == f"/artifacts/{page.slug}#block-b03"
    assert text_of(r["snippet"]) == "Layer normalisation keeps the gradients of the [unicorn] stable."


def test_glossary_field(session: Session, settings: Settings, page: Artifact) -> None:
    GlossaryManager(session, settings).replace(
        page,
        [GlossaryEntry(term="softmax", kind="term", definition="Turns any scores into probabilities.",
                       aliases=("soft max",))],
    )
    r = result(run(session, settings, "softmax"), "study_page")
    assert r["match"]["field"] == "glossary" and r["match"]["term"] == "softmax"
    assert "glossary" in r["fields"] and "body" in r["fields"]  # the option "softmax" too
    assert text_of(r["snippet"]) == "[softmax], soft max: Turns any scores into probabilities."
    # A word only in the definition matches as glossary text.
    r = result(run(session, settings, "probabilities"), "study_page")
    assert r["match"] == {**r["match"], "field": "body", "term": "softmax"}


def test_concept_fields(session: Session, settings: Settings, source: KnowledgeSource, page: Artifact) -> None:
    concepts = ConceptManager(session, settings)
    tagged = concepts.create("Self-attention", "Attending to the same sequence.", "user", aliases=["intra-attention"])
    linked = concepts.create("Positional encoding", "Where each token is.", "user")
    block = ArtifactBlockManager(session, settings).get_by_key(page, "b01")
    BlockConceptManager(session, settings).add(block, tagged, "main", "user")
    SourceConceptManager(session, settings).replace(
        source, [LinkSpec(concept=linked, role="introduces", resolution="created", candidate_name="positional encoding")]
    )
    r = result(run(session, settings, "intra"), "study_page")  # an alias, by prefix
    assert r["fields"] == ["concept"] and r["match"]["concept"] == "Self-attention"
    data = run(session, settings, "positional")
    assert [x["kind"] for x in data["results"]] == ["source"]  # the source's own concepts
    assert result(data, "source")["match"]["concept"] == "Positional encoding"


# -- ranking --------------------------------------------------------------------------------------


def test_title_and_concept_matches_rank_above_body_matches(
    session: Session, settings: Settings, source: KnowledgeSource
) -> None:
    body = make_page(session, settings, source, "in-body", title="Study: one",
                     blocks=[("prose", {"markdown": "Gradient descent, step by step."}, False)])
    titled = make_page(session, settings, source, "in-title", title="Gradient descent", blocks=[])
    concept_page = make_page(session, settings, source, "in-concept", title="Study: two",
                             blocks=[("prose", {"markdown": "Steps."}, False)])
    concept = ConceptManager(session, settings).create("Gradient descent", "Downhill.", "user")
    block = ArtifactBlockManager(session, settings).get_by_key(concept_page, "b01")
    BlockConceptManager(session, settings).add(block, concept, "main", "user")
    slugs = [r["slug"] for r in run(session, settings, "gradient descent")["results"]]
    assert slugs == [titled.slug, concept_page.slug, body.slug]


def test_older_versions_and_failed_pages_rank_lower(
    session: Session, settings: Settings, source: KnowledgeSource
) -> None:
    blocks = [("prose", {"markdown": "Backpropagation explained."}, False)]
    old = make_page(session, settings, source, "old", title="Backprop", blocks=blocks)
    new = make_page(session, settings, source, "new", title="Backprop", blocks=blocks, supersedes=old)
    failed = make_page(session, settings, source, "failed", title="Backprop", blocks=[], status="failed")
    data = run(session, settings, "backprop")
    assert [r["slug"] for r in data["results"]] == [new.slug, old.slug, failed.slug]
    by_slug = {r["slug"]: r for r in data["results"]}
    assert by_slug["old"]["superseded"] and not by_slug["new"]["superseded"]
    assert by_slug["failed"]["status"] == "failed" and by_slug["failed"]["study"]["state"] == "not_ready"


def test_limit_and_total(session: Session, settings: Settings, source: KnowledgeSource) -> None:
    for i in range(4):
        make_page(session, settings, source, f"p{i}", title=f"Optimiser {i}", blocks=[])
    data = run(session, settings, "optimiser", limit=2)
    assert data["total"] == 4 and len(data["results"]) == 2 and data["limit"] == 2


# -- snippets -------------------------------------------------------------------------------------


def test_segments_keep_text_as_text() -> None:
    q = parse_query("script")
    text = 'Never <b>bold</b>: <script>alert("x")</script> & more'
    segs = segments(text, q.marks(text))
    assert "".join(s["text"] for s in segs) == text  # nothing escaped or dropped: the page draws text
    assert [s["text"] for s in segs if s["mark"]] == ["script", "script"]
    assert all(set(s) == {"text", "mark"} for s in segs)


def test_snippet_windows_long_text_at_word_boundaries() -> None:
    words = [f"word{i}" for i in range(80)]
    words[50] = "needle"
    text = " ".join(words)
    segs = snippet(text, parse_query("needle"), chars=60)
    shown = text_of(segs)
    assert shown.startswith("… ") and shown.endswith(" …")
    assert "[needle]" in shown
    inner = shown.removeprefix("… ").removesuffix(" …")
    assert inner in text.replace("needle", "[needle]")  # whole words only
    assert len(inner) <= 62
    # Short text is quoted whole; text without a match starts at the beginning.
    assert text_of(snippet("A needle here.", parse_query("needle"))) == "A [needle] here."
    assert text_of(snippet(text, parse_query("absent"), chars=40)).startswith("word0 word1")


def test_prefix_marks_whole_words() -> None:
    q = parse_query("atten")
    text = "Attention and attentive readers"
    assert text_of(segments(text, q.marks(text))) == "[Attention] and [attentive] readers"


# -- study state ----------------------------------------------------------------------------------


def test_study_state(session: Session, settings: Settings, source: KnowledgeSource, page: Artifact) -> None:
    def study(kind: str) -> dict[str, Any]:
        return result(run(session, settings, "attention"), kind)["study"]

    assert study("study_page") == {
        "state": "not_opened", "active_seconds": 0.0, "last_studied_at": None,
        "gates_total": 1, "gates_cleared": 0,
    }
    assert study("source") == {
        "state": "not_opened", "active_seconds": 0.0, "last_studied_at": None, "study_pages": 1,
    }
    history = StudyHistoryManager(session, settings)
    history.log("study_page_opened", artifact=page)
    history.log("source_opened", source=source)
    assert study("study_page")["state"] == "in_progress"
    assert study("study_page")["last_studied_at"] is not None
    assert study("source")["state"] == "opened"
    gate = ArtifactBlockManager(session, settings).get_by_key(page, "b02")
    BlockResponseManager(session, settings).record(gate, response={"choice": 0}, passed=True, grader="test")
    assert study("study_page")["state"] == "complete"
    assert study("study_page")["gates_cleared"] == 1


# -- failing loudly -------------------------------------------------------------------------------


def test_an_unreadable_block_file_fails_the_search(
    session: Session, settings: Settings, page: Artifact
) -> None:
    (ArtifactManager(session, settings).dir(page) / "blocks" / "b01.json").unlink()
    with pytest.raises(IntegrityCheckError, match="b01"):
        run(session, settings, "attention")


def test_a_changed_block_file_fails_the_search(
    session: Session, settings: Settings, page: Artifact
) -> None:
    # Text that does not match its stored hash is not searched as it is.
    (ArtifactManager(session, settings).dir(page) / "blocks" / "b03.json").write_text('{"markdown": "y"}')
    with pytest.raises(IntegrityCheckError, match="b03"):
        run(session, settings, "transformer")


def test_a_predictions_prompt_and_options_are_searched_but_not_its_resolution(
    session: Session, settings: Settings, source: KnowledgeSource
) -> None:
    prediction = {
        "prompt": "What happens to the gradient of the platypus?",
        "options": ["it vanishes", "it explodes"],
        "private": {"correct_index": 0, "resolution": "The wombat resolution."},
    }
    make_page(session, settings, source, blocks=[
        ("prose", {"markdown": INTRO}, False), ("prediction", prediction, True),
    ])
    assert "platypus" in str(run(session, settings, "platypus"))
    assert "explodes" in str(run(session, settings, "explodes"))
    assert run(session, settings, "wombat")["results"] == []
