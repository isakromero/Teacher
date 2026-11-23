"""BlockResponseManager: the user's graded attempts at artifact blocks, and progress through gates."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import case, exists, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from teacher.config import Settings
from teacher.db.models import Artifact, ArtifactBlock, BlockResponse
from teacher.grading import CONFIDENCES, PARTIAL, VERDICTS, verdict_for
from teacher.managers.artifacts import (
    _is_unique_violation,
    _require_persistent,
    _validate_text,
)
from teacher.managers.errors import DuplicateError
from teacher.managers.study_history import StudyHistoryManager


@dataclass(frozen=True, slots=True)
class BlockProgress:
    """One block's standing: how many attempts were recorded and whether any passed."""

    block: ArtifactBlock
    attempts: int
    passed: bool


def check_verdict(passed: bool, verdict: str | None, missing: tuple[str, ...]) -> str:
    """The verdict to store for an attempt (shared with the chat's exercises): `verdict`, or
    for None `"passed"`/`"failed"` from `passed`. ValueError unless it is one of
    `teacher.grading.VERDICTS` agreeing with `passed`, and `missing` a tuple of non-blank
    strings that is non-empty exactly for `"partial"` (the database checks the same)."""
    if verdict is None:
        verdict = verdict_for(passed)
    if verdict not in VERDICTS:
        raise ValueError(f"verdict must be one of {list(VERDICTS)}, got {verdict!r}")
    if (verdict == "passed") != passed:
        raise ValueError(f"verdict {verdict!r} disagrees with passed={passed}")
    if not isinstance(missing, tuple) or not all(
        isinstance(item, str) and item.strip() for item in missing
    ):
        raise ValueError("missing must be a tuple of non-blank strings")
    if (verdict == PARTIAL) != bool(missing):
        raise ValueError("missing is listed for a partial verdict, and only for one")
    return verdict


def response_verdict(row: Any) -> str:
    """A stored attempt's verdict (BlockResponse or ChatBlockResponse). ValueError for a row
    without one, which only a row written outside the managers can be."""
    if row.verdict is None:
        raise ValueError(
            f"{type(row).__name__} {row.id} has no verdict: it was written outside the managers"
        )
    return row.verdict


class BlockResponseManager:
    """Record and summarise responses to blocks."""

    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings

    def record(
        self,
        block: ArtifactBlock,
        *,
        response: dict[str, Any] | list[Any] | str,
        passed: bool,
        grader: str,
        feedback: str | None = None,
        details: dict[str, Any] | None = None,
        verdict: str | None = None,
        missing: tuple[str, ...] = (),
        confidence: str | None = None,
    ) -> BlockResponse:
        """Store the next attempt (previous max + 1, starting at 1) for `block`, in the open
        study session (`StudyHistoryManager.activity_session`).

        `response` must be a JSON-serialisable dict, list or str (None is rejected: the JSON
        column would store it as JSON null). `details` is the grader's private verdict: a
        JSON-serialisable dict, or None for none (stored as SQL NULL). `verdict`, `missing` and
        `confidence` are checked by `check_verdict` (`verdict` None: a right-or-wrong grade,
        `"passed"` or `"failed"` from `passed`).
        """
        _require_persistent(block, "block")
        if response is None:
            raise ValueError("response must not be None")
        if not isinstance(response, (dict, list, str)):
            raise TypeError(
                f"response must be a dict, list or str, got {response.__class__.__name__}"
            )
        try:
            json.dumps(response, allow_nan=False)
        except (TypeError, ValueError) as err:
            raise ValueError(f"response is not JSON-serialisable: {err}") from err
        if not isinstance(passed, bool):
            raise TypeError("passed must be a bool")
        verdict = check_verdict(passed, verdict, missing)
        if confidence is not None and confidence not in CONFIDENCES:
            raise ValueError(
                f"confidence must be one of {list(CONFIDENCES)} or None, got {confidence!r}"
            )
        _validate_text(grader, "grader")
        if details is not None:
            if not isinstance(details, dict):
                raise TypeError(f"details must be a dict or None, got {details.__class__.__name__}")
            try:
                json.dumps(details, allow_nan=False)
            except (TypeError, ValueError) as err:
                raise ValueError(f"details is not JSON-serialisable: {err}") from err

        last = self.session.scalar(
            select(func.max(BlockResponse.attempt)).where(BlockResponse.block_id == block.id)
        )
        attempt = (last or 0) + 1
        row = BlockResponse(
            block_id=block.id,
            attempt=attempt,
            response=response,
            passed=passed,
            feedback=feedback,
            grader=grader,
            details=details,
            verdict=verdict,
            missing=list(missing),
            confidence=confidence,
            session_id=StudyHistoryManager(self.session, self.settings).activity_session().id,
        )
        try:
            with self.session.begin_nested():
                self.session.add(row)
                self.session.flush()
        except IntegrityError as err:
            if _is_unique_violation(err):
                raise DuplicateError(f"block {block.key}: attempt {attempt} already exists") from err
            raise
        self.session.expire(block, ["responses"])
        return row

    def list(self, block: ArtifactBlock) -> list[BlockResponse]:
        """All responses to `block`, by attempt."""
        return list(
            self.session.scalars(
                select(BlockResponse)
                .where(BlockResponse.block_id == block.id)
                .order_by(BlockResponse.attempt)
            )
        )

    def latest(self, block: ArtifactBlock) -> BlockResponse | None:
        """The highest-numbered attempt, or None if there is none."""
        return self.session.scalar(
            select(BlockResponse)
            .where(BlockResponse.block_id == block.id)
            .order_by(BlockResponse.attempt.desc())
            .limit(1)
        )

    def attempt_count(self, block: ArtifactBlock) -> int:
        """Number of recorded attempts for `block`."""
        return self.session.scalar(
            select(func.count()).select_from(BlockResponse).where(BlockResponse.block_id == block.id)
        ) or 0

    def is_passed(self, block: ArtifactBlock) -> bool:
        """True if any attempt at `block` passed."""
        return bool(
            self.session.scalar(
                select(
                    exists().where(BlockResponse.block_id == block.id, BlockResponse.passed.is_(True))
                )
            )
        )

    def progress(self, artifact: Artifact) -> list[BlockProgress]:
        """Per block, in reading order: attempts and whether any passed (two queries)."""
        blocks = list(
            self.session.scalars(
                select(ArtifactBlock)
                .where(ArtifactBlock.artifact_id == artifact.id)
                .order_by(ArtifactBlock.position)
            )
        )
        stats = {
            block_id: (count, bool(any_passed))
            for block_id, count, any_passed in self.session.execute(
                select(
                    BlockResponse.block_id,
                    func.count(BlockResponse.id),
                    func.max(case((BlockResponse.passed.is_(True), 1), else_=0)),
                )
                .join(ArtifactBlock, ArtifactBlock.id == BlockResponse.block_id)
                .where(ArtifactBlock.artifact_id == artifact.id)
                .group_by(BlockResponse.block_id)
            )
        }
        return [BlockProgress(block, *stats.get(block.id, (0, False))) for block in blocks]

    def next_gate(self, artifact: Artifact) -> ArtifactBlock | None:
        """The first gate block, in reading order, with no passed attempt; None if all passed."""
        passed = exists().where(
            BlockResponse.block_id == ArtifactBlock.id, BlockResponse.passed.is_(True)
        )
        return self.session.scalar(
            select(ArtifactBlock)
            .where(ArtifactBlock.artifact_id == artifact.id, ArtifactBlock.is_gate.is_(True))
            .where(~passed)
            .order_by(ArtifactBlock.position)
            .limit(1)
        )


__all__ = ["BlockProgress", "BlockResponseManager", "check_verdict", "response_verdict"]
