"""ClaudeGenerator with a fake streaming client (no network): the pipeline through
`run_generation`, segments, stage files, tokens, request shape, the glossary stage, and every
loud failure."""

from __future__ import annotations

import base64
import copy
import json
import random
from collections.abc import Callable
from pathlib import Path
from typing import Any

import anthropic
import pytest
from pydantic import ValidationError
from sqlalchemy import select
from fastapi.testclient import TestClient

from teacher.blocks import validate_block_content
from teacher.claude_generator import (
    ANALYSE_MAX_TOKENS,
    DEFAULT_TIMEOUT,
    DEPTHS,
    EFFORT,
    GLOSSARY_FORMAT,
    GLOSSARY_MAX_TOKENS,
    OUTLINE_FORMAT,
    PAGE_FORMAT,
    SYSTEM_PROMPT,
    WRITE_INSTRUCTIONS,
    WRITE_MAX_TOKENS,
    ClaudeGenerator,
    GenerationError,
    GlossaryBuilder,
    Outline,
    StudyPage,
    WireGlossary,
    check_page,
    page_violations,
    shuffle_options,
    to_draft,
)
from teacher.config import Settings
from teacher.db.engine import session_scope
from teacher.db.models import ApiCall, GenerationRun
from teacher.page_revision import REVIEW_FORMAT, REVIEW_MAX_TOKENS
from teacher.generation import run_generation
from teacher.glossary import GlossaryEntry
from teacher.grading import MultipleChoiceGrader, WordCountBlurbGrader
from teacher.judge import SYSTEM_PROMPT as JUDGE_SYSTEM_PROMPT
from teacher.judge import LLMJudgeGrader
from teacher.locators import PdfLocator
from teacher.managers import (
    ArtifactBlockManager,
    ArtifactManager,
    GenerationRunManager,
    GlossaryManager,
    KnowledgeSourceManager,
    SourceSegmentManager,
)
from teacher.web.app import create_app

# A one-page blank PDF: the figures stage opens the PDF itself (and finds no figure in it).
PDF_BYTES = (
    b"%PDF-1.4\n% a tiny stand-in for a paper\n1 0 obj <</Type /Catalog /Pages 2 0 R>> endobj\n"
    b"2 0 obj <</Type /Pages /Kids [3 0 R] /Count 1>> endobj\n"
    b"3 0 obj <</Type /Page /Parent 2 0 R /MediaBox [0 0 612 792]>> endobj\n"
    b"trailer <</Root 1 0 R>>\n%%EOF\n"
)
TEXT = "# A note\n\nGradient descent follows the negative gradient.\n"

OUTLINE: dict[str, Any] = {
    "summary": "The paper proposes a method and shows that it works.",
    "main_claims": ["The method beats the baseline."],
    "background": ["linear algebra"],
    "sections": [
        {
            "label": "problem",
            "title": "The problem",
            "start_page": 1,
            "end_page": 2,
            "key_points": ["Why the problem is hard."],
        },
        {
            "label": "method",
            "title": "The method",
            "start_page": 3,
            "end_page": 5,
            "key_points": ["How the method works."],
        },
    ],
}
TEXT_OUTLINE: dict[str, Any] = {
    **OUTLINE,
    "sections": [
        {**section, "start_page": None, "end_page": None} for section in OUTLINE["sections"]
    ],
}


def prose(markdown: str, section: str | None = None, role: str = "explanation") -> dict[str, Any]:
    return {"type": "prose", "section": section or "", "role": role, "markdown": markdown}


def critique(markdown: str = "## A critical look\n\nThe paper never tests small inputs.",
             section: str | None = None) -> dict[str, Any]:
    return prose(markdown, section, "critique")


def mcq(section: str | None = "problem", options: list[str] | None = None,
        correct_index: int = 1, draws_on: list[str] | None = None, new_scenario: bool = False,
        **changes: Any) -> dict[str, Any]:
    options = options if options is not None else ["It is small", "It is large", "No"]
    block = {
        "type": "multiple_choice",
        "section": section or "",
        "draws_on": draws_on if draws_on is not None else ([section] if section else []),
        "new_scenario": new_scenario,
        "question": "Why is the problem hard?",
        "options": options,
        "correct_index": correct_index,
        "option_explanations": [
            "Right." if index == correct_index else "Not so." for index in range(len(options))
        ],
        "explanation": "Because it is large; 'small' is the tempting misreading.",
    }
    block.update(changes)
    return block


MODEL_ANSWER = (
    "It works in two steps: the first prepares the data, the second fits the model, and the "
    "order matters here."
)


def blurb(section: str | None = "method", rubric: list[dict] | None = None,
          draws_on: list[str] | None = None, new_scenario: bool = False,
          **changes: Any) -> dict[str, Any]:
    block = {
        "type": "blurb",
        "section": section or "",
        "draws_on": draws_on if draws_on is not None else ([section] if section else []),
        "new_scenario": new_scenario,
        "prompt": "Explain how the method works.",
        "min_words": 20,
        "model_answer": MODEL_ANSWER,
        "rubric": rubric
        if rubric is not None
        else [
            {"criterion": "Names both steps", "required": True},
            {"criterion": "Says why the order matters", "required": False},
        ],
    }
    block.update(changes)
    return block


def prediction(section: str | None = "method", **changes: Any) -> dict[str, Any]:
    block = {
        "type": "prediction",
        "section": section or "",
        "prompt": "What do you expect the two steps to do?",
        "options": ["Speed it up", "Slow it down", "Change nothing"],
        "correct_index": 0,
        "resolution": "They speed it up, as the next part explains.",
    }
    block.update(changes)
    return block


def chart(section: str | None = "method", kind: str = "bar",
          series: list[dict] | None = None) -> dict[str, Any]:
    return {
        "type": "chart",
        "section": section or "",
        "kind": kind,
        "title": "Accuracy by method",
        "x_label": "Method",
        "y_label": "Accuracy (%)",
        "series": series
        if series is not None
        else [
            {"name": "Test", "points": [{"x": "Baseline", "y": 71.5}, {"x": "Ours", "y": 78}]},
        ],
        "caption": "The method beats the baseline by **6.5 points**.",
        "source": "Table 2",
    }


WIDGET = (
    '<label>Steps <input id="steps" type="range" min="1" max="5" value="2"></label>'
    '<output id="out">2</output>'
    "<style>output { color: var(--accent); }</style>"
    "<script>steps.oninput = () => { out.textContent = steps.value; };</script>"
)


def interactable(section: str | None = "method", html: str = WIDGET) -> dict[str, Any]:
    return {
        "type": "interactable",
        "section": section or "",
        "title": "Step through the method",
        "instructions": "Move the slider; the values are made up.",
        "html": html,
    }


def term(term: str, kind: str = "term", expansion: str | None = None,
         definition: str = "A plain definition.", aliases: list[str] | None = None) -> dict:
    return {"term": term, "kind": kind, "expansion": expansion, "definition": definition,
            "aliases": aliases or []}


SGD = term("SGD", "acronym", "stochastic gradient descent",
           "An optimisation method that follows the gradient of a random sample of the data.")
GD = term("gradient descent", definition="Repeatedly stepping against the gradient.")
GLOSSARY: dict[str, Any] = {"terms": [SGD, GD]}


def page(*blocks: dict[str, Any]) -> dict[str, Any]:
    return {"blocks": list(blocks)}


# A page that meets every page rule: an opening explanation, a question on the problem, a
# prediction opening the method, a mixed blurb, the critique, and the closing set (one of its
# questions a new scenario).
PAGE: dict[str, Any] = page(
    prose("# The paper\n\nWhat it is about."),
    prose("The problem is hard because $n$ is large.", "problem"),
    mcq(),
    prediction(),
    prose("The method runs stochastic gradient descent (SGD) in two steps.", "method"),
    blurb(draws_on=["problem", "method"]),
    critique(),
    mcq(None, draws_on=["problem", "method"], new_scenario=True,
        question="Suppose $n$ doubled: what would the method do?",
        options=["Take longer", "Fail at once", "Run as before"], correct_index=0),
    blurb(None, draws_on=["method"], prompt="Explain why the order of the steps matters.",
          min_words=15),
)
SECTIONS = ["problem", "method"]
# A page that teaches one section only: it needs no mixed exercise and no closing set over two.
ONE_SECTION_PAGE: dict[str, Any] = page(
    prose("# The paper\n\nWhat it is about."),
    prediction("problem"),
    prose("The problem is hard because $n$ is large.", "problem"),
    mcq(),
    critique(),
    mcq(None, draws_on=["problem"], new_scenario=True),
    blurb(None, draws_on=["problem"]),
)


# -- fixtures -------------------------------------------------------------------------------


@pytest.fixture
def make_source(settings: Settings, tmp_path: Path) -> Callable[..., str]:
    """`make_source(slug, content, filename)` stores a knowledge source; returns its slug."""

    def make(
        slug: str = "paper", content: bytes = PDF_BYTES, filename: str = "paper.pdf",
        media_type: str | None = None,
    ) -> str:
        folder = tmp_path / "uploads" / slug
        folder.mkdir(parents=True)
        path = folder / filename
        path.write_bytes(content)
        with session_scope(settings) as session:
            KnowledgeSourceManager(session, settings).add(
                path, slug=slug, kind="paper", title=f"Title of {slug}", media_type=media_type
            )
        return slug

    return make


@pytest.fixture
def make_artifact(settings: Settings) -> Callable[..., str]:
    """`make_artifact(slug, sources=[...], params=...)`: a `generating` artifact."""

    def make(slug: str = "page", sources: tuple[str, ...] = ("paper",),
             params: dict | None = None) -> str:
        with session_scope(settings) as session:
            manager = KnowledgeSourceManager(session, settings)
            ArtifactManager(session, settings).create(
                slug=slug,
                kind="study_page",
                title="Study",
                generator="claude-study-v1",
                params=params,
                sources=[(manager.get_by_slug(s), "primary") for s in sources],
            )
        return slug

    return make


CHECK_TOKENS = (20, 3)  # input and output tokens of every check reply below


def check_replies(fake_anthropic, page_: dict, *, review: bool = True) -> list[Any]:
    """The replies that find nothing wrong with the wire page `page_`, in the order the checks
    ask for them: a review with no issues (if `review`), then per run of consecutive exercises
    an open-book reply with the right answers followed by one judge reply per blurb of the run
    (every criterion met), then a closed-book reply that guesses wrong on every question."""
    def reply(body: dict) -> Any:
        return fake_anthropic.reply(text=json.dumps(body), model="claude-sonnet-5-5",
                                    input_tokens=CHECK_TOKENS[0], output_tokens=CHECK_TOKENS[1])

    blocks = page_["blocks"]
    out: list[Any] = [reply({"issues": []})] if review else []
    exercises = [i for i, b in enumerate(blocks) if b["type"] in ("multiple_choice", "blurb")]
    groups: list[list[int]] = []
    for index in exercises:
        if groups and groups[-1][-1] == index - 1:
            groups[-1].append(index)
        else:
            groups.append([index])
    for group in groups:
        answers = []
        for index in group:
            block = blocks[index]
            if block["type"] == "multiple_choice":
                answers.append({"block": index + 1, "reasoning": "The page says so.",
                                "choice": block["correct_index"], "answer": None})
            else:
                answers.append({"block": index + 1, "reasoning": "The page explains it.",
                                "choice": None,
                                "answer": " ".join(["word"] * (block["min_words"] + 5))})
        out.append(reply({"answers": answers}))
        for index in group:
            block = blocks[index]
            if block["type"] == "blurb" and block["rubric"]:
                out.append(fake_anthropic.reply(
                    [(True, "Met.")] * len(block["rubric"]),
                    input_tokens=CHECK_TOKENS[0], output_tokens=CHECK_TOKENS[1],
                ))
    questions = [i for i, b in enumerate(blocks) if b["type"] == "multiple_choice"]
    if questions:
        out.append(reply({"answers": [
            {"block": i + 1, "choice": (blocks[i]["correct_index"] + 1) % len(blocks[i]["options"]),
             "basis": "guess"}
            for i in questions
        ]}))
    return out


AUTO = object()  # `replies(checks=AUTO)`: the clean checks of a valid page


@pytest.fixture
def replies(fake_anthropic) -> Callable[..., Any]:
    """`replies(outline, page, glossary, checks=AUTO)`: a fake client answering analyse, write,
    the page checks and glossary (a dict becomes a reply with that JSON; None queues nothing;
    anything else is queued). With `checks=AUTO` a page that is a valid `StudyPage` gets the
    replies of clean checks (`check_replies`) right after it; a list is queued as it is."""

    def make(outline: Any = OUTLINE, page_: Any = PAGE, glossary: Any = GLOSSARY,
             checks: Any = AUTO) -> Any:
        if checks is AUTO:
            checks = []
            if isinstance(page_, dict):
                try:
                    StudyPage.model_validate(page_)
                except ValueError:
                    pass
                else:
                    checks = check_replies(fake_anthropic, page_)
        outcomes = []
        for index, body in enumerate((outline, page_, glossary)):
            if index == 2:
                outcomes += checks
            if body is None:
                continue
            if isinstance(body, dict):
                outcomes.append(
                    fake_anthropic.reply(
                        text=json.dumps(body),
                        model="claude-sonnet-5-5",
                        input_tokens=50 + index,
                        output_tokens=400 + index,
                        cache_creation_input_tokens=0 if index else 3000,
                        cache_read_input_tokens=3000 if index else 0,
                    )
                )
            else:
                outcomes.append(body)
        return fake_anthropic(*outcomes)

    return make


# The stages of a generation whose draft the checks find nothing wrong with.
CLEAN_STAGES = [
    "preflight", "analyse", "figures", "reader_profile", "write", "review", "question_check",
    "verify", "concepts", "block_concepts", "glossary",
]


def writer_calls(fake: Any) -> list[dict]:
    """The calls on the artifact's model: all but the judge's (which grades the open-book
    answers to blurbs on the judge model)."""
    return [call for call in fake.calls if call["system"] != JUDGE_SYSTEM_PROMPT]


def judge_calls(fake: Any) -> list[dict]:
    return [call for call in fake.calls if call["system"] == JUDGE_SYSTEM_PROMPT]


def document_calls(fake: Any) -> dict[str, dict]:
    """The calls that carry the document, by what their instructions start with."""
    found: dict[str, dict] = {}
    for call in writer_calls(fake):
        content = call["messages"][0]["content"]
        if isinstance(content, list) and content[0]["type"] == "document":
            found[content[1]["text"].split(":", 1)[0]] = call
    return found


def runs(settings: Settings, slug: str) -> list[dict[str, Any]]:
    with session_scope(settings) as session:
        artifact = ArtifactManager(session, settings).get_by_slug(slug)
        return [
            {
                "stage": r.stage,
                "status": r.status,
                "model": r.model,
                "error": r.error,
                "input": r.input_tokens,
                "output": r.output_tokens,
                "file": r.output_file,
            }
            for r in GenerationRunManager(session, settings).list(artifact)
        ]


def status(settings: Settings, slug: str) -> str:
    with session_scope(settings) as session:
        return ArtifactManager(session, settings).get_by_slug(slug).status


def segments_of(settings: Settings, source: str = "paper") -> list[tuple]:
    with session_scope(settings) as session:
        knowledge = KnowledgeSourceManager(session, settings).get_by_slug(source)
        return [
            (s.id, s.kind, s.label, s.title, s.position, s.locator)
            for s in SourceSegmentManager(session, settings).list(knowledge)
        ]


def full_page_regions(first: int, last: int) -> dict[str, Any]:
    return {
        "type": "pdf_regions",
        "regions": [
            {"page": p, "x0": 0.0, "y0": 0.0, "x1": 1.0, "y1": 1.0} for p in range(first, last + 1)
        ],
    }


class FakeConceptRun:
    def record(self, trigger: str) -> dict[str, Any]:
        return {"fake": True, "trigger": trigger}


class FakeConcepts:
    """A `ConceptStage` stand-in (the real one is tested in test_concept_extraction.py): records
    each run's source and document, reports 11/7 tokens, and raises `error` if set."""

    name = "fake-concepts"

    def __init__(self, model: str = "claude-sonnet-5-5", error: Exception | None = None,
                 check_error: Exception | None = None) -> None:
        self.model = model
        self.error = error
        self.check_error = check_error
        self.checks = 0
        self.calls: list[dict[str, Any]] = []

    def check_available(self) -> None:
        self.checks += 1
        if self.check_error is not None:
            raise self.check_error

    def run(self, settings: Settings, source: Any, *, document: Any = None,
            stage: Any = None) -> FakeConceptRun:
        self.calls.append({"source": source, "document": document})
        if stage is not None:
            stage.set_tokens(11, 7)
        if self.error is not None:
            raise self.error
        return FakeConceptRun()


class FakeTaggingRun:
    plan = None  # nothing to store: the page stays untagged

    def record(self, trigger: str) -> dict[str, Any]:
        return {"fake": True, "trigger": trigger}


class FakeTagger:
    """A `BlockTaggingStage` stand-in (the real one is tested in test_block_tagging.py): records
    each run's blocks and tags nothing."""

    name = "fake-tagger"

    def __init__(self, model: str = "claude-sonnet-5-5") -> None:
        self.model = model
        self.checks = 0
        self.calls: list[list[tuple[str, dict]]] = []

    def check_available(self) -> None:
        self.checks += 1

    def run(self, settings: Settings, sources: Any, blocks: Any, *, stage: Any = None) -> Any:
        self.calls.append(list(blocks))
        return FakeTaggingRun()


def generate(settings: Settings, slug: str, client: Any, **kwargs: Any) -> str | None:
    kwargs.setdefault("concept_extractor", FakeConcepts())
    kwargs.setdefault("block_tagger", FakeTagger())
    kwargs.setdefault("rng", random.Random(7))
    return run_generation(settings, slug, ClaudeGenerator(client, **kwargs))


def page_blocks(page_: dict, seed: int = 0) -> list[tuple[str, dict]]:
    """The stored (type, content) of each block of a wire page (options shuffled by `seed`)."""
    rng = random.Random(seed)
    drafts = [to_draft(block, {}, rng) for block in StudyPage.model_validate(page_).blocks]
    return [(d.type, d.content) for d in drafts]


def by_option(content: dict) -> dict[str, tuple[str | None, bool]]:
    """A stored question or prediction as option -> (its explanation, whether it is correct),
    which shuffling must not change."""
    private = content["private"]
    explanations = private.get("option_explanations") or [None] * len(content["options"])
    return {
        option: (explanation, index == private["correct_index"])
        for index, (option, explanation) in enumerate(
            zip(content["options"], explanations, strict=True)
        )
    }


def wire_by_option(block: dict) -> dict[str, tuple[str | None, bool]]:
    explanations = block.get("option_explanations") or [None] * len(block["options"])
    return {
        option: (explanation, index == block["correct_index"])
        for index, (option, explanation) in enumerate(
            zip(block["options"], explanations, strict=True)
        )
    }


# -- the happy path -------------------------------------------------------------------------


def test_pdf_happy_path(settings: Settings, make_source, make_artifact, replies) -> None:
    make_source()
    slug = make_artifact()
    fake = replies()
    assert generate(settings, slug, fake) == "ready"

    rows = runs(settings, slug)
    assert [(r["stage"], r["status"], r["model"]) for r in rows] == [
        (stage, "succeeded", "claude-sonnet-5-5") for stage in CLEAN_STAGES
    ]
    # input = uncached + cache creation + cache read; output as reported. The question check
    # adds up its six calls: three open-book calls, two judge calls, one closed-book call.
    check = (6 * CHECK_TOKENS[0], 6 * CHECK_TOKENS[1])
    assert [(r["input"], r["output"]) for r in rows] == [
        (None, None), (3050, 400), (None, None), (None, None), (3051, 401), CHECK_TOKENS, check,
        (None, None), (11, 7), (None, None), (3052, 402),
    ]
    assert [r["file"] for r in rows] == [
        None,
        f"artifacts/{slug}/generation/analyse.json",
        f"artifacts/{slug}/generation/figures.json",
        f"artifacts/{slug}/generation/reader_profile.json",
        f"artifacts/{slug}/generation/write.json",
        f"artifacts/{slug}/generation/review.json",
        f"artifacts/{slug}/generation/question_check.json",
        None,
        f"artifacts/{slug}/generation/concepts.json",
        f"artifacts/{slug}/generation/block_concepts.json",
        f"artifacts/{slug}/generation/glossary.json",
    ]
    figures = json.loads((settings.data_root / rows[2]["file"]).read_text(encoding="utf-8"))
    assert figures["outcome"] == "none_detected" and figures["figures"] == []
    concepts = json.loads((settings.data_root / rows[8]["file"]).read_text(encoding="utf-8"))
    assert concepts == {"fake": True, "trigger": "generation"}
    tags = json.loads((settings.data_root / rows[9]["file"]).read_text(encoding="utf-8"))
    assert tags == {"fake": True, "trigger": "generation"}
    # A clean draft: nothing found, so no revise stage, and the page written is the page stored.
    assert not (settings.artifacts_dir / slug / "generation" / "revise_1.json").exists()
    rows = [rows[1], rows[4], rows[7], rows[10]]  # the page stages, as below

    analyse = json.loads((settings.data_root / rows[0]["file"]).read_text(encoding="utf-8"))
    assert analyse["usage"] == {
        "input_tokens": 50,
        "cache_creation_input_tokens": 3000,
        "cache_read_input_tokens": 0,
        "output_tokens": 400,
    }
    assert analyse["output"] == OUTLINE
    assert analyse["model"] == "claude-sonnet-5-5" and analyse["stop_reason"] == "end_turn"
    assert analyse["source"]["slug"] == "paper" and analyse["ignored_sources"] == []
    write = json.loads((settings.data_root / rows[1]["file"]).read_text(encoding="utf-8"))
    assert write["usage"]["cache_read_input_tokens"] == 3000
    assert write["output"] == PAGE and write["depth"] == "standard"
    glossary = json.loads((settings.data_root / rows[3]["file"]).read_text(encoding="utf-8"))
    assert glossary["output"] == GLOSSARY and glossary["trigger"] == "generation"
    assert glossary["proposed"] == 2 and glossary["dropped"] == [] and glossary["merged"] == []
    assert glossary["usage"]["cache_read_input_tokens"] == 3000
    assert glossary["source"]["slug"] == "paper"

    # segments: one per outline section, full-page regions over the page range
    segs = segments_of(settings)
    assert [(kind, label, title, pos) for _, kind, label, title, pos, _ in segs] == [
        ("section", "problem", "The problem", 1),
        ("section", "method", "The method", 2),
    ]
    assert segs[0][5] == full_page_regions(1, 2) and segs[1][5] == full_page_regions(3, 5)
    PdfLocator.model_validate(segs[1][5])
    ids = {label: seg_id for seg_id, _, label, *_ in segs}
    assert analyse["segments"] == [
        {"label": "problem", "id": ids["problem"], "created": True},
        {"label": "method", "id": ids["method"], "created": True},
    ]

    with session_scope(settings) as session:
        artifact = ArtifactManager(session, settings).get_by_slug(slug)
        assert artifact.status == "ready"
        blocks = ArtifactBlockManager(session, settings)
        stored = blocks.list(artifact)
        assert [(b.type, b.is_gate) for b in stored] == [
            ("prose", False), ("prose", False), ("multiple_choice", True),
            ("prediction", True), ("prose", False), ("blurb", True), ("prose", False),
            ("multiple_choice", True), ("blurb", True),
        ]
        linked = [[s.label for s in blocks.segments(b)] for b in stored]
        # An exercise is linked to its own section and to every section it draws on.
        assert linked == [
            [], ["problem"], ["problem"], ["method"], ["method"], ["problem", "method"], [],
            ["problem", "method"], ["method"],
        ]
        contents = [blocks.read_content(b) for b in stored]
        assert GlossaryManager(session, settings).entries(artifact) == [
            GlossaryEntry("SGD", "acronym", SGD["definition"], "stochastic gradient descent"),
            GlossaryEntry("gradient descent", "term", GD["definition"]),
        ]
        assert artifact.glossary_built_at is not None
    for block, content in zip(stored, contents, strict=True):
        assert validate_block_content(block.type, content) == content
    assert contents[0] == {"markdown": "# The paper\n\nWhat it is about.", "role": "explanation"}
    assert contents[6]["role"] == "critique"
    # Stored exactly as the seeded shuffle in `to_draft` orders them (`page_blocks` uses the
    # same seed), each option keeping its explanation and whether it is correct.
    assert contents == [content for _type, content in page_blocks(PAGE, seed=7)]
    for index in (2, 3, 7):
        assert by_option(contents[index]) == wire_by_option(PAGE["blocks"][index])
        assert {k: v for k, v in contents[index].items() if k not in ("options", "private")} == {
            k: v for k, v in PAGE["blocks"][index].items()
            if k in ("question", "prompt")
        }
    assert contents[2]["private"]["explanation"] == PAGE["blocks"][2]["explanation"]
    assert contents[3]["private"]["resolution"] == PAGE["blocks"][3]["resolution"]
    assert contents[5] == {
        "prompt": "Explain how the method works.",
        "min_words": 20,
        "private": {
            "model_answer": MODEL_ANSWER,
            "rubric": [
                {"criterion": "Names both steps", "required": True},
                {"criterion": "Says why the order matters", "required": False},
            ],
        },
    }
    assert all(stream.closed for stream in fake.streams)


def test_chart_and_interactable_happy_path(
    settings: Settings, make_source, make_artifact, replies
) -> None:
    make_source()
    slug = make_artifact()
    blocks = [*PAGE["blocks"][:5], chart(), interactable(), *PAGE["blocks"][5:]]
    fake = replies(OUTLINE, page(*blocks))
    assert generate(settings, slug, fake) == "ready"
    with session_scope(settings) as session:
        artifact = ArtifactManager(session, settings).get_by_slug(slug)
        manager = ArtifactBlockManager(session, settings)
        stored = manager.list(artifact)
        assert [b.type for b in stored][4:8] == ["prose", "chart", "interactable", "blurb"]
        assert [b.is_gate for b in stored][5:7] == [False, False]
        linked = [[s.label for s in manager.segments(b)] for b in stored]
        assert linked[5:7] == [["method"], ["method"]]
        contents = [manager.read_content(b) for b in stored]
    assert contents[5] == {
        "kind": "bar",
        "title": "Accuracy by method",
        "x_label": "Method",
        "y_label": "Accuracy (%)",
        "series": [
            {"name": "Test", "points": [{"x": "Baseline", "y": 71.5}, {"x": "Ours", "y": 78.0}]},
        ],
        "caption": "The method beats the baseline by **6.5 points**.",
        "source": "Table 2",
    }
    assert contents[6] == {
        "title": "Step through the method",
        "instructions": "Move the slider; the values are made up.",
        "html": WIDGET,
    }
    # The glossary step sees the chart's and the widget's text, but not the widget's source.
    glossary_text = document_calls(fake)["Step 3 of 3"]["messages"][0]["content"][1]["text"]
    assert '<block number="6" type="chart">\n<title>\nAccuracy by method\n</title>' in (
        glossary_text
    )
    assert "<category>\nBaseline\n</category>" in glossary_text
    assert "<series_name>\nTest\n</series_name>" in glossary_text
    assert "<source>\nTable 2\n</source>" in glossary_text
    assert '<block number="7" type="interactable">\n<title>\nStep through' in glossary_text
    assert "<instructions>\nMove the slider" in glossary_text
    assert "oninput" not in glossary_text and "<kind>" not in glossary_text


def test_line_chart_numbers_are_stored_as_floats(
    settings: Settings, make_source, make_artifact, replies
) -> None:
    make_source()
    slug = make_artifact()
    points = [{"x": "1", "y": 0.5}, {"x": "2.0", "y": 3}]  # x is text on the wire
    line = chart(kind="line", series=[{"name": "Loss", "points": points}])
    blocks = [*PAGE["blocks"][:5], line, *PAGE["blocks"][5:]]
    assert generate(settings, slug, replies(OUTLINE, page(*blocks))) == "ready"
    with session_scope(settings) as session:
        artifact = ArtifactManager(session, settings).get_by_slug(slug)
        manager = ArtifactBlockManager(session, settings)
        content = manager.read_content(manager.list(artifact)[5])
    assert content["series"][0]["points"] == [{"x": 1.0, "y": 0.5}, {"x": 2.0, "y": 3.0}]


def test_to_draft_rejects_an_unknown_wire_block() -> None:
    with pytest.raises(TypeError, match="unknown wire block Outline"):
        to_draft(Outline.model_validate(OUTLINE), {}, random.Random(0))  # type: ignore[arg-type]


def test_second_generation_reuses_segments(
    settings: Settings, make_source, make_artifact, replies
) -> None:
    make_source()
    assert generate(settings, make_artifact("first"), replies()) == "ready"
    before = segments_of(settings)

    # The second outline moves "method" to other pages and adds a section.
    outline = copy.deepcopy(OUTLINE)
    outline["sections"][1].update(start_page=4, end_page=9, title="Renamed")
    outline["sections"].append(
        {"label": "results", "title": "Results", "start_page": 6, "end_page": 7,
         "key_points": ["r"]}
    )
    second = page(*PAGE["blocks"][:5], prose("Results.", "results"), *PAGE["blocks"][5:])
    assert generate(settings, make_artifact("second"), replies(outline, second)) == "ready"

    after = segments_of(settings)
    assert after[:2] == before  # same ids, title and locator left alone
    assert after[2][2:4] == ("results", "Results") and after[2][5] == full_page_regions(6, 7)
    analyse = json.loads(
        (settings.artifacts_dir / "second" / "generation" / "analyse.json").read_text()
    )
    assert [s["created"] for s in analyse["segments"]] == [False, False, True]
    with session_scope(settings) as session:
        artifact = ArtifactManager(session, settings).get_by_slug("second")
        blocks = ArtifactBlockManager(session, settings)
        results = blocks.list(artifact)[5]
        assert [s.id for s in blocks.segments(results)] == [after[2][0]]


def test_text_source_has_no_segments(
    settings: Settings, make_source, make_artifact, replies
) -> None:
    make_source("note", TEXT.encode(), "note.md")
    slug = make_artifact(sources=("note",))
    fake = replies(TEXT_OUTLINE)
    assert generate(settings, slug, fake) == "ready"
    document = fake.calls[0]["messages"][0]["content"][0]
    assert document["source"] == {"type": "text", "media_type": "text/plain", "data": TEXT}
    assert "always null, because this document is plain text" in (
        fake.calls[0]["messages"][0]["content"][1]["text"]
    )
    assert segments_of(settings, "note") == []
    analyse = json.loads((settings.artifacts_dir / slug / "generation" / "analyse.json").read_text())
    assert analyse["segments"].startswith("none: a plain-text source has no pages")
    with session_scope(settings) as session:
        artifact = ArtifactManager(session, settings).get_by_slug(slug)
        blocks = ArtifactBlockManager(session, settings)
        assert all(blocks.segments(b) == [] for b in blocks.list(artifact))


def test_only_the_first_source_is_taught(
    settings: Settings, make_source, make_artifact, replies
) -> None:
    make_source()
    make_source("other", b"%PDF-1.4\nother\n", "other.pdf")
    slug = make_artifact(sources=("paper", "other"))
    fake = replies()
    assert generate(settings, slug, fake) == "ready"
    data = fake.calls[0]["messages"][0]["content"][0]["source"]["data"]
    assert base64.b64decode(data) == PDF_BYTES
    analyse = json.loads((settings.artifacts_dir / slug / "generation" / "analyse.json").read_text())
    assert analyse["ignored_sources"] == ["other"]


# -- the requests ---------------------------------------------------------------------------


def test_request_shape(settings: Settings, make_source, make_artifact, replies) -> None:
    make_source()
    slug = make_artifact()
    fake = replies()
    assert generate(settings, slug, fake, timeout=99.0) == "ready"
    calls = document_calls(fake)  # the question check's calls carry no document
    assert list(calls) == ["Step 1 of 3", "Step 2 of 3", "Review", "Step 3 of 3"]
    analyse, write, review, glossary = calls.values()
    for call in (analyse, write, review, glossary):
        # no thinking (adaptive is the default), sampling params, tool_choice, betas, fallbacks
        assert set(call) == {"model", "max_tokens", "system", "messages", "output_config",
                             "timeout"}
        assert call["model"] == "claude-sonnet-5-5"
        assert call["system"] == SYSTEM_PROMPT
        assert call["timeout"] == 99.0
        assert call["output_config"]["effort"] == EFFORT == "medium"
        (turn,) = call["messages"]  # a single user turn: no assistant prefill
        assert turn["role"] == "user"
        document, instructions = turn["content"]
        assert document == {
            "type": "document",
            "source": {
                "type": "base64",
                "media_type": "application/pdf",
                "data": base64.standard_b64encode(PDF_BYTES).decode("ascii"),
            },
            "title": "Title of paper",
            "cache_control": {"type": "ephemeral"},
        }
        assert "\n" not in document["source"]["data"]
        assert instructions["type"] == "text" and "cache_control" not in instructions
    assert analyse["max_tokens"] == ANALYSE_MAX_TOKENS and write["max_tokens"] == WRITE_MAX_TOKENS
    assert analyse["output_config"]["format"] == OUTLINE_FORMAT
    assert write["output_config"]["format"] == PAGE_FORMAT
    assert glossary["max_tokens"] == GLOSSARY_MAX_TOKENS
    assert glossary["output_config"]["format"] == GLOSSARY_FORMAT
    assert review["max_tokens"] == REVIEW_MAX_TOKENS
    assert review["output_config"]["format"] == REVIEW_FORMAT

    # The prefix up to the cache breakpoint is byte-identical; only the instructions differ.
    def prefix(call: dict) -> str:
        return json.dumps([call["model"], call["system"], call["messages"][0]["content"][0],
                           call["output_config"]["effort"]])

    assert prefix(analyse) == prefix(write) == prefix(review) == prefix(glossary)
    analyse_text = analyse["messages"][0]["content"][1]["text"]
    write_text = write["messages"][0]["content"][1]["text"]
    glossary_text = glossary["messages"][0]["content"][1]["text"]
    assert analyse_text.startswith("Step 1 of 3") and write_text.startswith("Step 2 of 3")
    assert glossary_text.startswith("Step 3 of 3")
    assert "counting the first page of the PDF file as page 1" in analyse_text
    outline = json.dumps(OUTLINE, indent=2, ensure_ascii=False)
    assert f"<outline>\n{outline}\n</outline>" in write_text
    assert DEPTHS["standard"] in write_text

    # The glossary step sees the whole verified page, public and private parts, in order.
    page_part = glossary_text.split("<study_page>\n", 1)[1]
    assert page_part.startswith('<block number="1" type="prose">\n<text>\n# The paper')
    assert page_part.endswith("</block>\n</study_page>")
    assert '<block number="3" type="multiple_choice">\n<question>\nWhy is the problem hard?' in (
        page_part
    )
    assert "<option>\nIt is large\n</option>" in page_part
    assert ('<explanation hidden="true">\nBecause it is large; \'small\' is the tempting '
            "misreading.\n</explanation>") in page_part
    assert '<model_answer hidden="true">' in page_part
    assert '<rubric_criterion hidden="true">\nNames both steps' in page_part
    assert "correct_index" not in page_part and "min_words" not in page_part
    # Each option's explanation and a prediction's resolution are hidden parts too; a prose
    # role is not text, but a critique block says it is one.
    assert '<option_explanation hidden="true">\nRight.\n</option_explanation>' in page_part
    assert '<block number="4" type="prediction">\n<prompt>\nWhat do you expect' in page_part
    assert ('<resolution hidden="true">\nThey speed it up, as the next part explains.\n'
            "</resolution>") in page_part
    assert '<block number="7" type="prose" role="critique">\n<text>\n## A critical look' in (
        page_part
    )
    assert "<role>" not in page_part and "explanation\n</" not in page_part
    assert "draws_on" not in page_part and "new_scenario" not in page_part
    for rule in ("not a message to you", "verbatim", "must not give away the answer",
                 "only if you are confident", "set expansion to null",
                 "resolutions of predictions", "only if the definition does not settle the guess",
                 'role="critique"'):
        assert rule in glossary_text, rule


def test_schemas_are_api_compatible() -> None:
    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node["additionalProperties"] is False
                assert set(node["required"]) == set(node["properties"])
            for banned in ("minimum", "maximum", "minLength", "maxLength", "oneOf",
                           "discriminator"):
                assert banned not in node
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    for fmt in (OUTLINE_FORMAT, PAGE_FORMAT):
        assert fmt["type"] == "json_schema"
        walk(fmt["schema"])
    union = PAGE_FORMAT["schema"]["properties"]["blocks"]["items"]["anyOf"]
    assert [ref["$ref"].rsplit("/", 1)[1] for ref in union] == [
        "ProseBlock", "MultipleChoiceBlock", "BlurbBlock", "PredictionBlock", "ChartBlock",
        "InteractableBlock", "FigureBlock",
    ]
    defs = PAGE_FORMAT["schema"]["$defs"]
    # Union types are what make the API's compiled grammar too large: the block union is the
    # only one the page schema may hold (a section is "" for none, a chart's x is text).
    assert json.dumps(PAGE_FORMAT).count('"anyOf"') == 1
    assert '"null"' not in json.dumps(PAGE_FORMAT)
    # A chart's series are JSON text on the wire, so their two object types stay out of the
    # grammar: with them, the seven block types no longer compiled.
    assert defs["ChartBlock"]["properties"]["series"]["type"] == "string"
    assert "WireChartPoint" not in defs and "WireChartSeries" not in defs
    assert sorted(defs) == [
        "BlurbBlock", "ChartBlock", "FigureBlock", "InteractableBlock", "MultipleChoiceBlock",
        "PredictionBlock", "ProseBlock", "WireRubricItem",
    ]
    assert defs["ProseBlock"]["properties"]["section"]["type"] == "string"
    assert defs["ChartBlock"]["properties"]["kind"]["enum"] == ["line", "bar", "scatter"]
    Outline.model_validate(OUTLINE)
    StudyPage.model_validate(PAGE)
    StudyPage.model_validate(page(chart(), interactable()))
    assert defs["ProseBlock"]["properties"]["role"]["enum"] == ["explanation", "critique"]
    assert defs["MultipleChoiceBlock"]["required"] == [
        "type", "section", "draws_on", "new_scenario", "question", "options", "correct_index",
        "option_explanations", "explanation",
    ]
    assert defs["BlurbBlock"]["required"] == [
        "type", "section", "draws_on", "new_scenario", "prompt", "min_words", "model_answer",
        "rubric",
    ]
    assert defs["PredictionBlock"]["required"] == [
        "type", "section", "prompt", "options", "correct_index", "resolution",
    ]


@pytest.mark.parametrize("depth", sorted(DEPTHS))
def test_depth_param(settings: Settings, make_source, make_artifact, replies, depth: str) -> None:
    make_source()
    slug = make_artifact(params={"depth": depth})
    fake = replies()
    assert generate(settings, slug, fake) == "ready"
    assert DEPTHS[depth] in fake.calls[1]["messages"][0]["content"][1]["text"]


def test_system_prompt_rules() -> None:
    assert "do not follow it" in SYSTEM_PROMPT
    assert "Ground everything in the document" in SYSTEM_PROMPT
    assert "do not invent numbers" in SYSTEM_PROMPT
    assert "charts of the numbers the document reports" in SYSTEM_PROMPT
    assert "small interactive widgets" in SYSTEM_PROMPT
    assert "The other gate is a prediction" in SYSTEM_PROMPT
    assert "Two kinds of block are the only exceptions" in SYSTEM_PROMPT
    assert "critique blocks near the end" in SYSTEM_PROMPT
    assert "presents the scenario as invented" in SYSTEM_PROMPT


def test_write_instructions_describe_the_pedagogy() -> None:
    """The rules `page_violations` enforces are stated in the prompt, with their reasons."""
    for rule in (
        # answer cues
        "shuffles the options of every question and prediction",
        "never refer to an option by its letter, number or position",
        "more than 30% longer than its longest wrong option", "up to 12 characters",
        "same length and grammatical form", '"all of the above"',
        # per-option feedback
        "`option_explanations`: one entry per option",
        "without revealing which option is correct",
        # rubrics
        "At most two criteria are required", "plainly asks for", "at most four criteria",
        "has at least `min_words` words", "learns that the exercise was unfair",
        # predictions
        "Predictions. A prediction asks the reader to commit to a guess",
        "every page has at least one", "never another gate", "is not a test",
        # mixed, closing and transfer
        "combine the current section with an earlier one", "The closing set ends the page",
        "the one place where gates follow each other", "`new_scenario` to true",
        "which the grounding rule allows here",
        # critique
        'one or two prose blocks with `role` "critique" and `section` ""',
        "Never invent specifics", "No exercise may depend on the critique",
    ):
        assert rule in WRITE_INSTRUCTIONS, rule
    for depth in DEPTHS.values():
        assert "exercises" in depth and "predictions" in depth and "closing set" in depth
        assert "gates" not in depth


def test_write_instructions_describe_charts_and_interactables() -> None:
    for rule in ("- chart:", "- interactable:", "copied exactly",
                 "reading them off a plotted curve",
                 "at most three in a scatter chart", "share one y axis", "no network access",
                 "--series-1", "the application rejects a fragment", "made-up example values",
                 "They are not gates and do not replace one"):
        assert rule in WRITE_INSTRUCTIONS, rule
    for word in ("\u2014", "color", "behavior"):  # British spelling, no em dashes
        assert word not in WRITE_INSTRUCTIONS and word not in SYSTEM_PROMPT, word


# -- failures -------------------------------------------------------------------------------


def assert_failed(settings: Settings, slug: str, stage: str, *fragments: str) -> dict:
    assert status(settings, slug) == "failed"
    last = runs(settings, slug)[-1]
    assert (last["stage"], last["status"]) == (stage, "failed"), last
    assert last["error"].startswith("GenerationError: "), last["error"]
    for fragment in fragments:
        assert fragment in last["error"], (fragment, last["error"])
    with session_scope(settings) as session:
        artifact = ArtifactManager(session, settings).get_by_slug(slug)
        assert ArtifactBlockManager(session, settings).list(artifact) == []
    return last


def test_unknown_depth_fails_before_any_call(
    settings: Settings, make_source, make_artifact, fake_anthropic
) -> None:
    make_source()
    slug = make_artifact(params={"depth": "medium"})
    fake = fake_anthropic()
    assert generate(settings, slug, fake) == "failed"
    assert_failed(settings, slug, "analyse", "unknown depth 'medium'")
    assert fake.calls == []


def test_missing_credentials(
    settings: Settings, make_source, make_artifact, monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path, forbid_real_messages_api: None,
) -> None:
    monkeypatch.delenv("ANTHROPIC_CONFIG_DIR")
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    make_source()
    slug = make_artifact()
    concepts = FakeConcepts()
    generator = ClaudeGenerator(concept_extractor=concepts)
    assert run_generation(settings, slug, generator) == "failed"
    assert_failed(settings, slug, "preflight", "no Anthropic API credentials", "ANTHROPIC_API_KEY")
    assert generator._client is None  # checked again next time
    assert concepts.checks == 1 and concepts.calls == []
    assert [r["stage"] for r in runs(settings, slug)] == ["preflight"]


def test_unloadable_credentials(
    settings: Settings, make_source, make_artifact, forbid_real_messages_api: None
) -> None:
    make_source()  # the autouse fixture points ANTHROPIC_CONFIG_DIR at an empty directory
    slug = make_artifact()
    generator = ClaudeGenerator(concept_extractor=FakeConcepts())
    assert run_generation(settings, slug, generator) == "failed"
    assert_failed(settings, slug, "preflight", "Anthropic credentials could not be loaded")


STATUS_ERRORS = [
    (anthropic.AuthenticationError, 401, "authentication_error", "rejected the credentials"),
    (anthropic.PermissionDeniedError, 403, "permission_error", "not allowed to use model"),
    (anthropic.RateLimitError, 429, "rate_limit_error", "rate limit"),
    (anthropic.InternalServerError, 500, "api_error", "server error"),
    (anthropic.OverloadedError, 529, "overloaded_error", "overloaded"),
    (anthropic.BadRequestError, 400, "invalid_request_error", "message: fake"),
    (anthropic.NotFoundError, 404, "not_found_error", "returned an error"),
    (anthropic.RequestTooLargeError, 413, "request_too_large", "returned an error"),
]


@pytest.mark.parametrize(("error_class", "code", "error_type", "fragment"), STATUS_ERRORS)
def test_api_status_errors(
    settings: Settings, make_source, make_artifact, fake_anthropic,
    error_class: type, code: int, error_type: str, fragment: str,
) -> None:
    make_source()
    slug = make_artifact()
    fake = fake_anthropic(fake_anthropic.status_error(error_class, code, error_type))
    assert generate(settings, slug, fake) == "failed"
    assert_failed(
        settings, slug, "analyse", "the analyse call to Claude failed", f"HTTP {code}",
        error_type, "req_fake", fragment,
    )


def test_timeout_and_connection_errors(
    settings: Settings, make_source, make_artifact, fake_anthropic
) -> None:
    make_source()
    first, second = make_artifact("first"), make_artifact("second")
    assert generate(settings, first, fake_anthropic(fake_anthropic.timeout())) == "failed"
    assert_failed(settings, first, "analyse", f"did not answer within {DEFAULT_TIMEOUT:g} seconds")
    assert generate(settings, second, fake_anthropic(fake_anthropic.connection_error())) == "failed"
    assert_failed(settings, second, "analyse", "could not connect")


def test_mid_stream_error_in_write(
    settings: Settings, make_source, make_artifact, fake_anthropic, replies
) -> None:
    make_source()
    slug = make_artifact()
    overloaded = fake_anthropic.status_error(anthropic.OverloadedError, 529, "overloaded_error")
    fake = replies(OUTLINE, fake_anthropic.mid_stream(overloaded))
    assert generate(settings, slug, fake) == "failed"
    assert_failed(settings, slug, "write", "the write call to Claude failed", "HTTP 529")
    # preflight, analyse, figures and reader_profile succeeded
    assert [r["status"] for r in runs(settings, slug)] == ["succeeded"] * 4 + ["failed"]


def test_refusal_records_tokens(
    settings: Settings, make_source, make_artifact, fake_anthropic
) -> None:
    make_source()
    slug = make_artifact()
    refusal = fake_anthropic.reply(
        text="", stop_reason="refusal", input_tokens=10, output_tokens=2,
        stop_details={"type": "refusal", "category": "cyber", "explanation": None},
    )
    assert generate(settings, slug, fake_anthropic(refusal)) == "failed"
    last = assert_failed(settings, slug, "analyse", "declined the analyse step", "refusal",
                         "cyber")
    assert (last["input"], last["output"]) == (10, 2)
    assert segments_of(settings) == []


def ledger(settings: Settings) -> list[tuple]:
    """The cost ledger's rows: (purpose, step, model, run stage, run status, input, cache
    write, cache read, output, cost), in order."""
    with session_scope(settings) as session:
        return [tuple(row) for row in session.execute(
            select(ApiCall.purpose, ApiCall.step, ApiCall.model, GenerationRun.stage,
                   GenerationRun.status, ApiCall.input_tokens, ApiCall.cache_write_tokens,
                   ApiCall.cache_read_tokens, ApiCall.output_tokens, ApiCall.cost_usd)
            .outerjoin(GenerationRun, GenerationRun.id == ApiCall.generation_run_id)
            .order_by(ApiCall.id)
        )]  # fmt: skip


def test_every_call_of_a_generation_is_in_the_cost_ledger(
    settings: Settings, make_source, make_artifact, replies
) -> None:
    make_source()
    slug = make_artifact()
    fake = replies()
    assert generate(settings, slug, fake) == "ready"
    rows = ledger(settings)
    assert len(rows) == len(fake.calls)  # one row per call made
    assert {row[0] for row in rows} == {"generation"}
    assert [(row[1], row[3]) for row in rows] == [
        ("analyse", "analyse"), ("write", "write"), ("review", "review"),
        *[(step, "question_check") for step in (
            "open-book check", "open-book check", "judge", "open-book check", "judge",
            "closed-book check",
        )],
        ("glossary", "glossary"),
    ]  # fmt: skip
    analyse = rows[0]
    assert analyse[5:9] == (50, 3000, 0, 400)
    # These fake replies do not say which TTL their cache writes had (`usage.cache_creation`),
    # so the writes are priced at the dearer 1-hour rate, an upper bound.
    assert analyse[9] == pytest.approx((50 * 2 + 3000 * 4 + 400 * 10) / 1_000_000)
    assert rows[3][2] == "claude-sonnet-5-5" and rows[5][2] == "claude-haiku-5-5"
    with session_scope(settings) as session:
        artifact = ArtifactManager(session, settings).get_by_slug(slug)
        assert set(session.scalars(select(ApiCall.artifact_id))) == {artifact.id}


def test_a_failed_generation_keeps_its_ledger_rows(
    settings: Settings, make_source, make_artifact, fake_anthropic
) -> None:
    """A reply that is rejected (here a refusal of the write step) still cost money: its row
    stays although the generation fails."""
    make_source()
    slug = make_artifact()
    outline = fake_anthropic.reply(text=json.dumps(OUTLINE), model="claude-sonnet-5-5",
                                   input_tokens=50, output_tokens=400)
    refusal = fake_anthropic.reply(text="", stop_reason="refusal", model="claude-sonnet-5-5",
                                   input_tokens=10, output_tokens=2)
    assert generate(settings, slug, fake_anthropic(outline, refusal)) == "failed"
    assert [(row[1], row[3], row[4], row[5], row[8]) for row in ledger(settings)] == [
        ("analyse", "analyse", "succeeded", 50, 400), ("write", "write", "failed", 10, 2),
    ]


def bad_replies(fake_anthropic) -> dict[str, tuple[Any, Any, str, tuple[str, ...]]]:
    """case -> (outline reply, page reply, failing stage, error fragments)."""
    reply = fake_anthropic.reply
    outline = json.dumps(OUTLINE)

    def with_sections(*changes: dict) -> dict:
        data = copy.deepcopy(OUTLINE)
        for section, change in zip(data["sections"], changes, strict=False):
            section.update(change)
        return data

    return {
        "analyse max_tokens": (
            reply(text=outline[:40], stop_reason="max_tokens"), None, "analyse",
            ("cut off at max_tokens=32000",),
        ),
        "write max_tokens": (
            OUTLINE, reply(text='{"blocks": [', stop_reason="max_tokens"), "write",
            ("cut off at max_tokens=64000",),
        ),
        "unexpected stop": (
            reply(text=outline, stop_reason="pause_turn"), None, "analyse",
            ("stopped unexpectedly ('pause_turn')",),
        ),
        "invalid json": (
            reply(text="Here is the outline: {"), None, "analyse",
            ("not a valid Outline", "Invalid JSON"),
        ),
        "outline schema": (
            {"summary": "s", "main_claims": [], "background": []}, None, "analyse",
            ("not a valid Outline", "sections: Field required"),
        ),
        "outline extra key": (
            {**OUTLINE, "title": "x"}, None, "analyse", ("not a valid Outline", "title"),
        ),
        "page schema": (
            OUTLINE, page(prose("x"), {"type": "multiple_choice", "section": ""}), "write",
            ("not a valid StudyPage", "blocks.1.multiple_choice.draws_on: Field required"),
        ),
        "page unknown type": (
            OUTLINE, page(prose("x"), {"type": "video", "section": ""}), "write",
            ("not a valid StudyPage",),
        ),
        "no sections": (
            {**OUTLINE, "sections": []}, None, "analyse", ("the outline has no sections",),
        ),
        "duplicate label": (
            with_sections({}, {"label": "problem"}), None, "analyse",
            ("outline section 2 ('problem')", "used by an earlier section"),
        ),
        "bad label": (
            with_sections({"label": "The Problem"}), None, "analyse",
            ("outline section 1 ('The Problem')", "lowercase letters and digits"),
        ),
        "missing pages": (
            with_sections({"start_page": None}), None, "analyse",
            ("a PDF section needs start_page and end_page",),
        ),
        "reversed pages": (
            with_sections({}, {"start_page": 5, "end_page": 3}), None, "analyse",
            ("invalid page range 5-3",),
        ),
        "page zero": (
            with_sections({"start_page": 0}), None, "analyse", ("invalid page range 0-2",),
        ),
    }


@pytest.mark.parametrize(
    "case",
    ["analyse max_tokens", "write max_tokens", "unexpected stop", "invalid json",
     "outline schema", "outline extra key", "page schema", "page unknown type", "no sections",
     "duplicate label", "bad label", "missing pages", "reversed pages", "page zero"],
)
def test_bad_replies_fail(
    settings: Settings, make_source, make_artifact, fake_anthropic, replies, case: str
) -> None:
    outline, page_, stage, fragments = bad_replies(fake_anthropic)[case]
    make_source()
    slug = make_artifact()
    fake = replies(outline, page_, None)
    assert generate(settings, slug, fake) == "failed"
    assert_failed(settings, slug, stage, *fragments)
    assert fake.outcomes == []
    if stage == "analyse":
        assert segments_of(settings) == []  # nothing ingested from a rejected outline


def test_text_outline_with_pages_fails(
    settings: Settings, make_source, make_artifact, replies
) -> None:
    make_source("note", TEXT.encode(), "note.txt")
    slug = make_artifact(sources=("note",))
    assert generate(settings, slug, replies(OUTLINE, None)) == "failed"
    assert_failed(settings, slug, "analyse", "has page numbers, but the document is plain text")


def blocks_of(*changes: tuple[str, int, Any]) -> dict[str, Any]:
    """PAGE with `changes` applied in order: ("set", i, block), ("insert", i, block or list of
    blocks), ("drop", i, None), all with 0-based indices."""
    blocks = copy.deepcopy(PAGE["blocks"])
    for action, index, value in changes:
        if action == "set":
            blocks[index] = value
        elif action == "insert":
            blocks[index:index] = value if isinstance(value, list) else [value]
        elif action == "drop":
            del blocks[index]
        else:
            raise AssertionError(action)
    return page(*blocks)


CLOSING_MCQ = PAGE["blocks"][7]
CLOSING_BLURB = PAGE["blocks"][8]
Q = "'Why is the problem hard?'"  # how a violation names the default question

# case -> (page, fragments that must all occur in one violation each, in this order)
VIOLATIONS: dict[str, tuple[dict[str, Any], tuple[str, ...]]] = {
    "no blocks": (page(), ("the page has no blocks",)),
    "starts with a gate": (
        page(mcq(None, draws_on=[]), *PAGE["blocks"][1:]),
        (f"block 1 (multiple_choice {Q}): the page must start with an explanation",),
    ),
    "starts with a critique": (
        blocks_of(("set", 0, critique())),
        ("block 1 (critique 'A critical look'): the page must start with an explanation",),
    ),
    "duplicate options": (
        blocks_of(("set", 2, mcq(options=["Large", " large ", "small"], correct_index=2))),
        (f"block 3 (multiple_choice {Q}): two options are the same",),
    ),
    "correct index out of range": (
        blocks_of(("set", 2, mcq(correct_index=3))),
        (f"block 3 (multiple_choice {Q}): invalid multiple_choice block content",),
    ),
    "one option": (
        blocks_of(("set", 2, mcq(options=["only"], correct_index=0))),
        (f"block 3 (multiple_choice {Q}): invalid multiple_choice block content",),
    ),
    "option explanations do not fit the options": (
        blocks_of(("set", 2, mcq(option_explanations=["a", "b"]))),
        ("option_explanations has 2 entries for 3 options",),
    ),
    "blank option explanation": (
        blocks_of(("set", 2, mcq(option_explanations=["a", " ", "c"]))),
        ("option_explanations entry 2 must not be blank",),
    ),
    "empty rubric": (
        blocks_of(("set", 5, blurb(rubric=[], draws_on=SECTIONS))),
        ("block 6 (blurb 'Explain how the method works.'): invalid blurb block content",),
    ),
    "blank criterion": (
        blocks_of(("set", 5, blurb(rubric=[{"criterion": " ", "required": True}],
                                   draws_on=SECTIONS))),
        ("block 6 (blurb 'Explain how the method works.'): invalid blurb block content",),
    ),
    "empty prose": (
        blocks_of(("set", 1, prose("", "problem"))), ("block 2 (prose", "invalid prose block"),
    ),
    "blank prediction resolution": (
        blocks_of(("set", 3, prediction(resolution=" "))),
        ("block 4 (prediction 'What do you expect the two steps to do?'): invalid prediction",),
    ),
    "five prediction options": (
        blocks_of(("set", 3, prediction(options=["a", "b", "c", "d", "e"]))),
        ("block 4 (prediction", "invalid prediction block content"),
    ),
    "chart with a number for x in a bar chart": (
        blocks_of(("insert", 5, chart(series=[{"name": "a", "points": [{"x": 1, "y": 2}]}]))),
        ("block 6 (chart 'Accuracy by method'): invalid chart block content",),
    ),
    "interactable that fetches": (
        blocks_of(("insert", 5, interactable(html="<script>fetch('data.json')</script>"))),
        ("block 6 (interactable 'Step through the method'): invalid interactable block",),
    ),
    "unknown section": (
        blocks_of(("set", 1, prose("x", "results"))),
        ("block 2 (prose 'x'): section 'results' is not a label in the outline",),
    ),
    "the correct option is clearly the longest": (
        blocks_of(("set", 2, mcq(options=[
            "It is small", "It is large, because the search space grows with n", "No"]))),
        (f"block 3 (multiple_choice {Q}): the correct option (50 characters) is more than 30% "
         "longer than the longest wrong option (11 characters)",),
    ),
    "a pointing option": (
        blocks_of(("set", 2, mcq(options=["It is small", "It is large", "All of the above"]))),
        ("option 'All of the above' points at the other options",),
    ),
    "an option named by its letter": (
        blocks_of(("set", 2, mcq(explanation="Option B is right: the size grows the search."))),
        ("the explanation refers to an option by its letter, number or position ('Option B')",),
    ),
    "an option named by its position": (
        blocks_of(("set", 2, mcq(option_explanations=["a", "The second option is right.", "c"]))),
        ("the explanation of option 'It is large' refers to an option by its letter, number or "
         "position ('second option')",),
    ),
    "a resolution naming an option by position": (
        blocks_of(("set", 3, prediction(resolution="The first option holds, as shown next."))),
        ("block 4 (prediction", "the resolution refers to an option", "'first option'"),
    ),
    "a wrong option's explanation gives the answer away": (
        blocks_of(("set", 2, mcq(
            options=["It is very small", "It is very large", "It is not hard"],
            option_explanations=["No: in fact it is very large.", "Right.", "It is hard."]))),
        ("the explanation of the wrong option 'It is very small' quotes the correct option",),
    ),
    "no required criterion": (
        blocks_of(("set", 5, blurb(draws_on=SECTIONS, rubric=[
            {"criterion": "Names both steps", "required": False}]))),
        ("block 6 (blurb", "no rubric criterion is required, so any answer would pass"),
    ),
    "three required criteria": (
        blocks_of(("set", 5, blurb(draws_on=SECTIONS, rubric=[
            {"criterion": c, "required": True} for c in ("One", "Two", "Three")]))),
        ("3 rubric criteria are required, at most 2 may be",),
    ),
    "five criteria": (
        blocks_of(("set", 5, blurb(draws_on=SECTIONS, rubric=[
            {"criterion": c, "required": c == "One"} for c in ("One", "Two", "3", "4", "5")]))),
        ("the rubric has 5 criteria, at most 4 are allowed",),
    ),
    "min_words too high": (
        blocks_of(("set", 5, blurb(draws_on=SECTIONS, min_words=60))),
        ("min_words is 60; it must be between 15 and 40",),
    ),
    "model answer shorter than min_words": (
        blocks_of(("set", 5, blurb(draws_on=SECTIONS, model_answer="Two steps."))),
        ("the model answer has 2 words, fewer than min_words (20)",),
    ),
    "exercise before its section is taught": (
        blocks_of(("drop", 2, None), ("insert", 1, mcq())),
        (f"block 2 (multiple_choice {Q}): it asks about section 'problem', which no earlier "
         "block has explained yet",),
    ),
    "draws on a section not taught yet": (
        blocks_of(("set", 2, mcq(draws_on=["problem", "method"]))),
        (f"block 3 (multiple_choice {Q}): it asks about section 'method', which no earlier",),
    ),
    "draws on an unknown section": (
        blocks_of(("set", 2, mcq(draws_on=["problem", "results"]))),
        ("draws_on names 'results', which is not an outline label",),
    ),
    "prediction before a gate": (
        blocks_of(("insert", 5, prediction())),
        ("block 6 (prediction 'What do you expect the two steps to do?'): a prediction comes "
         "right before the material that answers it", "but block 7 is a blurb"),
    ),
    "prediction before the critique": (
        blocks_of(("insert", 6, prediction(None))),
        ("block 7 (prediction", "but block 8 is a critique"),
    ),
    "prediction at the end": (
        page(*PAGE["blocks"], prediction(None)), ("block 10 (prediction", "it is the last block"),
    ),
    "prediction options that give the answer away": (
        blocks_of(("set", 3, prediction(options=[
            "It speeds the method up by running both steps at once", "Slower", "Same"]))),
        ("block 4 (prediction", "more than 30% longer than the longest wrong option"),
    ),
    "critique with a section": (
        blocks_of(("set", 6, critique(section="method"))),
        ("block 7 (critique 'A critical look'): a critique block steps back", 'must be ""'),
    ),
    "no exercise": (
        page(*[b for b in PAGE["blocks"] if b["type"] not in ("multiple_choice", "blurb")]),
        ("the page has no exercise (multiple_choice or blurb)",),
    ),
    "no prediction": (blocks_of(("drop", 3, None)), ("the page has no prediction",)),
    "no critique": (
        blocks_of(("drop", 6, None)),
        ('the page has no critique; add one or two prose blocks with role "critique"',),
    ),
    "three critique blocks": (
        blocks_of(("insert", 6, [critique(), critique()])),
        ("the page has 3 critique blocks; at most 2 are allowed",),
    ),
    "critique in the body": (
        blocks_of(("drop", 6, None), ("insert", 2, critique())),
        ("the critique (block(s) 3) must come together right before the closing set",),
    ),
    "critique blocks apart": (
        blocks_of(("insert", 5, critique())),
        ("the critique (block(s) 6, 8) must come together right before the closing set",),
    ),
    "no closing set": (
        page(*PAGE["blocks"][:7]),
        ("the page must end with the closing set, 2 or 3 exercises in a row", "but it ends with "
         "a critique block"),
    ),
    "closing set of one": (
        blocks_of(("drop", 8, None)), ("but it ends with 1 exercise(s) in a row",),
    ),
    "closing set of four": (
        page(*PAGE["blocks"], CLOSING_MCQ, CLOSING_BLURB),
        ("but it ends with 4 exercise(s) in a row",),
    ),
    "closing exercise in a section": (
        blocks_of(("set", 8, {**CLOSING_BLURB, "section": "method"})),
        ("block 9 (blurb 'Explain why the order of the steps matters.'): an exercise of the "
         "closing set draws on the whole page, so its section must be \"\"",),
    ),
    "closing exercise without draws_on": (
        blocks_of(("set", 8, {**CLOSING_BLURB, "draws_on": []})),
        ("block 9 (blurb", "must name the sections it draws on in draws_on"),
    ),
    "closing set on one section": (
        blocks_of(("set", 7, {**CLOSING_MCQ, "draws_on": ["method"]})),
        ("the closing set draws on fewer than two sections",),
    ),
    "no mixed exercise": (
        blocks_of(("set", 5, blurb(draws_on=["method"]))),
        ("no exercise before the closing set combines its own section with an earlier one",),
    ),
    "no new scenario": (
        blocks_of(("set", 7, {**CLOSING_MCQ, "new_scenario": False})),
        ("no exercise applies an idea to a situation the document does not contain",),
    ),
}


@pytest.mark.parametrize("case", sorted(VIOLATIONS))
def test_page_violations(case: str) -> None:
    bad_page, fragments = VIOLATIONS[case]
    violations = page_violations(StudyPage.model_validate(bad_page), SECTIONS)
    assert any(all(f in v for f in fragments) for v in violations), violations
    # Every listed violation is one sentence, without line breaks except pydantic's own.
    assert all(v.strip() and v[0].islower() for v in violations), violations


@pytest.mark.parametrize(
    "good",
    [
        PAGE,
        # Short options may differ by a few characters, however large the ratio.
        blocks_of(("set", 2, mcq(options=["No", "Yes, always", "Maybe"]))),
        ONE_SECTION_PAGE,
        # "To answer a question" and "the above equation" do not point at options.
        blocks_of(("set", 2, mcq(question="To answer a question about the above equation: why "
                                          "is the problem hard?"))),
    ],
)
def test_good_pages_have_no_violations(good: dict[str, Any]) -> None:
    assert page_violations(StudyPage.model_validate(good), SECTIONS) == []


def test_every_violation_is_listed_in_order_and_check_page_raises_with_all() -> None:
    bad = blocks_of(
        ("set", 2, mcq(options=["It is small", "It is large", "All of the above"],
                       option_explanations=["Too small.", "Right.", "Not so."])),
        ("set", 5, blurb(draws_on=["method"], min_words=60)),
        ("drop", 6, None),
    )
    violations = page_violations(StudyPage.model_validate(bad), SECTIONS)
    assert [v.split(":", 1)[0] for v in violations] == [
        f"block 3 (multiple_choice {Q})",
        "block 6 (blurb 'Explain how the method works.')",  # min_words out of range
        "block 6 (blurb 'Explain how the method works.')",  # model answer shorter than that
        'the page has no critique; add one or two prose blocks with role "critique" right '
        "before the closing set",
        "no exercise before the closing set combines its own section with an earlier one; at "
        "least one must draw on an earlier section too (and name both in draws_on)",
    ]
    with pytest.raises(GenerationError) as info:
        check_page(StudyPage.model_validate(bad), SECTIONS)
    message = str(info.value)
    assert message.startswith("the page breaks 5 rule(s): (1) block 3 (multiple_choice")
    for number, violation in enumerate(violations, start=1):
        assert f"({number}) {violation}" in message
    check_page(StudyPage.model_validate(PAGE), SECTIONS)  # a good page passes


@pytest.mark.parametrize(
    "case", ["starts with a gate", "the correct option is clearly the longest", "no critique",
             "no prediction"],
)
def test_verify_rules(
    settings: Settings, make_source, make_artifact, replies, fake_anthropic, case: str
) -> None:
    """Through a generation: a page that breaks a rule goes to revise; when both revisions
    return it unchanged, the last question check fails the generation naming the rule, and
    nothing is stored."""
    bad_page, fragments = VIOLATIONS[case]
    make_source()
    slug = make_artifact()
    checks = check_replies(fake_anthropic, bad_page)
    for _round in range(2):
        checks += [fake_anthropic.reply(text=json.dumps(bad_page)),
                   *check_replies(fake_anthropic, bad_page, review=False)]
    fake = replies(OUTLINE, bad_page, None, checks=checks)
    assert generate(settings, slug, fake) == "failed"
    assert_failed(settings, slug, "question_check", "after 2 revisions the page still has",
                  "page rule", *fragments)
    assert [r["stage"] for r in runs(settings, slug)] == [
        "preflight", "analyse", "figures", "reader_profile", "write", "review",
        "question_check", "revise", "question_check", "revise", "question_check",
    ]
    assert fake.outcomes == []


# -- shuffling the options ------------------------------------------------------------------

STORED_QUESTION = {
    "question": "Which one?",
    "options": ["alpha", "beta", "gamma", "delta"],
    "private": {
        "correct_index": 2,
        "explanation": "Gamma.",
        "option_explanations": ["not alpha", "not beta", "yes gamma", "not delta"],
    },
}


def test_shuffle_remaps_the_correct_index_and_the_option_explanations() -> None:
    original = copy.deepcopy(STORED_QUESTION)
    for seed in range(20):
        shuffled = shuffle_options(STORED_QUESTION, random.Random(seed))
        assert sorted(shuffled["options"]) == sorted(STORED_QUESTION["options"])
        assert by_option(shuffled) == by_option(STORED_QUESTION)
        assert shuffled["options"][shuffled["private"]["correct_index"]] == "gamma"
        assert shuffled["question"] == "Which one?"
        assert shuffled["private"]["explanation"] == "Gamma."
    assert STORED_QUESTION == original  # the input is not changed


def test_shuffle_is_deterministic_under_a_seed_and_moves_the_answer() -> None:
    runs_ = [shuffle_options(STORED_QUESTION, random.Random(42)) for _ in range(3)]
    assert runs_[0] == runs_[1] == runs_[2]
    positions = {
        shuffle_options(STORED_QUESTION, random.Random(seed))["private"]["correct_index"]
        for seed in range(50)
    }
    assert positions == {0, 1, 2, 3}  # the correct option can land anywhere


def test_shuffle_of_a_prediction_and_of_an_old_question() -> None:
    stored_prediction = {
        "prompt": "Guess.", "options": ["up", "down"],
        "private": {"correct_index": 1, "resolution": "Down."},
    }
    shuffled = shuffle_options(stored_prediction, random.Random(1))
    assert shuffled["options"][shuffled["private"]["correct_index"]] == "down"
    assert "option_explanations" not in shuffled["private"]
    with pytest.raises(ValueError, match="correct_index 4 does not name one of 4 options"):
        shuffle_options({**STORED_QUESTION, "private": {**STORED_QUESTION["private"],
                                                        "correct_index": 4}}, random.Random(0))
    with pytest.raises(ValueError, match="option_explanations has 1 entries for 4 options"):
        shuffle_options({**STORED_QUESTION, "private": {**STORED_QUESTION["private"],
                                                        "option_explanations": ["x"]}},
                        random.Random(0))


def test_to_draft_shuffles_questions_and_predictions_and_links_draws_on() -> None:
    wire = StudyPage.model_validate(PAGE).blocks
    segments = {"problem": 11, "method": 12}
    first = [to_draft(block, segments, random.Random(5)) for block in wire]
    second = [to_draft(block, segments, random.Random(5)) for block in wire]
    assert first == second  # deterministic under a seeded source
    assert [d.segment_ids for d in first] == [
        (), (11,), (11,), (12,), (12,), (12, 11), (), (11, 12), (12,),
    ]
    for index in (2, 3, 7):
        assert by_option(first[index].content) == wire_by_option(PAGE["blocks"][index])
    orders = {
        tuple(to_draft(wire[2], {}, random.Random(seed)).content["options"])
        for seed in range(30)
    }
    assert len(orders) > 1  # the order really changes
    # Blocks without options are not touched (and draw nothing from the source).
    rng = random.Random(9)
    state = rng.getstate()
    assert to_draft(wire[5], {}, rng).content["prompt"] == "Explain how the method works."
    assert rng.getstate() == state


def test_generations_with_the_same_seed_store_the_same_order(
    settings: Settings, make_source, make_artifact, replies
) -> None:
    make_source()

    def stored(slug: str) -> list[dict]:
        with session_scope(settings) as session:
            artifact = ArtifactManager(session, settings).get_by_slug(slug)
            manager = ArtifactBlockManager(session, settings)
            return [manager.read_content(b) for b in manager.list(artifact)]

    assert generate(settings, make_artifact("one"), replies(), rng=random.Random(3)) == "ready"
    assert generate(settings, make_artifact("two"), replies(), rng=random.Random(3)) == "ready"
    assert stored("one") == stored("two") == [c for _t, c in page_blocks(PAGE, seed=3)]
    # write.json keeps the page as the model wrote it.
    write = json.loads((settings.artifacts_dir / "one" / "generation" / "write.json").read_text())
    assert write["output"] == PAGE


def test_default_random_source() -> None:
    assert isinstance(ClaudeGenerator(object()).rng, random.Random)


# -- sources --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("content", "filename", "media_type", "fragment"),
    [
        (b"\x89PNG\r\n", "figure.png", None, "media type 'image/png'"),
        (b"PK\x03\x04", "slides.pptx", "application/vnd.ms-powerpoint", "cannot read"),
        (b"just text", "fake.pdf", None, "labelled application/pdf but is not a PDF file"),
        (b"\xff\xfe\x00bad", "latin.txt", None, "is not valid UTF-8 text"),
        (b"  \n\t", "blank.txt", None, "contains only whitespace"),
    ],
)
def test_unusable_sources_fail_before_any_call(
    settings: Settings, make_source, make_artifact, fake_anthropic,
    content: bytes, filename: str, media_type: str | None, fragment: str,
) -> None:
    make_source("doc", content, filename, media_type)
    slug = make_artifact(sources=("doc",))
    fake = fake_anthropic()
    assert generate(settings, slug, fake) == "failed"
    assert_failed(settings, slug, "analyse", fragment)
    assert fake.calls == []


def test_markdown_with_charset_is_text(
    settings: Settings, make_source, make_artifact, replies
) -> None:
    make_source("note", TEXT.encode(), "note.md", "text/markdown; charset=utf-8")
    slug = make_artifact(sources=("note",))
    assert generate(settings, slug, replies(TEXT_OUTLINE)) == "ready"


def test_oversized_pdf_fails_before_any_call(
    settings: Settings, make_source, make_artifact, fake_anthropic
) -> None:
    # 24 MB of PDF is 32 MB as base64: over the request limit once the prompts are added.
    make_source(content=b"%PDF-1.4\n" + b"\0" * 23_900_000)
    slug = make_artifact()
    fake = fake_anthropic()
    assert generate(settings, slug, fake) == "failed"
    assert_failed(
        settings, slug, "analyse", "too large to send", "32 MB request limit",
        "it was not truncated",
    )
    assert fake.calls == []


def test_no_sources(settings: Settings, make_artifact, fake_anthropic) -> None:
    slug = make_artifact(sources=())
    assert generate(settings, slug, fake_anthropic()) == "failed"
    assert_failed(settings, slug, "analyse", "has no linked source")


# -- client and app wiring ------------------------------------------------------------------


def test_real_client_created_lazily(
    settings: Settings, make_source, make_artifact, monkeypatch: pytest.MonkeyPatch,
    fake_anthropic,
) -> None:
    key = "sk-ant-test-NOT-A-REAL-KEY"
    monkeypatch.setenv("ANTHROPIC_API_KEY", key)
    fake = fake_anthropic(
        fake_anthropic.reply(text=json.dumps(OUTLINE)),
        fake_anthropic.status_error(anthropic.AuthenticationError, 401, "authentication_error"),
    )

    def stream(self: Any, **kwargs: Any) -> Any:  # replaces the real HTTP call
        return fake.stream(**kwargs)

    monkeypatch.setattr(anthropic.resources.messages.Messages, "stream", stream)
    make_source()
    slug = make_artifact()
    generator = ClaudeGenerator(concept_extractor=FakeConcepts())
    assert generator._client is None
    assert run_generation(settings, slug, generator) == "failed"
    assert isinstance(generator._client, anthropic.Anthropic)
    last = assert_failed(settings, slug, "write", "rejected the credentials", "ANTHROPIC_API_KEY")
    assert key not in last["error"]
    assert len(fake.calls) == 2


def test_default_app_uses_claude_generator_and_env_models(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TEACHER_GENERATION_MODEL", raising=False)
    monkeypatch.delenv("TEACHER_JUDGE_MODEL", raising=False)
    app = create_app(settings)
    assert isinstance(app.state.generator, ClaudeGenerator)
    assert app.state.generator.name == "claude-study-v1"
    assert app.state.generator.model == "claude-sonnet-5-5"
    assert app.state.generator._client is None  # nothing created until the first generation
    assert app.state.graders["blurb"].model == "claude-haiku-5-5"

    monkeypatch.setenv("TEACHER_GENERATION_MODEL", "claude-test-writer")
    monkeypatch.setenv("TEACHER_JUDGE_MODEL", "claude-test-judge")
    app = create_app(settings)
    assert app.state.generator.model == "claude-test-writer"
    assert isinstance(app.state.graders["blurb"], LLMJudgeGrader)
    assert app.state.graders["blurb"].model == "claude-test-judge"


def test_blank_model_env_var_is_an_error(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEACHER_GENERATION_MODEL", "  ")
    with pytest.raises(ValueError, match="TEACHER_GENERATION_MODEL is set but empty"):
        create_app(settings)


def test_env_model_is_used_in_requests_and_runs(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, make_source, replies
) -> None:
    """Through the API: the env-selected model is sent and recorded on every run."""
    monkeypatch.setenv("TEACHER_GENERATION_MODEL", "claude-test-writer")
    fake = replies()
    app = create_app(settings, graders={"multiple_choice": MultipleChoiceGrader(),
                                        "blurb": WordCountBlurbGrader()},
                     concept_extractor=FakeConcepts("claude-test-concepts"),
                     block_tagger=FakeTagger("claude-test-concepts"))
    app.state.review_bank_builder = None  # this test is about the generator's own runs
    app.state.generator._client = fake  # the env-configured generator, on the fake client
    make_source()
    with TestClient(app) as client:
        created = client.post("/api/artifacts", json={"sources": ["paper"]})
        assert created.status_code == 202, created.text
        assert created.json()["generator"] == "claude-study-v1"
        slug = created.json()["slug"]
        assert client.get(f"/api/artifacts/{slug}").json()["status"] == "ready"
        api_runs = client.get(f"/api/artifacts/{slug}/runs").json()
    assert [(r["stage"], r["model"]) for r in api_runs] == [
        # the concept stage records its own model, and so does the tagging stage
        (stage, "claude-test-concepts" if stage in ("concepts", "block_concepts")
         else "claude-test-writer")
        for stage in CLEAN_STAGES
    ]
    # Only the figures stage notes what it found (here: nothing, in a blank PDF).
    assert [r["stage"] for r in api_runs if r["note"] is not None] == ["figures"]
    assert [call["model"] for call in writer_calls(fake)] == ["claude-test-writer"] * 8
    # The open-book answers to the two blurbs are graded by the app's judge model.
    assert [call["model"] for call in judge_calls(fake)] == ["claude-haiku-5-5"] * 2


def test_chosen_model_is_used_stored_and_kept_on_regenerate(
    settings: Settings, make_source, replies
) -> None:
    """Through the API: a model given on create is sent on every call, recorded on every run,
    kept by a plain regenerate and replaced by a regenerate that names another."""
    fake = replies()
    app = create_app(settings, graders={"multiple_choice": MultipleChoiceGrader(),
                                        "blurb": WordCountBlurbGrader()},
                     concept_extractor=FakeConcepts("claude-test-concepts"),
                     block_tagger=FakeTagger("claude-test-concepts"))
    app.state.review_bank_builder = None  # this test is about the generator's own runs
    app.state.generator._client = fake
    make_source()
    with TestClient(app) as client:
        offered = client.get("/api/generation-models").json()
        assert offered["selectable"] is True
        assert offered["default"] == "claude-sonnet-5-5"
        assert [m["id"] for m in offered["models"]] == [
            "claude-fable-5-2", "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5",
        ]

        created = client.post(
            "/api/artifacts", json={"sources": ["paper"], "model": "claude-opus-5-5"}
        )
        assert created.status_code == 202, created.text
        first = created.json()
        assert first["model"] == "claude-opus-5-5"
        assert first["params"] == {"model": "claude-opus-5-5"}
        runs = client.get(f"/api/artifacts/{first['slug']}/runs").json()
        assert {
            r["model"] for r in runs if r["stage"] not in ("concepts", "block_concepts")
        } == {"claude-opus-5-5"}
        assert len(runs) == len(CLEAN_STAGES)
        assert [call["model"] for call in writer_calls(fake)] == ["claude-opus-5-5"] * 8

        unknown = client.post(
            "/api/artifacts", json={"sources": ["paper"], "model": "claude-nope"}
        )
        assert unknown.status_code == 422
        assert "unknown model 'claude-nope'" in unknown.text
        assert len(client.get("/api/artifacts").json()) == 1  # nothing was created

        default = replies()  # each fake client answers one generation
        app.state.generator._client = default
        plain = client.post("/api/artifacts", json={"sources": ["paper"], "slug": "plain"})
        assert plain.status_code == 202, plain.text
        assert plain.json()["model"] is None and plain.json()["params"] == {}
    assert [call["model"] for call in writer_calls(default)] == ["claude-sonnet-5-5"] * 8


def test_regenerate_keeps_or_replaces_the_model(settings: Settings, make_source, replies) -> None:
    app = create_app(settings, graders={"multiple_choice": MultipleChoiceGrader(),
                                        "blurb": WordCountBlurbGrader()},
                     concept_extractor=FakeConcepts("claude-test-concepts"),
                     block_tagger=FakeTagger("claude-test-concepts"))
    fakes = []

    def fresh_client() -> None:  # each fake client answers one generation
        fakes.append(replies())
        app.state.generator._client = fakes[-1]

    make_source()
    with TestClient(app) as client:
        fresh_client()
        slug = client.post(
            "/api/artifacts", json={"sources": ["paper"], "model": "claude-opus-5-5"}
        ).json()["slug"]
        fresh_client()
        kept = client.post(f"/api/artifacts/{slug}/regenerate")
        assert kept.status_code == 202, kept.text
        assert kept.json()["model"] == "claude-opus-5-5"
        fresh_client()
        changed = client.post(
            f"/api/artifacts/{kept.json()['slug']}/regenerate",
            json={"model": "claude-haiku-5-5"},
        )
        assert changed.status_code == 202, changed.text
        assert changed.json()["model"] == "claude-haiku-5-5"
        bad = client.post(
            f"/api/artifacts/{changed.json()['slug']}/regenerate", json={"model": ""}
        )
        assert bad.status_code == 422
    assert [[call["model"] for call in writer_calls(fake)] for fake in fakes] == [
        ["claude-opus-5-5"] * 8,
        ["claude-opus-5-5"] * 8,
        ["claude-haiku-5-5"] * 8,
    ]


def test_env_default_outside_the_choices_is_offered(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEACHER_GENERATION_MODEL", "claude-test-writer")
    with TestClient(create_app(settings)) as client:
        offered = client.get("/api/generation-models").json()
    assert offered["default"] == "claude-test-writer"
    assert offered["models"][-1] == {"id": "claude-test-writer", "label": "claude-test-writer"}


# -- the glossary stage ---------------------------------------------------------------------


def stored_glossary(settings: Settings, slug: str) -> tuple[Any, list[str]]:
    with session_scope(settings) as session:
        artifact = ArtifactManager(session, settings).get_by_slug(slug)
        terms = [t.term for t in GlossaryManager(session, settings).list(artifact)]
        return artifact.glossary_built_at, terms


def test_glossary_drops_terms_not_on_the_page_and_merges_duplicates(
    settings: Settings, make_source, make_artifact, replies
) -> None:
    make_source()
    slug = make_artifact()
    proposal = {"terms": [
        SGD,
        GD,
        term("Stochastic gradient descent"),
        term("sgd", "acronym", definition="Duplicate, written differently."),
        term("Adam", "acronym", definition="Not used on this page."),
    ]}
    assert generate(settings, slug, replies(glossary=proposal)) == "ready"
    built, terms = stored_glossary(settings, slug)
    assert built is not None
    assert terms == ["SGD", "gradient descent", "Stochastic gradient descent"]
    record = json.loads((settings.artifacts_dir / slug / "generation" / "glossary.json").read_text())
    assert record["proposed"] == 4  # SGD (with its duplicate), the two descents, Adam
    assert record["dropped"] == [
        {"term": "Adam", "reason": "does not occur in the study page text"},
    ]
    assert record["merged"] == [{"term": "sgd", "into": "SGD"}]
    assert record["dropped_aliases"] == [
        {"term": "SGD", "alias": "sgd", "reason": "does not occur in the study page text"},
    ]
    assert [t["term"] for t in record["output"]["terms"]] == terms


def test_glossary_with_no_terms_on_a_short_page_is_built_and_empty(
    settings: Settings, make_source, make_artifact, replies
) -> None:
    make_source()
    slug = make_artifact()
    assert generate(settings, slug, replies(OUTLINE, ONE_SECTION_PAGE, {"terms": []})) == "ready"
    built, terms = stored_glossary(settings, slug)
    assert built is not None and terms == []  # built, with no terms: not "never built"


def test_glossary_terms_found_only_in_private_parts_are_kept(
    settings: Settings, make_source, make_artifact, replies
) -> None:
    make_source()
    slug = make_artifact()
    misreading = term("misreading", definition="Reading something wrongly.")
    assert generate(settings, slug, replies(glossary={"terms": [SGD, misreading]})) == "ready"
    assert stored_glossary(settings, slug)[1] == ["SGD", "misreading"]


LONG_PAGE = page(
    prose(" ".join(["The method fits a model to the data and checks it."] * 20)),
    *PAGE["blocks"][1:],
)


@pytest.mark.parametrize(
    ("case", "page_", "glossary", "fragments"),
    [
        ("too many dropped", PAGE, {"terms": [SGD, term("Adam", "acronym"), term("ResNet")]},
         ("2 of the 3 terms Claude proposed had to be dropped, more than a third",
          "'Adam' (does not occur in the study page text)", "'ResNet'")),
        ("blank definitions count", PAGE,
         {"terms": [SGD, GD, term("baseline"), {**GD, "term": "two steps", "definition": " "}]},
         ("2 of the 4 terms", "'two steps' (the definition is blank)")),
        ("no terms for a long page", LONG_PAGE, {"terms": []},
         ("Claude proposed no usable terms for a page with", "words of text")),
        ("invalid json", PAGE, None, ("Claude's glossary reply is not a valid WireGlossary",)),
        ("wrong kind", PAGE, {"terms": [{**SGD, "kind": "word"}]},
         ("not a valid WireGlossary", "terms.0.kind")),
    ],
)
def test_glossary_failures_fail_the_generation(
    settings: Settings, make_source, make_artifact, replies, fake_anthropic,
    case: str, page_: dict, glossary: Any, fragments: tuple[str, ...],
) -> None:
    make_source()
    slug = make_artifact()
    if glossary is None:
        glossary = fake_anthropic.reply(text='{"terms": [', input_tokens=7, output_tokens=3)
    assert generate(settings, slug, replies(OUTLINE, page_, glossary)) == "failed"
    last = assert_failed(settings, slug, "glossary", "The glossary could not be built: ",
                         *fragments)
    assert [r["status"] for r in runs(settings, slug)] == ["succeeded"] * 10 + ["failed"]
    assert last["input"] is not None  # the reply's tokens are recorded on the failed run
    assert stored_glossary(settings, slug) == (None, [])
    assert not (settings.artifacts_dir / slug / "generation" / "glossary.json").exists()


@pytest.mark.parametrize(
    ("make_outcome", "fragments"),
    [
        (lambda f: f.status_error(anthropic.OverloadedError, 529, "overloaded_error"),
         ("the glossary call to Claude failed", "HTTP 529", "overloaded")),
        (lambda f: f.reply(text="", stop_reason="refusal", input_tokens=9, output_tokens=1,
                           stop_details={"type": "refusal", "category": "bio",
                                         "explanation": None}),
         ("Claude declined the glossary step", "category bio", "no glossary was built")),
        (lambda f: f.reply(text='{"terms": [', stop_reason="max_tokens"),
         ("glossary reply was cut off at max_tokens=32000",)),
        (lambda f: f.timeout(), ("did not answer within",)),
    ],
)
def test_glossary_call_failures_fail_the_generation(
    settings: Settings, make_source, make_artifact, replies, fake_anthropic,
    make_outcome: Callable[[Any], Any], fragments: tuple[str, ...],
) -> None:
    make_source()
    slug = make_artifact()
    fake = replies(OUTLINE, PAGE, make_outcome(fake_anthropic))
    assert generate(settings, slug, fake) == "failed"
    assert_failed(settings, slug, "glossary", "The glossary could not be built: ", *fragments)
    assert stored_glossary(settings, slug) == (None, [])
    assert fake.outcomes == []


def test_generator_glossary_builder_uses_the_generator_model_and_client(fake_anthropic) -> None:
    fake = fake_anthropic()
    generator = ClaudeGenerator(fake, model="claude-test-writer", timeout=12.0)
    assert isinstance(generator.glossary, GlossaryBuilder)
    assert (generator.glossary.model, generator.glossary.timeout) == ("claude-test-writer", 12.0)
    assert generator.glossary._client is None  # the stage passes the generator's client


def test_glossary_schema_is_api_compatible() -> None:
    assert GLOSSARY_FORMAT["type"] == "json_schema"
    schema = GLOSSARY_FORMAT["schema"]
    item = schema["$defs"]["WireGlossaryTerm"]
    assert item["additionalProperties"] is False
    assert set(item["required"]) == {"term", "kind", "expansion", "definition", "aliases"}
    assert item["properties"]["kind"]["enum"] == ["acronym", "term", "symbol"]
    WireGlossary.model_validate(GLOSSARY)


# -- the concepts stage and the preflight ---------------------------------------------------

ATTENTION = {"name": "attention", "aliases": [], "summary": "Weights over inputs.",
             "role": "uses", "evidence": "the method runs"}


def test_concept_stage_shares_the_cached_document_prefix(
    settings: Settings, make_source, make_artifact, replies, fake_anthropic, fake_embedder
) -> None:
    """With the concept model equal to the artifact's model, the extraction request repeats
    the page stages' prefix up to the cache breakpoint byte for byte, and runs after verify,
    before glossary."""
    from teacher.concept_extraction import ConceptExtractor

    make_source()
    slug = make_artifact()
    page_fake = replies()
    concept_fake = fake_anthropic(fake_anthropic.concepts_reply([ATTENTION]))
    extractor = ConceptExtractor(concept_fake, embedder=fake_embedder(),
                                 model="claude-sonnet-5-5", timeout=99.0)
    assert generate(settings, slug, page_fake, timeout=99.0, concept_extractor=extractor) == "ready"
    (concepts_call,) = concept_fake.calls
    analyse_call = page_fake.calls[0]

    def prefix(call: dict) -> str:
        return json.dumps([call["model"], call["system"], call["messages"][0]["content"][0],
                           call["output_config"]["effort"], call["timeout"]])

    assert prefix(concepts_call) == prefix(analyse_call)
    assert concepts_call["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert [r["stage"] for r in runs(settings, slug)] == CLEAN_STAGES
    record = json.loads(
        (settings.artifacts_dir / slug / "generation" / "concepts.json").read_text()
    )
    assert record["trigger"] == "generation"
    assert record["output"]["links"][0]["slug"] == "attention"


def test_a_failed_concept_stage_stops_before_the_glossary(
    settings: Settings, make_source, make_artifact, replies, fake_anthropic, fake_embedder
) -> None:
    from teacher.concept_extraction import ConceptExtractor

    make_source()
    slug = make_artifact()
    page_fake = replies(glossary=None)  # the glossary call must not happen
    concept_fake = fake_anthropic(
        fake_anthropic.status_error(anthropic.OverloadedError, 529, "overloaded_error")
    )
    extractor = ConceptExtractor(concept_fake, embedder=fake_embedder())
    assert generate(settings, slug, page_fake, concept_extractor=extractor) == "failed"
    last = runs(settings, slug)[-1]
    assert (last["stage"], last["status"]) == ("concepts", "failed")
    assert last["error"].startswith(
        "ConceptsUnavailableError: Concepts could not be extracted: the concepts call to Claude "
        "failed"
    )
    # analyse, write, review and the question check's six calls: no glossary call
    assert len(page_fake.calls) == 9 and page_fake.outcomes == []
    assert stored_glossary(settings, slug) == (None, [])
    with session_scope(settings) as session:
        artifact = ArtifactManager(session, settings).get_by_slug(slug)
        assert ArtifactBlockManager(session, settings).list(artifact) == []


def test_preflight_names_every_missing_key_before_any_call(
    settings: Settings, make_source, make_artifact, fake_anthropic, forbid_real_messages_api: None
) -> None:
    from teacher.concept_extraction import ConceptExtractor
    from teacher.embeddings import OpenAIEmbedder

    make_source()
    slug = make_artifact()
    page_fake = fake_anthropic()
    extractor = ConceptExtractor(fake_anthropic(), embedder=OpenAIEmbedder())  # no OPENAI key
    assert generate(settings, slug, page_fake, concept_extractor=extractor) == "failed"
    last = assert_failed(settings, slug, "preflight", "Concepts could not be extracted: ",
                         "no OpenAI API key was found", "OPENAI_API_KEY")
    assert [r["stage"] for r in runs(settings, slug)] == ["preflight"]
    assert page_fake.calls == [] and extractor._client.calls == []

    # All three clients (generator, concepts, block tagger) without credentials, too: every
    # problem is named in one message.
    other = make_artifact("other")
    generator = ClaudeGenerator(concept_extractor=ConceptExtractor(embedder=OpenAIEmbedder()))
    assert run_generation(settings, other, generator) == "failed"
    last = runs(settings, other)[-1]
    assert last["error"].count("Anthropic credentials could not be loaded") == 3
    assert "Block concepts could not be tagged: " in last["error"]
    assert "no OpenAI API key was found" in last["error"]


def test_default_concept_extractor_of_the_generator() -> None:
    from teacher.concept_extraction import ConceptExtractor
    from teacher.config import DEFAULT_CONCEPT_MODEL, DEFAULT_EMBEDDING_MODEL
    from teacher.embeddings import OpenAIEmbedder

    generator = ClaudeGenerator(timeout=12.0)
    extractor = generator.concept_extractor
    assert isinstance(extractor, ConceptExtractor) and extractor._client is None
    assert (extractor.model, extractor.timeout) == (DEFAULT_CONCEPT_MODEL, 12.0)
    assert DEFAULT_CONCEPT_MODEL == "claude-sonnet-5-5" == generator.model
    assert isinstance(extractor.embedder, OpenAIEmbedder)
    assert extractor.embedder.model == DEFAULT_EMBEDDING_MODEL


def test_block_concepts_stage_tags_the_verified_page(
    settings: Settings, make_source, make_artifact, replies, fake_anthropic
) -> None:
    """After concepts: the tagger (its own client and model) tags the verified drafts with the
    source's concepts; the record is saved and the tags are stored with the blocks."""
    from teacher.block_tagging import BlockTagger
    from teacher.managers import BlockConceptManager, ConceptManager, LinkSpec, SourceConceptManager

    make_source()
    slug = make_artifact()
    with session_scope(settings) as session:
        source = KnowledgeSourceManager(session, settings).get_by_slug("paper")
        sgd = ConceptManager(session, settings).create(
            "stochastic gradient descent", "Follows sampled gradients.", "paper"
        )
        SourceConceptManager(session, settings).replace(
            source, [LinkSpec(sgd, "introduces", "created", "SGD", evidence="SGD")]
        )
        sgd_id = sgd.id
    body = {"blocks": [
        {"block": n, "concepts": [{"concept": sgd_id, "role": "main", "rationale": "It explains SGD."}]
         if n == 5 else []}
        for n in range(1, len(PAGE["blocks"]) + 1)
    ]}
    tag_fake = fake_anthropic(fake_anthropic.reply(text=json.dumps(body), model="claude-test-concepts",
                                                   input_tokens=77, output_tokens=8))
    tagger = BlockTagger(tag_fake, model="claude-test-concepts")
    assert generate(settings, slug, replies(), block_tagger=tagger) == "ready"
    stage = runs(settings, slug)[CLEAN_STAGES.index("block_concepts")]
    assert (stage["stage"], stage["status"], stage["model"], stage["input"], stage["output"]) == (
        "block_concepts", "succeeded", "claude-test-concepts", 77, 8
    )
    assert stage["file"] == f"artifacts/{slug}/generation/block_concepts.json"
    record = json.loads((settings.data_root / stage["file"]).read_text(encoding="utf-8"))
    assert record["trigger"] == "generation" and record["output"]["counts"]["tags"] == 1
    with session_scope(settings) as session:
        artifact = ArtifactManager(session, settings).get_by_slug(slug)
        assert artifact.block_concepts_tagged_at is not None
        assert [(b.key, c.slug, t.role) for b, t, c in
                BlockConceptManager(session, settings).for_artifact(artifact)] == [
            ("b05", "stochastic-gradient-descent", "main"),
        ]
    (call,) = tag_fake.calls
    assert "stochastic gradient descent (SGD) in two steps" in call["messages"][0]["content"][0]["text"]


def test_a_source_without_concepts_fails_the_block_concepts_stage(
    settings: Settings, make_source, make_artifact, replies, fake_anthropic
) -> None:
    """The fail-loudly precedent of `concepts`: a tagging failure fails the page, before the
    glossary call, and nothing of the page is stored."""
    from teacher.block_tagging import BlockTagger

    make_source()
    slug = make_artifact()
    page_fake = replies(glossary=None)  # the glossary call must not happen
    tag_fake = fake_anthropic()
    assert generate(settings, slug, page_fake, block_tagger=BlockTagger(tag_fake)) == "failed"
    last = runs(settings, slug)[-1]  # like the concepts stage, its own error class is recorded
    assert (last["stage"], last["status"]) == ("block_concepts", "failed")
    assert last["error"] == (
        "TaggingInputError: Block concepts could not be tagged: no concepts are linked to "
        "'paper', so there is nothing to tag the blocks with"
    )
    assert tag_fake.calls == [] and page_fake.outcomes == []
    with session_scope(settings) as session:
        artifact = ArtifactManager(session, settings).get_by_slug(slug)
        assert ArtifactBlockManager(session, settings).list(artifact) == []


def test_line_chart_with_a_category_for_x_is_rejected() -> None:
    """A chart's x is text on the wire; a line chart's must read as a number."""
    points = [{"x": "January", "y": 0.5}, {"x": "2", "y": 3}]
    line = chart(kind="line", series=[{"name": "Loss", "points": points}])
    with pytest.raises(ValidationError, match="a line chart needs numeric x values.*'January'"):
        StudyPage.model_validate(page(line))
    bar = chart(kind="bar", series=[{"name": "Loss", "points": points}])
    assert StudyPage.model_validate(page(bar)).blocks[0].series[0].points[0].x == "January"


def test_a_charts_series_are_json_text_on_the_wire() -> None:
    points = [{"x": "1", "y": 0.5}, {"x": "2", "y": 3.0}]
    text = json.dumps([{"name": "Loss", "points": points}])
    block = StudyPage.model_validate(page(chart(kind="line") | {"series": text})).blocks[0]
    assert [(p.x, p.y) for p in block.series[0].points] == [(1.0, 0.5), (2.0, 3.0)]
    # Shown back to the model in the same form it writes.
    assert json.loads(block.model_dump()["series"]) == [
        {"name": "Loss", "points": [{"x": 1.0, "y": 0.5}, {"x": 2.0, "y": 3.0}]}
    ]
    for bad, message in (("[{", "does not parse"), ('[{"name": "Loss"}]', "points")):
        with pytest.raises(ValidationError, match=message):
            StudyPage.model_validate(page(chart(kind="line") | {"series": bad}))
