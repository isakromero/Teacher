"""SourceSegmentManager: locator validation, positions, parents, duplicates, add_many, deletes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from teacher.config import Settings
from teacher.db.models import KnowledgeSource, SourceSegment
from teacher.locators import DiffLocator, PdfLocator, PdfRegion, TextSpanLocator
from teacher.managers.errors import DuplicateError, NotFoundError
from teacher.managers.knowledge_sources import KnowledgeSourceManager
from teacher.managers.source_segments import SourceSegmentManager

SPAN = {"type": "text_span", "start_char": 0, "end_char": 10}
PDF = {"type": "pdf_regions", "regions": [{"page": 2, "x0": 0.1, "y0": 0.2, "x1": 0.9, "y1": 0.5}]}
DIFF = {
    "type": "diff_lines",
    "file_path": "src/a.py",
    "side": "new",
    "start_line": 3,
    "end_line": 7,
}


@pytest.fixture
def ksm(session: Session, settings: Settings) -> KnowledgeSourceManager:
    return KnowledgeSourceManager(session, settings)


@pytest.fixture
def ssm(session: Session, settings: Settings) -> SourceSegmentManager:
    return SourceSegmentManager(session, settings)


def _make_source(ksm: KnowledgeSourceManager, tmp_path: Path, slug: str) -> KnowledgeSource:
    inputs = tmp_path / "inputs"
    inputs.mkdir(exist_ok=True)
    path = inputs / f"{slug}.pdf"
    path.write_bytes(f"%PDF {slug}".encode())
    return ksm.add(path, slug=slug, kind="paper", title=slug)


@pytest.fixture
def source(ksm: KnowledgeSourceManager, tmp_path: Path) -> KnowledgeSource:
    return _make_source(ksm, tmp_path, "attention")


@pytest.fixture
def other_source(ksm: KnowledgeSourceManager, tmp_path: Path) -> KnowledgeSource:
    return _make_source(ksm, tmp_path, "resnet")


def count(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(SourceSegment)) or 0


# -- add / locators -----------------------------------------------------------------------


def test_add_stores_validated_locator(
    ssm: SourceSegmentManager, session: Session, source: KnowledgeSource
) -> None:
    seg = ssm.add(source, kind="figure", label="Figure 2", locator=PDF, title="Architecture")
    assert seg.id is not None
    assert (seg.kind, seg.label, seg.title, seg.position) == (
        "figure",
        "Figure 2",
        "Architecture",
        1,
    )
    assert seg.locator_type == "pdf_regions"
    assert seg.locator == PDF
    raw = session.execute(
        text("SELECT locator_type, json_extract(locator, '$.type') FROM source_segments")
    ).one()
    assert tuple(raw) == ("pdf_regions", "pdf_regions")


@pytest.mark.parametrize(
    ("data", "model"),
    [(PDF, PdfLocator), (DIFF, DiffLocator), (SPAN, TextSpanLocator)],
)
def test_locator_round_trip(
    ssm: SourceSegmentManager,
    session: Session,
    source: KnowledgeSource,
    data: dict[str, Any],
    model: type,
) -> None:
    seg = ssm.add(source, kind="section", label="1", locator=data)
    session.expire_all()
    loc = ssm.locator(ssm.get(seg.id))
    assert isinstance(loc, model)
    assert loc.model_dump() == data


def test_add_accepts_model_locator_and_fills_defaults(
    ssm: SourceSegmentManager, source: KnowledgeSource
) -> None:
    loc = PdfLocator(regions=[PdfRegion(page=1, x0=0, y0=0, x1=1, y1=1)])
    seg = ssm.add(source, kind="figure", label="Figure 1", locator=loc)
    assert seg.locator_type == "pdf_regions"
    assert seg.locator["type"] == "pdf_regions"
    assert ssm.locator(seg) == loc
    # A dict without `type` default cannot be discriminated, so it is rejected.
    with pytest.raises(ValidationError):
        ssm.add(source, kind="figure", label="Figure 9", locator={"start_char": 0, "end_char": 1})


@pytest.mark.parametrize(
    "bad",
    [
        {},
        {"type": "unknown"},
        {"type": "pdf_regions", "regions": []},
        {"type": "pdf_regions", "regions": [{"page": 1, "x0": 0.5, "y0": 0, "x1": 0.2, "y1": 1}]},
        {"type": "pdf_regions", "regions": [{"page": 0, "x0": 0, "y0": 0, "x1": 1, "y1": 1}]},
        {"type": "diff_lines", "file_path": "a", "side": "new", "start_line": 5, "end_line": 4},
        {"type": "diff_lines", "file_path": "a", "side": "mid", "start_line": 1, "end_line": 1},
        {"type": "text_span", "start_char": 3, "end_char": 3},
        {**SPAN, "extra": 1},
    ],
)
def test_add_rejects_invalid_locator(
    ssm: SourceSegmentManager, session: Session, source: KnowledgeSource, bad: dict[str, Any]
) -> None:
    with pytest.raises(ValueError):  # pydantic.ValidationError is a ValueError
        ssm.add(source, kind="section", label="1", locator=bad)
    assert count(session) == 0


def test_add_rejects_empty_kind_or_label(
    ssm: SourceSegmentManager, source: KnowledgeSource
) -> None:
    with pytest.raises(ValueError):
        ssm.add(source, kind="", label="1", locator=SPAN)
    with pytest.raises(ValueError):
        ssm.add(source, kind="section", label="", locator=SPAN)


def test_positions_default_to_next(ssm: SourceSegmentManager, source: KnowledgeSource) -> None:
    a = ssm.add(source, kind="section", label="1", locator=SPAN)
    b = ssm.add(source, kind="section", label="2", locator=SPAN)
    c = ssm.add(source, kind="section", label="3", locator=SPAN, position=10)
    d = ssm.add(source, kind="figure", label="Figure 1", locator=PDF)
    assert [s.position for s in (a, b, c, d)] == [1, 2, 10, 11]


def test_positions_are_per_source(
    ssm: SourceSegmentManager, source: KnowledgeSource, other_source: KnowledgeSource
) -> None:
    ssm.add(source, kind="section", label="1", locator=SPAN)
    ssm.add(source, kind="section", label="2", locator=SPAN)
    assert ssm.add(other_source, kind="section", label="1", locator=SPAN).position == 1


def test_list_ordered_by_position_and_filtered(
    ssm: SourceSegmentManager, source: KnowledgeSource, other_source: KnowledgeSource
) -> None:
    late = ssm.add(source, kind="section", label="2", locator=SPAN, position=5)
    early = ssm.add(source, kind="section", label="1", locator=SPAN, position=1)
    fig = ssm.add(source, kind="figure", label="Figure 1", locator=PDF, position=3)
    ssm.add(other_source, kind="section", label="1", locator=SPAN)
    assert ssm.list(source) == [early, fig, late]
    assert ssm.list(source, kind="section") == [early, late]
    assert ssm.list(source, kind="table") == []
    assert source.segments == [early, fig, late]


def test_content_file_stored_relative_to_root(
    ssm: SourceSegmentManager, settings: Settings, source: KnowledgeSource
) -> None:
    a = ssm.add(
        source, kind="section", label="1", locator=SPAN, content_file="derived/attention/1.txt"
    )
    b = ssm.add(
        source,
        kind="section",
        label="2",
        locator=SPAN,
        content_file=settings.derived_dir / "attention" / "2.txt",
    )
    assert a.content_file == "derived/attention/1.txt"
    assert b.content_file == "derived/attention/2.txt"
    with pytest.raises(ValueError):
        ssm.add(source, kind="section", label="3", locator=SPAN, content_file="../outside.txt")
    with pytest.raises(ValueError):
        ssm.add(source, kind="section", label="4", locator=SPAN, content_file=Path("/etc/passwd"))


# -- duplicates ---------------------------------------------------------------------------


def test_duplicate_kind_and_label(
    ssm: SourceSegmentManager,
    session: Session,
    source: KnowledgeSource,
    other_source: KnowledgeSource,
) -> None:
    ssm.add(source, kind="section", label="1", locator=SPAN)
    with pytest.raises(DuplicateError):
        ssm.add(source, kind="section", label="1", locator=SPAN)
    # Same label under another kind or another source is fine.
    ssm.add(source, kind="table", label="1", locator=SPAN)
    ssm.add(other_source, kind="section", label="1", locator=SPAN)
    assert count(session) == 3


def test_duplicate_caught_at_flush_keeps_session_usable(
    ssm: SourceSegmentManager,
    session: Session,
    source: KnowledgeSource,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ssm.add(source, kind="section", label="1", locator=SPAN)
    monkeypatch.setattr(ssm, "_find_by_label", lambda *args: None)
    with pytest.raises(DuplicateError):
        ssm.add(source, kind="section", label="1", locator=SPAN)
    monkeypatch.undo()
    assert session.is_active
    ssm.add(source, kind="section", label="2", locator=SPAN)
    assert [s.label for s in ssm.list(source)] == ["1", "2"]


# -- lookups ------------------------------------------------------------------------------


def test_get_and_get_by_label(ssm: SourceSegmentManager, source: KnowledgeSource) -> None:
    seg = ssm.add(source, kind="equation", label="Eq. 4", locator=SPAN)
    assert ssm.get(seg.id) is seg
    assert ssm.get_by_label(source, "equation", "Eq. 4") is seg
    with pytest.raises(NotFoundError):
        ssm.get(9999)
    with pytest.raises(NotFoundError):
        ssm.get_by_label(source, "section", "Eq. 4")


# -- parents ------------------------------------------------------------------------------


def test_parent_and_children(ssm: SourceSegmentManager, source: KnowledgeSource) -> None:
    sec = ssm.add(source, kind="section", label="3", locator=SPAN)
    sub2 = ssm.add(source, kind="section", label="3.2", locator=SPAN, parent=sec, position=20)
    sub1 = ssm.add(source, kind="section", label="3.1", locator=SPAN, parent=sec, position=10)
    fig = ssm.add(source, kind="figure", label="Figure 1", locator=PDF, parent=sub1)
    assert sub1.parent is sec and sub1.parent_id == sec.id
    assert ssm.children(sec) == [sub1, sub2]
    assert ssm.children(sub1) == [fig]
    assert ssm.children(fig) == []


def test_parent_from_other_source_rejected(
    ssm: SourceSegmentManager,
    session: Session,
    source: KnowledgeSource,
    other_source: KnowledgeSource,
) -> None:
    foreign = ssm.add(other_source, kind="section", label="1", locator=SPAN)
    with pytest.raises(ValueError):
        ssm.add(source, kind="section", label="1.1", locator=SPAN, parent=foreign)
    assert ssm.list(source) == []


# -- add_many -----------------------------------------------------------------------------


def test_add_many_resolves_parent_labels(
    ssm: SourceSegmentManager, source: KnowledgeSource
) -> None:
    stored = ssm.add(source, kind="section", label="1", locator=SPAN)
    added = ssm.add_many(
        source,
        [
            {"kind": "section", "label": "2", "locator": SPAN, "title": "Model"},
            {
                "kind": "section",
                "label": "2.1",
                "locator": SPAN,
                "parent_kind": "section",
                "parent_label": "2",
            },
            {
                "kind": "figure",
                "label": "Figure 1",
                "locator": PDF,
                "parent_kind": "section",
                "parent_label": "2.1",
            },
            {
                "kind": "table",
                "label": "Table 1",
                "locator": SPAN,
                "parent_kind": "section",
                "parent_label": "1",
            },
            {
                "kind": "equation",
                "label": "Eq. 1",
                "locator": TextSpanLocator(start_char=1, end_char=2),
                "parent": stored,
            },
        ],
    )
    assert [s.label for s in added] == ["2", "2.1", "Figure 1", "Table 1", "Eq. 1"]
    assert [s.position for s in added] == [2, 3, 4, 5, 6]
    sec2, sub, fig, table, eq = added
    assert sub.parent is sec2 and fig.parent is sub
    assert table.parent is stored and eq.parent is stored
    assert added[0].title == "Model"
    assert ssm.add_many(source, []) == []


@pytest.mark.parametrize(
    ("bad", "exc"),
    [
        ({"kind": "section", "label": "x", "locator": {"type": "nope"}}, ValueError),
        ({"kind": "section", "label": "1", "locator": SPAN}, DuplicateError),  # dup within call
        ({"kind": "section", "label": "x", "locator": SPAN, "colour": "red"}, ValueError),
        ({"kind": "section", "label": "x", "locator": SPAN, "parent_label": "1"}, ValueError),
        (
            {
                "kind": "section",
                "label": "x",
                "locator": SPAN,
                "parent_kind": "section",
                "parent_label": "missing",
            },
            NotFoundError,
        ),
    ],
)
def test_add_many_is_all_or_nothing(
    ssm: SourceSegmentManager,
    session: Session,
    source: KnowledgeSource,
    bad: dict[str, Any],
    exc: type[Exception],
) -> None:
    ssm.add(source, kind="section", label="0", locator=SPAN)
    with pytest.raises(exc):
        ssm.add_many(
            source,
            [
                {"kind": "section", "label": "1", "locator": SPAN},
                {"kind": "section", "label": "2", "locator": SPAN},
                bad,
            ],
        )
    assert session.is_active
    assert [s.label for s in ssm.list(source)] == ["0"]
    assert [s.label for s in source.segments] == ["0"]


def test_add_many_rejects_parent_and_parent_label(
    ssm: SourceSegmentManager, source: KnowledgeSource
) -> None:
    sec = ssm.add(source, kind="section", label="1", locator=SPAN)
    with pytest.raises(ValueError):
        ssm.add_many(
            source,
            [
                {
                    "kind": "section",
                    "label": "1.1",
                    "locator": SPAN,
                    "parent": sec,
                    "parent_kind": "section",
                    "parent_label": "1",
                }
            ],
        )


# -- delete -------------------------------------------------------------------------------


def test_delete_cascades_to_descendants(
    ssm: SourceSegmentManager, session: Session, source: KnowledgeSource
) -> None:
    root = ssm.add(source, kind="section", label="1", locator=SPAN)
    keep = ssm.add(source, kind="section", label="2", locator=SPAN)
    child = ssm.add(source, kind="section", label="1.1", locator=SPAN, parent=root)
    ssm.add(source, kind="figure", label="Figure 1", locator=PDF, parent=child)
    ssm.delete(child)
    assert [s.label for s in ssm.list(source)] == ["1", "2"]
    assert root.children == []
    ssm.delete(root)
    assert ssm.list(source) == [keep]
    assert source.segments == [keep]


def test_delete_all(
    ssm: SourceSegmentManager,
    session: Session,
    source: KnowledgeSource,
    other_source: KnowledgeSource,
) -> None:
    sec = ssm.add(source, kind="section", label="1", locator=SPAN)
    ssm.add(source, kind="section", label="1.1", locator=SPAN, parent=sec)
    ssm.add(source, kind="figure", label="Figure 1", locator=PDF)
    other = ssm.add(other_source, kind="section", label="1", locator=SPAN)
    assert len(source.segments) == 3
    assert ssm.delete_all(source) == 3
    assert ssm.list(source) == []
    assert source.segments == []
    assert ssm.list(other_source) == [other]
    assert ssm.delete_all(source) == 0


def test_deleting_source_removes_segments(
    ssm: SourceSegmentManager,
    ksm: KnowledgeSourceManager,
    session: Session,
    source: KnowledgeSource,
) -> None:
    sec = ssm.add(source, kind="section", label="1", locator=SPAN)
    ssm.add(source, kind="section", label="1.1", locator=SPAN, parent=sec)
    ksm.delete(source)
    assert count(session) == 0
