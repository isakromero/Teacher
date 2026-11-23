"""Schema behaviour through the ORM: FKs, CHECKs, cascades, defaults and relationships."""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from teacher.config import Settings
from teacher.db.engine import session_scope
from teacher.db.models import (
    Artifact,
    ArtifactBlock,
    ArtifactSource,
    BlockResponse,
    BlockSegmentRef,
    GenerationRun,
    KnowledgeSource,
    KnowledgeSourceIdentifier,
    LabRunRecord,
    LabWorkspace,
    SourceSegment,
)

LOCATOR = {"type": "text_span", "start_char": 0, "end_char": 10}


def make_source(session: Session, slug: str = "attention", sha: str = "a" * 64) -> KnowledgeSource:
    source = KnowledgeSource(
        slug=slug,
        kind="paper",
        title="Attention Is All You Need",
        dir_path=f"knowledge_sources/{slug}",
        primary_file="source.pdf",
        media_type="application/pdf",
        content_sha256=sha,
    )
    session.add(source)
    session.flush()
    return source


def make_artifact(session: Session, slug: str = "guide-1", **kw: object) -> Artifact:
    artifact = Artifact(
        slug=slug, kind="guide", title="Guide", dir_path=f"artifacts/{slug}", generator="g/1", **kw
    )
    session.add(artifact)
    session.flush()
    return artifact


def make_block(session: Session, artifact: Artifact, position: int = 1) -> ArtifactBlock:
    block = ArtifactBlock(
        artifact=artifact,
        position=position,
        key=f"b{position:02d}",
        type="prose",
        content_file=f"blocks/b{position:02d}.json",
        content_sha256="b" * 64,
    )
    session.add(block)
    session.flush()
    return block


def count(session: Session, model: type) -> int:
    return session.scalar(select(func.count()).select_from(model)) or 0


def populate(session: Session) -> tuple[KnowledgeSource, Artifact]:
    """A source with an identifier and nested segments; an artifact using it with blocks etc."""
    source = make_source(session)
    source.identifiers.append(KnowledgeSourceIdentifier(scheme="arxiv", value="1706.03762"))
    sec = SourceSegment(
        source=source, kind="section", label="3", position=1, locator_type="text_span",
        locator=LOCATOR,
    )
    sub = SourceSegment(
        source=source, kind="section", label="3.1", position=2, locator_type="text_span",
        locator=LOCATOR, parent=sec,
    )
    session.add_all([sec, sub])
    artifact = make_artifact(session)
    artifact.source_links.append(ArtifactSource(source=source))
    block = make_block(session, artifact)
    block.segments.append(sub)
    block.responses.append(BlockResponse(attempt=1, response={"a": 1}, passed=True, grader="exact"))
    artifact.runs.append(GenerationRun(stage="write", model="m"))
    session.flush()
    return source, artifact


# --- foreign keys -----------------------------------------------------------------------------


def test_foreign_keys_pragma_on(session: Session) -> None:
    assert session.execute(text("PRAGMA foreign_keys")).scalar() == 1


def test_foreign_key_violation_rejected(session: Session) -> None:
    session.add(
        SourceSegment(
            source_id=999, kind="section", label="1", position=1, locator_type="text_span",
            locator=LOCATOR,
        )
    )
    with pytest.raises(IntegrityError, match="FOREIGN KEY"):
        session.flush()


# --- CHECK constraints ------------------------------------------------------------------------


def test_artifact_status_check(session: Session) -> None:
    artifact = make_artifact(session)
    for ok in ("generating", "ready", "failed"):
        artifact.status = ok
        session.flush()
    artifact.status = "done"
    with pytest.raises(IntegrityError, match="ck_artifacts_status"):
        session.flush()


def test_generation_run_status_check(session: Session) -> None:
    artifact = make_artifact(session)
    run = GenerationRun(artifact=artifact, stage="write", model="m")
    session.add(run)
    session.flush()
    assert run.status == "running"
    for ok in ("succeeded", "failed", "running"):
        run.status = ok
        session.flush()
    run.status = "ready"
    with pytest.raises(IntegrityError, match="ck_generation_runs_status"):
        session.flush()


@pytest.mark.parametrize(
    ("table", "column", "constraint"),
    [
        ("knowledge_sources", "creators", "ck_knowledge_sources_creators_json"),
        ("knowledge_sources", "metadata", "ck_knowledge_sources_metadata_json"),
        ("artifacts", "params", "ck_artifacts_params_json"),
    ],
)
def test_json_valid_checks_on_update(
    session: Session, table: str, column: str, constraint: str
) -> None:
    make_source(session)
    make_artifact(session)
    with pytest.raises(IntegrityError, match=constraint):
        session.execute(text(f'UPDATE {table} SET "{column}" = :v'), {"v": "{not json"})


def test_json_valid_check_on_locator_and_response(session: Session) -> None:
    source = make_source(session)
    with pytest.raises(IntegrityError, match="ck_source_segments_locator_json"):
        with session.begin_nested():
            session.execute(
                text(
                    "INSERT INTO source_segments (source_id, kind, label, position, locator_type,"
                    " locator) VALUES (:s, 'section', '1', 1, 'text_span', 'nope')"
                ),
                {"s": source.id},
            )
    block = make_block(session, make_artifact(session))
    with pytest.raises(IntegrityError, match="ck_block_responses_response_json"):
        session.execute(
            text(
                "INSERT INTO block_responses (block_id, attempt, response, passed, grader)"
                " VALUES (:b, 1, '[1,', 0, 'exact')"
            ),
            {"b": block.id},
        )


def test_unique_constraints(session: Session) -> None:
    make_source(session, slug="one", sha="1" * 64)
    with pytest.raises(IntegrityError, match="UNIQUE"):
        with session.begin_nested():
            make_source(session, slug="two", sha="1" * 64)  # same content hash
    artifact = make_artifact(session)
    make_block(session, artifact, 1)
    with pytest.raises(IntegrityError, match="UNIQUE"):
        make_block(session, artifact, 1)


# --- defaults ---------------------------------------------------------------------------------


def test_server_defaults_apply_to_raw_inserts(session: Session) -> None:
    session.execute(
        text(
            "INSERT INTO knowledge_sources (slug, kind, title, dir_path, primary_file, media_type,"
            " content_sha256) VALUES ('raw', 'paper', 'T', 'knowledge_sources/raw', 'source.pdf',"
            " 'application/pdf', 'c')"
        )
    )
    source = session.scalars(select(KnowledgeSource).where(KnowledgeSource.slug == "raw")).one()
    assert source.creators == []
    assert source.metadata_ == {}
    assert isinstance(source.added_at, datetime) and isinstance(source.updated_at, datetime)
    session.execute(
        text(
            "INSERT INTO artifacts (slug, kind, title, dir_path, generator)"
            " VALUES ('raw', 'guide', 'T', 'artifacts/raw', 'g')"
        )
    )
    artifact = session.scalars(select(Artifact).where(Artifact.slug == "raw")).one()
    assert artifact.status == "generating"
    assert artifact.params == {}
    assert isinstance(artifact.created_at, datetime)


def test_python_defaults_and_updated_at(session: Session) -> None:
    source = make_source(session)
    assert source.creators == [] and source.metadata_ == {}
    assert source.added_at.tzinfo is None
    first = source.updated_at
    source.title = "Changed"
    session.flush()
    assert source.updated_at >= first
    artifact = make_artifact(session)
    assert artifact.status == "generating" and artifact.params == {}
    block = make_block(session, artifact)
    assert block.is_gate is False
    link = ArtifactSource(artifact=artifact, source=source)
    session.add(link)
    session.flush()
    assert link.role == "primary"


def test_metadata_attribute_maps_metadata_column(session: Session) -> None:
    source = make_source(session)
    source.metadata_ = {"venue": "NeurIPS"}
    session.flush()
    raw = session.execute(
        text("SELECT metadata FROM knowledge_sources WHERE id = :i"), {"i": source.id}
    ).scalar()
    assert raw == '{"venue": "NeurIPS"}'


# --- cascades ---------------------------------------------------------------------------------


def test_db_cascade_on_artifact_delete(session: Session) -> None:
    source, artifact = populate(session)
    session.execute(delete(Artifact).where(Artifact.id == artifact.id))
    session.expire_all()
    for model in (ArtifactBlock, BlockResponse, BlockSegmentRef, GenerationRun, ArtifactSource):
        assert count(session, model) == 0, model
    assert count(session, KnowledgeSource) == 1
    assert count(session, SourceSegment) == 2


def test_db_cascade_on_source_delete(session: Session) -> None:
    source, artifact = populate(session)
    session.execute(delete(Artifact))  # release the RESTRICT link first
    session.execute(delete(KnowledgeSource).where(KnowledgeSource.id == source.id))
    session.expire_all()
    for model in (KnowledgeSourceIdentifier, SourceSegment, BlockSegmentRef):
        assert count(session, model) == 0, model


def test_segment_delete_cascades_children_and_refs(session: Session) -> None:
    source, artifact = populate(session)
    parent = session.scalars(select(SourceSegment).where(SourceSegment.label == "3")).one()
    session.execute(delete(SourceSegment).where(SourceSegment.id == parent.id))
    session.expire_all()
    assert count(session, SourceSegment) == 0
    assert count(session, BlockSegmentRef) == 0
    assert count(session, ArtifactBlock) == 1


def test_orm_delete_cascades(session: Session) -> None:
    source, artifact = populate(session)
    session.delete(artifact)
    session.flush()
    for model in (ArtifactBlock, BlockResponse, BlockSegmentRef, GenerationRun, ArtifactSource):
        assert count(session, model) == 0, model
    session.delete(source)
    session.flush()
    assert count(session, KnowledgeSourceIdentifier) == 0
    assert count(session, SourceSegment) == 0


def test_source_delete_restricted_while_used_by_artifact(session: Session) -> None:
    source, _ = populate(session)
    with pytest.raises(IntegrityError, match="FOREIGN KEY"):
        with session.begin_nested():
            session.execute(delete(KnowledgeSource).where(KnowledgeSource.id == source.id))
    with pytest.raises(IntegrityError, match="FOREIGN KEY"):
        session.delete(source)
        session.flush()


def test_lab_rows_cascade_with_their_artifact(session: Session) -> None:
    lab = make_artifact(session, "lab-1")
    other = make_artifact(session, "lab-2")
    lab.lab_workspace = LabWorkspace(files=[{"path": "a.py", "content": "x = 1"}])
    lab.lab_runs.append(LabRunRecord(kind="run", passed=True, files=[], output="1"))
    lab.lab_runs.append(
        LabRunRecord(kind="check", step_index=2, passed=False, files=[], output="", error="no")
    )
    other.lab_runs.append(LabRunRecord(kind="run", passed=True, files=[], output=""))
    session.flush()
    session.expire_all()
    lab = session.get(Artifact, lab.id)  # type: ignore[assignment]
    assert lab.lab_workspace is not None and lab.lab_workspace.files[0]["path"] == "a.py"
    assert lab.lab_workspace.solution_revealed_at is None
    assert [(r.kind, r.step_index) for r in lab.lab_runs] == [("run", None), ("check", 2)]
    assert isinstance(lab.lab_runs[0].created_at, datetime)

    session.execute(delete(Artifact).where(Artifact.id == lab.id))  # the database cascade
    session.expire_all()
    assert count(session, LabWorkspace) == 0
    assert count(session, LabRunRecord) == 1
    session.delete(session.get(Artifact, other.id))  # the ORM cascade
    session.flush()
    assert count(session, LabRunRecord) == 0


def test_lab_run_checks(session: Session) -> None:
    lab = make_artifact(session, "lab-1")
    for values, constraint in [
        ({"kind": "test"}, "ck_lab_runs_kind"),
        ({"kind": "check"}, "ck_lab_runs_check_step"),
        ({"kind": "check", "step_index": -1}, "ck_lab_runs_step_index"),
    ]:
        with pytest.raises(IntegrityError, match=constraint):
            with session.begin_nested():
                session.add(
                    LabRunRecord(artifact=lab, passed=False, files=[], output="", **values)
                )
                session.flush()


def test_supersedes_set_null(session: Session) -> None:
    old = make_artifact(session, "v1")
    new = make_artifact(session, "v2", supersedes=old)
    assert new.supersedes_id == old.id
    assert old.superseded_by == [new]
    session.execute(delete(Artifact).where(Artifact.id == old.id))
    session.expire_all()
    assert session.get(Artifact, new.id).supersedes_id is None  # type: ignore[union-attr]


# --- relationships ----------------------------------------------------------------------------


def test_relationships_and_ordering(session: Session) -> None:
    source, artifact = populate(session)
    make_block(session, artifact, 3)
    make_block(session, artifact, 2)
    block1 = artifact.blocks[0]
    block1.responses.append(BlockResponse(attempt=2, response="x", passed=False, grader="self"))
    session.flush()
    session.expire_all()

    artifact = session.get(Artifact, artifact.id)  # type: ignore[assignment]
    assert [b.position for b in artifact.blocks] == [1, 2, 3]
    assert [r.attempt for r in artifact.blocks[0].responses] == [1, 2]
    assert artifact.sources == [source]
    assert [link.role for link in artifact.source_links] == ["primary"]
    assert [r.stage for r in artifact.runs] == ["write"]
    assert source.artifacts == [artifact]
    assert [i.value for i in source.identifiers] == ["1706.03762"]
    assert [s.label for s in source.segments] == ["3", "3.1"]
    parent, child = source.segments
    assert child.parent is parent and parent.children == [child]
    assert artifact.blocks[0].segments == [child]
    assert child.blocks == [artifact.blocks[0]]
    assert artifact.blocks[0].responses[0].block is artifact.blocks[0]


def test_session_scope_commits_and_rolls_back(settings: Settings) -> None:
    with session_scope(settings) as s:
        make_source(s, slug="kept", sha="k" * 64)
    with pytest.raises(RuntimeError):
        with session_scope(settings) as s:
            make_source(s, slug="dropped", sha="d" * 64)
            raise RuntimeError("boom")
    with session_scope(settings) as s:
        slugs = s.scalars(select(KnowledgeSource.slug)).all()
        assert slugs == ["kept"]
        kept = s.scalars(select(KnowledgeSource)).one()
    assert kept.slug == "kept"  # expire_on_commit=False: usable after the scope closes
