"""The review bank (`teacher.review_bank`): the quality rules, the Claude builder on a fake
client (and every way it fails loudly), storing a build as a run, and the bank's status."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

import anthropic
import pytest
from sqlalchemy import select

from teacher.config import Settings
from teacher.db.engine import session_scope
from teacher.db.models import GenerationRun, ReviewVariant, utcnow
from teacher.managers import ArtifactBlockManager, ArtifactManager, GenerationRunManager
from teacher.review_bank import (
    CORRECT_LENGTH_RATIO,
    REVIEW_BANK_STAGE,
    SYSTEM_PROMPT,
    BankQuestion,
    BankVariant,
    ClaudeReviewBankBuilder,
    ReviewBankBusyError,
    ReviewBankInputError,
    ReviewBankOutputError,
    ReviewBankUnavailableError,
    StubReviewBankBuilder,
    bank_status,
    build_after_generation,
    build_review_bank,
    check_bank,
    correct_option_violation,
    rubric_violation,
)

# The components here make their API calls directly: an in-memory cost meter records them
# (without a meter a component refuses to make a call; see `teacher.costs`).
pytestmark = pytest.mark.usefixtures("cost_meter")

MC = {
    "question": "Which colour does the filter pass?",
    "options": ["red light", "blue light", "green light"],
    "private": {
        "correct_index": 1,
        "explanation": "The filter passes blue light.",
        "option_explanations": ["Red is absorbed.", "Blue passes.", "Green is absorbed."],
    },
}
BLURB = {
    "prompt": "Explain why the filter looks blue.",
    "min_words": 12,
    "private": {
        "model_answer": "It passes blue and absorbs the rest.",
        "rubric": [
            {"criterion": "Says it passes blue", "required": True},
            {"criterion": "Says it absorbs other colours", "required": True},
            {"criterion": "Mentions wavelengths", "required": False},
        ],
    },
}
PREDICTION = {
    "prompt": "Guess first.",
    "options": ["a", "b"],
    "private": {"correct_index": 0, "resolution": "It is a."},
}
QUESTIONS = [BankQuestion(1, 11, "b02", "multiple_choice", MC), BankQuestion(2, 12, "b04", "blurb", BLURB)]


def mc_variant(number: int = 1, question: str = "A lamp shines through the filter: what comes out?",
               options: list[str] | None = None, correct: int = 0) -> dict[str, Any]:
    options = options or ["blue light", "white light", "no light"]
    return {
        "question_number": number,
        "question": question,
        "options": options,
        "correct_index": correct,
        "explanation": "Only blue passes.",
        "option_explanations": [f"Why {o}." for o in options],
    }


def blurb_variant(number: int = 2, prompt: str = "Why does a white wall look blue through it?",
                  required: int = 2) -> dict[str, Any]:
    return {
        "question_number": number,
        "prompt": prompt,
        "model_answer": "Only blue reaches the eye.",
        "rubric": [{"criterion": f"Criterion {i}", "required": True} for i in range(required)]
        + [{"criterion": "Extra", "required": False}],
    }


def good_bank() -> dict[str, Any]:
    return {
        "multiple_choice": [
            mc_variant(question="A lamp shines through the filter: what comes out?"),
            mc_variant(question="Which light is absorbed least?", options=["red", "blue", "green"], correct=1),
        ],
        "blurb": [blurb_variant(), blurb_variant(prompt="Describe what the filter does to sunlight.")],
    }


# -- the rules --------------------------------------------------------------------------------


def test_the_correct_option_must_not_be_clearly_longer() -> None:
    assert CORRECT_LENGTH_RATIO == 1.3
    assert correct_option_violation(["abcdefghijklm", "abcdefghij"], 0) is None  # 13 = 1.3 * 10
    why = correct_option_violation(["abcdefghijklmn", "abcdefghij", "abc"], 0)
    assert why is not None and "14 characters" in why and "longest distractor (10)" in why
    assert correct_option_violation(["  short  ", "longer one"], 0) is None


def test_a_rubric_has_one_or_two_required_criteria() -> None:
    assert rubric_violation([{"criterion": "a", "required": True}]) is None
    assert rubric_violation([{"criterion": "a"}, {"criterion": "b", "required": True}]) is None
    assert "3 required" in rubric_violation([{"criterion": c} for c in "abc"])  # type: ignore[operator]
    assert "0 required" in rubric_violation([{"criterion": "a", "required": False}])  # type: ignore[operator]


def test_check_bank_lists_every_problem() -> None:
    def variants(bank: dict[str, Any]) -> list[BankVariant]:
        from teacher.review_bank import WireBank, variants_from_wire

        return variants_from_wire(QUESTIONS, WireBank.model_validate(bank))

    checked = check_bank(QUESTIONS, variants(good_bank()))
    assert [(v.number, v.type) for v in checked] == [(1, "multiple_choice"), (1, "multiple_choice"), (2, "blurb"), (2, "blurb")]
    assert checked[2].content["min_words"] == 12  # a blurb variant keeps its question's

    bad = good_bank()
    bad["multiple_choice"][0]["options"] = ["blue light, which the dye lets through", "red", "green"]
    bad["multiple_choice"][1]["question"] = "  which colour does the filter PASS? "  # the original
    bad["blurb"][0] = blurb_variant(required=3)
    bad["blurb"].append(blurb_variant(number=1))  # a blurb for the multiple-choice question
    bad["multiple_choice"].append(mc_variant(number=9))
    with pytest.raises(ReviewBankOutputError) as info:
        check_bank(QUESTIONS, variants(bad))
    message = str(info.value)
    assert "question 9, which does not exist" in message
    assert "question 1 is a multiple_choice, but a variant of it is a blurb" in message
    assert "question 1 (b02), variant 1: the correct option is" in message
    assert "question 1 (b02), variant 2: it asks the same question" in message
    assert "question 2 (b04), variant 1: the rubric has 3 required criteria" in message

    short = good_bank()
    short["blurb"].pop()
    short["multiple_choice"][0]["option_explanations"] = ["only one"]
    with pytest.raises(ReviewBankOutputError) as info:
        check_bank(QUESTIONS, variants(short))
    assert "question 2 (b04) has 1 variants; exactly 2 are needed" in str(info.value)
    assert "option_explanations has 1 entries for 3 options" in str(info.value)


# -- the Claude builder -----------------------------------------------------------------------


def test_the_builder_sends_the_questions_with_their_answers(fake_anthropic: Any) -> None:
    client = fake_anthropic(fake_anthropic.reply(text=json.dumps(good_bank()), model="claude-test",
                                                 input_tokens=100, output_tokens=50))
    build = ClaudeReviewBankBuilder(client).build(QUESTIONS, model="claude-sonnet-5-5")
    assert (len(build.variants), build.model, build.input_tokens, build.output_tokens) == (4, "claude-test", 100, 50)
    call = client.calls[0]
    assert call["model"] == "claude-sonnet-5-5"
    assert call["system"] == SYSTEM_PROMPT
    assert call["output_config"]["format"]["type"] == "json_schema"
    prompt = client.prompt
    assert '<question number="1" type="multiple_choice">' in prompt
    assert "1. blue light (correct)" in prompt and "Blue passes." in prompt
    assert "Model answer: It passes blue and absorbs the rest." in prompt
    assert "(optional) Mentions wavelengths" in prompt
    assert "exactly 2 variants for each of the 2 questions" in prompt


@pytest.mark.parametrize(
    ("reply", "error", "match"),
    [
        (dict(text="not json"), ReviewBankOutputError, "does not match the schema"),
        (dict(text=json.dumps({"multiple_choice": []})), ReviewBankOutputError, "does not match the schema"),
        (dict(text=json.dumps(good_bank()), stop_reason="refusal"), ReviewBankOutputError, "declined"),
        (dict(text="{", stop_reason="max_tokens"), ReviewBankOutputError, "cut off"),
        (dict(text="{}", stop_reason="pause_turn"), ReviewBankOutputError, "stopped unexpectedly"),
    ],
)
def test_an_unusable_reply_fails_loudly(fake_anthropic: Any, reply: dict, error: type, match: str) -> None:
    client = fake_anthropic(fake_anthropic.reply(**reply))
    with pytest.raises(error, match=match) as info:
        ClaudeReviewBankBuilder(client).build(QUESTIONS, model="m")
    assert info.value.tokens == (321, 54)  # the reply's tokens go to the failed run


def test_rule_violations_fail_the_build(fake_anthropic: Any) -> None:
    bank = good_bank()
    bank["blurb"][1] = blurb_variant(required=3)
    client = fake_anthropic(fake_anthropic.reply(text=json.dumps(bank)))
    with pytest.raises(ReviewBankOutputError, match="3 required criteria") as info:
        ClaudeReviewBankBuilder(client).build(QUESTIONS, model="m")
    assert info.value.tokens == (321, 54)


def test_api_failures_and_missing_credentials_are_unavailable(fake_anthropic: Any) -> None:
    client = fake_anthropic(fake_anthropic.status_error(anthropic.RateLimitError, 429, "rate_limit_error"))
    with pytest.raises(ReviewBankUnavailableError, match="rate limit"):
        ClaudeReviewBankBuilder(client).build(QUESTIONS, model="m")
    with pytest.raises(ReviewBankUnavailableError, match="credentials"):
        ClaudeReviewBankBuilder().check_available()
    with pytest.raises(ReviewBankInputError, match="no questions"):
        ClaudeReviewBankBuilder(client).build([], model="m")


def test_the_stub_builder_rotates_options_and_goes_through_the_checks() -> None:
    build = StubReviewBankBuilder().build(QUESTIONS[:1], model="none")
    first, second = build.variants
    assert first.content["question"] == "(Review form 1) Which colour does the filter pass?"
    assert first.content["options"] == ["blue light", "green light", "red light"]
    assert first.content["private"]["correct_index"] == 0
    assert first.content["private"]["option_explanations"][0] == "Blue passes."
    assert second.content["options"][second.content["private"]["correct_index"]] == "blue light"
    too_many = {**BLURB, "private": {**BLURB["private"], "rubric": [{"criterion": c} for c in "abc"]}}
    with pytest.raises(ReviewBankOutputError, match="3 required criteria"):
        StubReviewBankBuilder().build([BankQuestion(1, 1, "b01", "blurb", too_many)], model="none")


# -- storing a build --------------------------------------------------------------------------


@pytest.fixture
def page(settings: Settings) -> str:
    with session_scope(settings) as session:
        artifact = ArtifactManager(session, settings).create(
            slug="filters", kind="study_page", title="Filters", generator="test"
        )
        blocks = ArtifactBlockManager(session, settings)
        blocks.add(artifact, type="prose", content={"markdown": "# Filters"})
        blocks.add(artifact, type="multiple_choice", content=MC, is_gate=True)
        blocks.add(artifact, type="prediction", content=PREDICTION, is_gate=True)
        blocks.add(artifact, type="blurb", content=BLURB, is_gate=True)
        ArtifactManager(session, settings).mark_ready(artifact)
    return "filters"


def status(settings: Settings, slug: str = "filters", now: datetime | None = None) -> dict[str, Any]:
    with session_scope(settings) as session:
        artifact = ArtifactManager(session, settings).get_by_slug(slug)
        return bank_status(session, settings, artifact, now=now or utcnow())


def test_a_build_is_a_run_and_replaces_the_variants(settings: Settings, page: str, fake_anthropic: Any) -> None:
    assert status(settings)["status"] == "not_built"
    assert "original questions only" in status(settings)["reason"]
    client = fake_anthropic(fake_anthropic.reply(text=json.dumps(good_bank()), input_tokens=10, output_tokens=5))
    outcome = build_review_bank(settings, page, ClaudeReviewBankBuilder(client), model="claude-x",
                                trigger="on_demand")
    assert (outcome.questions, outcome.variants) == (2, 4)  # the prediction is not a question
    with session_scope(settings) as session:
        artifact = ArtifactManager(session, settings).get_by_slug(page)
        run = GenerationRunManager(session, settings).latest(artifact, REVIEW_BANK_STAGE)
        assert (run.status, run.model, run.input_tokens, run.output_tokens) == ("succeeded", "claude-x", 10, 5)
        assert run.output_file == "artifacts/filters/generation/review_bank.json"
        record = json.loads((settings.data_root / run.output_file).read_text())
        assert record["trigger"] == "on_demand" and len(record["variants"]) == 4
        rows = session.scalars(select(ReviewVariant).order_by(ReviewVariant.id)).all()
        assert [(r.position, r.type, r.run_id) for r in rows] == [
            (1, "multiple_choice", run.id), (2, "multiple_choice", run.id), (1, "blurb", run.id), (2, "blurb", run.id),
        ]
    got = status(settings)
    assert (got["status"], got["variants"], got["questions_with_variants"]) == ("built", 4, 2)

    # a rebuild whose reply breaks the rules fails loudly, and the old bank and the page stay
    bad = good_bank()
    bad["multiple_choice"][0]["options"] = ["the blue part of the light passes", "red", "green"]
    client = fake_anthropic(fake_anthropic.reply(text=json.dumps(bad)))
    with pytest.raises(ReviewBankOutputError, match="correct option"):
        build_review_bank(settings, page, ClaudeReviewBankBuilder(client), model="claude-x", trigger="on_demand")
    got = status(settings)
    assert (got["status"], got["variants"]) == ("failed", 4)
    assert "The last build of the variants failed: ReviewBankOutputError" in got["reason"]
    assert "are still used" in got["reason"]
    assert got["run"]["status"] == "failed"
    with session_scope(settings) as session:
        artifact = ArtifactManager(session, settings).get_by_slug(page)
        assert artifact.status == "ready"
        assert len(ArtifactBlockManager(session, settings).list(artifact)) == 4
        runs = GenerationRunManager(session, settings).list(artifact)
        assert [(r.stage, r.status) for r in runs] == [(REVIEW_BANK_STAGE, "succeeded"), (REVIEW_BANK_STAGE, "failed")]
        assert runs[1].input_tokens == 321


def test_missing_credentials_are_recorded_on_the_run(settings: Settings, page: str) -> None:
    with pytest.raises(ReviewBankUnavailableError):
        build_review_bank(settings, page, ClaudeReviewBankBuilder(), model="m", trigger="on_demand")
    got = status(settings)
    assert got["status"] == "failed" and "credentials" in got["reason"]
    assert "original questions only" in got["reason"]


def test_busy_stale_and_pages_without_questions(settings: Settings, page: str) -> None:
    with session_scope(settings) as session:
        artifact = ArtifactManager(session, settings).get_by_slug(page)
        run = GenerationRunManager(session, settings).start(artifact, stage=REVIEW_BANK_STAGE, model="m")
        run_id = run.id
    assert status(settings)["status"] == "building"
    with pytest.raises(ReviewBankBusyError, match="already running"):
        build_review_bank(settings, page, StubReviewBankBuilder(), model="none", trigger="on_demand")
    later = utcnow() + timedelta(hours=1)
    assert status(settings, now=later)["status"] == "interrupted"
    with session_scope(settings) as session:  # make the running run an hour old
        session.get(GenerationRun, run_id).started_at = utcnow() - timedelta(hours=1)
    build_review_bank(settings, page, StubReviewBankBuilder(), model="none", trigger="on_demand")
    with session_scope(settings) as session:
        assert session.get(GenerationRun, run_id).status == "failed"
        assert "interrupted" in session.get(GenerationRun, run_id).error
    assert status(settings)["status"] == "built"

    with session_scope(settings) as session:
        empty = ArtifactManager(session, settings).create(
            slug="prose-only", kind="study_page", title="Prose", generator="test"
        )
        ArtifactBlockManager(session, settings).add(empty, type="prose", content={"markdown": "x"})
        ArtifactManager(session, settings).mark_ready(empty)
    with pytest.raises(ReviewBankInputError, match="no questions"):
        build_review_bank(settings, "prose-only", StubReviewBankBuilder(), model="none", trigger="on_demand")
    assert status(settings, "prose-only")["status"] == "no_questions"
    assert build_after_generation(settings, "prose-only", StubReviewBankBuilder(), object()) is None


def test_after_generation_skips_a_failed_page_and_records_failures(settings: Settings, page: str) -> None:
    from teacher.generation import StubGenerator

    with session_scope(settings) as session:
        broken = ArtifactManager(session, settings).create(
            slug="broken", kind="study_page", title="Broken", generator="test"
        )
        ArtifactManager(session, settings).mark_failed(broken)
    assert build_after_generation(settings, "broken", StubReviewBankBuilder(), StubGenerator()) is None
    assert build_after_generation(settings, "gone", StubReviewBankBuilder(), StubGenerator()) is None
    assert build_after_generation(settings, page, ClaudeReviewBankBuilder(), StubGenerator()) == "failed"
    assert status(settings)["status"] == "failed"
    assert build_after_generation(settings, page, StubReviewBankBuilder(), StubGenerator()) == "built"
