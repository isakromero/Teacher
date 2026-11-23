"""BlockResponseManager: attempt numbering, validation, pass state, progress and gates."""

from __future__ import annotations

import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.orm import Session

from teacher.config import Settings
from teacher.db.models import Artifact, ArtifactBlock, BlockResponse
from teacher.managers.artifact_blocks import ArtifactBlockManager
from teacher.managers.artifacts import ArtifactManager
from teacher.managers.block_responses import (
    BlockProgress,
    BlockResponseManager,
    response_verdict,
)
from teacher.managers.errors import DuplicateError


@pytest.fixture
def artifact(session: Session, settings: Settings) -> Artifact:
    return ArtifactManager(session, settings).create(
        slug="guide-1", kind="guide", title="Guide", generator="g/1"
    )


@pytest.fixture
def blocks(session: Session, settings: Settings) -> ArtifactBlockManager:
    return ArtifactBlockManager(session, settings)


@pytest.fixture
def mgr(session: Session, settings: Settings) -> BlockResponseManager:
    return BlockResponseManager(session, settings)


def add_blocks(blocks: ArtifactBlockManager, artifact: Artifact, gates: str) -> list[ArtifactBlock]:
    """One block per char: 'G' is a gate, '.' is not."""
    return [
        blocks.add(artifact, type="mcq" if g == "G" else "prose", content={"i": i}, is_gate=g == "G")
        for i, g in enumerate(gates)
    ]


# -- record ---------------------------------------------------------------------------------


def test_record_numbers_attempts_per_block(
    mgr: BlockResponseManager, blocks: ArtifactBlockManager, artifact: Artifact
) -> None:
    b1, b2 = add_blocks(blocks, artifact, "GG")
    r1 = mgr.record(b1, response={"choice": "a"}, passed=False, grader="exact", feedback="no")
    r2 = mgr.record(b1, response=["x", 1], passed=True, grader="model-x")
    r3 = mgr.record(b2, response="free text", passed=False, grader="self")
    assert (r1.attempt, r2.attempt, r3.attempt) == (1, 2, 1)
    assert r1.response == {"choice": "a"} and r1.feedback == "no" and r1.created_at is not None
    assert r2.response == ["x", 1]
    assert mgr.list(b1) == [r1, r2]
    assert b1.responses == [r1, r2]
    assert mgr.latest(b1) is r2
    assert mgr.attempt_count(b1) == 2
    assert mgr.attempt_count(b2) == 1


def test_record_continues_from_max_attempt(
    mgr: BlockResponseManager, blocks: ArtifactBlockManager, artifact: Artifact, session: Session
) -> None:
    (b,) = add_blocks(blocks, artifact, ".")
    r1 = mgr.record(b, response="a", passed=False, grader="exact")
    mgr.record(b, response="b", passed=False, grader="exact")
    session.delete(r1)
    session.flush()
    assert mgr.record(b, response="c", passed=True, grader="exact").attempt == 3


def test_record_round_trips_after_reload(
    mgr: BlockResponseManager, blocks: ArtifactBlockManager, artifact: Artifact, session: Session
) -> None:
    (b,) = add_blocks(blocks, artifact, ".")
    mgr.record(b, response={"ü": [1, {"k": None}]}, passed=True, grader="exact")
    session.commit()
    session.expire_all()
    assert mgr.latest(b).response == {"ü": [1, {"k": None}]}


def test_record_details_round_trip_and_null(
    mgr: BlockResponseManager, blocks: ArtifactBlockManager, artifact: Artifact, session: Session
) -> None:
    (b,) = add_blocks(blocks, artifact, "G")
    details = {"model": "m", "score": 0.5, "criteria": [{"criterion": "c", "met": None}]}
    with_details = mgr.record(b, response={"text": "a"}, passed=False, grader="j", details=details)
    without = mgr.record(b, response={"text": "b"}, passed=True, grader="j")
    session.commit()
    session.expire_all()
    first, second = mgr.list(b)
    assert first.details == details
    assert second.details is None
    raw = dict(
        session.execute(
            text("SELECT id, details IS NULL FROM block_responses WHERE block_id = :b"),
            {"b": b.id},
        ).all()
    )
    # SQL NULL, not the JSON text 'null', when there are no details
    assert raw == {with_details.id: 0, without.id: 1}


def test_record_verdict_missing_and_confidence(
    mgr: BlockResponseManager, blocks: ArtifactBlockManager, artifact: Artifact, session: Session
) -> None:
    (b,) = add_blocks(blocks, artifact, "G")
    plain = mgr.record(b, response={}, passed=False, grader="g")
    partial = mgr.record(
        b, response={}, passed=False, grader="g", verdict="partial",
        missing=("names the cost",), confidence="high",
    )
    passed = mgr.record(b, response={}, passed=True, grader="g", confidence="low")
    session.commit()
    session.expire_all()
    rows = [(r.verdict, r.missing, r.confidence) for r in mgr.list(b)]
    assert rows == [
        ("failed", [], None), ("partial", ["names the cost"], "high"), ("passed", [], "low")
    ]
    assert plain.id and partial.id and passed.id


def test_a_row_written_outside_the_managers_has_no_verdict(
    mgr: BlockResponseManager, blocks: ArtifactBlockManager, artifact: Artifact, session: Session
) -> None:
    (b,) = add_blocks(blocks, artifact, "G")
    session.execute(
        text(
            "INSERT INTO block_responses (block_id, attempt, response, passed, grader) "
            "VALUES (:b, 1, '{}', 0, 'raw')"
        ),
        {"b": b.id},
    )
    (row,) = mgr.list(b)
    assert row.verdict is None and row.missing == []
    with pytest.raises(ValueError, match="has no verdict: it was written outside the managers"):
        response_verdict(row)
    assert response_verdict(mgr.record(b, response={}, passed=True, grader="g")) == "passed"


@pytest.mark.parametrize(
    ("kwargs", "fragment"),
    [
        ({"passed": True, "verdict": "failed"}, "disagrees with passed"),
        ({"passed": False, "verdict": "passed"}, "disagrees with passed"),
        ({"passed": False, "verdict": "partial"}, "only for one"),
        ({"passed": False, "verdict": "failed", "missing": ("x",)}, "only for one"),
        ({"passed": False, "verdict": "partial", "missing": ("",)}, "non-blank"),
        ({"passed": False, "verdict": "partial", "missing": ["x"]}, "tuple"),
        ({"passed": False, "verdict": "meh"}, "verdict must be one of"),
        ({"passed": False, "confidence": "sure"}, "confidence must be one of"),
    ],
)
def test_record_rejects_inconsistent_verdicts(
    mgr: BlockResponseManager,
    blocks: ArtifactBlockManager,
    artifact: Artifact,
    kwargs: dict,
    fragment: str,
) -> None:
    (b,) = add_blocks(blocks, artifact, "G")
    with pytest.raises(ValueError, match=fragment):
        mgr.record(b, response={}, grader="g", **kwargs)
    assert mgr.attempt_count(b) == 0


@pytest.mark.parametrize(
    ("kwargs", "exc"),
    [
        ({"response": None}, ValueError),
        ({"response": 3}, TypeError),
        ({"response": {"x": object()}}, ValueError),
        ({"response": {"x": float("inf")}}, ValueError),
        ({"passed": 1}, TypeError),
        ({"grader": ""}, ValueError),
        ({"details": ["not", "a", "dict"]}, TypeError),
        ({"details": "text"}, TypeError),
        ({"details": {"x": object()}}, ValueError),
        ({"details": {"score": float("nan")}}, ValueError),
    ],
)
def test_record_validation(
    mgr: BlockResponseManager, blocks: ArtifactBlockManager, artifact: Artifact,
    session: Session, kwargs: dict, exc: type[Exception],
) -> None:
    (b,) = add_blocks(blocks, artifact, ".")
    args = {"response": "ok", "passed": True, "grader": "exact"} | kwargs
    with pytest.raises(exc):
        mgr.record(b, **args)
    assert mgr.attempt_count(b) == 0
    assert session.is_active


def test_record_attempt_race_maps_to_duplicate_error(
    mgr: BlockResponseManager, blocks: ArtifactBlockManager, artifact: Artifact,
    session: Session, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (b,) = add_blocks(blocks, artifact, ".")
    mgr.record(b, response="a", passed=False, grader="exact")
    monkeypatch.setattr(session, "scalar", lambda *a, **k: None)  # max(attempt) -> None -> 1
    with pytest.raises(DuplicateError):
        mgr.record(b, response="b", passed=False, grader="exact")
    monkeypatch.undo()
    assert session.is_active
    assert mgr.attempt_count(b) == 1


# -- pass state -----------------------------------------------------------------------------


def test_latest_and_is_passed(mgr: BlockResponseManager, blocks: ArtifactBlockManager, artifact: Artifact) -> None:
    (b,) = add_blocks(blocks, artifact, "G")
    assert mgr.latest(b) is None
    assert mgr.list(b) == []
    assert mgr.is_passed(b) is False
    mgr.record(b, response="a", passed=False, grader="exact")
    assert mgr.is_passed(b) is False
    mgr.record(b, response="b", passed=True, grader="exact")
    mgr.record(b, response="c", passed=False, grader="exact")
    assert mgr.is_passed(b) is True  # any passed attempt counts
    assert mgr.latest(b).passed is False


# -- progress / next_gate -------------------------------------------------------------------


def test_progress(mgr: BlockResponseManager, blocks: ArtifactBlockManager, artifact: Artifact) -> None:
    b1, b2, b3 = add_blocks(blocks, artifact, ".G.")
    mgr.record(b2, response="a", passed=False, grader="exact")
    mgr.record(b2, response="b", passed=True, grader="exact")
    mgr.record(b2, response="c", passed=False, grader="exact")
    mgr.record(b3, response="a", passed=False, grader="exact")
    assert mgr.progress(artifact) == [
        BlockProgress(b1, 0, False),
        BlockProgress(b2, 3, True),
        BlockProgress(b3, 1, False),
    ]


def test_progress_ignores_other_artifacts(
    mgr: BlockResponseManager, blocks: ArtifactBlockManager, artifact: Artifact,
    session: Session, settings: Settings,
) -> None:
    other = ArtifactManager(session, settings).create(slug="other", kind="guide", title="O", generator="g")
    (mine,) = add_blocks(blocks, artifact, "G")
    (theirs,) = add_blocks(blocks, other, "G")
    mgr.record(theirs, response="x", passed=True, grader="exact")
    assert mgr.progress(artifact) == [BlockProgress(mine, 0, False)]
    assert mgr.next_gate(artifact) is mine
    assert mgr.next_gate(other) is None


def test_progress_and_next_gate_empty(mgr: BlockResponseManager, artifact: Artifact) -> None:
    assert mgr.progress(artifact) == []
    assert mgr.next_gate(artifact) is None


def test_progress_uses_constant_queries(
    mgr: BlockResponseManager, blocks: ArtifactBlockManager, artifact: Artifact, session: Session
) -> None:
    bs = add_blocks(blocks, artifact, "G.G.G.")
    for b in bs:
        mgr.record(b, response="a", passed=True, grader="exact")
    session.expire_all()
    _ = artifact.id  # refresh the artifact itself outside the measured window
    statements: list[str] = []
    engine = session.get_bind()

    def count_select(conn, cursor, statement, *args) -> None:  # noqa: ANN001
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", count_select)
    try:
        progress = mgr.progress(artifact)
        mgr.next_gate(artifact)
    finally:
        event.remove(engine, "before_cursor_execute", count_select)
    assert [p.attempts for p in progress] == [1] * 6
    assert len(statements) == 3  # progress: blocks + aggregate; next_gate: one query


def test_next_gate_across_several_gates(
    mgr: BlockResponseManager, blocks: ArtifactBlockManager, artifact: Artifact
) -> None:
    b1, g1, b3, g2, g3 = add_blocks(blocks, artifact, ".G.GG")
    assert mgr.next_gate(artifact) is g1
    mgr.record(g1, response="wrong", passed=False, grader="exact")
    assert mgr.next_gate(artifact) is g1  # failed attempts do not open the gate
    mgr.record(g2, response="right", passed=True, grader="exact")
    assert mgr.next_gate(artifact) is g1  # passing a later gate does not skip an earlier one
    mgr.record(g1, response="right", passed=True, grader="exact")
    assert mgr.next_gate(artifact) is g3
    mgr.record(b1, response="note", passed=False, grader="self")  # non-gates never block
    mgr.record(g3, response="right", passed=True, grader="exact")
    assert mgr.next_gate(artifact) is None


def test_responses_cascade_when_block_deleted(
    mgr: BlockResponseManager, blocks: ArtifactBlockManager, artifact: Artifact, session: Session
) -> None:
    (b,) = add_blocks(blocks, artifact, "G")
    mgr.record(b, response="a", passed=True, grader="exact")
    session.expire_all()
    session.delete(blocks.get(b.id))
    session.flush()
    assert session.scalar(select(func.count()).select_from(BlockResponse)) == 0
