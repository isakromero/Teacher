"""ArtifactBlockManager: appending immutable block files, reading/verifying them, segment refs, check()."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from teacher.config import Settings
from teacher.db.models import (
    Artifact,
    ArtifactBlock,
    BlockSegmentRef,
    KnowledgeSource,
    SourceSegment,
)
from teacher.managers.artifact_blocks import ArtifactBlockManager, block_key
from teacher.managers.artifacts import ArtifactManager
from teacher.managers.errors import (
    DuplicateError,
    IntegrityCheckError,
    Issue,
    NotFoundError,
)


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


def make_segment(session: Session, source: KnowledgeSource, label: str, position: int = 1) -> SourceSegment:
    segment = SourceSegment(
        source_id=source.id,
        kind="section",
        label=label,
        position=position,
        locator_type="text_span",
        locator={"type": "text_span", "start_char": 0, "end_char": 10},
    )
    session.add(segment)
    session.flush()
    return segment


@pytest.fixture
def paper(session: Session) -> KnowledgeSource:
    return make_source(session)


@pytest.fixture
def artifact(session: Session, settings: Settings, paper: KnowledgeSource) -> Artifact:
    return ArtifactManager(session, settings).create(
        slug="guide-1", kind="guide", title="Guide", generator="g/1", sources=[paper]
    )


@pytest.fixture
def mgr(session: Session, settings: Settings) -> ArtifactBlockManager:
    return ArtifactBlockManager(session, settings)


def blocks_dir(settings: Settings) -> Path:
    return settings.artifacts_dir / "guide-1" / "blocks"


# -- add ------------------------------------------------------------------------------------


def test_block_key() -> None:
    assert block_key(1) == "b01"
    assert block_key(12) == "b12"
    assert block_key(100) == "b100"


def test_add_appends_with_positions_and_keys(
    mgr: ArtifactBlockManager, artifact: Artifact, settings: Settings
) -> None:
    b1 = mgr.add(artifact, type="prose", content={"text": "one"})
    b2 = mgr.add(artifact, type="mcq", content={"q": "two"}, is_gate=True)
    b3 = mgr.add(artifact, type="prose", content={"text": "three"})
    assert [(b.position, b.key, b.content_file) for b in (b1, b2, b3)] == [
        (1, "b01", "blocks/b01.json"),
        (2, "b02", "blocks/b02.json"),
        (3, "b03", "blocks/b03.json"),
    ]
    assert (b1.is_gate, b2.is_gate) == (False, True)
    assert mgr.list(artifact) == [b1, b2, b3]
    assert artifact.blocks == [b1, b2, b3]
    assert sorted(p.name for p in blocks_dir(settings).iterdir()) == ["b01.json", "b02.json", "b03.json"]


def test_add_uses_max_position_plus_one(
    mgr: ArtifactBlockManager, artifact: Artifact, session: Session, settings: Settings
) -> None:
    b1 = mgr.add(artifact, type="prose", content={})
    mgr.add(artifact, type="prose", content={})
    session.delete(b1)  # gaps are not refilled: next is max + 1, not count + 1
    session.flush()
    (blocks_dir(settings) / "b01.json").unlink()
    assert mgr.add(artifact, type="prose", content={}).key == "b03"


def test_add_file_format_and_hash(mgr: ArtifactBlockManager, artifact: Artifact, settings: Settings) -> None:
    content = {"text": "Äpfel – naïve ✓", "z": 1, "a": [1, 2]}
    block = mgr.add(artifact, type="prose", content=content)
    raw = (blocks_dir(settings) / "b01.json").read_bytes()
    assert raw.decode("utf-8").startswith('{\n  "text": "Äpfel – naïve ✓",\n  "z": 1')  # indent 2, no escapes, key order kept
    assert block.content_sha256 == hashlib.sha256(raw).hexdigest()
    assert json.loads(raw) == content


def test_read_content_round_trip(mgr: ArtifactBlockManager, artifact: Artifact) -> None:
    content = {"text": "π ≈ 3.14", "nested": {"list": [1, None, True, "x"]}, "n": 2.5}
    block = mgr.add(artifact, type="prose", content=content)
    assert mgr.read_content(block) == content
    assert mgr.read_content(mgr.get(block.id), verify=False) == content


@pytest.mark.parametrize("content", [["a list"], "text", None, 3])
def test_add_rejects_non_dict(mgr: ArtifactBlockManager, artifact: Artifact, settings: Settings, content: object) -> None:
    with pytest.raises(TypeError):
        mgr.add(artifact, type="prose", content=content)  # type: ignore[arg-type]
    assert list(blocks_dir(settings).iterdir()) == []


@pytest.mark.parametrize("content", [{"x": object()}, {"x": float("nan")}, {"x": {1, 2}}])
def test_add_rejects_non_serialisable(
    mgr: ArtifactBlockManager, artifact: Artifact, settings: Settings, session: Session, content: dict
) -> None:
    with pytest.raises(ValueError):
        mgr.add(artifact, type="prose", content=content)
    assert list(blocks_dir(settings).iterdir()) == []
    assert mgr.list(artifact) == []


def test_add_rejects_empty_type(mgr: ArtifactBlockManager, artifact: Artifact) -> None:
    with pytest.raises(ValueError):
        mgr.add(artifact, type="", content={})


def test_add_removes_file_when_insert_fails(
    mgr: ArtifactBlockManager, artifact: Artifact, settings: Settings, session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mgr.add(artifact, type="prose", content={"n": 1})

    def boom() -> None:
        raise RuntimeError("flush failed")

    monkeypatch.setattr(session, "flush", boom)
    with pytest.raises(RuntimeError):
        mgr.add(artifact, type="prose", content={"n": 2})
    monkeypatch.undo()
    assert sorted(p.name for p in blocks_dir(settings).iterdir()) == ["b01.json"]
    assert [b.key for b in mgr.list(artifact)] == ["b01"]


def test_add_duplicate_position_race_maps_to_duplicate_error(
    mgr: ArtifactBlockManager, artifact: Artifact, settings: Settings, session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If another writer took the position between the max() query and the insert."""
    first = mgr.add(artifact, type="prose", content={"n": 1})
    (blocks_dir(settings) / "b01.json").rename(blocks_dir(settings) / "moved.json")
    monkeypatch.setattr(session, "scalar", lambda *a, **k: None)  # max(position) -> None -> 1
    with pytest.raises(DuplicateError):
        mgr.add(artifact, type="prose", content={"n": 2})
    monkeypatch.undo()
    assert not (blocks_dir(settings) / "b01.json").exists()  # the file this call wrote is gone
    assert session.is_active
    assert mgr.list(artifact) == [first]


def test_add_refuses_to_overwrite_orphan_file(
    mgr: ArtifactBlockManager, artifact: Artifact, settings: Settings
) -> None:
    orphan = blocks_dir(settings) / "b01.json"
    orphan.write_text("{}")
    with pytest.raises(IntegrityCheckError):
        mgr.add(artifact, type="prose", content={"n": 1})
    assert orphan.read_text() == "{}"
    assert mgr.list(artifact) == []


def test_add_with_missing_blocks_folder(
    mgr: ArtifactBlockManager, artifact: Artifact, settings: Settings
) -> None:
    blocks_dir(settings).rmdir()
    with pytest.raises(IntegrityCheckError):
        mgr.add(artifact, type="prose", content={})


# -- lookups --------------------------------------------------------------------------------


def test_get_and_get_by_key(mgr: ArtifactBlockManager, artifact: Artifact) -> None:
    block = mgr.add(artifact, type="prose", content={})
    assert mgr.get(block.id) is block
    assert mgr.get_by_key(artifact, "b01") is block
    with pytest.raises(NotFoundError):
        mgr.get(9999)
    with pytest.raises(NotFoundError):
        mgr.get_by_key(artifact, "b02")


def test_no_update_method() -> None:
    assert not hasattr(ArtifactBlockManager, "update")


# -- read_content integrity -----------------------------------------------------------------


def test_read_content_hash_mismatch(mgr: ArtifactBlockManager, artifact: Artifact, settings: Settings) -> None:
    block = mgr.add(artifact, type="prose", content={"text": "original"})
    path = blocks_dir(settings) / "b01.json"
    path.write_text(json.dumps({"text": "tampered"}), encoding="utf-8")
    with pytest.raises(IntegrityCheckError):
        mgr.read_content(block)
    assert mgr.read_content(block, verify=False) == {"text": "tampered"}


def test_read_content_missing_file(mgr: ArtifactBlockManager, artifact: Artifact, settings: Settings) -> None:
    block = mgr.add(artifact, type="prose", content={})
    (blocks_dir(settings) / "b01.json").unlink()
    with pytest.raises(IntegrityCheckError):
        mgr.read_content(block)
    with pytest.raises(IntegrityCheckError):
        mgr.read_content(block, verify=False)


def test_read_content_corrupt_json_unverified(
    mgr: ArtifactBlockManager, artifact: Artifact, settings: Settings
) -> None:
    block = mgr.add(artifact, type="prose", content={})
    (blocks_dir(settings) / "b01.json").write_text("[1, 2]")
    with pytest.raises(IntegrityCheckError):
        mgr.read_content(block, verify=False)
    (blocks_dir(settings) / "b01.json").write_text("{not json")
    with pytest.raises(IntegrityCheckError):
        mgr.read_content(block, verify=False)


# -- check ----------------------------------------------------------------------------------


def test_check_clean(mgr: ArtifactBlockManager, artifact: Artifact) -> None:
    mgr.add(artifact, type="prose", content={"a": 1})
    mgr.add(artifact, type="prose", content={"b": 2})
    assert mgr.check(artifact) == []


def test_check_reports_issues(mgr: ArtifactBlockManager, artifact: Artifact, settings: Settings) -> None:
    mgr.add(artifact, type="prose", content={"a": 1})
    mgr.add(artifact, type="prose", content={"b": 2})
    mgr.add(artifact, type="prose", content={"c": 3})
    folder = blocks_dir(settings)
    (folder / "b01.json").unlink()
    (folder / "b02.json").write_text('{"b": 3}')
    (folder / "b07.json").write_text("{}")
    issues = mgr.check(artifact)
    assert [(i.code, i.slug) for i in issues] == [
        ("missing_file", "guide-1"),
        ("hash_mismatch", "guide-1"),
        ("orphan_file", "guide-1"),
    ]
    assert issues[0].detail.startswith("b01")
    assert issues[1].detail.startswith("b02")
    assert issues[2].detail.startswith("b07")
    assert all(isinstance(i, Issue) for i in issues)


def test_check_missing_dir(mgr: ArtifactBlockManager, artifact: Artifact, settings: Settings) -> None:
    import shutil

    mgr.add(artifact, type="prose", content={})
    shutil.rmtree(blocks_dir(settings))
    assert [i.code for i in mgr.check(artifact)] == ["missing_dir", "missing_file"]


# -- segment refs ---------------------------------------------------------------------------


def test_add_with_segments(mgr: ArtifactBlockManager, artifact: Artifact, session: Session, paper: KnowledgeSource) -> None:
    s2 = make_segment(session, paper, "2", position=2)
    s1 = make_segment(session, paper, "1", position=1)
    block = mgr.add(artifact, type="prose", content={}, segments=[s2, s1, s2])
    assert mgr.segments(block) == [s1, s2]
    assert block.segments == [s1, s2]
    assert s1.blocks == [block]


def test_add_with_segment_from_reference_source(
    mgr: ArtifactBlockManager, artifact: Artifact, session: Session, settings: Settings
) -> None:
    ref = make_source(session, "ref")
    ArtifactManager(session, settings).add_source(artifact, ref, role="reference")
    seg = make_segment(session, ref, "Figure 1")
    block = mgr.add(artifact, type="figure", content={}, segments=[seg])
    assert mgr.segments(block) == [seg]


def test_add_rejects_segment_of_unlinked_source(
    mgr: ArtifactBlockManager, artifact: Artifact, session: Session, settings: Settings
) -> None:
    other = make_source(session, "other")
    seg = make_segment(session, other, "1")
    with pytest.raises(ValueError):
        mgr.add(artifact, type="prose", content={}, segments=[seg])
    assert list(blocks_dir(settings).iterdir()) == []
    assert mgr.list(artifact) == []


def test_add_and_remove_segment_ref(
    mgr: ArtifactBlockManager, artifact: Artifact, session: Session, paper: KnowledgeSource
) -> None:
    s1 = make_segment(session, paper, "1", position=1)
    s2 = make_segment(session, paper, "2", position=2)
    block = mgr.add(artifact, type="prose", content={}, segments=[s1])
    assert block.segments == [s1]
    mgr.add_segment_ref(block, s2)
    assert mgr.segments(block) == [s1, s2]
    assert block.segments == [s1, s2]
    with pytest.raises(DuplicateError):
        mgr.add_segment_ref(block, s2)
    assert session.is_active
    mgr.remove_segment_ref(block, s1)
    assert mgr.segments(block) == [s2]
    assert block.segments == [s2]
    assert s1.blocks == []
    with pytest.raises(NotFoundError):
        mgr.remove_segment_ref(block, s1)


def test_add_segment_ref_rejects_unlinked_source(
    mgr: ArtifactBlockManager, artifact: Artifact, session: Session
) -> None:
    seg = make_segment(session, make_source(session, "other"), "1")
    block = mgr.add(artifact, type="prose", content={})
    with pytest.raises(ValueError):
        mgr.add_segment_ref(block, seg)
    assert mgr.segments(block) == []


def test_segment_refs_cascade(
    mgr: ArtifactBlockManager, artifact: Artifact, session: Session, paper: KnowledgeSource
) -> None:
    s1 = make_segment(session, paper, "1", position=1)
    s2 = make_segment(session, paper, "2", position=2)
    b1 = mgr.add(artifact, type="prose", content={}, segments=[s1, s2])
    session.delete(s1)
    session.flush()
    session.expire_all()
    assert mgr.segments(b1) == [s2]
    session.delete(b1)
    session.flush()
    assert session.scalar(select(func.count()).select_from(BlockSegmentRef)) == 0
    assert session.scalar(select(func.count()).select_from(ArtifactBlock)) == 0
