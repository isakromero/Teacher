"""The highlights manager (`teacher.managers.highlights`): per-owner scope, anchors and notes,
tombstones, and the rules for changes made offline."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.orm import Session

from teacher.config import Settings
from teacher.db.models import HIGHLIGHT_MAX_CONTEXT, Highlight
from teacher.managers import ArtifactBlockManager, ArtifactManager, UserManager
from teacher.managers.errors import DuplicateError, NotFoundError
from teacher.managers.highlights import (
    Anchor,
    HighlightManager,
    canonical_uid,
    check_anchor,
    clean_note,
)

T0 = datetime(2026, 11, 26, 9, 0, 0)
A = Anchor("b01", "a passage", "before ", " after", 7, 16)


def make_page(session: Session, settings: Settings, slug: str = "page") -> Any:
    page = ArtifactManager(session, settings).create(
        slug=slug, kind="study_page", title="Page", generator="test"
    )
    blocks = ArtifactBlockManager(session, settings)
    blocks.add(page, type="prose", content={"markdown": "one"})
    blocks.add(page, type="prose", content={"markdown": "two"})
    return page


def manager(session: Session, settings: Settings, owner: int | None = None, now=T0):
    return HighlightManager(session, settings, owner=owner, clock=lambda: now)


def test_owners_see_only_their_own(session: Session, settings: Settings) -> None:
    page = make_page(session, settings)
    ann = UserManager(session, settings).create("ann@example.com", "correct horse battery").id
    anonymous = manager(session, settings)
    mine = manager(session, settings, ann)
    a = anonymous.create(page, A)
    b = mine.create(page, A, note="Ann's")
    assert [r.uid for r, _ in anonymous.list(page)] == [a.uid]
    assert [r.uid for r, _ in mine.list(page)] == [b.uid]
    with pytest.raises(NotFoundError):
        anonymous.get(page, b.uid)
    with pytest.raises(NotFoundError):
        mine.get(page, a.uid)
    assert mine.get(page, b.uid.upper()) is b
    for bad in (True, "1", 1.0):
        with pytest.raises(TypeError):
            HighlightManager(session, settings, owner=bad)  # type: ignore[arg-type]


def test_list_order_and_unknown_blocks(session: Session, settings: Settings) -> None:
    page = make_page(session, settings)
    hm = manager(session, settings)
    second_block = hm.create(page, Anchor("b02", "two", "", "", 0, 3))
    late = hm.create(page, Anchor("b01", "one", "", "", 2, 3))
    early = hm.create(page, Anchor("b01", "on", "", "", 0, 2))
    # a highlight in a block the page no longer has (written directly: the manager refuses)
    orphan = Highlight(
        uid=str(uuid.uuid4()), user_id=None, artifact_id=page.id, block_key="b99", quote="x",
        prefix="", suffix="", start_offset=0, end_offset=1, created_at=T0, updated_at=T0,
    )
    session.add(orphan)
    session.flush()
    listed = hm.list(page)
    assert [r.uid for r, _ in listed] == [early.uid, late.uid, second_block.uid, orphan.uid]
    assert [pos for _, pos in listed] == [1, 1, 2, None]


def test_create_checks(session: Session, settings: Settings) -> None:
    page = make_page(session, settings)
    hm = manager(session, settings)
    with pytest.raises(ValueError, match="no block 'b09'"):
        hm.create(page, Anchor("b09", "x", "", "", 0, 1))
    made = hm.create(page, A, uid=uuid.UUID(int=5))
    assert made.uid == "00000000-0000-0000-0000-000000000005"
    assert (made.created_at, made.updated_at, made.note) == (T0, T0, None)
    with pytest.raises(DuplicateError):
        hm.create(page, A, uid=made.uid)
    other = manager(session, settings, UserManager(session, settings).create(
        "bob@example.com", "correct horse battery").id)
    with pytest.raises(DuplicateError):  # an id is unique across accounts
        other.create(page, A, uid=made.uid)


def test_notes_and_tombstones(session: Session, settings: Settings) -> None:
    page = make_page(session, settings)
    hm = manager(session, settings)
    row = hm.create(page, A, note=" first ")
    assert row.note == "first"
    hm.update_note(row, "", at=T0 + timedelta(minutes=1))
    assert row.note is None and row.updated_at == T0 + timedelta(minutes=1)
    hm.update_note(row, "again")
    hm.delete(row, at=T0 + timedelta(minutes=2))
    assert row.note is None and row.deleted_at == T0 + timedelta(minutes=2)
    assert hm.list(page) == []
    with pytest.raises(NotFoundError):
        hm.get(page, row.uid)
    with pytest.raises(ValueError, match="removed"):
        hm.update_note(row, "x")
    with pytest.raises(ValueError, match="removed already"):
        hm.delete(row)


@pytest.mark.parametrize(
    ("anchor", "message"),
    [
        (Anchor(" ", "q", "", "", 0, 1), "block_key"),
        (Anchor("b01", "", "", "", 0, 1), "not blank"),
        (Anchor("b01", "q", "p" * (HIGHLIGHT_MAX_CONTEXT + 1), "", 0, 1), "prefix"),
        (Anchor("b01", "q", "", None, 0, 1), "suffix must be a string"),  # type: ignore[arg-type]
        (Anchor("b01", "q", "", "", 1, 1), "0 <= start < end"),
        (Anchor("b01", "q", "", "", False, 1), "integers"),  # type: ignore[arg-type]
        (Anchor("b01", "half \ud835", "", "", 0, 6), "lone surrogate"),
        (Anchor("b01", "q", "\udc00", "", 0, 1), "prefix is not valid"),
        (Anchor("b01", "q\x00", "", "", 0, 2), "NUL"),
    ],
)
def test_check_anchor(anchor: Anchor, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        check_anchor(anchor)


def test_small_helpers() -> None:
    assert clean_note(None) is None and clean_note("  ") is None and clean_note(" x ") == "x"
    with pytest.raises(ValueError):
        clean_note(3)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="limit"):
        clean_note("n" * 10001)
    with pytest.raises(ValueError, match="lone surrogate"):
        clean_note("\ud835")
    assert canonical_uid("0B7F0000-0000-4000-8000-000000000000") == (
        "0b7f0000-0000-4000-8000-000000000000"
    )
    with pytest.raises(ValueError, match="not a UUID"):
        canonical_uid("nope")


def test_offline_changes(session: Session, settings: Settings) -> None:
    page = make_page(session, settings)
    hm = manager(session, settings)
    uid = str(uuid.uuid4())
    limit = T0 + timedelta(minutes=5)

    def apply(op: str, made_at: datetime = T0, **kw: Any):
        return hm.apply_change(page, op, uid, made_at=made_at, max_future=limit, **kw)  # type: ignore[arg-type]

    assert apply("update_note", note="x").reason == "unknown_highlight"
    assert apply("delete").reason == "unknown_highlight"
    assert apply("create", made_at=limit + timedelta(seconds=1), anchor=A).reason == (
        "made_at_in_future"
    )
    created = apply("create", T0 - timedelta(hours=1), anchor=A, note="phone")
    assert created.status == "applied" and created.highlight is not None
    assert created.highlight.created_at == T0 - timedelta(hours=1)
    assert apply("create", T0 - timedelta(hours=1), anchor=A, note="phone").status == "unchanged"
    assert apply("create", anchor=Anchor("b01", "other", "", "", 0, 5)).reason == "id_conflict"
    assert apply("update_note", T0 - timedelta(hours=2), note="older").status == "stale"
    assert apply("update_note", T0, note="newer").status == "applied"
    assert hm.get(page, uid).note == "newer"
    assert apply("update_note", T0, note="n" * 10001).reason == "invalid_note"
    assert apply("delete", T0 - timedelta(days=1)).status == "applied"  # a delete always wins
    assert apply("delete").status == "unchanged"
    assert apply("update_note", note="back?").status == "deleted"
    assert apply("create", anchor=A).status == "deleted"
    other_page = make_page(session, settings, "other")
    assert hm.apply_change(
        other_page, "delete", uid, made_at=T0, max_future=limit
    ).reason == "id_conflict"
