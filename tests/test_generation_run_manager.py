"""GenerationRunManager: run lifecycle, status rules, ordering and token totals."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from teacher.config import Settings
from teacher.db.models import Artifact, GenerationRun
from teacher.managers.artifacts import ArtifactManager
from teacher.managers.generation_runs import GenerationRunManager


@pytest.fixture
def artifact(session: Session, settings: Settings) -> Artifact:
    return ArtifactManager(session, settings).create(
        slug="guide-1", kind="guide", title="Guide", generator="g/1"
    )


@pytest.fixture
def mgr(session: Session, settings: Settings) -> GenerationRunManager:
    return GenerationRunManager(session, settings)


def test_start(mgr: GenerationRunManager, artifact: Artifact) -> None:
    run = mgr.start(artifact, stage="analyse", model="model-a")
    assert run.id is not None
    assert (run.status, run.stage, run.model, run.artifact_id) == ("running", "analyse", "model-a", artifact.id)
    assert run.started_at is not None and run.finished_at is None
    assert artifact.runs == [run]


def test_start_validates(mgr: GenerationRunManager, artifact: Artifact) -> None:
    with pytest.raises(ValueError):
        mgr.start(artifact, stage="", model="m")
    with pytest.raises(ValueError):
        mgr.start(artifact, stage="write", model="")


def test_succeed(mgr: GenerationRunManager, artifact: Artifact, settings: Settings, session: Session) -> None:
    run = mgr.start(artifact, stage="write", model="m")
    out = settings.derived_dir / "attention" / "write.json"
    mgr.succeed(run, input_tokens=100, output_tokens=20, output_file=out)
    session.expire(run)
    assert run.status == "succeeded"
    assert (run.input_tokens, run.output_tokens) == (100, 20)
    assert run.output_file == "derived/attention/write.json"
    assert run.finished_at is not None and run.finished_at >= run.started_at
    assert run.error is None


def test_succeed_accepts_relative_output_file(mgr: GenerationRunManager, artifact: Artifact) -> None:
    run = mgr.succeed(mgr.start(artifact, stage="write", model="m"), output_file="derived/x/out.json")
    assert run.output_file == "derived/x/out.json"
    assert run.input_tokens is None and run.output_tokens is None


def test_succeed_rejects_output_file_outside_root(
    mgr: GenerationRunManager, artifact: Artifact, tmp_path
) -> None:  # noqa: ANN001
    run = mgr.start(artifact, stage="write", model="m")
    with pytest.raises(ValueError):
        mgr.succeed(run, output_file="../escape.json")
    with pytest.raises(ValueError):
        mgr.succeed(run, output_file=tmp_path / "elsewhere.json")
    assert run.status == "running"


def test_fail(mgr: GenerationRunManager, artifact: Artifact, session: Session) -> None:
    run = mgr.start(artifact, stage="verify", model="m")
    mgr.fail(run, "rate limited", input_tokens=7)
    session.expire(run)
    assert (run.status, run.error, run.input_tokens, run.output_tokens) == ("failed", "rate limited", 7, None)
    assert run.finished_at is not None


def test_fail_requires_error(mgr: GenerationRunManager, artifact: Artifact) -> None:
    run = mgr.start(artifact, stage="verify", model="m")
    with pytest.raises(ValueError):
        mgr.fail(run, "")
    assert run.status == "running"


@pytest.mark.parametrize("finish", ["succeed", "fail"])
def test_finishing_twice_is_rejected(mgr: GenerationRunManager, artifact: Artifact, finish: str) -> None:
    run = mgr.start(artifact, stage="write", model="m")
    mgr.succeed(run) if finish == "succeed" else mgr.fail(run, "boom")
    status, finished_at = run.status, run.finished_at
    with pytest.raises(ValueError):
        mgr.succeed(run)
    with pytest.raises(ValueError):
        mgr.fail(run, "again")
    assert (run.status, run.finished_at) == (status, finished_at)


@pytest.mark.parametrize("tokens", [-1, 1.5, "10", True])
def test_token_validation(mgr: GenerationRunManager, artifact: Artifact, tokens: object) -> None:
    run = mgr.start(artifact, stage="write", model="m")
    with pytest.raises(ValueError):
        mgr.succeed(run, input_tokens=tokens)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        mgr.fail(run, "x", output_tokens=tokens)  # type: ignore[arg-type]
    assert run.status == "running"


def test_list_and_latest(mgr: GenerationRunManager, artifact: Artifact, session: Session) -> None:
    assert mgr.list(artifact) == []
    assert mgr.latest(artifact) is None
    a = mgr.start(artifact, stage="analyse", model="m")
    w1 = mgr.start(artifact, stage="write", model="m")
    w2 = mgr.start(artifact, stage="write", model="m")
    assert mgr.list(artifact) == [a, w1, w2]
    assert mgr.latest(artifact) is w2
    assert mgr.latest(artifact, stage="write") is w2
    assert mgr.latest(artifact, stage="analyse") is a
    assert mgr.latest(artifact, stage="verify") is None
    # ordering follows started_at, then id
    w1.started_at = w2.started_at + timedelta(seconds=1)
    session.flush()
    assert mgr.latest(artifact, stage="write") is w1
    assert mgr.list(artifact) == [a, w2, w1]


def test_token_totals(mgr: GenerationRunManager, artifact: Artifact, session: Session, settings: Settings) -> None:
    assert mgr.token_totals(artifact) == (0, 0)
    mgr.succeed(mgr.start(artifact, stage="analyse", model="m"), input_tokens=100, output_tokens=10)
    mgr.fail(mgr.start(artifact, stage="write", model="m"), "boom", input_tokens=50)
    mgr.start(artifact, stage="verify", model="m")  # still running, both NULL
    other = ArtifactManager(session, settings).create(slug="other", kind="guide", title="O", generator="g")
    mgr.succeed(mgr.start(other, stage="write", model="m"), input_tokens=999, output_tokens=999)
    assert mgr.token_totals(artifact) == (150, 10)
    totals = mgr.token_totals(artifact)
    assert all(type(t) is int for t in totals)


def test_runs_cascade_when_artifact_deleted(
    mgr: GenerationRunManager, artifact: Artifact, session: Session, settings: Settings
) -> None:
    mgr.start(artifact, stage="write", model="m")
    mgr.start(artifact, stage="verify", model="m")
    session.expire_all()
    ArtifactManager(session, settings).delete(artifact)
    assert session.scalar(select(func.count()).select_from(GenerationRun)) == 0
