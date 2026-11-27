"""Offline reading: a study page saved on a phone, and the attempts made there uploaded later.

Two endpoints, both under the account gate like every /api route (with the gate on, a request
without a valid session, cookie or `Authorization: Bearer <token>`, is a 401
`{"detail": ...}`). Pages, answers and history are shared by every account, as everywhere
else; the bundle's `highlights` are the signed-in account's own. Two more offline endpoints
live with what they serve: `POST /api/offline/artifacts/{slug}/highlights` (highlight changes
made offline, `routers/highlights.py`) and `GET /api/offline/review` with `POST
/api/offline/review/answers` (the review queue offline, `routers/offline_review.py`). Every
error body is `{"detail": "<message>"}`. Timestamps are ISO 8601 strings in UTC with an
explicit `+00:00` offset (Python's `isoformat()`: `2026-11-25T10:00:00+00:00`, with `.ffffff`
microseconds only when they are not zero).

Unlike `routers/reading.py`, the bundle carries every block (locked ones too) WITH its private
part (the correct option, the explanations, the model answer and rubric) and every response
WITH its grader `details`: the phone holds what it needs to grade and to unlock on its own, and
it must hide what the web page hides (see "Reproducing the reading endpoints").


GET /api/offline/artifacts/{slug}
=================================

The offline bundle of one ready study page. Errors: 404 no artifact `slug`; 422 the artifact
is a lab (labs have no blocks); 409 the page is not `ready` (still generating, or failed); the
same checks, in that order, as `GET /api/artifacts/{slug}/blocks`. 200 body
(`OfflineBundleOut`):

    {
      "format": "teacher-offline-bundle",   # constant
      "format_version": 1,                  # bumped on any incompatible change of this shape
      "artifact": "<slug>",
      "artifact_id": 12,
      "title": "<page title>",
      "generated_at": "<ISO>",    # when this page (this artifact row) was created
      "version": "<64 hex>",      # see "Version" below
      "superseded_by": "<slug>" | null,   # the newest regeneration of this page, if any
      "bundled_at": "<ISO>",      # when this bundle was built (server time)
      "max_attempts": 2,          # attempts after which an unpassed gate is "revealed"
      "default_min_words": 20,    # a blurb's min_words when its content has none
      "grading": {                # the deterministic graders' constants, see "Grading"
        "multiple_choice_grader": "multiple-choice-v2",
        "multiple_choice_wrong_prefix": "Not quite. ",
        "no_option_feedback": "<text>",
        "prediction_grader": "prediction-v1",
        "prediction_feedback": "<text>",
        "device_grader_prefix": "device:"
      },
      "total_blocks": 5, "gates_total": 2, "gates_cleared": 0, "locked_blocks": 3,
      "complete": false,          # the server's reading state now, as GET .../blocks has it
      "blocks": [ <block>, ... ], # EVERY block, by position (1, 2, ...)
      "assets": [ <asset>, ... ], # every file under the page's assets/ folder, by path
      "highlights": [ <highlight>, ... ]   # the signed-in account's highlights and notes on
                                  # the page, as GET /api/artifacts/{slug}/highlights lists them
                                  # (routers/highlights.py documents <highlight>); a key added
                                  # within format_version 1 (older readers ignore it)
    }

`<block>` (`OfflineBlockOut`) has every field of the reading API's `BlockOut`, with the values
GET .../blocks would give if the block were visible, plus five more:

    {
      "key": "b02", "position": 2, "type": "multiple_choice",
      "is_gate": true,
      "content": {...},        # the PUBLIC content (no "private" key)
      "state": "open" | "passed" | "revealed" | null,   # null for a block that is no gate
      "attempts": 0,
      "revealed": {...} | null,   # = private when state is "passed" or "revealed", else null
      "segments": [{"id", "source", "kind", "label", "title", "locator"}, ...],
      "concepts": [{"slug", "name", "role", "origin"}, ...],
      # the five extra fields:
      "is_question": true,     # multiple_choice and blurb; false for a prediction and non-gates
      "private": {...} | null, # the private content (null for a block that has none)
      "passed": false,         # some recorded attempt passed
      "visible": true,         # false: locked behind an uncleared gate (see below)
      "responses": [ <response>, ... ]   # every recorded attempt, by attempt number
    }

`<response>` is the reading API's `ResponseOut` with `details` always filled in:
`{"attempt", "response", "passed", "verdict", "feedback", "missing", "confidence", "grader",
"details", "created_at"}`. `<asset>` is `{"path": "img/fig.png" (relative to assets/, "/"
separated), "url": "/api/artifacts/<slug>/assets/img/fig.png" (relative to the server root,
percent-encoded; GET it with the same session), "bytes": 1234, "sha256": "<64 hex>"}`; a
symlink that leads out of the folder is not listed (the asset endpoint does not serve it).

Version. `version` is the SHA-256 (hex) of the page's identity and every block's key, position,
type, gate flag and stored content hash. It changes when, and only when, the slug names
another page (a deleted page whose slug was reused by a regeneration) or a block's content
changed; concepts and segments are not part of it (tagging a page again does not change it).
Keep the `version` of the bundle the attempts were made on; send it with the upload.


Reproducing the reading endpoints offline
=========================================

The phone keeps the bundle's blocks and responses and adds its own attempts to `responses`.
Everything below is what `teacher.reading` does on the server; doing exactly this gives the
same JSON as the endpoints would.

State. Walk the blocks by position. For each: `attempts` = number of responses, `passed` = any
response has `passed: true`, `state` = null if `is_gate` is false, else "passed" if `passed`,
else "revealed" if `attempts >= max_attempts`, else "open". A gate is *cleared* when its state
is "passed" or "revealed". Every block up to and including the first block whose state is
"open" is visible; every block after it is locked. `revealed` = `private` if cleared, else
null.

GET /api/artifacts/{slug}/blocks = `{"artifact": <slug>, "total_blocks": <number of blocks>,
"locked_blocks": <number not visible>, "complete": <every gate cleared (true without gates)>,
"blocks": [<the BlockOut fields of each VISIBLE block, by position>]}`. The BlockOut fields are
`key, position, type, is_gate, content, state, attempts, revealed, segments, concepts`.

GET /api/artifacts/{slug}/blocks/{key}/responses (a visible block; locked is a 409) = the
block's responses by attempt number, each with `details` replaced by null unless the block is
cleared now.

POST /api/artifacts/{slug}/blocks/{key}/responses with `{"response": ..., "confidence": ...}`
(the server's errors: 404 unknown key; 409 locked, not a gate, already passed; 422 malformed
response, or a confidence on a gate that is not a question): grade (see "Grading"; a malformed
response is not an attempt and records nothing), append the response `{"attempt": attempts +
1, "response", "passed": verdict == "passed", "verdict", "feedback", "missing", "confidence"
(null when not given), "grader", "details", "created_at": <now>}`, recompute the state, and
the reply is `{"attempt", "passed", "verdict", "feedback", "missing", "confidence", "details"
(null unless the gate is cleared now), "state" (the gate's), "revealed" (the gate's),
"unlocked": [<BlockOut of every block visible now that was not before, by position>],
"locked_blocks", "complete"}`.

Offline, offer an attempt only at a visible gate whose state is "open". The web page also takes
attempts at a "revealed" gate, but such an attempt made offline is rejected on upload
(`attempts_exhausted`, below).

Grading. A multiple choice or a prediction takes exactly `{"choice": <integer option index>}`
(not a bool; 0 <= choice < number of options); anything else is malformed.
- multiple_choice: choice == private.correct_index: verdict "passed", feedback =
  private.explanation. Otherwise verdict "failed", feedback = multiple_choice_wrong_prefix +
  private.option_explanations[choice], or no_option_feedback when the page has no
  option_explanations. details null, missing [], grader multiple_choice_grader.
- prediction: verdict always "passed", feedback prediction_feedback, details `{"correct":
  choice == private.correct_index}`, missing [], grader prediction_grader. Takes no confidence.
- blurb: the response is exactly `{"text": <string>}` whose text is not blank and has at least
  min_words words (content.min_words, else default_min_words; words are what Python's
  `str.split()` separates: runs of whitespace); otherwise it is malformed (not an attempt).
  Graded on the phone against private.model_answer and private.rubric (each criterion
  `{"criterion", "required"}`): "passed" when every required criterion is met (also when
  there is none), "partial" when some but not all required criteria are met, with `missing`
  = the unmet required criteria's texts word for word, "failed" when none is. `missing` is
  non-empty exactly for "partial". grader = device_grader_prefix + a name for the on-device
  grader (e.g. "device:apple-foundation-models"). details: anything JSON (suggested: the
  server judge's shape `{"model", "score", "criteria": [{"criterion", "required", "met",
  "justification"}]}`), or null.
- confidence ("low", "medium" or "high", optional) only on a question (`is_question`).


POST /api/offline/artifacts/{slug}/attempts
===========================================

Upload attempts made on the phone, in the order they were made. Request (`OfflineUploadIn`;
unknown keys are a 422):

    {
      "version": "<the version of the bundle the attempts were made on>",
      "attempts": [                       # 1 to 500 items, in the order they were made
        {
          "client_id": "<UUID>",          # made by the phone for this attempt; the
                                          # idempotency key (any UUID form; reported back
                                          # in canonical lowercase form)
          "key": "b02",                   # the block
          "response": {"choice": 1},      # any JSON value; checked per item
          "confidence": "low" | "medium" | "high" | null,   # optional, default null
          "verdict": "passed" | "partial" | "failed",       # the phone's
          "feedback": "<text>" | null,    # optional, default null
          "missing": ["<criterion>", ...],# optional, default []
          "details": {...} | null,        # optional, default null
          "grader": "device:<name>",      # the phone's grader, for every block type
          "made_at": "<ISO 8601 WITH an offset>"   # when it was made on the phone
        }
      ]
    }

Errors for the whole request (nothing is recorded): 401 no session; 404 no artifact `slug`
(the page was deleted: its attempts can never be applied; tell the reader);
422 a lab; 409 the page is not ready; 422 a body that does not match the shape above (a
missing or unknown field, a bad UUID, a `made_at` without an offset, a verdict or confidence
not in the list, no items or more than 500, the same client_id twice in one request); 409 a
concurrent upload stored the same attempt or attempt number first (retry: the items it stored
then come back as "already_accepted"). Any other failure is a 5xx and also records nothing:
the whole upload is one transaction.

Otherwise 200, with one result per item, in request order. Items are applied one after the
other, so an item sees the attempts accepted before it (an attempt that clears a gate unlocks
the blocks after it for the next items). Each item is checked in this order; the first rule
it breaks rejects it with that `reason`, and a rejected item records nothing:

1. `client_id` was accepted before (by any earlier upload): status "already_accepted", nothing
   is recorded again; unless that attempt was for another page, another block or another
   response: rejected, `client_id_conflict`.
2. `page_changed`: `version` is not the page's current version.
3. `made_at_in_future`: `made_at` is more than `MAX_CLOCK_SKEW` (5 minutes) after the
   server's clock.
4. `unknown_block`: the page has no block `key`.
5. `block_locked`: the block is locked on the server (a gate before it is not cleared there).
6. `not_a_gate`: the block takes no answers.
7. `gate_already_passed`: the gate has a passing attempt on the server already.
8. `attempts_exhausted`: the gate is "revealed" on the server (max_attempts attempts, none
   passed): the reader may have seen the answer, so an attempt made offline no longer counts.
9. `unsupported_block_type`: a gate type this endpoint cannot grade (all gate types today,
   multiple_choice, prediction and blurb, are supported).
10. `invalid_grader`: `grader` does not start with "device:" or names nothing after it.
11. `confidence_not_allowed`: a confidence on a gate that is not a question (a prediction).
12. `invalid_grade`: the phone's grade breaks the rules every grade keeps (`missing` non-empty
    exactly for "partial", and each entry a non-blank string; `details` an object of plain
    JSON values, or null); for a blurb also: each `missing` entry the text of one of the
    blurb's required rubric criteria, word for word, none twice, and a "partial" leaves at
    least one required criterion met.
13. `malformed_response`: the response is malformed for its block type (see "Grading").
14. `invalid_attempt`: the attempt could not be stored for another reason (the detail says
    which); should not happen once the rules above hold.

An item that passes them all is accepted and stored like an attempt made on the web page
(`BlockResponseManager.record`): the next attempt number, in the study session open at
`made_at`, with `created_at` = `made_at` (in UTC). A multiple choice or a prediction is graded
again on the server with the deterministic graders above, and the server's grade is stored
(verdict, feedback, details, missing, grader "multiple-choice-v2"/"prediction-v1"): the
phone's verdict, feedback, missing and details for it are not stored; when its verdict differs
from the server's, the item says so (`verdict_mismatch`). A blurb keeps the phone's grade
(verdict, feedback, missing, details, grader). The phone's grader and verdict are kept in
`offline_attempts` (revision 0018) with the client_id. An attempt that clears its gate logs
the same `gate_cleared` study event as the web page (at `made_at`). Attempt numbers follow the
upload order, whatever the `made_at` times.

200 body (`OfflineUploadOut`):

    {
      "artifact": "<slug>",
      "version": "<the page's current version>",
      "version_matched": true,           # the request's version is the current one
      "accepted": 2, "already_accepted": 0, "rejected": 1,
      "results": [
        {
          "client_id": "<canonical UUID>",
          "key": "b02",
          "status": "accepted" | "already_accepted" | "rejected",
          "reason": null | "<one of the reasons above>",    # set exactly when rejected
          "detail": null | "<message for people>",         # set exactly when rejected
          "attempt": 1 | null,           # the stored attempt number (null when rejected)
          "recorded": <response> | null, # the stored attempt as in the bundle (details
                                         # included); null when rejected
          "device_verdict": "failed",    # the verdict the phone sent
          "regraded": true,              # graded again on the server (multiple choice,
                                         # prediction); false when rejected
          "verdict_mismatch": false,     # regraded and the server's verdict differs
          "gate_cleared": false,         # this upload's attempt cleared the gate (and logged
                                         # gate_cleared); false for already_accepted
          "state": "open" | "passed" | "revealed" | null,  # the gate's state on the server
                                         # right after the item was applied (for
                                         # already_accepted: when it was reached in this
                                         # upload); null when rejected
          "unlocked": ["b03", "b04"]     # keys this attempt unlocked ([] otherwise)
        }
      ],
      "bundle": <OfflineBundleOut>       # the page as it is after this upload
    }

After an upload, replace the saved blocks, responses and highlights with `bundle` (when `version_matched`
is false the bundle is the page now behind the slug: the saved one is gone). A rejected item
stays rejected if sent again (except one rejected for the server's state, which may change):
show its reason to the reader rather than retrying it forever.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import replace
from datetime import UTC, timedelta
from pathlib import Path
from typing import Any, Literal, get_args
from urllib.parse import quote

from fastapi import APIRouter
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from teacher.blocks import is_question_type, private_content
from teacher.config import Settings
from teacher.db.models import (
    DEVICE_GRADER_PREFIX,
    Artifact,
    ArtifactBlock,
    BlockResponse,
    OfflineAttempt,
    utcnow,
)
from teacher.files import sha256_file
from teacher.grading import (
    DEFAULT_MIN_WORDS,
    NO_OPTION_FEEDBACK,
    PREDICTION_FEEDBACK,
    Confidence,
    Grader,
    GradeResult,
    MultipleChoiceGrader,
    PredictionGrader,
    Verdict,
)
from teacher.judge import _answer_text
from teacher.labs import require_not_lab
from teacher.managers import (
    ArtifactBlockManager,
    ArtifactManager,
    BlockResponseManager,
    NotFoundError,
    StudyHistoryManager,
)
from teacher.managers.artifacts import _is_unique_violation
from teacher.managers.block_responses import response_verdict
from teacher.managers.errors import DuplicateError
from teacher.reading import (
    OPEN,
    REVEALED,
    reading_state,
    require_ready,
)
from teacher.web.common import to_iso
from teacher.web.deps import CurrentUserDep, SessionDep, SettingsDep
from teacher.web.routers.highlights import HighlightOut, list_highlights
from teacher.web.routers.reading import (
    ASSETS_DIR,
    BlockOut,
    MaxAttemptsDep,
    ResponseOut,
    _conflicts_as_409,
    block_out,
)

router = APIRouter(prefix="/api/offline", tags=["offline"])

BUNDLE_FORMAT = "teacher-offline-bundle"
BUNDLE_FORMAT_VERSION = 1
MAX_CLOCK_SKEW = timedelta(minutes=5)
MAX_UPLOAD_ITEMS = 500
MULTIPLE_CHOICE_WRONG_PREFIX = "Not quite. "

# The gate types graded again on the server, with the deterministic graders the bundle
# describes (never the app's configured graders: the phone grades with these rules).
SERVER_GRADERS: dict[str, Grader] = {
    "multiple_choice": MultipleChoiceGrader(),
    "prediction": PredictionGrader(),
}
DEVICE_GRADED = ("blurb",)

RejectionReason = Literal[
    "client_id_conflict",
    "page_changed",
    "made_at_in_future",
    "unknown_block",
    "block_locked",
    "not_a_gate",
    "gate_already_passed",
    "attempts_exhausted",
    "unsupported_block_type",
    "invalid_grader",
    "confidence_not_allowed",
    "invalid_grade",
    "malformed_response",
    "invalid_attempt",
]
REJECTION_REASONS: tuple[str, ...] = get_args(RejectionReason)


# -- models ---------------------------------------------------------------------------------


class OfflineGradingOut(BaseModel):
    multiple_choice_grader: str
    multiple_choice_wrong_prefix: str
    no_option_feedback: str
    prediction_grader: str
    prediction_feedback: str
    device_grader_prefix: str


class OfflineAssetOut(BaseModel):
    path: str
    url: str
    bytes: int
    sha256: str


class OfflineBlockOut(BlockOut):
    is_question: bool
    private: dict[str, Any] | None
    passed: bool
    visible: bool
    responses: list[ResponseOut]  # details always included


class OfflineBundleOut(BaseModel):
    format: str
    format_version: int
    artifact: str
    artifact_id: int
    title: str
    generated_at: str
    version: str
    superseded_by: str | None
    bundled_at: str
    max_attempts: int
    default_min_words: int
    grading: OfflineGradingOut
    total_blocks: int
    gates_total: int
    gates_cleared: int
    locked_blocks: int
    complete: bool
    blocks: list[OfflineBlockOut]
    assets: list[OfflineAssetOut]
    highlights: list[HighlightOut]


class OfflineAttemptIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_id: uuid.UUID
    key: str
    response: Any
    confidence: Confidence | None = None
    verdict: Verdict
    feedback: str | None = None
    missing: list[str] = Field(default_factory=list)
    details: dict[str, Any] | None = None
    grader: str
    made_at: AwareDatetime


class OfflineUploadIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str
    attempts: list[OfflineAttemptIn] = Field(min_length=1, max_length=MAX_UPLOAD_ITEMS)

    @model_validator(mode="after")
    def _distinct_client_ids(self) -> OfflineUploadIn:
        seen: set[uuid.UUID] = set()
        for item in self.attempts:
            if item.client_id in seen:
                raise ValueError(f"client_id {item.client_id} is given twice in one upload")
            seen.add(item.client_id)
        return self


class OfflineAttemptResultOut(BaseModel):
    client_id: str
    key: str
    status: Literal["accepted", "already_accepted", "rejected"]
    reason: RejectionReason | None
    detail: str | None
    attempt: int | None
    recorded: ResponseOut | None
    device_verdict: Verdict
    regraded: bool
    verdict_mismatch: bool
    gate_cleared: bool
    state: str | None
    unlocked: list[str]


class OfflineUploadOut(BaseModel):
    artifact: str
    version: str
    version_matched: bool
    accepted: int
    already_accepted: int
    rejected: int
    results: list[OfflineAttemptResultOut]
    bundle: OfflineBundleOut


# -- the bundle -----------------------------------------------------------------------------


def page_version(artifact: Artifact, blocks: list[ArtifactBlock]) -> str:
    """The page's `version` (see the module docstring): SHA-256 of its identity and blocks."""
    identity = {
        "artifact_id": artifact.id,
        "created_at": to_iso(artifact.created_at),
        "blocks": [[b.key, b.position, b.type, b.is_gate, b.content_sha256] for b in blocks],
    }
    data = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _response_out(row: BlockResponse) -> ResponseOut:
    """A stored attempt as the reading API shows it, with `details` always included."""
    return ResponseOut(
        attempt=row.attempt,
        response=row.response,
        passed=row.passed,
        verdict=response_verdict(row),
        feedback=row.feedback,
        missing=row.missing,
        confidence=row.confidence,
        grader=row.grader,
        details=row.details,
        created_at=to_iso(row.created_at),
    )


def _assets(folder: Path, slug: str) -> list[OfflineAssetOut]:
    """Every file `GET /api/artifacts/{slug}/assets/{path}` serves, by path."""
    root = folder.resolve()
    if not root.is_dir():
        return []
    out: list[OfflineAssetOut] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            rel = (Path(dirpath) / name).relative_to(root).as_posix()
            target = (root / rel).resolve()
            if not target.is_relative_to(root) or target == root or not target.is_file():
                continue  # a link out of the folder: the asset endpoint does not serve it
            out.append(
                OfflineAssetOut(
                    path=rel,
                    url=f"/api/artifacts/{quote(slug)}/{ASSETS_DIR}/{quote(rel)}",
                    bytes=target.stat().st_size,
                    sha256=sha256_file(target),
                )
            )
    return sorted(out, key=lambda a: a.path)


def build_bundle(
    session: Session,
    settings: Settings,
    artifact: Artifact,
    *,
    max_attempts: int,
    owner: int | None,
) -> OfflineBundleOut:
    """The offline bundle of a ready study page (the caller checks kind and status), with the
    highlights of `owner` (a user id, or None without accounts)."""
    blocks_manager = ArtifactBlockManager(session, settings)
    responses = BlockResponseManager(session, settings)
    state = reading_state(session, settings, artifact, max_attempts=max_attempts)
    blocks: list[OfflineBlockOut] = []
    for block_state in state.blocks:
        block = block_state.block
        # The reader's view of the block as if it were visible: the same code as GET .../blocks.
        base = block_out(session, settings, replace(block_state, visible=True))
        blocks.append(
            OfflineBlockOut(
                **base.model_dump(),
                is_question=block.is_gate and is_question_type(block.type),
                private=private_content(blocks_manager.read_content(block)),
                passed=block_state.passed,
                visible=block_state.visible,
                responses=[_response_out(r) for r in responses.list(block)],
            )
        )
    successors = artifact.superseded_by
    return OfflineBundleOut(
        format=BUNDLE_FORMAT,
        format_version=BUNDLE_FORMAT_VERSION,
        artifact=artifact.slug,
        artifact_id=artifact.id,
        title=artifact.title,
        generated_at=to_iso(artifact.created_at),
        version=page_version(artifact, [b.block for b in state.blocks]),
        superseded_by=successors[-1].slug if successors else None,
        bundled_at=to_iso(utcnow()),
        max_attempts=max_attempts,
        default_min_words=DEFAULT_MIN_WORDS,
        grading=OfflineGradingOut(
            multiple_choice_grader=MultipleChoiceGrader.name,
            multiple_choice_wrong_prefix=MULTIPLE_CHOICE_WRONG_PREFIX,
            no_option_feedback=NO_OPTION_FEEDBACK,
            prediction_grader=PredictionGrader.name,
            prediction_feedback=PREDICTION_FEEDBACK,
            device_grader_prefix=DEVICE_GRADER_PREFIX,
        ),
        total_blocks=state.total_blocks,
        gates_total=state.gates_total,
        gates_cleared=state.gates_cleared,
        locked_blocks=state.locked_blocks,
        complete=state.complete,
        blocks=blocks,
        assets=_assets(ArtifactManager(session, settings).dir(artifact) / ASSETS_DIR, artifact.slug),
        highlights=list_highlights(session, settings, artifact, owner),
    )


def _study_page(session: Session, settings: Settings, slug: str) -> Artifact:
    """The ready study page `slug`: 404 unknown, 422 a lab, 409 not ready."""
    artifact = ArtifactManager(session, settings).get_by_slug(slug)
    require_not_lab(artifact, "blocks")
    with _conflicts_as_409():
        require_ready(artifact)
    return artifact


# -- uploading attempts ---------------------------------------------------------------------


class _Rejected(Exception):
    def __init__(self, reason: RejectionReason, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


def _check_json(value: Any, what: str) -> None:
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as err:
        raise _Rejected("invalid_grade", f"{what} is not plain JSON: {err}") from err


def _device_grade(item: OfflineAttemptIn) -> GradeResult:
    """The phone's grade as a GradeResult (its invariants checked); `invalid_grade` if not."""
    try:
        result = GradeResult(
            item.verdict,
            feedback=item.feedback,
            grader=item.grader,
            details=item.details,
            missing=tuple(item.missing),
        )
    except ValueError as err:
        raise _Rejected("invalid_grade", f"the grade is invalid: {err}") from err
    if item.details is not None:
        _check_json(item.details, "details")
    return result


def _check_blurb_grade(content: dict[str, Any], result: GradeResult) -> None:
    """A blurb's `missing`: required criteria of its rubric, word for word, none twice, and
    a partial answer meets at least one."""
    required = [c["criterion"] for c in content["private"]["rubric"] if c.get("required", True)]
    if len(set(result.missing)) != len(result.missing):
        raise _Rejected("invalid_grade", "missing lists a criterion twice")
    unknown = [m for m in result.missing if m not in required]
    if unknown:
        raise _Rejected(
            "invalid_grade",
            f"missing lists {unknown!r}, which is not a required criterion of this blurb's "
            "rubric word for word",
        )
    if result.missing and len(result.missing) >= len(required):
        raise _Rejected(
            "invalid_grade",
            "a partial answer meets at least one required criterion, but missing lists all "
            f"{len(required)} of them (that is a failed answer)",
        )


def _apply(
    session: Session,
    settings: Settings,
    artifact: Artifact,
    item: OfflineAttemptIn,
    *,
    version_matched: bool,
    max_attempts: int,
) -> OfflineAttemptResultOut:
    """Check one uploaded attempt and, if it may apply, store it (see the module docstring)."""
    client_id = str(item.client_id)
    base = {"client_id": client_id, "key": item.key, "device_verdict": item.verdict}

    existing = session.scalar(select(OfflineAttempt).where(OfflineAttempt.client_id == client_id))
    if existing is not None:
        row = session.get(BlockResponse, existing.response_id)
        assert row is not None  # the foreign key: the record goes with its response
        block = row.block
        if block.artifact_id != artifact.id or block.key != item.key or row.response != item.response:
            where = (
                "another page"
                if block.artifact_id != artifact.id
                else f"block {block.key}" if block.key != item.key else "another response"
            )
            raise _Rejected(
                "client_id_conflict",
                f"client_id {client_id} was accepted before for {where}; a client_id names "
                "one attempt",
            )
        now = reading_state(session, settings, artifact, max_attempts=max_attempts)
        regraded = block.type in SERVER_GRADERS
        return OfflineAttemptResultOut(
            **base,
            status="already_accepted",
            reason=None,
            detail=None,
            attempt=row.attempt,
            recorded=_response_out(row),
            regraded=regraded,
            verdict_mismatch=regraded and existing.device_verdict != row.verdict,
            gate_cleared=False,
            state=now.get(block.key).state,
            unlocked=[],
        )

    if not version_matched:
        raise _Rejected(
            "page_changed",
            "the page changed since the bundle these attempts were made on was saved "
            "(another version): download the page again",
        )
    made_at = item.made_at.astimezone(UTC).replace(tzinfo=None)
    if made_at > utcnow() + MAX_CLOCK_SKEW:
        raise _Rejected(
            "made_at_in_future",
            f"made_at {item.made_at.isoformat()} is later than the server's clock (by more "
            f"than {int(MAX_CLOCK_SKEW.total_seconds() // 60)} minutes): check the phone's clock",
        )
    before = reading_state(session, settings, artifact, max_attempts=max_attempts)
    try:
        current = before.get(item.key)
    except NotFoundError as err:
        raise _Rejected("unknown_block", f"the page has no block {item.key!r}") from err
    block = current.block
    if current.locked:
        raise _Rejected(
            "block_locked", f"block {item.key} is locked: a gate before it is not cleared"
        )
    if not current.is_gate:
        raise _Rejected("not_a_gate", f"block {item.key} is not a gate and takes no responses")
    if current.passed:
        raise _Rejected("gate_already_passed", f"block {item.key} is already passed")
    if current.state == REVEALED:
        raise _Rejected(
            "attempts_exhausted",
            f"block {item.key} has had its {max_attempts} attempts and is revealed: an attempt "
            "made offline after that no longer counts",
        )
    if block.type not in SERVER_GRADERS and block.type not in DEVICE_GRADED:
        raise _Rejected(
            "unsupported_block_type", f"a {block.type} gate cannot be graded offline"
        )
    if not item.grader.startswith(DEVICE_GRADER_PREFIX) or not item.grader[
        len(DEVICE_GRADER_PREFIX):
    ].strip():
        raise _Rejected(
            "invalid_grader",
            f"grader must be {DEVICE_GRADER_PREFIX!r} followed by the on-device grader's name, "
            f"got {item.grader!r}",
        )
    if item.confidence is not None and not is_question_type(block.type):
        raise _Rejected(
            "confidence_not_allowed",
            f"block {item.key} is a {block.type}, not a question, and takes no confidence",
        )
    device = _device_grade(item)
    content = ArtifactBlockManager(session, settings).read_content(block)
    if block.type in DEVICE_GRADED:
        _check_blurb_grade(content, device)
        try:
            _answer_text(content, item.response)
        except ValueError as err:
            raise _Rejected("malformed_response", str(err)) from err
        result = device
    else:
        try:
            result = SERVER_GRADERS[block.type].grade(content, item.response)
        except ValueError as err:
            raise _Rejected("malformed_response", str(err)) from err

    try:
        row = BlockResponseManager(session, settings).record(
            block,
            response=item.response,
            passed=result.passed,
            grader=result.grader,
            feedback=result.feedback,
            details=result.details,
            verdict=result.verdict,
            missing=result.missing,
            confidence=item.confidence,
            at=made_at,
        )
    except (ValueError, TypeError) as err:
        raise _Rejected("invalid_attempt", str(err)) from err
    record = OfflineAttempt(
        client_id=client_id,
        response_id=row.id,
        made_at=made_at,
        device_grader=item.grader,
        device_verdict=item.verdict,
    )
    try:
        with session.begin_nested():
            session.add(record)
            session.flush()
    except IntegrityError as err:
        if _is_unique_violation(err):
            raise DuplicateError(
                f"attempt {client_id} was stored by a concurrent upload; send it again"
            ) from err
        raise

    after = reading_state(session, settings, artifact, max_attempts=max_attempts)
    now = after.get(block.key)
    was_visible = {b.key for b in before.blocks if b.visible}
    unlocked = [b.key for b in after.blocks if b.visible and b.key not in was_visible]
    cleared = now.cleared and current.state == OPEN
    if cleared:
        StudyHistoryManager(session, settings).log(
            "gate_cleared",
            artifact=artifact,
            block=block,
            payload={"state": now.state, "attempt": row.attempt, "unlocked": unlocked},
            at=made_at,
        )
    regraded = block.type in SERVER_GRADERS
    return OfflineAttemptResultOut(
        **base,
        status="accepted",
        reason=None,
        detail=None,
        attempt=row.attempt,
        recorded=_response_out(row),
        regraded=regraded,
        verdict_mismatch=regraded and result.verdict != item.verdict,
        gate_cleared=cleared,
        state=now.state,
        unlocked=unlocked,
    )


# -- endpoints ------------------------------------------------------------------------------


@router.get("/artifacts/{slug}")
def get_bundle(
    slug: str,
    session: SessionDep,
    settings: SettingsDep,
    max_attempts: MaxAttemptsDep,
    user: CurrentUserDep,
) -> OfflineBundleOut:
    """The offline bundle of a ready study page (see the module docstring)."""
    artifact = _study_page(session, settings, slug)
    return build_bundle(
        session, settings, artifact, max_attempts=max_attempts, owner=user.id if user else None
    )


@router.post("/artifacts/{slug}/attempts")
def upload_attempts(
    slug: str,
    body: OfflineUploadIn,
    session: SessionDep,
    settings: SettingsDep,
    max_attempts: MaxAttemptsDep,
    user: CurrentUserDep,
) -> OfflineUploadOut:
    """Apply attempts made offline, in order; one result per item (see the module docstring)."""
    artifact = _study_page(session, settings, slug)
    blocks = ArtifactBlockManager(session, settings).list(artifact)
    version = page_version(artifact, blocks)
    matched = body.version == version
    results: list[OfflineAttemptResultOut] = []
    for item in body.attempts:
        try:
            results.append(
                _apply(
                    session,
                    settings,
                    artifact,
                    item,
                    version_matched=matched,
                    max_attempts=max_attempts,
                )
            )
        except _Rejected as rejected:
            results.append(
                OfflineAttemptResultOut(
                    client_id=str(item.client_id),
                    key=item.key,
                    status="rejected",
                    reason=rejected.reason,
                    detail=rejected.detail,
                    attempt=None,
                    recorded=None,
                    device_verdict=item.verdict,
                    regraded=False,
                    verdict_mismatch=False,
                    gate_cleared=False,
                    state=None,
                    unlocked=[],
                )
            )
    counts = {
        status: sum(1 for r in results if r.status == status)
        for status in ("accepted", "already_accepted", "rejected")
    }
    return OfflineUploadOut(
        artifact=artifact.slug,
        version=version,
        version_matched=matched,
        accepted=counts["accepted"],
        already_accepted=counts["already_accepted"],
        rejected=counts["rejected"],
        results=results,
        bundle=build_bundle(
            session, settings, artifact, max_attempts=max_attempts, owner=user.id if user else None
        ),
    )


__all__ = [
    "BUNDLE_FORMAT",
    "BUNDLE_FORMAT_VERSION",
    "MAX_CLOCK_SKEW",
    "MAX_UPLOAD_ITEMS",
    "REJECTION_REASONS",
    "OfflineAssetOut",
    "OfflineAttemptIn",
    "OfflineAttemptResultOut",
    "OfflineBlockOut",
    "OfflineBundleOut",
    "OfflineGradingOut",
    "OfflineUploadIn",
    "OfflineUploadOut",
    "build_bundle",
    "page_version",
    "router",
]
