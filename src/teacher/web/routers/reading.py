"""Reading endpoints: gated blocks, submitting and listing responses, artifact assets.

Block `content` is always the public part; the private part is sent as `revealed` only once the
gate is cleared, and locked blocks (after the first uncleared gate) are never sent at all. The
same goes for a response's grader `details` (they reveal the rubric): null until the gate is
cleared. Each visible block also lists the concepts it covers (`concepts`, from the block
tagger; empty for an untagged page).

An attempt's `verdict` is "passed", "partial" or "failed" (`teacher.grading`); `missing` lists
the unmet required criteria of a partial answer and is sent straight away (empty otherwise).
A submit may carry the reader's `confidence` ("low", "medium" or "high"; the page labels them
Guessing / Fairly sure / Certain), recorded with the attempt; it is a 422 on a gate that is
not a question (a prediction). The tutor's exercises (`routers/chat.py`) take `ResponseIn`,
which has no confidence.

A lab (`kind = "lab"`) has no blocks: these endpoints are a 422 for it (`teacher.labs.LabKindError`).
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from teacher.config import Settings
from teacher.grading import Confidence, Grader, Verdict
from teacher.labs import require_not_lab
from teacher.managers import (
    ArtifactBlockManager,
    ArtifactManager,
    BlockConceptManager,
    BlockResponseManager,
    NotFoundError,
    SourceSegmentManager,
    StudyHistoryManager,
)
from teacher.managers.block_responses import response_verdict
from teacher.reading import (
    OPEN,
    BlockState,
    ReadingConflict,
    gate_state,
    reading_state,
    require_ready,
    require_visible,
    split_content,
    submit_response,
)
from teacher.web.common import to_iso
from teacher.web.deps import SessionDep, SettingsDep

router = APIRouter(prefix="/api", tags=["reading"])

ASSETS_DIR = "assets"


# -- models ---------------------------------------------------------------------------------


class BlockSegmentOut(BaseModel):
    id: int
    source: str
    kind: str
    label: str
    title: str | None
    locator: dict[str, Any]


class BlockConceptOut(BaseModel):
    """A concept the block covers (`block_concepts`): `main` (the block is about it, a question
    tests it) or `incidental`. The tagger's rationale is not sent: on a question it can hint at
    the answer."""

    slug: str
    name: str
    role: str
    origin: str


class BlockOut(BaseModel):
    key: str
    position: int
    type: str
    is_gate: bool
    content: dict[str, Any]
    state: str | None
    attempts: int
    revealed: dict[str, Any] | None
    segments: list[BlockSegmentOut]
    concepts: list[BlockConceptOut]  # main first; empty when the page is not tagged


class BlocksOut(BaseModel):
    artifact: str
    total_blocks: int
    locked_blocks: int
    complete: bool
    blocks: list[BlockOut]


class ResponseIn(BaseModel):
    """An answer to a gate or a tutor's exercise."""

    model_config = ConfigDict(extra="forbid")

    response: dict[str, Any]


class GateResponseIn(ResponseIn):
    """An answer to a study-page gate, with how sure the reader is (optional; not for a
    prediction)."""

    confidence: Confidence | None = None


class SubmitOut(BaseModel):
    attempt: int
    passed: bool  # true only for verdict "passed"
    verdict: Verdict
    feedback: str | None
    missing: list[str]  # a partial answer's unmet required criteria; [] otherwise
    confidence: Confidence | None  # as submitted
    details: dict[str, Any] | None  # null until the gate is cleared
    state: str | None
    revealed: dict[str, Any] | None
    unlocked: list[BlockOut]
    locked_blocks: int
    complete: bool


class ResponseOut(BaseModel):
    attempt: int
    response: Any
    passed: bool
    verdict: Verdict
    feedback: str | None
    missing: list[str]
    confidence: Confidence | None  # always null for a tutor's exercise
    grader: str
    details: dict[str, Any] | None  # null until the gate is cleared
    created_at: str


# -- dependencies and helpers ---------------------------------------------------------------


def get_graders(request: Request) -> Mapping[str, Grader]:
    return request.app.state.graders or {}


def get_max_attempts(request: Request) -> int:
    return request.app.state.max_attempts


GradersDep = Annotated[Mapping[str, Grader], Depends(get_graders)]
MaxAttemptsDep = Annotated[int, Depends(get_max_attempts)]


@contextmanager
def _conflicts_as_409() -> Iterator[None]:
    try:
        yield
    except ReadingConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def block_out(session: Session, settings: Settings, block_state: BlockState) -> BlockOut:
    """The reader's view of one VISIBLE block (BlockLockedError for a locked one)."""
    blocks = ArtifactBlockManager(session, settings)
    segments = SourceSegmentManager(session, settings)
    block = block_state.block
    public, revealed = split_content(block_state, blocks.read_content(block))
    return BlockOut(
        key=block.key,
        position=block.position,
        type=block.type,
        is_gate=block.is_gate,
        content=public,
        state=block_state.state,
        attempts=block_state.attempts,
        revealed=revealed,
        segments=[
            BlockSegmentOut(
                id=seg.id,
                source=seg.source.slug,
                kind=seg.kind,
                label=seg.label,
                title=seg.title,
                locator=segments.locator(seg).model_dump(),
            )
            for seg in blocks.segments(block)
        ],
        concepts=[
            BlockConceptOut(slug=c.slug, name=c.name, role=tag.role, origin=tag.origin)
            for tag, c in BlockConceptManager(session, settings).for_blocks([block.id]).get(
                block.id, []
            )
        ],
    )


# -- endpoints ------------------------------------------------------------------------------


@router.get("/artifacts/{slug}/blocks")
def list_blocks(
    slug: str, session: SessionDep, settings: SettingsDep, max_attempts: MaxAttemptsDep
) -> BlocksOut:
    """The visible blocks: everything up to and including the first uncleared gate."""
    artifact = ArtifactManager(session, settings).get_by_slug(slug)
    require_not_lab(artifact, "blocks")
    with _conflicts_as_409():
        require_ready(artifact)
    state = reading_state(session, settings, artifact, max_attempts=max_attempts)
    return BlocksOut(
        artifact=artifact.slug,
        total_blocks=state.total_blocks,
        locked_blocks=state.locked_blocks,
        complete=state.complete,
        blocks=[block_out(session, settings, b) for b in state.visible],
    )


@router.post("/artifacts/{slug}/blocks/{key}/responses", status_code=201)
def create_response(
    slug: str,
    key: str,
    body: GateResponseIn,
    session: SessionDep,
    settings: SettingsDep,
    graders: GradersDep,
    max_attempts: MaxAttemptsDep,
) -> SubmitOut:
    """Grade and record an attempt at a visible, not yet passed gate. The attempt that clears
    the gate (passed, or the last one allowed) also logs a `gate_cleared` study event."""
    artifact = ArtifactManager(session, settings).get_by_slug(slug)
    require_not_lab(artifact, "blocks")
    with _conflicts_as_409():
        outcome = submit_response(
            session,
            settings,
            artifact,
            key,
            body.response,
            graders=graders,
            max_attempts=max_attempts,
            confidence=body.confidence,
        )
    before = gate_state(
        is_gate=True,
        attempts=outcome.response.attempt - 1,
        passed=False,  # a passed gate takes no more attempts
        max_attempts=max_attempts,
    )
    if outcome.block.cleared and before == OPEN:
        StudyHistoryManager(session, settings).log(
            "gate_cleared",
            artifact=artifact,
            block=outcome.block.block,
            payload={
                "state": outcome.block.state,
                "attempt": outcome.response.attempt,
                "unlocked": [b.key for b in outcome.unlocked],
            },
        )
    row = outcome.response
    return SubmitOut(
        attempt=row.attempt,
        passed=row.passed,
        verdict=response_verdict(row),
        feedback=row.feedback,
        missing=row.missing,
        confidence=row.confidence,
        details=row.details if outcome.block.cleared else None,
        state=outcome.block.state,
        revealed=outcome.revealed,
        unlocked=[block_out(session, settings, b) for b in outcome.unlocked],
        locked_blocks=outcome.state.locked_blocks,
        complete=outcome.state.complete,
    )


@router.get("/artifacts/{slug}/blocks/{key}/responses")
def list_responses(
    slug: str, key: str, session: SessionDep, settings: SettingsDep, max_attempts: MaxAttemptsDep
) -> list[ResponseOut]:
    """Recorded attempts at a visible block, by attempt number (`details` once cleared)."""
    artifact = ArtifactManager(session, settings).get_by_slug(slug)
    require_not_lab(artifact, "blocks")
    with _conflicts_as_409():
        require_ready(artifact)
        block = ArtifactBlockManager(session, settings).get_by_key(artifact, key)
        state = reading_state(session, settings, artifact, max_attempts=max_attempts)
        block_state = state.get(block.key)
        require_visible(block_state)
    return [
        ResponseOut(
            attempt=r.attempt,
            response=r.response,
            passed=r.passed,
            verdict=response_verdict(r),
            feedback=r.feedback,
            missing=r.missing,
            confidence=r.confidence,
            grader=r.grader,
            details=r.details if block_state.cleared else None,
            created_at=to_iso(r.created_at),
        )
        for r in BlockResponseManager(session, settings).list(block)
    ]


@router.get("/artifacts/{slug}/assets/{path:path}", response_class=FileResponse)
def get_asset(slug: str, path: str, session: SessionDep, settings: SettingsDep) -> FileResponse:
    """A file from the artifact's `assets/` folder; 404 if missing or outside that folder."""
    manager = ArtifactManager(session, settings)
    artifact = manager.get_by_slug(slug)
    assets = (manager.dir(artifact) / ASSETS_DIR).resolve()
    missing = NotFoundError(f"{artifact.slug}: no asset {path!r}")
    if not path or "\x00" in path:
        raise missing
    target = (assets / path).resolve()
    if not target.is_relative_to(assets) or target == assets or not target.is_file():
        raise missing
    return FileResponse(target, filename=target.name, content_disposition_type="inline")


__all__ = [
    "BlockConceptOut",
    "BlockOut",
    "BlockSegmentOut",
    "BlocksOut",
    "GateResponseIn",
    "ResponseIn",
    "ResponseOut",
    "SubmitOut",
    "router",
]
