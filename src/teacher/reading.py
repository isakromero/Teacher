"""Reading and gating: which blocks of an artifact the reader may see, and submitting attempts.

Gate state of a gate block, given `max_attempts`:

- `"open"`: not passed and attempts < max_attempts;
- `"passed"`: some attempt passed;
- `"revealed"`: not passed and attempts >= max_attempts (the escape hatch: the reader may
  continue and sees the private content).

A gate is *cleared* when passed or revealed. The visible blocks are all blocks up to and
including the first uncleared gate; the rest are locked. Non-gate blocks have state None.

Entry points:

- `reading_state(session, settings, artifact, max_attempts=...)` -> `ReadingState` (per-block
  `BlockState` plus `total_blocks`, `gates_total`, `gates_cleared`, `locked_blocks`, `complete`);
  `compute_reading_state(progress, max_attempts)` is the pure core over
  `BlockResponseManager.progress()` rows.
- `split_content(block_state, content)` -> `(public, revealed)`: the only way content leaves here.
- `submit_response(...)` grades and records an attempt and returns a `SubmitOutcome`.

A `prediction` is a gate like any other here: it locks what follows until the reader commits
to a guess, and its grader passes every guess, so one attempt clears it. It is not a question
(`teacher.blocks.is_question_type`), so it takes no `confidence`; counting pass rates, scores
or standing is for the callers, which leave predictions out. A partial answer (verdict
`"partial"`) does not clear a gate.

No web framework imports: conflicts are raised as `ReadingConflict` subclasses (the API maps them
to 409), unknown blocks as `NotFoundError`, bad input as `ValueError`; a grader that cannot
reach a verdict raises `teacher.grading.GradingUnavailableError` (the API maps it to 503).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from teacher.blocks import is_question_type, private_content, public_content
from teacher.config import Settings
from teacher.db.models import Artifact, ArtifactBlock, BlockResponse
from teacher.grading import CONFIDENCES, Grader, GradeResult
from teacher.managers.artifact_blocks import ArtifactBlockManager
from teacher.managers.block_responses import BlockProgress, BlockResponseManager
from teacher.managers.errors import NotFoundError

#: The attempts a gate allows before it reveals its answer, unless the app is given another
#: (`teacher.web.app.create_app(max_attempts=...)`; `teacher serve` uses this one).
DEFAULT_MAX_ATTEMPTS = 2

OPEN = "open"
PASSED = "passed"
REVEALED = "revealed"
GATE_STATES: tuple[str, ...] = (OPEN, PASSED, REVEALED)


# -- errors ---------------------------------------------------------------------------------


class ReadingConflict(Exception):
    """The request conflicts with the artifact's reading state (HTTP 409)."""


class ArtifactNotReadyError(ReadingConflict):
    """The artifact is not in status `ready`."""


class BlockLockedError(ReadingConflict):
    """The block lies after the first uncleared gate."""


class NotAGateError(ReadingConflict):
    """Responses can only be submitted to gate blocks."""


class GateAlreadyPassedError(ReadingConflict):
    """The gate has a passing attempt already."""


# -- state ----------------------------------------------------------------------------------


def _check_max_attempts(max_attempts: int) -> int:
    if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1:
        raise ValueError("max_attempts must be a positive integer")
    return max_attempts


def gate_state(*, is_gate: bool, attempts: int, passed: bool, max_attempts: int) -> str | None:
    """`"open"`, `"passed"` or `"revealed"` for a gate; None for a non-gate block."""
    if not is_gate:
        return None
    if passed:
        return PASSED
    return REVEALED if attempts >= max_attempts else OPEN


@dataclass(frozen=True, slots=True)
class BlockState:
    """One block's standing for the reader."""

    block: ArtifactBlock
    attempts: int
    passed: bool
    state: str | None  # None for non-gate blocks
    visible: bool

    @property
    def key(self) -> str:
        return self.block.key

    @property
    def is_gate(self) -> bool:
        return self.block.is_gate

    @property
    def cleared(self) -> bool:
        """True for a passed or revealed gate (False for non-gate blocks)."""
        return self.state in (PASSED, REVEALED)

    @property
    def locked(self) -> bool:
        return not self.visible


@dataclass(frozen=True, slots=True)
class ReadingState:
    """Per-block states of one artifact in reading order, plus totals."""

    blocks: tuple[BlockState, ...]
    max_attempts: int

    @property
    def total_blocks(self) -> int:
        return len(self.blocks)

    @property
    def gates_total(self) -> int:
        return sum(1 for b in self.blocks if b.is_gate)

    @property
    def gates_cleared(self) -> int:
        return sum(1 for b in self.blocks if b.cleared)

    @property
    def locked_blocks(self) -> int:
        return sum(1 for b in self.blocks if b.locked)

    @property
    def complete(self) -> bool:
        """True when every gate is cleared (also for an artifact without gates)."""
        return self.gates_cleared == self.gates_total

    @property
    def visible(self) -> list[BlockState]:
        return [b for b in self.blocks if b.visible]

    def get(self, key: str) -> BlockState:
        """The state of the block with `key`; NotFoundError if there is none."""
        for b in self.blocks:
            if b.key == key:
                return b
        raise NotFoundError(f"no block {key!r}")

    def summary(self) -> dict[str, int | bool]:
        """`{total_blocks, gates_total, gates_cleared, locked_blocks, complete}`."""
        return {
            "total_blocks": self.total_blocks,
            "gates_total": self.gates_total,
            "gates_cleared": self.gates_cleared,
            "locked_blocks": self.locked_blocks,
            "complete": self.complete,
        }


def compute_reading_state(progress: Iterable[BlockProgress], max_attempts: int) -> ReadingState:
    """Pure: turn `BlockResponseManager.progress()` rows (in reading order) into a ReadingState."""
    _check_max_attempts(max_attempts)
    states: list[BlockState] = []
    visible = True
    for row in progress:
        state = gate_state(
            is_gate=row.block.is_gate,
            attempts=row.attempts,
            passed=row.passed,
            max_attempts=max_attempts,
        )
        states.append(BlockState(row.block, row.attempts, row.passed, state, visible))
        if state == OPEN:
            visible = False  # everything after the first uncleared gate is locked
    return ReadingState(tuple(states), max_attempts)


def reading_state(
    session: Session, settings: Settings, artifact: Artifact, *, max_attempts: int
) -> ReadingState:
    """The artifact's current ReadingState (two queries; block files are not read)."""
    progress = BlockResponseManager(session, settings).progress(artifact)
    return compute_reading_state(progress, max_attempts)


def require_ready(artifact: Artifact) -> None:
    """ArtifactNotReadyError unless the artifact's status is `ready`."""
    if artifact.status != "ready":
        raise ArtifactNotReadyError(
            f"artifact {artifact.slug!r} is {artifact.status}, not ready"
        )


def require_visible(block_state: BlockState) -> None:
    """BlockLockedError if the block is locked."""
    if block_state.locked:
        raise BlockLockedError(f"block {block_state.key} is locked: clear the gate before it first")


def split_content(
    block_state: BlockState, content: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """`(public, revealed)` for a visible block: `revealed` is the private part once cleared.

    Raises BlockLockedError for a locked block: locked content never leaves this module.
    """
    require_visible(block_state)
    content = dict(content)
    revealed = private_content(content) if block_state.cleared else None
    return public_content(content), revealed


# -- submitting -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SubmitOutcome:
    """Result of `submit_response`."""

    response: BlockResponse
    result: GradeResult
    block: BlockState  # the gate's state after this attempt
    revealed: dict[str, Any] | None  # private content if the gate is now cleared
    unlocked: tuple[BlockState, ...]  # blocks visible now that were locked before
    state: ReadingState  # the whole artifact after this attempt


def submit_response(
    session: Session,
    settings: Settings,
    artifact: Artifact,
    key: str,
    response: dict[str, Any],
    *,
    graders: Mapping[str, Grader],
    max_attempts: int,
    confidence: str | None = None,
) -> SubmitOutcome:
    """Grade `response` to gate `key` and record it, with the reader's `confidence` (one of
    `teacher.grading.CONFIDENCES`, or None when not given).

    Raises ArtifactNotReadyError, NotFoundError (unknown block), BlockLockedError,
    NotAGateError, GateAlreadyPassedError, and ValueError for a malformed response, a block
    type without a grader, or a `confidence` that is not a level or is given for a gate that
    is not a question (a prediction); the grader may raise GradingUnavailableError (nothing is
    recorded in any of those cases). A revealed gate still accepts attempts. The grader's
    `details` are stored with the attempt; callers must not show them before the gate is
    cleared.
    """
    require_ready(artifact)
    block = ArtifactBlockManager(session, settings).get_by_key(artifact, key)
    before = reading_state(session, settings, artifact, max_attempts=max_attempts)
    current = before.get(block.key)
    require_visible(current)
    if not current.is_gate:
        raise NotAGateError(f"block {key} is not a gate and takes no responses")
    if current.passed:
        raise GateAlreadyPassedError(f"block {key} is already passed")
    grader = graders.get(block.type)
    if grader is None:
        raise ValueError(f"no grader for block type {block.type!r}")
    if not isinstance(response, dict):
        raise ValueError("response must be an object")
    if confidence is not None:
        if confidence not in CONFIDENCES:
            raise ValueError(
                f"confidence must be one of {list(CONFIDENCES)}, got {confidence!r}"
            )
        if not is_question_type(block.type):
            raise ValueError(
                f"block {key} is a {block.type}, not a question, and takes no confidence"
            )

    content = ArtifactBlockManager(session, settings).read_content(block)
    result = grader.grade(content, response)
    row = BlockResponseManager(session, settings).record(
        block,
        response=response,
        passed=result.passed,
        grader=result.grader,
        feedback=result.feedback,
        details=result.details,
        verdict=result.verdict,
        missing=result.missing,
        confidence=confidence,
    )

    after = reading_state(session, settings, artifact, max_attempts=max_attempts)
    now = after.get(block.key)
    _, revealed = split_content(now, content)
    was_visible = {b.key for b in before.blocks if b.visible}
    unlocked = tuple(b for b in after.blocks if b.visible and b.key not in was_visible)
    return SubmitOutcome(row, result, now, revealed, unlocked, after)


__all__ = [
    "DEFAULT_MAX_ATTEMPTS",
    "GATE_STATES",
    "OPEN",
    "PASSED",
    "REVEALED",
    "ArtifactNotReadyError",
    "BlockLockedError",
    "BlockState",
    "GateAlreadyPassedError",
    "NotAGateError",
    "ReadingConflict",
    "ReadingState",
    "SubmitOutcome",
    "compute_reading_state",
    "gate_state",
    "reading_state",
    "require_ready",
    "require_visible",
    "split_content",
    "submit_response",
]
