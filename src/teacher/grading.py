"""Graders: decide whether a reader's response to a gate block passes.

A grader receives the block's FULL content (including the `private` part) and the reader's
response dict, and returns a `GradeResult`. A malformed response raises `ValueError` (the API
turns that into a 422 and records nothing). A grader that cannot reach a verdict (e.g. the
grading model is unreachable or misconfigured) raises `GradingUnavailableError` (the API turns
that into a 503 and records nothing); it never falls back to another grader.

`verdict` is `"passed"`, `"partial"` or `"failed"`; `passed` is true only for `"passed"`, and
only a passed attempt clears the gate (a partial one does not). `"partial"` is a written answer
that meets at least one but not all of its required criteria; `missing` then lists the unmet
required criteria word for word, and is empty for every other verdict.

Contract for `feedback` and `missing`: they are shown to the reader right away and stored with
the attempt, so a FAILED or PARTIAL attempt's must not reveal private content beyond what each
grader below documents: a wrong multiple-choice answer is told why the chosen option is wrong
(its `option_explanations` entry, written not to give the answer away), and a partial blurb is
told which required criteria it missed, never the model answer. Private content becomes
visible through the gate's `revealed` field once the gate is cleared.

`details` is the grader's private verdict (for the LLM judge: per-criterion judgements, which
reveal the rubric). It is stored with the attempt and only shown once the gate is cleared.

A `prediction` is a gate but not a question: `PredictionGrader` passes every choice, so it only
records the reader's guess (and in `details` whether it matched).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol, get_args, runtime_checkable

DEFAULT_MIN_WORDS = 20

Verdict = Literal["passed", "partial", "failed"]
VERDICTS: tuple[str, ...] = get_args(Verdict)
PASSED: Verdict = "passed"
PARTIAL: Verdict = "partial"
FAILED: Verdict = "failed"

# How sure the reader said they were before submitting an answer (shown as Guessing / Fairly
# sure / Certain). Recorded with the attempt; it does not change the grade.
Confidence = Literal["low", "medium", "high"]
CONFIDENCES: tuple[str, ...] = get_args(Confidence)


@dataclass(frozen=True)
class GradeResult:
    """A grader's decision on one attempt. `passed` is derived: true only for `"passed"`.

    `missing` is non-empty exactly for a `"partial"` verdict (ValueError otherwise)."""

    verdict: Verdict
    feedback: str | None
    grader: str
    details: dict[str, Any] | None = None
    missing: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.verdict not in VERDICTS:
            raise ValueError(f"verdict must be one of {list(VERDICTS)}, got {self.verdict!r}")
        if not isinstance(self.missing, tuple) or not all(
            isinstance(item, str) and item.strip() for item in self.missing
        ):
            raise ValueError("missing must be a tuple of non-blank strings")
        if (self.verdict == PARTIAL) != bool(self.missing):
            raise ValueError(
                "a partial verdict lists what is missing, and no other verdict does "
                f"(verdict {self.verdict!r}, missing {list(self.missing)!r})"
            )

    @property
    def passed(self) -> bool:
        return self.verdict == PASSED


def verdict_for(passed: bool) -> Verdict:
    """`"passed"` or `"failed"`, for a grader whose answers are only right or wrong."""
    return PASSED if passed else FAILED


class GradingUnavailableError(Exception):
    """The grader could not reach a verdict (HTTP 503); nothing is recorded for the attempt.

    The message is shown to the user, so it must say what to fix and never contain a secret.
    """


@runtime_checkable
class Grader(Protocol):
    def grade(self, content: dict[str, Any], response: dict[str, Any]) -> GradeResult: ...


def _require_keys(response: Any, expected: set[str]) -> dict[str, Any]:
    if not isinstance(response, dict):
        raise ValueError(f"response must be an object, got {response.__class__.__name__}")
    missing = expected - set(response)
    if missing:
        raise ValueError(f"response is missing {', '.join(sorted(missing))}")
    unknown = set(response) - expected
    if unknown:
        raise ValueError(f"response has unknown key(s) {', '.join(sorted(unknown))}")
    return response


def _choice(content: dict[str, Any], response: dict[str, Any]) -> int:
    """The validated option index of a `{"choice": <int>}` response."""
    choice = _require_keys(response, {"choice"})["choice"]
    if isinstance(choice, bool) or not isinstance(choice, int):
        raise ValueError("choice must be an integer option index")
    options = content["options"]
    if not 0 <= choice < len(options):
        raise ValueError(f"choice must be between 0 and {len(options) - 1}, got {choice}")
    return choice


# What a wrong answer on a page stored before per-option feedback existed is told: it says so
# plainly instead of passing off a generic message as feedback.
NO_OPTION_FEEDBACK = (
    "Not quite. Try again. This page was generated before per-option feedback existed, so "
    "there is no explanation of why this option is wrong; regenerating the page adds one."
)


class MultipleChoiceGrader:
    """Response `{"choice": <option index>}`; passes when it equals the private `correct_index`.

    Feedback is the private `explanation` when the answer is correct. When it is wrong, it is
    the chosen option's `option_explanations` entry, which says why that option is wrong
    without giving the answer away; a block stored before those existed gets
    `NO_OPTION_FEEDBACK`, which says so.
    """

    name = "multiple-choice-v2"

    def grade(self, content: dict[str, Any], response: dict[str, Any]) -> GradeResult:
        choice = _choice(content, response)
        private = content["private"]
        if choice == private["correct_index"]:
            return GradeResult(PASSED, feedback=private.get("explanation"), grader=self.name)
        explanations = private.get("option_explanations")
        if explanations is None:
            return GradeResult(FAILED, feedback=NO_OPTION_FEEDBACK, grader=self.name)
        return GradeResult(
            FAILED, feedback=f"Not quite. {explanations[choice]}", grader=self.name
        )


PREDICTION_FEEDBACK = (
    "Your prediction is recorded. Read on: the next part shows what actually happens, and you "
    "can check your guess against it."
)


class PredictionGrader:
    """Response `{"choice": <option index>}` to a `prediction`; every choice passes.

    A prediction is not a test: the gate only makes the reader commit to a guess before the
    explanation. Feedback is neutral (`PREDICTION_FEEDBACK`); `details` is `{"correct": <bool>}`,
    whether the guess matched the private `correct_index`. The `resolution` reaches the reader
    through the gate's `revealed` content, like any cleared gate's private part.
    """

    name = "prediction-v1"

    def grade(self, content: dict[str, Any], response: dict[str, Any]) -> GradeResult:
        choice = _choice(content, response)
        correct = choice == content["private"]["correct_index"]
        return GradeResult(
            PASSED, feedback=PREDICTION_FEEDBACK, grader=self.name, details={"correct": correct}
        )


class WordCountBlurbGrader:
    """Response `{"text": <str>}`; passes when the text has at least `min_words` words.

    TEST/OFFLINE TOOL ONLY: it counts whitespace-separated words and ignores the model answer
    and rubric. It is not a default grader: the default for `blurb` is the LLM judge
    (`teacher.judge.LLMJudgeGrader`), and this grader is used only when passed explicitly, e.g.
    `create_app(graders={"blurb": WordCountBlurbGrader(), ...})` in tests.
    """

    name = "word-count-v1"

    def grade(self, content: dict[str, Any], response: dict[str, Any]) -> GradeResult:
        text = _require_keys(response, {"text"})["text"]
        if not isinstance(text, str):
            raise ValueError("text must be a string")
        min_words = content.get("min_words", DEFAULT_MIN_WORDS)
        words = len(text.split())
        if words >= min_words:
            return GradeResult(
                PASSED,
                feedback=f"Accepted ({words} words). Compare your answer with the model answer.",
                grader=self.name,
            )
        return GradeResult(
            FAILED,
            feedback=f"Write at least {min_words} words (you wrote {words}).",
            grader=self.name,
        )


def default_graders(judge_model: str | None = None) -> dict[str, Grader]:
    """The built-in graders keyed by block type: multiple choice, predictions, and the LLM
    judge for blurbs.

    `judge_model` defaults to `teacher.config.DEFAULT_JUDGE_MODEL`. The judge creates its
    Anthropic client on first use, so building these needs no credentials; grading a blurb
    without credentials raises GradingUnavailableError.
    """
    from teacher.judge import LLMJudgeGrader  # judge imports this module

    judge = LLMJudgeGrader() if judge_model is None else LLMJudgeGrader(model=judge_model)
    return {
        "multiple_choice": MultipleChoiceGrader(),
        "prediction": PredictionGrader(),
        "blurb": judge,
    }


def offline_graders() -> dict[str, Grader]:
    """TEST/OFFLINE TOOL ONLY: a grader for every gate type that needs no network, blurbs
    graded by `WordCountBlurbGrader` (see there). Never a default."""
    return {
        "multiple_choice": MultipleChoiceGrader(),
        "prediction": PredictionGrader(),
        "blurb": WordCountBlurbGrader(),
    }


__all__ = [
    "CONFIDENCES",
    "DEFAULT_MIN_WORDS",
    "FAILED",
    "NO_OPTION_FEEDBACK",
    "PARTIAL",
    "PASSED",
    "PREDICTION_FEEDBACK",
    "VERDICTS",
    "Confidence",
    "GradeResult",
    "Grader",
    "GradingUnavailableError",
    "MultipleChoiceGrader",
    "PredictionGrader",
    "Verdict",
    "WordCountBlurbGrader",
    "default_graders",
    "offline_graders",
    "verdict_for",
]
