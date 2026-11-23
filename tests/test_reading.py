"""teacher.reading: gate states, visibility, totals, content split and submitting attempts."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.orm import Session

from teacher.blocks import is_gate_type, validate_block_content
from teacher.config import Settings
from teacher.db.models import Artifact, ArtifactBlock
from teacher.grading import (
    GradeResult,
    GradingUnavailableError,
    offline_graders,
)
from teacher.managers import (
    ArtifactBlockManager,
    ArtifactManager,
    BlockProgress,
    BlockResponseManager,
    KnowledgeSourceManager,
    NotFoundError,
)
from teacher.reading import (
    OPEN,
    PASSED,
    REVEALED,
    ArtifactNotReadyError,
    BlockLockedError,
    GateAlreadyPassedError,
    NotAGateError,
    ReadingConflict,
    compute_reading_state,
    gate_state,
    reading_state,
    split_content,
    submit_response,
)

MC = {
    "question": "Pick b",
    "options": ["a", "b"],
    "private": {"correct_index": 1, "explanation": "b is right"},
}
BLURB = {
    "prompt": "Explain",
    "min_words": 3,
    "private": {"model_answer": "model", "rubric": [{"criterion": "point"}]},
}
# Explicit offline graders: the default blurb grader is the LLM judge.
OFFLINE_GRADERS = offline_graders()
PAGE = [
    ("prose", {"markdown": "one"}),
    ("multiple_choice", MC),
    ("prose", {"markdown": "three"}),
    ("blurb", BLURB),
    ("prose", {"markdown": "five"}),
]


# -- pure functions -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("is_gate", "attempts", "passed", "expected"),
    [
        (False, 0, False, None),
        (False, 5, True, None),
        (True, 0, False, OPEN),
        (True, 1, False, OPEN),
        (True, 2, False, REVEALED),
        (True, 3, False, REVEALED),
        (True, 1, True, PASSED),
        (True, 2, True, PASSED),
    ],
)
def test_gate_state(is_gate: bool, attempts: int, passed: bool, expected: str | None) -> None:
    assert gate_state(is_gate=is_gate, attempts=attempts, passed=passed, max_attempts=2) == expected


def _progress(spec: list[tuple[bool, int, bool]]) -> list[BlockProgress]:
    """Transient blocks: (is_gate, attempts, passed) per position."""
    return [
        BlockProgress(ArtifactBlock(key=f"b{i:02d}", position=i, is_gate=g), n, p)
        for i, (g, n, p) in enumerate(spec, start=1)
    ]


def test_compute_visibility_stops_at_first_open_gate() -> None:
    state = compute_reading_state(
        _progress([(False, 0, False), (True, 1, False), (False, 0, False), (True, 0, False)]),
        max_attempts=2,
    )
    assert [b.visible for b in state.blocks] == [True, True, False, False]
    assert [b.state for b in state.blocks] == [None, OPEN, None, OPEN]
    assert state.summary() == {
        "total_blocks": 4,
        "gates_total": 2,
        "gates_cleared": 0,
        "locked_blocks": 2,
        "complete": False,
    }
    assert [b.key for b in state.visible] == ["b01", "b02"]
    assert state.get("b03").locked and not state.get("b02").locked
    with pytest.raises(NotFoundError):
        state.get("b99")


def test_compute_cleared_gates_unlock_until_next_open_gate() -> None:
    state = compute_reading_state(
        _progress([(True, 1, True), (False, 0, False), (True, 2, False), (True, 0, False), (False, 0, False)]),
        max_attempts=2,
    )
    assert [b.state for b in state.blocks] == [PASSED, None, REVEALED, OPEN, None]
    assert [b.visible for b in state.blocks] == [True, True, True, True, False]
    assert (state.gates_total, state.gates_cleared, state.locked_blocks) == (3, 2, 1)
    assert state.complete is False


def test_compute_complete_and_no_gates() -> None:
    done = compute_reading_state(_progress([(True, 3, False), (False, 0, False)]), max_attempts=3)
    assert done.complete and done.locked_blocks == 0
    plain = compute_reading_state(_progress([(False, 0, False)]), max_attempts=2)
    assert plain.complete and plain.gates_total == 0
    empty = compute_reading_state([], max_attempts=2)
    assert empty.complete and empty.total_blocks == 0


@pytest.mark.parametrize("bad", [0, -1, True, "2"])
def test_compute_rejects_bad_max_attempts(bad: Any) -> None:
    with pytest.raises(ValueError):
        compute_reading_state([], bad)


# -- with the database ----------------------------------------------------------------------


@pytest.fixture
def artifact(session: Session, settings: Settings, tmp_path: Path) -> Artifact:
    src = tmp_path / "src.txt"
    src.write_text("source text\n")
    source = KnowledgeSourceManager(session, settings).add(src, slug="src", kind="note", title="S")
    manager = ArtifactManager(session, settings)
    artifact = manager.create(
        slug="page", kind="study_page", title="Page", generator="test", sources=[source]
    )
    blocks = ArtifactBlockManager(session, settings)
    for type_, content in PAGE:
        blocks.add(
            artifact,
            type=type_,
            content=validate_block_content(type_, content),
            is_gate=is_gate_type(type_),
        )
    manager.mark_ready(artifact)
    return artifact


def _submit(session: Session, settings: Settings, artifact: Artifact, key: str, response: Any,
            max_attempts: int = 2, graders: Any = None):
    return submit_response(
        session,
        settings,
        artifact,
        key,
        response,
        graders=OFFLINE_GRADERS if graders is None else graders,
        max_attempts=max_attempts,
    )


def test_reading_state_from_db(session: Session, settings: Settings, artifact: Artifact) -> None:
    state = reading_state(session, settings, artifact, max_attempts=2)
    assert [b.key for b in state.visible] == ["b01", "b02"]
    assert state.locked_blocks == 3 and state.gates_total == 2 and not state.complete


def test_split_content(session: Session, settings: Settings, artifact: Artifact) -> None:
    blocks = ArtifactBlockManager(session, settings)
    state = reading_state(session, settings, artifact, max_attempts=2)
    gate = state.get("b02")
    content = blocks.read_content(gate.block)
    public, revealed = split_content(gate, content)
    assert public == {"question": "Pick b", "options": ["a", "b"]} and revealed is None
    with pytest.raises(BlockLockedError):
        split_content(state.get("b03"), blocks.read_content(state.get("b03").block))
    prose_public, prose_revealed = split_content(state.get("b01"), {"markdown": "one"})
    assert prose_public == {"markdown": "one"} and prose_revealed is None


def test_submit_pass_unlocks(session: Session, settings: Settings, artifact: Artifact) -> None:
    outcome = _submit(session, settings, artifact, "b02", {"choice": 1})
    assert outcome.response.attempt == 1 and outcome.response.passed is True
    assert outcome.result.grader == "multiple-choice-v2"
    assert outcome.response.verdict == "passed" and outcome.response.missing == []
    assert outcome.response.confidence is None
    assert outcome.block.state == PASSED
    assert outcome.revealed == {"correct_index": 1, "explanation": "b is right"}
    assert [b.key for b in outcome.unlocked] == ["b03", "b04"]
    assert outcome.state.locked_blocks == 1 and not outcome.state.complete

    with pytest.raises(GateAlreadyPassedError):
        _submit(session, settings, artifact, "b02", {"choice": 1})

    final = _submit(session, settings, artifact, "b04", {"text": "three whole words"})
    assert [b.key for b in final.unlocked] == ["b05"]
    assert final.revealed == {
        "model_answer": "model",
        "rubric": [{"criterion": "point", "required": True}],
    }
    assert final.state.complete and final.state.locked_blocks == 0


def test_submit_escape_hatch(session: Session, settings: Settings, artifact: Artifact) -> None:
    first = _submit(session, settings, artifact, "b02", {"choice": 0})
    assert first.block.state == OPEN and first.revealed is None and first.unlocked == ()
    second = _submit(session, settings, artifact, "b02", {"choice": 0})
    assert second.block.state == REVEALED
    assert second.revealed == {"correct_index": 1, "explanation": "b is right"}
    assert [b.key for b in second.unlocked] == ["b03", "b04"]
    third = _submit(session, settings, artifact, "b02", {"choice": 1})  # revealed still accepts
    assert third.response.attempt == 3 and third.block.state == PASSED and third.unlocked == ()


def test_submit_respects_max_attempts(session: Session, settings: Settings, artifact: Artifact) -> None:
    for _ in range(2):
        assert _submit(session, settings, artifact, "b02", {"choice": 0}, max_attempts=3).block.state == OPEN
    assert _submit(session, settings, artifact, "b02", {"choice": 0}, max_attempts=3).block.state == REVEALED


@pytest.mark.parametrize(
    ("key", "error"),
    [("b01", NotAGateError), ("b03", BlockLockedError), ("b04", BlockLockedError)],
)
def test_submit_conflicts(
    session: Session, settings: Settings, artifact: Artifact, key: str, error: type[Exception]
) -> None:
    with pytest.raises(error) as info:
        _submit(session, settings, artifact, key, {"choice": 1})
    assert isinstance(info.value, ReadingConflict)


def test_submit_unknown_block(session: Session, settings: Settings, artifact: Artifact) -> None:
    with pytest.raises(NotFoundError):
        _submit(session, settings, artifact, "b99", {"choice": 1})


def test_submit_requires_ready(session: Session, settings: Settings, artifact: Artifact) -> None:
    ArtifactManager(session, settings).set_status(artifact, "generating")
    with pytest.raises(ArtifactNotReadyError):
        _submit(session, settings, artifact, "b02", {"choice": 1})


@pytest.mark.parametrize("graders", [{}, {"blurb": object()}])
def test_submit_without_grader(
    session: Session, settings: Settings, artifact: Artifact, graders: dict
) -> None:
    with pytest.raises(ValueError, match="no grader"):
        _submit(session, settings, artifact, "b02", {"choice": 1}, graders=graders)


def test_submit_malformed_records_nothing(
    session: Session, settings: Settings, artifact: Artifact
) -> None:
    with pytest.raises(ValueError):
        _submit(session, settings, artifact, "b02", {"choice": "b"})
    block = ArtifactBlockManager(session, settings).get_by_key(artifact, "b02")
    assert BlockResponseManager(session, settings).attempt_count(block) == 0


def test_submit_uses_custom_grader_with_full_content(
    session: Session, settings: Settings, artifact: Artifact
) -> None:
    seen: list[dict] = []

    class Spy:
        def grade(self, content: dict, response: dict) -> GradeResult:
            seen.append(content)
            return GradeResult("passed", feedback=None, grader="spy-v1")

    outcome = _submit(session, settings, artifact, "b02", {"anything": 1}, graders={"multiple_choice": Spy()})
    assert seen[0]["private"]["correct_index"] == 1
    assert outcome.response.grader == "spy-v1" and outcome.response.feedback is None


def test_submit_stores_grader_details(
    session: Session, settings: Settings, artifact: Artifact
) -> None:
    details = {"model": "m", "score": 0.5, "criteria": [{"criterion": "c", "met": False}]}

    class Detailed:
        def grade(self, content: dict, response: dict) -> GradeResult:
            return GradeResult("failed", feedback="hint", grader="detailed-v1", details=details)

    outcome = _submit(session, settings, artifact, "b02", {"x": 1},
                      graders={"multiple_choice": Detailed()})
    assert outcome.response.details == details and outcome.result.details == details
    plain = _submit(session, settings, artifact, "b02", {"choice": 1})
    assert plain.response.details is None


def test_submit_grading_unavailable_records_nothing(
    session: Session, settings: Settings, artifact: Artifact
) -> None:
    class Down:
        def grade(self, content: dict, response: dict) -> GradeResult:
            raise GradingUnavailableError("judge is down")

    with pytest.raises(GradingUnavailableError):
        _submit(session, settings, artifact, "b02", {"choice": 1},
                graders={"multiple_choice": Down()})
    block = ArtifactBlockManager(session, settings).get_by_key(artifact, "b02")
    assert BlockResponseManager(session, settings).attempt_count(block) == 0


def test_submit_records_confidence(session: Session, settings: Settings, artifact: Artifact) -> None:
    first = _submit(session, settings, artifact, "b02", {"choice": 0}, max_attempts=3)
    assert first.response.verdict == "failed"
    with_confidence = submit_response(
        session, settings, artifact, "b02", {"choice": 0},
        graders=OFFLINE_GRADERS, max_attempts=3, confidence="high",
    )
    assert with_confidence.response.confidence == "high"
    assert first.response.confidence is None


@pytest.mark.parametrize("confidence", ["certain", "", "HIGH", 1])
def test_submit_rejects_an_unknown_confidence(
    session: Session, settings: Settings, artifact: Artifact, confidence: Any
) -> None:
    with pytest.raises(ValueError, match="confidence must be one of"):
        submit_response(
            session, settings, artifact, "b02", {"choice": 1},
            graders=OFFLINE_GRADERS, max_attempts=2, confidence=confidence,
        )
    block = ArtifactBlockManager(session, settings).get_by_key(artifact, "b02")
    assert BlockResponseManager(session, settings).attempt_count(block) == 0


def test_a_partial_answer_is_recorded_and_does_not_clear_the_gate(
    session: Session, settings: Settings, artifact: Artifact
) -> None:
    class Partial:
        def grade(self, content: dict, response: dict) -> GradeResult:
            return GradeResult("partial", "Half there.", "partial-v1", missing=("names the cost",))

    outcome = _submit(session, settings, artifact, "b02", {"x": 1}, max_attempts=3,
                      graders={"multiple_choice": Partial()})
    assert outcome.response.passed is False and outcome.response.verdict == "partial"
    assert outcome.response.missing == ["names the cost"]
    assert outcome.block.state == OPEN and outcome.revealed is None and outcome.unlocked == ()


PREDICTION = {
    "prompt": "Guess: up or down?",
    "options": ["up", "down"],
    "private": {"correct_index": 1, "resolution": "It goes down."},
}


@pytest.fixture
def predicting(session: Session, settings: Settings, tmp_path: Path) -> Artifact:
    """A page whose first gate is a prediction: prose, prediction, prose."""
    src = tmp_path / "p.txt"
    src.write_text("prediction source\n")
    source = KnowledgeSourceManager(session, settings).add(src, slug="p", kind="note", title="P")
    manager = ArtifactManager(session, settings)
    artifact = manager.create(
        slug="predict", kind="study_page", title="P", generator="test", sources=[source]
    )
    blocks = ArtifactBlockManager(session, settings)
    for type_, content in [
        ("prose", {"markdown": "before"}),
        ("prediction", PREDICTION),
        ("prose", {"markdown": "after", "role": "critique"}),
    ]:
        blocks.add(artifact, type=type_, content=validate_block_content(type_, content),
                   is_gate=is_gate_type(type_))
    manager.mark_ready(artifact)
    return artifact


@pytest.mark.parametrize(("choice", "correct"), [(0, False), (1, True)])
def test_a_prediction_locks_until_any_guess_clears_it(
    session: Session, settings: Settings, predicting: Artifact, choice: int, correct: bool
) -> None:
    before = reading_state(session, settings, predicting, max_attempts=2)
    assert [b.key for b in before.visible] == ["b01", "b02"] and before.gates_total == 1
    outcome = _submit(session, settings, predicting, "b02", {"choice": choice})
    assert outcome.response.grader == "prediction-v1" and outcome.response.passed is True
    assert outcome.response.details == {"correct": correct}
    assert outcome.block.state == PASSED and outcome.state.complete
    assert outcome.revealed == PREDICTION["private"]  # the resolution and the right option
    assert [b.key for b in outcome.unlocked] == ["b03"]
    assert outcome.response.confidence is None


def test_a_prediction_takes_no_confidence(
    session: Session, settings: Settings, predicting: Artifact
) -> None:
    with pytest.raises(ValueError, match="is a prediction, not a question, and takes no confidence"):
        submit_response(
            session, settings, predicting, "b02", {"choice": 0},
            graders=OFFLINE_GRADERS, max_attempts=2, confidence="low",
        )
    block = ArtifactBlockManager(session, settings).get_by_key(predicting, "b02")
    assert BlockResponseManager(session, settings).attempt_count(block) == 0
