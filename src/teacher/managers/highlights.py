"""HighlightManager: the passages of a study page a reader highlighted, with their notes.

Scope. Every highlight belongs to one account and one study page. The manager is made for one
`owner` (a user id, or None when the app runs without accounts: then every highlight has
`user_id` NULL) and only ever sees that owner's highlights: another account's highlight is
"not found", never shown or changed. Unlike the rest of the learner data (answers, history,
reviews), which every account still shares, highlights and notes are private to the account
that made them.

Anchor. A highlight is anchored to its block by key (`block_key`, stable for the life of a
page) and to the passage by its text: `quote` (1 to `HIGHLIGHT_MAX_QUOTE` characters, not
blank), up to `HIGHLIGHT_MAX_CONTEXT` characters before and after it (`prefix`, `suffix`) and
its offsets in the block's text (`start`, `end`, 0 <= start < end; only a hint for choosing
between repeated occurrences). The text these count in is the web client's
(`static/js/highlight_model.js`); the server does not render blocks, so it checks only that the
block exists, never that the quote is still in it: a highlight whose passage can no longer be
found is the client's to flag.

Notes. `note` is optional; a note that is blank after stripping is no note (None), one longer
than `HIGHLIGHT_MAX_NOTE` characters is a ValueError. `updated_at` is the time of the latest
change of the note (or of the creation).

Removing. `delete` keeps the row as a tombstone (`deleted_at` set, the note cleared) so that an
offline upload made before the removal cannot bring the highlight back; tombstones are never
listed and their ids are never reused.

Offline changes (`apply_change`), as the phone uploads them (`teacher.web.routers.highlights`
documents the endpoint): `create`, `update_note` and `delete`, each with the time it was made on
the phone. Last writer wins for notes, by that time against `updated_at`; a delete always wins.

Time comes from `clock` (naive UTC), so tests can move it. Like every manager it flushes and
never commits.
"""

from __future__ import annotations

import builtins
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from teacher.config import Settings
from teacher.db.models import (
    HIGHLIGHT_MAX_CONTEXT,
    HIGHLIGHT_MAX_NOTE,
    HIGHLIGHT_MAX_QUOTE,
    Artifact,
    ArtifactBlock,
    Highlight,
    utcnow,
)
from teacher.managers.artifacts import _is_unique_violation
from teacher.managers.errors import DuplicateError, NotFoundError

ChangeStatus = Literal["applied", "unchanged", "stale", "deleted", "rejected"]
ChangeReason = Literal[
    "made_at_in_future",
    "id_conflict",
    "unknown_highlight",
    "unknown_block",
    "invalid_anchor",
    "invalid_note",
]


@dataclass(frozen=True, slots=True)
class Anchor:
    """Where a highlight is: its block and passage (see the module docstring)."""

    block_key: str
    quote: str
    prefix: str
    suffix: str
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class ChangeOutcome:
    """What an offline change did: `status`, and for a rejected one `reason` and `detail`;
    `highlight` is the row as it is afterwards (None when there is none: rejected)."""

    status: ChangeStatus
    highlight: Highlight | None
    reason: ChangeReason | None = None
    detail: str | None = None


class ChangeRejected(Exception):
    """An offline change that cannot apply (the reason is one of `ChangeReason`)."""

    def __init__(self, reason: ChangeReason, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _check_text(value: str, name: str) -> None:
    """ValueError for text that cannot be stored: a lone half of a UTF-16 surrogate pair (a
    client that cut a string inside an emoji or a maths letter) or a NUL character."""
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as err:
        raise ValueError(f"{name} is not valid Unicode text (it has a lone surrogate)") from err
    if "\x00" in value:
        raise ValueError(f"{name} contains a NUL character")


def check_anchor(anchor: Anchor) -> Anchor:
    """`anchor` if it keeps the rules of the module docstring; ValueError saying which it
    breaks otherwise (the block's existence is checked against the page separately)."""
    if not isinstance(anchor.block_key, str) or not anchor.block_key.strip():
        raise ValueError("block_key must be a block's key")
    if not isinstance(anchor.quote, str) or not anchor.quote.strip():
        raise ValueError("quote must be the highlighted text, not blank")
    _check_text(anchor.block_key, "block_key")
    _check_text(anchor.quote, "quote")
    if len(anchor.quote) > HIGHLIGHT_MAX_QUOTE:
        raise ValueError(
            f"quote is {len(anchor.quote)} characters long; at most {HIGHLIGHT_MAX_QUOTE} are kept"
        )
    for name in ("prefix", "suffix"):
        value = getattr(anchor, name)
        if not isinstance(value, str):
            raise ValueError(f"{name} must be a string")
        _check_text(value, name)
        if len(value) > HIGHLIGHT_MAX_CONTEXT:
            raise ValueError(
                f"{name} is {len(value)} characters long; at most {HIGHLIGHT_MAX_CONTEXT} are kept"
            )
    if not _is_int(anchor.start) or not _is_int(anchor.end):
        raise ValueError("start and end must be integers")
    if not 0 <= anchor.start < anchor.end:
        raise ValueError(f"0 <= start < end is required, got start {anchor.start}, end {anchor.end}")
    return anchor


def clean_note(note: str | None) -> str | None:
    """The note to store: None for none or a blank one; ValueError if it is not a string or
    longer than `HIGHLIGHT_MAX_NOTE` characters (after stripping)."""
    if note is None:
        return None
    if not isinstance(note, str):
        raise ValueError("note must be a string or null")
    _check_text(note, "note")
    text = note.strip()
    if len(text) > HIGHLIGHT_MAX_NOTE:
        raise ValueError(f"note is {len(text)} characters long; the limit is {HIGHLIGHT_MAX_NOTE}")
    return text or None


def canonical_uid(value: str | uuid.UUID) -> str:
    """A UUID in canonical lowercase form; ValueError for anything that is not a UUID."""
    if isinstance(value, uuid.UUID):
        return str(value)
    try:
        return str(uuid.UUID(str(value)))
    except ValueError as err:
        raise ValueError(f"{value!r} is not a UUID") from err


def anchor_of(row: Highlight) -> Anchor:
    return Anchor(row.block_key, row.quote, row.prefix, row.suffix, row.start_offset, row.end_offset)


class HighlightManager:
    """One owner's highlights (see the module docstring)."""

    def __init__(
        self,
        session: Session,
        settings: Settings,
        *,
        owner: int | None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        if owner is not None and not _is_int(owner):
            raise TypeError("owner is a user id (int) or None")
        self.session = session
        self.settings = settings
        self.owner = owner
        self.clock = clock

    def _mine(self) -> object:
        return Highlight.user_id.is_(None) if self.owner is None else Highlight.user_id == self.owner

    # -- reading ------------------------------------------------------------------------------

    def list(self, artifact: Artifact) -> builtins.list[tuple[Highlight, int | None]]:
        """The owner's highlights on `artifact` (not removed ones), each with its block's
        position (None when the page has no such block), in page order: by block position
        (unknown blocks last), then by start offset, then oldest first."""
        rows = self.session.execute(
            select(Highlight, ArtifactBlock.position)
            .outerjoin(
                ArtifactBlock,
                (ArtifactBlock.artifact_id == Highlight.artifact_id)
                & (ArtifactBlock.key == Highlight.block_key),
            )
            .where(
                Highlight.artifact_id == artifact.id,
                Highlight.deleted_at.is_(None),
                self._mine(),
            )
        ).all()
        out = [(row, position) for row, position in rows]
        out.sort(
            key=lambda pair: (
                pair[1] is None,
                pair[1] or 0,
                pair[0].start_offset,
                pair[0].created_at,
                pair[0].id,
            )
        )
        return out

    def _find(self, uid: str) -> Highlight | None:
        """The row with `uid`, whoever owns it, removed or not."""
        return self.session.scalar(select(Highlight).where(Highlight.uid == uid))

    def get(self, artifact: Artifact, uid: str | uuid.UUID) -> Highlight:
        """The owner's live highlight `uid` on `artifact`; NotFoundError otherwise (another
        account's, another page's and a removed one alike)."""
        key = canonical_uid(uid)
        row = self._find(key)
        if (
            row is None
            or row.artifact_id != artifact.id
            or row.user_id != self.owner
            or row.deleted_at is not None
        ):
            raise NotFoundError(f"no highlight {key} on {artifact.slug!r}")
        return row

    def block_position(self, artifact: Artifact, key: str) -> int | None:
        return self.session.scalar(
            select(ArtifactBlock.position).where(
                ArtifactBlock.artifact_id == artifact.id, ArtifactBlock.key == key
            )
        )

    # -- changing -----------------------------------------------------------------------------

    def create(
        self,
        artifact: Artifact,
        anchor: Anchor,
        *,
        note: str | None = None,
        uid: str | uuid.UUID | None = None,
        at: datetime | None = None,
    ) -> Highlight:
        """A new highlight. ValueError for an anchor or note that breaks the rules or a block
        the page does not have; DuplicateError when `uid` is taken (by any account)."""
        check_anchor(anchor)
        text = clean_note(note)
        if self.block_position(artifact, anchor.block_key) is None:
            raise ValueError(f"the page {artifact.slug!r} has no block {anchor.block_key!r}")
        key = canonical_uid(uid) if uid is not None else str(uuid.uuid4())
        if self._find(key) is not None:
            raise DuplicateError(f"a highlight with id {key} exists already")
        when = at if at is not None else self.clock()
        row = Highlight(
            uid=key,
            user_id=self.owner,
            artifact_id=artifact.id,
            block_key=anchor.block_key,
            quote=anchor.quote,
            prefix=anchor.prefix,
            suffix=anchor.suffix,
            start_offset=anchor.start,
            end_offset=anchor.end,
            note=text,
            created_at=when,
            updated_at=when,
        )
        try:
            with self.session.begin_nested():
                self.session.add(row)
                self.session.flush()
        except IntegrityError as err:
            if _is_unique_violation(err):
                raise DuplicateError(
                    f"highlight {key} was stored by a concurrent request; send it again"
                ) from err
            raise
        return row

    def update_note(self, row: Highlight, note: str | None, *, at: datetime | None = None) -> Highlight:
        """Set (or, with None or a blank note, clear) the note of a live highlight."""
        if row.deleted_at is not None:
            raise ValueError(f"highlight {row.uid} was removed")
        row.note = clean_note(note)
        row.updated_at = at if at is not None else self.clock()
        self.session.flush()
        return row

    def delete(self, row: Highlight, *, at: datetime | None = None) -> None:
        """Remove a live highlight (it stays as a tombstone, without its note)."""
        if row.deleted_at is not None:
            raise ValueError(f"highlight {row.uid} was removed already")
        row.note = None
        row.deleted_at = at if at is not None else self.clock()
        self.session.flush()

    # -- offline changes ----------------------------------------------------------------------

    def _owned(self, artifact: Artifact, uid: str) -> Highlight | None:
        """The row `uid` if it is the owner's on `artifact` (removed or not); None if there is
        no such row; ChangeRejected(id_conflict) if the id belongs to another account or page."""
        row = self._find(uid)
        if row is None:
            return None
        if row.user_id != self.owner or row.artifact_id != artifact.id:
            raise ChangeRejected(
                "id_conflict",
                f"id {uid} is taken by another highlight (another page or account); a new "
                "highlight needs a new id",
            )
        return row

    def _set_note_if_newer(self, row: Highlight, note: str | None, made_at: datetime) -> ChangeOutcome:
        if row.note == note:
            return ChangeOutcome("unchanged", row)
        if made_at <= row.updated_at:
            return ChangeOutcome("stale", row)
        self.update_note(row, note, at=made_at)
        return ChangeOutcome("applied", row)

    def apply_change(
        self,
        artifact: Artifact,
        op: Literal["create", "update_note", "delete"],
        uid: str | uuid.UUID,
        *,
        made_at: datetime,
        max_future: datetime,
        anchor: Anchor | None = None,
        note: str | None = None,
    ) -> ChangeOutcome:
        """Apply one change made offline at `made_at` (naive UTC); see
        `teacher.web.routers.highlights` for the rules. A change that cannot apply is an
        outcome with status "rejected" (nothing is changed), never an exception;
        `max_future` is the latest `made_at` accepted (the server's clock plus the skew
        allowed)."""
        key = canonical_uid(uid)
        try:
            if made_at > max_future:
                raise ChangeRejected(
                    "made_at_in_future",
                    "made_at is later than the server's clock (by more than the skew allowed): "
                    "check the phone's clock",
                )
            try:
                text = clean_note(note)
            except ValueError as err:
                raise ChangeRejected("invalid_note", str(err)) from err
            row = self._owned(artifact, key)
            if op == "create":
                if anchor is None:
                    raise TypeError("a create change needs its anchor")
                try:
                    check_anchor(anchor)
                except ValueError as err:
                    raise ChangeRejected("invalid_anchor", str(err)) from err
                if row is None:
                    if self.block_position(artifact, anchor.block_key) is None:
                        raise ChangeRejected(
                            "unknown_block", f"the page has no block {anchor.block_key!r}"
                        )
                    created = self.create(artifact, anchor, note=text, uid=key, at=made_at)
                    return ChangeOutcome("applied", created)
                if anchor_of(row) != anchor:
                    raise ChangeRejected(
                        "id_conflict",
                        f"highlight {key} exists with another passage; a new highlight needs a "
                        "new id",
                    )
                if row.deleted_at is not None:
                    return ChangeOutcome("deleted", row)
                return self._set_note_if_newer(row, text, made_at)
            if row is None:
                raise ChangeRejected(
                    "unknown_highlight", f"there is no highlight {key} on this page"
                )
            if op == "update_note":
                if row.deleted_at is not None:
                    return ChangeOutcome("deleted", row)
                return self._set_note_if_newer(row, text, made_at)
            if op == "delete":
                if row.deleted_at is not None:
                    return ChangeOutcome("unchanged", row)
                self.delete(row, at=made_at)
                return ChangeOutcome("applied", row)
            raise ValueError(f"unknown change op {op!r}")
        except ChangeRejected as rejected:
            return ChangeOutcome("rejected", None, rejected.reason, rejected.detail)


__all__ = [
    "Anchor",
    "ChangeOutcome",
    "ChangeReason",
    "ChangeRejected",
    "ChangeStatus",
    "HighlightManager",
    "anchor_of",
    "canonical_uid",
    "check_anchor",
    "clean_note",
]
