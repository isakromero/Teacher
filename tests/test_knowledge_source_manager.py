"""KnowledgeSourceManager: files + rows, identifiers, duplicates, cleanup, delete and check()."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from teacher.config import Settings
from teacher.db.engine import session_factory
from teacher.db.models import (
    Artifact,
    ArtifactSource,
    KnowledgeSource,
    KnowledgeSourceIdentifier,
    SourceSegment,
)
from teacher.files import sha256_bytes
from teacher.managers.errors import DuplicateError, Issue, ManagerError, NotFoundError
from teacher.managers.knowledge_sources import KnowledgeSourceManager, SourceInUseError

PDF_BYTES = b"%PDF-1.7\nattention is all you need\n"


@pytest.fixture
def inputs(tmp_path: Path) -> Path:
    """A folder outside `settings.root` holding files to import."""
    folder = tmp_path / "inputs"
    folder.mkdir()
    return folder


@pytest.fixture
def ksm(session: Session, settings: Settings) -> KnowledgeSourceManager:
    return KnowledgeSourceManager(session, settings)


def write(folder: Path, name: str, data: bytes) -> Path:
    path = folder / name
    path.write_bytes(data)
    return path


def add_paper(
    ksm: KnowledgeSourceManager,
    inputs: Path,
    slug: str = "attention",
    data: bytes = PDF_BYTES,
    **kw: object,
) -> KnowledgeSource:
    return ksm.add(
        write(inputs, f"{slug}.pdf", data), slug=slug, kind="paper", title=slug.title(), **kw
    )


def count(session: Session, model: type) -> int:
    return session.scalar(select(func.count()).select_from(model)) or 0


# -- add ----------------------------------------------------------------------------------


def test_add_copies_file_and_inserts_row(
    ksm: KnowledgeSourceManager, settings: Settings, inputs: Path
) -> None:
    src = write(inputs, "Attention.PDF", PDF_BYTES)
    source = ksm.add(
        src,
        slug="attention",
        kind="paper",
        title="Attention Is All You Need",
        creators=["Vaswani", "Shazeer"],
        originated_on="2017",
        origin_url="https://arxiv.org/abs/1706.03762",
        metadata={"venue": "NeurIPS"},
        notes="classic",
        identifiers={"arxiv": "1706.03762", "doi": "10.48550/arXiv.1706.03762"},
    )
    assert source.id is not None
    assert source.dir_path == "knowledge_sources/attention"
    assert source.primary_file == "source.pdf"
    assert source.media_type == "application/pdf"
    assert source.content_sha256 == sha256_bytes(PDF_BYTES)
    assert source.creators == ["Vaswani", "Shazeer"]
    assert source.metadata_ == {"venue": "NeurIPS"}
    assert source.added_at is not None and source.updated_at is not None
    assert {(i.scheme, i.value) for i in source.identifiers} == {
        ("arxiv", "1706.03762"),
        ("doi", "10.48550/arXiv.1706.03762"),
    }
    path = ksm.primary_path(source)
    assert path == settings.root / "knowledge_sources" / "attention" / "source.pdf"
    assert path.read_bytes() == PDF_BYTES
    assert src.exists()  # copied, not moved


def test_add_extra_files_keep_their_names(
    ksm: KnowledgeSourceManager, settings: Settings, inputs: Path
) -> None:
    extra = write(inputs, "supplement.zip", b"zip")
    source = add_paper(ksm, inputs, extra_files=[extra])
    folder = settings.sources_dir / source.slug
    assert sorted(p.name for p in folder.iterdir()) == ["source.pdf", "supplement.zip"]


def test_add_defaults_and_media_type_fallback(
    ksm: KnowledgeSourceManager, session: Session, inputs: Path
) -> None:
    source = ksm.add(
        write(inputs, "blob.unknownext", b"x"),
        slug="blob",
        kind="web_page",
        title="Blob",
        creators=None,
        metadata=None,
    )
    assert source.primary_file == "source.unknownext"
    assert source.media_type == "application/octet-stream"
    row = session.execute(
        text("SELECT creators, metadata FROM knowledge_sources WHERE id = :id"), {"id": source.id}
    ).one()
    assert tuple(row) == ("[]", "{}")


def test_add_explicit_media_type_wins(ksm: KnowledgeSourceManager, inputs: Path) -> None:
    source = add_paper(ksm, inputs, media_type="application/x-custom")
    assert source.media_type == "application/x-custom"


@pytest.mark.parametrize("slug", ["Attention", "../escape", "a_b", "", "-a", "a--b", "a/b"])
def test_add_rejects_bad_slug(
    ksm: KnowledgeSourceManager, settings: Settings, inputs: Path, slug: str
) -> None:
    with pytest.raises(ValueError):
        ksm.add(write(inputs, "p.pdf", PDF_BYTES), slug=slug, kind="paper", title="T")
    assert not settings.sources_dir.exists() or not any(settings.sources_dir.iterdir())


@pytest.mark.parametrize(
    "kw",
    [
        {"creators": "Vaswani"},
        {"creators": ["ok", 3]},
        {"metadata": ["not", "a", "dict"]},
        {"metadata": {"bad": object()}},
        {"title": ""},
        {"kind": None},
        {"identifiers": {"doi": ""}},
    ],
)
def test_add_rejects_bad_fields(
    ksm: KnowledgeSourceManager, settings: Settings, inputs: Path, kw: dict[str, object]
) -> None:
    args: dict[str, object] = {"slug": "attention", "kind": "paper", "title": "T"} | kw
    with pytest.raises(ValueError):
        ksm.add(write(inputs, "p.pdf", PDF_BYTES), **args)  # type: ignore[arg-type]
    assert not (settings.sources_dir / "attention").exists()


def test_add_missing_input_file(ksm: KnowledgeSourceManager, inputs: Path) -> None:
    with pytest.raises(FileNotFoundError):
        ksm.add(inputs / "nope.pdf", slug="nope", kind="paper", title="Nope")
    with pytest.raises(FileNotFoundError):
        add_paper(ksm, inputs, extra_files=[inputs / "missing.txt"])


def test_add_rejects_extra_named_like_primary(ksm: KnowledgeSourceManager, inputs: Path) -> None:
    clash = write(inputs, "source.pdf", b"other")
    with pytest.raises(ValueError):
        add_paper(ksm, inputs, extra_files=[clash])


def test_duplicate_slug(ksm: KnowledgeSourceManager, settings: Settings, inputs: Path) -> None:
    add_paper(ksm, inputs)
    with pytest.raises(DuplicateError):
        add_paper(ksm, inputs, data=b"different content")
    # The original folder is untouched.
    assert (settings.sources_dir / "attention" / "source.pdf").read_bytes() == PDF_BYTES


def test_duplicate_content_hash(
    ksm: KnowledgeSourceManager, settings: Settings, inputs: Path
) -> None:
    add_paper(ksm, inputs, slug="attention")
    with pytest.raises(DuplicateError, match="attention"):
        add_paper(ksm, inputs, slug="attention-copy")
    assert not (settings.sources_dir / "attention-copy").exists()


def test_duplicate_identifier(
    ksm: KnowledgeSourceManager, settings: Settings, inputs: Path
) -> None:
    add_paper(ksm, inputs, slug="a", identifiers={"doi": "10.1/x"})
    with pytest.raises(DuplicateError):
        add_paper(ksm, inputs, slug="b", data=b"b", identifiers={"doi": "10.1/x"})
    assert not (settings.sources_dir / "b").exists()


def test_existing_folder_without_row_is_refused_and_left_alone(
    ksm: KnowledgeSourceManager, settings: Settings, inputs: Path
) -> None:
    folder = settings.sources_dir / "attention"
    folder.mkdir(parents=True)
    (folder / "keep.txt").write_text("mine")
    with pytest.raises(DuplicateError):
        add_paper(ksm, inputs)
    assert (folder / "keep.txt").read_text() == "mine"
    assert not (folder / "source.pdf").exists()


def test_failed_flush_removes_files_and_keeps_session_usable(
    ksm: KnowledgeSourceManager,
    session: Session,
    settings: Settings,
    inputs: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    add_paper(ksm, inputs, slug="a", identifiers={"doi": "10.1/x"})
    session.commit()
    session.expunge_all()  # the clashing row now exists only in the database, as in a race
    add_paper(ksm, inputs, slug="pending", data=b"p")  # uncommitted work of the caller
    # Skip the pre-check so the duplicate identifier only fails at flush time.
    monkeypatch.setattr(ksm, "_ensure_identifier_free", lambda scheme, value: None)
    with pytest.raises(DuplicateError):
        add_paper(ksm, inputs, slug="b", data=b"b", identifiers={"doi": "10.1/x"})
    assert not (settings.sources_dir / "b").exists()
    # The caller's transaction is not poisoned: earlier work survives and commits.
    assert session.is_active
    add_paper(ksm, inputs, slug="c", data=b"c")
    session.commit()
    other = session_factory(settings)()
    try:
        slugs = other.scalars(select(KnowledgeSource.slug).order_by(KnowledgeSource.slug)).all()
        assert slugs == ["a", "c", "pending"]
    finally:
        other.close()


def test_failure_while_copying_removes_folder(
    ksm: KnowledgeSourceManager,
    session: Session,
    settings: Settings,
    inputs: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_copy = shutil.copyfile

    def flaky_copy(src: Path, dst: Path) -> object:
        if Path(src).name == "extra.txt":
            raise OSError("disk full")
        return real_copy(src, dst)

    monkeypatch.setattr(shutil, "copyfile", flaky_copy)
    with pytest.raises(OSError, match="disk full"):
        add_paper(ksm, inputs, extra_files=[write(inputs, "extra.txt", b"e")])
    assert not (settings.sources_dir / "attention").exists()
    assert count(session, KnowledgeSource) == 0


def test_add_is_undone_by_callers_rollback(
    ksm: KnowledgeSourceManager, session: Session, settings: Settings, inputs: Path
) -> None:
    """The internal SAVEPOINT must not commit on its own."""
    add_paper(ksm, inputs)
    session.rollback()
    assert count(session, KnowledgeSource) == 0
    # The copied folder is now an orphan, which check() reports.
    assert [i.code for i in ksm.check()] == ["orphan_dir"]


# -- lookups ------------------------------------------------------------------------------


def test_get_and_get_by_slug(ksm: KnowledgeSourceManager, inputs: Path) -> None:
    source = add_paper(ksm, inputs)
    assert ksm.get(source.id) is source
    assert ksm.get_by_slug("attention") is source
    with pytest.raises(NotFoundError):
        ksm.get(9999)
    with pytest.raises(NotFoundError):
        ksm.get_by_slug("missing")


def test_find_by_identifier(ksm: KnowledgeSourceManager, inputs: Path) -> None:
    source = add_paper(ksm, inputs, identifiers={"arxiv": "1706.03762"})
    assert ksm.find_by_identifier("arxiv", "1706.03762") is source
    assert ksm.find_by_identifier("doi", "1706.03762") is None
    assert ksm.find_by_identifier("arxiv", "0000.00000") is None


def test_list_filters_by_kind(ksm: KnowledgeSourceManager, inputs: Path) -> None:
    a = add_paper(ksm, inputs, slug="a", data=b"a")
    b = ksm.add(write(inputs, "b.diff", b"b"), slug="b", kind="code_diff", title="B")
    c = add_paper(ksm, inputs, slug="c", data=b"c")
    assert ksm.list() == [a, b, c]
    assert ksm.list(kind="paper") == [a, c]
    assert ksm.list(kind="web_page") == []


# -- update -------------------------------------------------------------------------------


def test_update_descriptive_fields(ksm: KnowledgeSourceManager, inputs: Path) -> None:
    source = add_paper(ksm, inputs, creators=["X"], metadata={"a": 1})
    before = source.updated_at
    ksm.update(
        source,
        title="New Title",
        summary="s",
        creators=["Y", "Z"],
        metadata={"b": 2},
        notes=None,
        originated_on="2017-06",
        origin_url="https://example.org",
        kind="preprint",
    )
    assert (source.title, source.summary, source.kind) == ("New Title", "s", "preprint")
    assert source.creators == ["Y", "Z"]
    assert source.metadata_ == {"b": 2}
    assert source.updated_at >= before
    ksm.update(source, creators=None, metadata=None)
    assert source.creators == [] and source.metadata_ == {}


@pytest.mark.parametrize(
    "fields",
    [
        {"slug": "other"},
        {"primary_file": "x.pdf"},
        {"content_sha256": "0" * 64},
        {"dir_path": "elsewhere"},
        {"media_type": "text/plain"},
        {"title": None},
        {"creators": "single"},
        {"metadata": 5},
    ],
)
def test_update_rejects_file_fields_and_bad_values(
    ksm: KnowledgeSourceManager, inputs: Path, fields: dict[str, object]
) -> None:
    source = add_paper(ksm, inputs)
    with pytest.raises(ValueError):
        ksm.update(source, **fields)
    assert source.slug == "attention" and source.title == "Attention"


# -- identifiers --------------------------------------------------------------------------


def test_add_and_remove_identifier(
    ksm: KnowledgeSourceManager, session: Session, inputs: Path
) -> None:
    source = add_paper(ksm, inputs)
    ident = ksm.add_identifier(source, "doi", "10.1/x")
    assert isinstance(ident, KnowledgeSourceIdentifier)
    assert ksm.find_by_identifier("doi", "10.1/x") is source
    ksm.remove_identifier(source, "doi", "10.1/x")
    assert ksm.find_by_identifier("doi", "10.1/x") is None
    assert source.identifiers == []
    assert count(session, KnowledgeSourceIdentifier) == 0


def test_add_identifier_duplicate(
    ksm: KnowledgeSourceManager,
    session: Session,
    inputs: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a = add_paper(ksm, inputs, slug="a", data=b"a", identifiers={"doi": "10.1/x"})
    b = add_paper(ksm, inputs, slug="b", data=b"b")
    with pytest.raises(DuplicateError):
        ksm.add_identifier(b, "doi", "10.1/x")
    with pytest.raises(DuplicateError):
        ksm.add_identifier(a, "doi", "10.1/x")
    # Same outcome when only the database catches it (clashing row not in the identity map,
    # as in a race); the session stays usable.
    session.commit()
    session.expunge_all()
    b = ksm.get_by_slug("b")
    monkeypatch.setattr(ksm, "_ensure_identifier_free", lambda scheme, value: None)
    with pytest.raises(DuplicateError):
        ksm.add_identifier(b, "doi", "10.1/x")
    assert b.identifiers == []
    ksm.add_identifier(b, "doi", "10.1/y")
    assert [i.value for i in b.identifiers] == ["10.1/y"]


def test_remove_identifier_not_found(ksm: KnowledgeSourceManager, inputs: Path) -> None:
    a = add_paper(ksm, inputs, slug="a", data=b"a", identifiers={"doi": "10.1/x"})
    b = add_paper(ksm, inputs, slug="b", data=b"b")
    with pytest.raises(NotFoundError):
        ksm.remove_identifier(a, "doi", "10.1/missing")
    with pytest.raises(NotFoundError):  # belongs to another source
        ksm.remove_identifier(b, "doi", "10.1/x")
    assert ksm.find_by_identifier("doi", "10.1/x") is a


# -- delete -------------------------------------------------------------------------------


def _add_segment(session: Session, source: KnowledgeSource) -> None:
    session.add(
        SourceSegment(
            source=source,
            kind="section",
            label="1",
            position=1,
            locator_type="text_span",
            locator={"type": "text_span", "start_char": 0, "end_char": 5},
        )
    )
    session.flush()


def test_delete_removes_row_cascades_and_files(
    ksm: KnowledgeSourceManager, session: Session, settings: Settings, inputs: Path
) -> None:
    source = add_paper(ksm, inputs, identifiers={"doi": "10.1/x"})
    _add_segment(session, source)
    derived = settings.derived_dir / "attention"
    derived.mkdir(parents=True)
    (derived / "text.txt").write_text("cache")
    folder = settings.sources_dir / "attention"

    ksm.delete(source)

    assert count(session, KnowledgeSource) == 0
    assert count(session, KnowledgeSourceIdentifier) == 0
    assert count(session, SourceSegment) == 0
    assert not folder.exists()
    assert not derived.exists()
    assert ksm.check() == []


def test_delete_keep_files(
    ksm: KnowledgeSourceManager, session: Session, settings: Settings, inputs: Path
) -> None:
    source = add_paper(ksm, inputs)
    ksm.delete(source, remove_files=False)
    assert count(session, KnowledgeSource) == 0
    assert (settings.sources_dir / "attention" / "source.pdf").exists()
    assert [(i.code, i.slug) for i in ksm.check()] == [("orphan_dir", "attention")]


def _link_artifact(session: Session, source: KnowledgeSource) -> Artifact:
    artifact = Artifact(
        slug="guide", kind="guide", title="Guide", dir_path="artifacts/guide", generator="g/1"
    )
    session.add(artifact)
    session.flush()
    session.add(ArtifactSource(artifact_id=artifact.id, source_id=source.id))
    session.flush()
    return artifact


def test_delete_refused_while_artifact_references_source(
    ksm: KnowledgeSourceManager, session: Session, settings: Settings, inputs: Path
) -> None:
    source = add_paper(ksm, inputs)
    artifact = _link_artifact(session, source)

    with pytest.raises(SourceInUseError) as excinfo:
        ksm.delete(source)
    assert isinstance(excinfo.value, ManagerError)
    assert (settings.sources_dir / "attention" / "source.pdf").exists()
    assert ksm.get_by_slug("attention") is source

    # Once the artifact is gone the delete goes through.
    session.delete(artifact)
    session.flush()
    ksm.delete(source)
    assert count(session, KnowledgeSource) == 0


def test_delete_translates_restrict_violation(
    ksm: KnowledgeSourceManager,
    session: Session,
    settings: Settings,
    inputs: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the pre-check is bypassed, the RESTRICT failure still surfaces as SourceInUseError."""
    source = add_paper(ksm, inputs)
    _link_artifact(session, source)
    monkeypatch.setattr(session, "scalar", lambda *args, **kwargs: 0)
    with pytest.raises(SourceInUseError):
        ksm.delete(source)
    monkeypatch.undo()
    assert (settings.sources_dir / "attention").exists()
    assert session.is_active
    assert ksm.get_by_slug("attention") is source


# -- check --------------------------------------------------------------------------------


def test_check_reports_every_issue_kind(
    ksm: KnowledgeSourceManager, settings: Settings, inputs: Path
) -> None:
    add_paper(ksm, inputs, slug="ok", data=b"ok")
    gone = add_paper(ksm, inputs, slug="gone", data=b"gone")
    nofile = add_paper(ksm, inputs, slug="nofile", data=b"nofile")
    altered = add_paper(ksm, inputs, slug="altered", data=b"altered")
    assert ksm.check() == []

    shutil.rmtree(settings.sources_dir / gone.slug)
    ksm.primary_path(nofile).unlink()
    ksm.primary_path(altered).write_bytes(b"tampered")
    (settings.sources_dir / "stray").mkdir()
    (settings.sources_dir / "loose-file.txt").write_text("ignored: not a folder")

    issues = ksm.check()
    assert all(isinstance(i, Issue) for i in issues)
    assert sorted((i.code, i.slug) for i in issues) == [
        ("hash_mismatch", "altered"),
        ("missing_dir", "gone"),
        ("missing_file", "nofile"),
        ("orphan_dir", "stray"),
    ]


def test_check_with_no_sources_dir(ksm: KnowledgeSourceManager, settings: Settings) -> None:
    if settings.sources_dir.exists():
        shutil.rmtree(settings.sources_dir)
    assert ksm.check() == []
