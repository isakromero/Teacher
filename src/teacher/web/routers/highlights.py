"""Highlights and notes on a study page (`teacher.managers.highlights` has the rules), and the
upload of the ones made offline on a phone.

Every route is under the account gate like every /api route (with the gate on, a request
without a valid session, cookie or `Authorization: Bearer <token>`, is a 401). Highlights are
private to the account: each request sees and changes only the signed-in account's highlights
(with the gate off, the one anonymous reader's), and another account's highlight is a 404. Every
error body is `{"detail": "<message>"}`. Timestamps are ISO 8601 in UTC with an explicit
`+00:00` offset. For every route: 404 no artifact `slug`; 422 the artifact is a lab (labs have
no blocks).

`<highlight>` (`HighlightOut`):

    {
      "id": "0b7f...-...",        # a UUID (canonical lowercase form): made by the server, or
                                  # by the phone for one made offline
      "artifact": "<slug>",
      "block_key": "b03",         # the block it is in
      "block_position": 3 | null, # that block's position on the page (null: no such block)
      "quote": "the highlighted text",
      "prefix": "up to 64 characters before it", "suffix": "... after it",
      "start": 120, "end": 140,   # offsets in the block's text (UTF-16 code units of the web
                                  # client's text model, static/js/highlight_model.js); a hint
      "note": "the reader's note" | null,
      "created_at": "<ISO>", "updated_at": "<ISO>"   # updated_at: the note's latest change
    }

The server never renders blocks: whether the quote can still be found in its block is for the
client to work out (the web page flags one it cannot find as "not found on the page").


GET /api/artifacts/{slug}/highlights
====================================

200 `{"artifact": "<slug>", "highlights": [<highlight>, ...]}`, in page order (by block
position, highlights in unknown blocks last, then by `start`, then oldest first). Removed
highlights are not listed.


POST /api/artifacts/{slug}/highlights
=====================================

Body (unknown keys are a 422): `{"id"?: <UUID>, "block_key", "quote", "prefix"?: "",
"suffix"?: "", "start", "end", "note"?: <string> | null}`. `id` is optional (the server makes
one); `quote` 1 to 4000 characters and not blank, `prefix`/`suffix` at most 64, 0 <= start <
end (integers), `note` at most 10000 characters (a blank note is no note). 201 `<highlight>`.
Errors: 422 a field that breaks these rules, or a block the page does not have; 409 `id` is
taken.


PATCH /api/artifacts/{slug}/highlights/{id}
===========================================

Body `{"note": <string> | null}` (null or blank removes the note; the highlight stays). 200
`<highlight>`. Errors: 404 no such highlight of this account on this page (also a removed one);
422 a bad id or note.


DELETE /api/artifacts/{slug}/highlights/{id}
============================================

Removes the highlight and its note: 204, no body. 404 as for PATCH (so a second DELETE is a
404).


POST /api/offline/artifacts/{slug}/highlights
=============================================

Upload the highlight changes made offline on the phone, in the order they were made. Request
(`HighlightUploadIn`; unknown keys, a missing field, a bad UUID, an `op` not in the list, a
`made_at` without an offset, no changes or more than 500: 422 for the whole request, nothing
is applied):

    {
      "changes": [
        {"op": "create", "id": "<UUID made on the phone>", "block_key": "b03",
         "quote": "...", "prefix": "...", "suffix": "...", "start": 120, "end": 140,
         "note": "..." | null, "made_at": "<ISO 8601 WITH an offset>"},
        {"op": "update_note", "id": "<UUID>", "note": "..." | null, "made_at": "<ISO>"},
        {"op": "delete", "id": "<UUID>", "made_at": "<ISO>"}
      ]
    }

The same id may appear in several changes (made, edited, then removed). Each change is applied
in turn and gets one result; one that cannot apply is "rejected" with a `reason`, and changes
nothing. Every rule, in order:

- any op: `made_at_in_future` when `made_at` is more than 5 minutes after the server's clock;
  `invalid_note` for a note that is not a string or longer than 10000 characters;
  `id_conflict` when the id belongs to a highlight of another page or account.
- create, id not known: the anchor must keep the POST rules (`invalid_anchor`) and the block
  must exist (`unknown_block`); then it is created with `created_at` = `updated_at` =
  `made_at`: "applied".
- create, id known (an upload sent again, or a create carrying the latest note): with another
  anchor (block_key, quote, prefix, suffix, start or end differ) `id_conflict`; removed since:
  "deleted" (it stays removed); otherwise the note is applied as by update_note below.
- update_note, id not known: `unknown_highlight`; removed: "deleted"; the same note as stored:
  "unchanged"; `made_at` not after the stored `updated_at` (the note was changed later
  elsewhere, e.g. on the web page): "stale", the stored note is kept; otherwise "applied",
  `updated_at` = `made_at`.
- delete, id not known: `unknown_highlight` (a highlight made and removed offline need not be
  sent at all); removed already: "unchanged"; otherwise "applied" (a delete wins whatever the
  times: the highlight and its note are removed).

Errors for the whole request: 401 no session; 404 no artifact `slug`; 422 a lab, or a body that
does not match the shape above; 409 a concurrent upload stored the same id first (send it
again: it then comes back "unchanged"). Any other failure is a 5xx and applies nothing (one
transaction).

200 body (`HighlightUploadOut`):

    {
      "artifact": "<slug>",
      "applied": 2, "unchanged": 0, "stale": 0, "deleted": 0, "rejected": 1,
      "results": [
        {"id": "<UUID>", "op": "create",
         "status": "applied" | "unchanged" | "stale" | "deleted" | "rejected",
         "reason": null | "made_at_in_future" | "invalid_note" | "id_conflict"
                   | "invalid_anchor" | "unknown_block" | "unknown_highlight",
         "detail": null | "<message for people>",     # both set exactly when rejected
         "highlight": <highlight> | null}             # as it is now; null when rejected or
                                                      # removed
      ],
      "highlights": [<highlight>, ...]   # every highlight of the account on the page now,
                                         # as GET gives them: replace the saved list with it
    }

A rejected change stays rejected if sent again: show its reason rather than retrying it.


Reproducing these endpoints offline
===================================

The offline bundle (`GET /api/offline/artifacts/{slug}`) carries the account's `highlights`
(the GET list). Offline, answer GET from the saved list; POST: make the id (a UUID v4,
lowercase), `created_at` = `updated_at` = now, `block_position` from the bundle's blocks, and
answer 201 with the highlight (the same 422 rules); PATCH: set the note (blank -> null) and
`updated_at`; DELETE: drop it and answer 204. Record each as a change for the upload above.
"""

from __future__ import annotations

import uuid
from datetime import UTC, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Response
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictInt
from sqlalchemy.orm import Session

from teacher.config import Settings
from teacher.db.models import Artifact, Highlight
from teacher.labs import require_not_lab
from teacher.managers import ArtifactManager
from teacher.managers.highlights import (
    Anchor,
    ChangeReason,
    ChangeStatus,
    HighlightManager,
)
from teacher.web.common import to_iso
from teacher.web.deps import CurrentUser, CurrentUserDep, SessionDep, SettingsDep
from teacher.web.routers.review import ClockDep

router = APIRouter(prefix="/api", tags=["highlights"])

# The same limits as the offline attempts upload (`routers/offline.py`, which imports this
# module, so they are not imported from there; a test checks that they agree).
MAX_CLOCK_SKEW = timedelta(minutes=5)
MAX_CHANGES = 500


# -- models ---------------------------------------------------------------------------------


class HighlightOut(BaseModel):
    id: str
    artifact: str
    block_key: str
    block_position: int | None
    quote: str
    prefix: str
    suffix: str
    start: int
    end: int
    note: str | None
    created_at: str
    updated_at: str


class HighlightsOut(BaseModel):
    artifact: str
    highlights: list[HighlightOut]


class HighlightIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID | None = None
    block_key: str
    quote: str
    prefix: str = ""
    suffix: str = ""
    start: StrictInt
    end: StrictInt
    note: str | None = None

    def anchor(self) -> Anchor:
        return Anchor(self.block_key, self.quote, self.prefix, self.suffix, self.start, self.end)


class NoteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str | None


class _ChangeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    made_at: AwareDatetime


class CreateChangeIn(_ChangeIn):
    op: Literal["create"]
    block_key: str
    quote: str
    prefix: str = ""
    suffix: str = ""
    start: StrictInt
    end: StrictInt
    note: str | None = None


class UpdateNoteChangeIn(_ChangeIn):
    op: Literal["update_note"]
    note: str | None


class DeleteChangeIn(_ChangeIn):
    op: Literal["delete"]


ChangeIn = Annotated[
    CreateChangeIn | UpdateNoteChangeIn | DeleteChangeIn, Field(discriminator="op")
]


class HighlightUploadIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    changes: list[ChangeIn] = Field(min_length=1, max_length=MAX_CHANGES)


class ChangeResultOut(BaseModel):
    id: str
    op: Literal["create", "update_note", "delete"]
    status: ChangeStatus
    reason: ChangeReason | None
    detail: str | None
    highlight: HighlightOut | None


class HighlightUploadOut(BaseModel):
    artifact: str
    applied: int
    unchanged: int
    stale: int
    deleted: int
    rejected: int
    results: list[ChangeResultOut]
    highlights: list[HighlightOut]


# -- helpers --------------------------------------------------------------------------------


def owner_of(user: CurrentUser | None) -> int | None:
    """The account the request's highlights belong to (None: the app has no accounts)."""
    return user.id if user is not None else None


def highlight_out(row: Highlight, slug: str, position: int | None) -> HighlightOut:
    return HighlightOut(
        id=row.uid,
        artifact=slug,
        block_key=row.block_key,
        block_position=position,
        quote=row.quote,
        prefix=row.prefix,
        suffix=row.suffix,
        start=row.start_offset,
        end=row.end_offset,
        note=row.note,
        created_at=to_iso(row.created_at) or "",
        updated_at=to_iso(row.updated_at) or "",
    )


def list_highlights(
    session: Session, settings: Settings, artifact: Artifact, owner: int | None
) -> list[HighlightOut]:
    """The owner's highlights on `artifact`, as GET .../highlights lists them."""
    manager = HighlightManager(session, settings, owner=owner)
    return [highlight_out(row, artifact.slug, pos) for row, pos in manager.list(artifact)]


def _study_page(session: Session, settings: Settings, slug: str) -> Artifact:
    artifact = ArtifactManager(session, settings).get_by_slug(slug)
    require_not_lab(artifact, "highlights")
    return artifact


def _one_out(manager: HighlightManager, artifact: Artifact, row: Highlight) -> HighlightOut:
    return highlight_out(row, artifact.slug, manager.block_position(artifact, row.block_key))


# -- endpoints ------------------------------------------------------------------------------


@router.get("/artifacts/{slug}/highlights")
def get_highlights(
    slug: str, session: SessionDep, settings: SettingsDep, user: CurrentUserDep
) -> HighlightsOut:
    """The account's highlights on the page, in page order."""
    artifact = _study_page(session, settings, slug)
    return HighlightsOut(
        artifact=artifact.slug,
        highlights=list_highlights(session, settings, artifact, owner_of(user)),
    )


@router.post("/artifacts/{slug}/highlights", status_code=201)
def create_highlight(
    slug: str,
    body: HighlightIn,
    session: SessionDep,
    settings: SettingsDep,
    user: CurrentUserDep,
    clock: ClockDep,
) -> HighlightOut:
    """Highlight a passage (with an optional note)."""
    artifact = _study_page(session, settings, slug)
    manager = HighlightManager(session, settings, owner=owner_of(user), clock=clock)
    row = manager.create(artifact, body.anchor(), note=body.note, uid=body.id)
    return _one_out(manager, artifact, row)


@router.patch("/artifacts/{slug}/highlights/{highlight_id}")
def update_highlight(
    slug: str,
    highlight_id: uuid.UUID,
    body: NoteIn,
    session: SessionDep,
    settings: SettingsDep,
    user: CurrentUserDep,
    clock: ClockDep,
) -> HighlightOut:
    """Change (or remove) a highlight's note."""
    artifact = _study_page(session, settings, slug)
    manager = HighlightManager(session, settings, owner=owner_of(user), clock=clock)
    row = manager.update_note(manager.get(artifact, highlight_id), body.note)
    return _one_out(manager, artifact, row)


@router.delete("/artifacts/{slug}/highlights/{highlight_id}", status_code=204)
def delete_highlight(
    slug: str,
    highlight_id: uuid.UUID,
    session: SessionDep,
    settings: SettingsDep,
    user: CurrentUserDep,
    clock: ClockDep,
) -> Response:
    """Remove a highlight and its note."""
    artifact = _study_page(session, settings, slug)
    manager = HighlightManager(session, settings, owner=owner_of(user), clock=clock)
    manager.delete(manager.get(artifact, highlight_id))
    return Response(status_code=204)


@router.post("/offline/artifacts/{slug}/highlights")
def upload_highlights(
    slug: str,
    body: HighlightUploadIn,
    session: SessionDep,
    settings: SettingsDep,
    user: CurrentUserDep,
    clock: ClockDep,
) -> HighlightUploadOut:
    """Apply highlight changes made offline, in order; one result per change."""
    artifact = _study_page(session, settings, slug)
    manager = HighlightManager(session, settings, owner=owner_of(user), clock=clock)
    max_future = clock() + MAX_CLOCK_SKEW
    results: list[ChangeResultOut] = []
    for change in body.changes:
        made_at = change.made_at.astimezone(UTC).replace(tzinfo=None)
        anchor = note = None
        if isinstance(change, CreateChangeIn):
            anchor = Anchor(
                change.block_key, change.quote, change.prefix, change.suffix, change.start,
                change.end,
            )
            note = change.note
        elif isinstance(change, UpdateNoteChangeIn):
            note = change.note
        outcome = manager.apply_change(
            artifact,
            change.op,
            change.id,
            made_at=made_at,
            max_future=max_future,
            anchor=anchor,
            note=note,
        )
        row = outcome.highlight
        live = row is not None and row.deleted_at is None
        results.append(
            ChangeResultOut(
                id=str(change.id),
                op=change.op,
                status=outcome.status,
                reason=outcome.reason,
                detail=outcome.detail,
                highlight=_one_out(manager, artifact, row) if live and row is not None else None,
            )
        )
    counts = {
        status: sum(1 for r in results if r.status == status)
        for status in ("applied", "unchanged", "stale", "deleted", "rejected")
    }
    return HighlightUploadOut(
        artifact=artifact.slug,
        **counts,
        results=results,
        highlights=list_highlights(session, settings, artifact, owner_of(user)),
    )


__all__ = [
    "MAX_CHANGES",
    "MAX_CLOCK_SKEW",
    "HighlightIn",
    "HighlightOut",
    "HighlightUploadIn",
    "HighlightUploadOut",
    "HighlightsOut",
    "highlight_out",
    "list_highlights",
    "owner_of",
    "router",
]
