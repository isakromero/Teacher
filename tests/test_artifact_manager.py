"""ArtifactManager: folders, rows, source links, statuses, supersedes chains and deletion."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from teacher.config import Settings
from teacher.db.engine import session_factory
from teacher.db.models import (
    Artifact,
    ArtifactBlock,
    ArtifactSource,
    BlockResponse,
    GenerationRun,
    KnowledgeSource,
)
from teacher.managers.artifact_blocks import ArtifactBlockManager
from teacher.managers.artifacts import ArtifactManager
from teacher.managers.block_responses import BlockResponseManager
from teacher.managers.errors import DuplicateError, NotFoundError
from teacher.managers.generation_runs import GenerationRunManager


def make_source(session: Session, slug: str = "attention") -> KnowledgeSource:
    source = KnowledgeSource(
        slug=slug,
        kind="paper",
        title=slug.title(),
        dir_path=f"knowledge_sources/{slug}",
        primary_file="source.pdf",
        media_type="application/pdf",
        content_sha256=f"{slug:0<64}"[:64],
    )
    session.add(source)
    session.flush()
    return source


@pytest.fixture
def mgr(session: Session, settings: Settings) -> ArtifactManager:
    return ArtifactManager(session, settings)


def create(mgr: ArtifactManager, slug: str = "guide-1", **kw: object) -> Artifact:
    kw.setdefault("kind", "guide")
    kw.setdefault("title", "A guide")
    kw.setdefault("generator", "pipeline/1")
    return mgr.create(slug=slug, **kw)  # type: ignore[arg-type]


def count(session: Session, model: type) -> int:
    return session.scalar(select(func.count()).select_from(model)) or 0


# -- create ---------------------------------------------------------------------------------


def test_create_makes_folders_and_row(mgr: ArtifactManager, settings: Settings) -> None:
    art = create(mgr, summary="s", params={"depth": 2})
    assert art.id is not None
    assert art.status == "generating"
    assert art.dir_path == "artifacts/guide-1"
    assert art.params == {"depth": 2}
    assert art.supersedes_id is None
    folder = settings.artifacts_dir / "guide-1"
    assert mgr.dir(art) == folder
    assert (folder / "blocks").is_dir()
    assert (folder / "assets").is_dir()


def test_create_defaults_params_to_empty_dict(mgr: ArtifactManager) -> None:
    assert create(mgr).params == {}


def test_create_persists_after_commit(mgr: ArtifactManager, session: Session, settings: Settings) -> None:
    source = make_source(session)
    create(mgr, sources=[source])
    session.commit()
    with session_factory(settings)() as other:
        art = ArtifactManager(other, settings).get_by_slug("guide-1")
        assert [s.slug for s in art.sources] == ["attention"]


def test_create_does_not_commit_callers_transaction(
    mgr: ArtifactManager, session: Session, settings: Settings
) -> None:
    """The internal SAVEPOINT must stay nested in the caller's transaction (pysqlite defers BEGIN)."""
    create(mgr)
    session.rollback()
    with session_factory(settings)() as other:
        assert count(other, Artifact) == 0


def test_failed_create_keeps_callers_earlier_work(
    mgr: ArtifactManager, session: Session, settings: Settings
) -> None:
    create(mgr)
    make_source(session)
    with pytest.raises(DuplicateError):
        create(mgr)
    session.commit()
    with session_factory(settings)() as other:
        assert count(other, Artifact) == 1
        assert count(other, KnowledgeSource) == 1


@pytest.mark.parametrize("slug", ["", "Bad", "a_b", "../x", "a--b", "-a", "a/b"])
def test_create_rejects_bad_slug(mgr: ArtifactManager, settings: Settings, slug: str) -> None:
    with pytest.raises(ValueError):
        create(mgr, slug=slug)
    assert not any(settings.artifacts_dir.iterdir())


def test_create_duplicate_slug(mgr: ArtifactManager, session: Session, settings: Settings) -> None:
    first = create(mgr)
    (settings.artifacts_dir / "guide-1" / "blocks" / "keep.txt").write_text("x")
    with pytest.raises(DuplicateError):
        create(mgr, title="Other")
    # the existing folder is untouched and the transaction is still usable
    assert (settings.artifacts_dir / "guide-1" / "blocks" / "keep.txt").exists()
    assert session.is_active
    assert mgr.list() == [first]


def test_create_rejects_existing_orphan_folder(mgr: ArtifactManager, settings: Settings) -> None:
    orphan = settings.artifacts_dir / "guide-1"
    orphan.mkdir(parents=True)
    (orphan / "note.txt").write_text("mine")
    with pytest.raises(DuplicateError):
        create(mgr)
    assert (orphan / "note.txt").read_text() == "mine"


def test_create_duplicate_slug_race_cleans_folder(
    mgr: ArtifactManager, session: Session, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the pre-check misses (another writer), the UNIQUE violation still maps to DuplicateError."""
    create(mgr)
    session.commit()
    # Simulate a racing writer: the row exists but its folder does not, and the pre-check is skipped.
    import shutil

    shutil.rmtree(settings.artifacts_dir / "guide-1")
    real_scalar = session.scalar
    monkeypatch.setattr(session, "scalar", lambda *a, **k: None)
    with pytest.raises(DuplicateError):
        create(mgr, title="Other")
    monkeypatch.setattr(session, "scalar", real_scalar)
    assert not (settings.artifacts_dir / "guide-1").exists()
    assert session.is_active
    assert count(session, Artifact) == 1


def test_create_cleans_folder_on_flush_failure(
    mgr: ArtifactManager, session: Session, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom() -> None:
        raise RuntimeError("flush failed")

    monkeypatch.setattr(session, "flush", boom)
    with pytest.raises(RuntimeError):
        create(mgr)
    assert not (settings.artifacts_dir / "guide-1").exists()


def test_create_validates_fields(mgr: ArtifactManager, session: Session) -> None:
    with pytest.raises(ValueError):
        create(mgr, title="")
    with pytest.raises(TypeError):
        create(mgr, params=["not", "a", "dict"])
    with pytest.raises(TypeError):
        create(mgr, supersedes="guide-0")
    with pytest.raises(ValueError):  # unsaved supersedes
        create(mgr, supersedes=Artifact(slug="x", kind="k", title="t", dir_path="d", generator="g"))
    assert count(session, Artifact) == 0


# -- sources --------------------------------------------------------------------------------


def test_create_with_sources_and_roles(mgr: ArtifactManager, session: Session) -> None:
    paper = make_source(session, "paper")
    ref = make_source(session, "ref")
    art = create(mgr, sources=[paper, (ref, "reference")])
    assert mgr.sources(art) == [(paper, "primary"), (ref, "reference")]
    assert art.sources == [paper, ref]


def test_create_rejects_source_listed_twice(
    mgr: ArtifactManager, session: Session, settings: Settings
) -> None:
    paper = make_source(session)
    with pytest.raises(DuplicateError):
        create(mgr, sources=[paper, (paper, "reference")])
    assert not (settings.artifacts_dir / "guide-1").exists()
    assert count(session, Artifact) == 0


def test_create_rejects_bad_source_items(mgr: ArtifactManager, session: Session) -> None:
    with pytest.raises(TypeError):
        create(mgr, sources=["attention"])
    unsaved = KnowledgeSource(slug="x")
    with pytest.raises(ValueError):
        create(mgr, sources=[unsaved])
    with pytest.raises(ValueError):
        create(mgr, sources=[(make_source(session), "")])
    assert unsaved not in session


def test_add_source(mgr: ArtifactManager, session: Session) -> None:
    paper = make_source(session, "paper")
    ref = make_source(session, "ref")
    art = create(mgr, sources=[paper])
    assert len(art.sources) == 1
    link = mgr.add_source(art, ref, role="reference")
    assert (link.artifact_id, link.source_id, link.role) == (art.id, ref.id, "reference")
    assert mgr.sources(art) == [(paper, "primary"), (ref, "reference")]
    assert art.sources == [paper, ref]  # relationship refreshed


def test_add_source_default_role(mgr: ArtifactManager, session: Session) -> None:
    art = create(mgr)
    paper = make_source(session)
    assert mgr.add_source(art, paper).role == "primary"


def test_add_source_duplicate(mgr: ArtifactManager, session: Session) -> None:
    paper = make_source(session)
    art = create(mgr, sources=[paper])
    with pytest.raises(DuplicateError):
        mgr.add_source(art, paper, role="reference")
    assert session.is_active
    assert mgr.sources(art) == [(paper, "primary")]


# -- lookups --------------------------------------------------------------------------------


def test_get_and_get_by_slug(mgr: ArtifactManager) -> None:
    art = create(mgr)
    assert mgr.get(art.id) is art
    assert mgr.get_by_slug("guide-1") is art
    with pytest.raises(NotFoundError):
        mgr.get(9999)
    with pytest.raises(NotFoundError):
        mgr.get_by_slug("nope")


def test_list_filters(mgr: ArtifactManager, session: Session) -> None:
    paper = make_source(session, "paper")
    other = make_source(session, "other")
    a = create(mgr, "a", sources=[paper])
    b = create(mgr, "b", kind="brief", sources=[paper, (other, "reference")])
    c = create(mgr, "c", sources=[other])
    mgr.mark_ready(b)
    assert mgr.list() == [a, b, c]
    assert mgr.list(kind="guide") == [a, c]
    assert mgr.list(status="ready") == [b]
    assert mgr.list(source=paper) == [a, b]
    assert mgr.list(kind="guide", source=other) == [c]
    with pytest.raises(ValueError):
        mgr.list(status="done")


# -- status ---------------------------------------------------------------------------------


def test_status_transitions(mgr: ArtifactManager, session: Session) -> None:
    art = create(mgr)
    assert mgr.mark_ready(art).status == "ready"
    assert mgr.mark_failed(art).status == "failed"
    assert mgr.set_status(art, "generating").status == "generating"
    session.expire(art)
    assert art.status == "generating"  # flushed to the database


@pytest.mark.parametrize("status", ["done", "READY", "", None])
def test_set_status_rejects_invalid(mgr: ArtifactManager, session: Session, status: object) -> None:
    art = create(mgr)
    with pytest.raises(ValueError):
        mgr.set_status(art, status)  # type: ignore[arg-type]
    assert art.status == "generating"
    assert session.is_active


# -- supersedes / latest_for_source ---------------------------------------------------------


def test_supersedes_chain_and_latest_for_source(mgr: ArtifactManager, session: Session) -> None:
    paper = make_source(session)
    assert mgr.latest_for_source(paper) is None
    v1 = create(mgr, "v1", sources=[paper])
    assert mgr.latest_for_source(paper) is v1
    v2 = create(mgr, "v2", sources=[paper], supersedes=v1)
    v3 = create(mgr, "v3", sources=[paper], supersedes=v2)
    assert v2.supersedes is v1 and v3.supersedes is v2
    assert v1.superseded_by == [v2]
    assert mgr.latest_for_source(paper) is v3


def test_latest_for_source_prefers_newest_and_filters_kind(
    mgr: ArtifactManager, session: Session
) -> None:
    paper = make_source(session)
    other = make_source(session, "other")
    guide = create(mgr, "guide", sources=[paper])
    brief = create(mgr, "brief", kind="brief", sources=[(paper, "reference")])
    create(mgr, "unrelated", sources=[other])
    assert mgr.latest_for_source(paper) is brief
    assert mgr.latest_for_source(paper, kind="guide") is guide
    assert mgr.latest_for_source(paper, kind="quiz") is None
    # newest by created_at, not by id
    guide.created_at = brief.created_at + timedelta(seconds=1)
    session.flush()
    assert mgr.latest_for_source(paper) is guide


def test_latest_for_source_superseded_by_artifact_of_other_source(
    mgr: ArtifactManager, session: Session
) -> None:
    paper = make_source(session)
    other = make_source(session, "other")
    old = create(mgr, "old", sources=[paper])
    create(mgr, "new", sources=[other], supersedes=old)
    assert mgr.latest_for_source(paper) is None


def test_deleting_superseded_artifact_sets_null(mgr: ArtifactManager, session: Session) -> None:
    paper = make_source(session)
    v1 = create(mgr, "v1", sources=[paper])
    v2 = create(mgr, "v2", sources=[paper], supersedes=v1)
    mgr.delete(v1)
    assert v2.supersedes_id is None
    session.expire(v2)
    assert v2.supersedes_id is None
    assert mgr.latest_for_source(paper) is v2


# -- delete ---------------------------------------------------------------------------------


def test_delete_removes_row_and_folder(mgr: ArtifactManager, session: Session, settings: Settings) -> None:
    paper = make_source(session)
    art = create(mgr, sources=[paper])
    folder = mgr.dir(art)
    mgr.delete(art)
    assert not folder.exists()
    with pytest.raises(NotFoundError):
        mgr.get_by_slug("guide-1")
    assert count(session, ArtifactSource) == 0
    assert session.get(KnowledgeSource, paper.id) is paper  # sources are not deleted


def test_delete_keep_files(mgr: ArtifactManager, session: Session) -> None:
    art = create(mgr)
    folder = mgr.dir(art)
    mgr.delete(art, remove_files=False)
    assert folder.is_dir()
    assert count(session, Artifact) == 0


def test_delete_with_folder_already_gone(mgr: ArtifactManager, session: Session) -> None:
    import shutil

    art = create(mgr)
    shutil.rmtree(mgr.dir(art))
    mgr.delete(art)
    assert count(session, Artifact) == 0


def test_delete_cascades_to_blocks_responses_runs(
    mgr: ArtifactManager, session: Session, settings: Settings
) -> None:
    paper = make_source(session)
    art = create(mgr, sources=[paper])
    blocks = ArtifactBlockManager(session, settings)
    block = blocks.add(art, type="mcq", content={"q": 1}, is_gate=True)
    BlockResponseManager(session, settings).record(block, response="a", passed=True, grader="exact")
    GenerationRunManager(session, settings).start(art, stage="write", model="m")
    session.commit()
    session.expunge_all()

    art = mgr.get_by_slug("guide-1")
    mgr.delete(art)
    session.commit()
    for model in (Artifact, ArtifactSource, ArtifactBlock, BlockResponse, GenerationRun):
        assert count(session, model) == 0, model
    assert count(session, KnowledgeSource) == 1
    assert not (settings.artifacts_dir / "guide-1").exists()
