"""Graders: pass/fail decisions, feedback, and rejection of malformed responses."""

from __future__ import annotations

from typing import Any

import pytest

from teacher.blocks import validate_block_content
from teacher.grading import (
    NO_OPTION_FEEDBACK,
    PREDICTION_FEEDBACK,
    Grader,
    GradeResult,
    MultipleChoiceGrader,
    PredictionGrader,
    WordCountBlurbGrader,
    default_graders,
    offline_graders,
    verdict_for,
)
from teacher.judge import LLMJudgeGrader

MC = validate_block_content(
    "multiple_choice",
    {
        "question": "Which?",
        "options": ["a", "b", "c"],
        "private": {"correct_index": 2, "explanation": "Because c."},
    },
)
BLURB = validate_block_content(
    "blurb",
    {
        "prompt": "Explain.",
        "min_words": 3,
        "private": {"model_answer": "M", "rubric": [{"criterion": "R"}]},
    },
)


MC_EXPLAINED = validate_block_content(
    "multiple_choice",
    {
        "question": "Which?",
        "options": ["a", "b", "c"],
        "private": {
            "correct_index": 2,
            "explanation": "Because c.",
            "option_explanations": ["A is too small.", "B ignores the cost.", "C fits."],
        },
    },
)
PREDICTION = validate_block_content(
    "prediction",
    {
        "prompt": "What happens?",
        "options": ["up", "down"],
        "private": {"correct_index": 1, "resolution": "It goes down."},
    },
)


def gate_types() -> set[str]:
    from teacher.blocks import BLOCK_TYPES

    return {name for name, entry in BLOCK_TYPES.items() if entry.is_gate}


def test_default_graders_cover_every_gate_type() -> None:
    graders = default_graders()
    assert set(graders) == gate_types() == {"multiple_choice", "blurb", "prediction"}
    assert isinstance(graders["multiple_choice"], MultipleChoiceGrader)
    assert isinstance(graders["prediction"], PredictionGrader)
    assert isinstance(graders["blurb"], LLMJudgeGrader)  # never the word-count placeholder
    assert all(isinstance(g, Grader) for g in graders.values())
    assert default_graders() is not default_graders()


def test_offline_graders_cover_every_gate_type_without_the_network() -> None:
    graders = offline_graders()
    assert set(graders) == gate_types()
    assert isinstance(graders["blurb"], WordCountBlurbGrader)


def test_grade_result_verdicts() -> None:
    assert GradeResult("passed", None, "g").passed is True
    assert GradeResult("failed", None, "g").passed is False
    partial = GradeResult("partial", "f", "g", missing=("names the cost",))
    assert partial.passed is False and partial.missing == ("names the cost",)
    assert verdict_for(True) == "passed" and verdict_for(False) == "failed"
    for verdict, missing, fragment in [
        ("ok", (), "verdict must be one of"),
        ("partial", (), "a partial verdict lists what is missing"),
        ("failed", ("x",), "a partial verdict lists what is missing"),
        ("passed", ("x",), "a partial verdict lists what is missing"),
        ("partial", (" ",), "non-blank strings"),
        ("partial", ["x"], "tuple"),
    ]:
        with pytest.raises(ValueError, match=fragment):
            GradeResult(verdict, None, "g", missing=missing)  # type: ignore[arg-type]


def test_multiple_choice_correct() -> None:
    result = MultipleChoiceGrader().grade(MC, {"choice": 2})
    assert result == GradeResult("passed", feedback="Because c.", grader="multiple-choice-v2")
    assert result.details is None and result.missing == ()
    explained = MultipleChoiceGrader().grade(MC_EXPLAINED, {"choice": 2})
    assert explained.feedback == "Because c."  # the explanation, not the option's entry


def test_multiple_choice_wrong_does_not_reveal_explanation() -> None:
    result = MultipleChoiceGrader().grade(MC, {"choice": 0})
    assert result.passed is False and result.verdict == "failed"
    assert result.grader == "multiple-choice-v2"
    assert "Because c." not in (result.feedback or "")


def test_multiple_choice_wrong_gets_the_chosen_options_explanation() -> None:
    for choice, text in [(0, "A is too small."), (1, "B ignores the cost.")]:
        result = MultipleChoiceGrader().grade(MC_EXPLAINED, {"choice": choice})
        assert result.verdict == "failed" and result.missing == ()
        assert result.feedback == f"Not quite. {text}"
        assert "Because c." not in result.feedback and "C fits." not in result.feedback


def test_multiple_choice_wrong_on_an_old_page_says_why_there_is_no_explanation() -> None:
    result = MultipleChoiceGrader().grade(MC, {"choice": 1})
    assert result.feedback == NO_OPTION_FEEDBACK
    assert "generated before per-option feedback" in NO_OPTION_FEEDBACK
    assert "regenerating the page adds" in NO_OPTION_FEEDBACK


@pytest.mark.parametrize(("choice", "correct"), [(0, False), (1, True)])
def test_prediction_passes_every_choice(choice: int, correct: bool) -> None:
    result = PredictionGrader().grade(PREDICTION, {"choice": choice})
    assert result == GradeResult(
        "passed", PREDICTION_FEEDBACK, "prediction-v1", details={"correct": correct}
    )
    assert "down" not in PREDICTION_FEEDBACK.lower().split()  # neutral, gives nothing away


@pytest.mark.parametrize("response", [{}, {"choice": 2}, {"choice": True}, {"text": "x"}, [0]])
def test_prediction_malformed(response: Any) -> None:
    with pytest.raises(ValueError):
        PredictionGrader().grade(PREDICTION, response)


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"choice": "2"},
        {"choice": 2.0},
        {"choice": True},
        {"choice": None},
        {"choice": 3},
        {"choice": -1},
        {"choice": 1, "extra": 1},
        {"text": "x"},
        [2],
        "2",
    ],
)
def test_multiple_choice_malformed(response: Any) -> None:
    with pytest.raises(ValueError):
        MultipleChoiceGrader().grade(MC, response)


@pytest.mark.parametrize(
    ("text", "passed"),
    [
        ("one two three", True),
        ("  one\ttwo\nthree four ", True),
        ("one two", False),
        ("", False),
    ],
)
def test_blurb_word_count(text: str, passed: bool) -> None:
    result = WordCountBlurbGrader().grade(BLURB, {"text": text})
    assert result.passed is passed
    assert result.grader == "word-count-v1"
    assert result.feedback
    assert "M" not in result.feedback.split()


def test_blurb_min_words_defaults_to_20() -> None:
    content = {"prompt": "p", "private": {"model_answer": "m", "rubric": [{"criterion": "c"}]}}
    grader = WordCountBlurbGrader()
    assert grader.grade(content, {"text": "w " * 19}).passed is False
    assert grader.grade(content, {"text": "w " * 20}).passed is True


@pytest.mark.parametrize(
    "response", [{}, {"text": 5}, {"text": None}, {"text": "a", "more": 1}, {"choice": 1}, "text"]
)
def test_blurb_malformed(response: Any) -> None:
    with pytest.raises(ValueError):
        WordCountBlurbGrader().grade(BLURB, response)
