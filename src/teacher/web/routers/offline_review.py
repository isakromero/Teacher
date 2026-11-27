"""Offline review: the review queue saved on a phone, and the answers made there uploaded later.

Both endpoints are under the account gate like every /api route (with the gate on, a request
without a valid session, cookie or `Authorization: Bearer <token>`, is a 401). The review queue
is shared by every account, as everywhere else. Every error body is `{"detail": "<message>"}`.
Timestamps are ISO 8601 in UTC with `+00:00`. Time is `app.state.clock` (the review queue's).
`routers/review.py` documents the online queue; `teacher.managers.review` its rules.


GET /api/offline/review?tz=<IANA zone>&days=3&cap=20
====================================================

The review questions due now and those becoming due in the next `days` days (0 to 14, default
3), each with everything needed to ask and grade it on the phone. `tz` is required (422 when
missing or unknown); `cap` (1 to 500, default 20) is the daily cap, as on the queue. Reading it
first creates the items of newly cleared questions, as the queue does, and gives every listed
item its presentation (the form it is asked in and its option order, with a token), exactly as
serving it in the queue does: the web review page and this bundle then ask the same thing, with
the same token, until one of them is answered. 200 body (`OfflineReviewBundleOut`):

    {
      "format": "teacher-offline-review-bundle", "format_version": 1,
      "bundled_at": "<ISO>",        # the server's now
      "tz": "Europe/Stockholm", "day_start": "<ISO>",   # the start of today in tz
      "cap": 20, "done_today": 2, "remaining_today": 18,  # as the queue has them now
      "days": 3, "until": "<ISO>",  # bundled_at + days
      "due": 5,                     # items due now (all listed, whatever the cap)
      "upcoming": 7,                # items becoming due by `until` (all listed)
      "default_min_words": 20,      # a blurb's min_words when its content has none
      "grading": {...},             # the same object as the study-page bundle's `grading`
                                    # (routers/offline.py): grader names and fixed feedback
      "schedule": {"first_interval_days": 1.0, "growth": 2.2, "max_interval_days": 180.0},
      "items": [ <item>, ... ]      # the due ones first, in the order the queue asks them;
                                    # then the upcoming ones, soonest first
    }

`<item>` has every field of an item of GET /api/review/queue (`id`, `presentation`, `form`,
`variant`, `variants_available`, `block` {key, type, is_gate, content (public, options in the
order shown), state "open", attempts 0, revealed null, segments [], concepts []}, `artifact`
{slug, title}, `question_key`, `due_at`, `overdue_days`, `confident_error`, `reviews`,
`interval_days`, `last_verdict`, `last_confidence`, `concepts`), plus:

    "due_now": true,              # due_at <= bundled_at
    "last_answered_at": "<ISO>",  # the latest answer to the question (on the page or a review)
    "private": {...}              # the answer key of the form shown, in the order shown:
                                  # multiple_choice {correct_index (a shown position),
                                  # explanation, option_explanations? (in shown order)};
                                  # blurb {model_answer, rubric: [{criterion, required?}]}

Asking offline. Ask an item only once it is due on the phone's clock (`due_at` <= now), and at
most once per bundle: answering clears its presentation on the server, so a second answer to
the same token is refused. Order: the `items` order for those due when bundled; when more come
due, confident errors first, then the most overdue, and not two in a row from the same page
when another page has one left (`teacher.review_schedule`). The cap: at most `remaining_today`
answers today (count the offline ones), `cap` on each later day (from that day's start in tz).

Grading on the phone, in the form and order shown (`response` is always in the order shown):
- multiple_choice: `{"choice": <shown position>}` (an int, 0 <= choice < number of options);
  `choice == private.correct_index`: "passed", feedback = private.explanation; else "failed",
  feedback = grading.multiple_choice_wrong_prefix + private.option_explanations[choice], or
  grading.no_option_feedback when there are none. missing [], details null.
- blurb: `{"text": <string>}`, not blank, at least min_words words (content.min_words, else
  default_min_words; words as Python's str.split() counts them); graded on the phone against
  private.model_answer and private.rubric exactly as a study-page blurb offline: "passed" when
  every required criterion (`required` absent means true) is met, "partial" (with `missing`,
  the unmet required criteria word for word) when some are, "failed" when none is.
- `confidence` ("low", "medium" or "high") is required, as online.
The schedule after an answer (for showing the next due time; the server's is authoritative):
passed: interval = min(interval_days * growth, max_interval_days); failed: interval =
first_interval_days; partial: unchanged; next due = the answer's time + interval days.


POST /api/offline/review/answers
================================

Upload review answers made offline, in the order they were made. Request (`OfflineReviewUploadIn`;
unknown keys are a 422):

    {
      "answers": [                        # 1 to 500, in the order they were made
        {
          "client_id": "<UUID>",          # the idempotency key, made by the phone
          "item_id": 17,                  # the item's `id`
          "presentation": "<the item's presentation token from the bundle>",
          "response": {"choice": 2} | {"text": "..."},  # in the order shown; checked per item
          "confidence": "low" | "medium" | "high",     # required
          "verdict": "passed" | "partial" | "failed",  # the phone's
          "feedback": "<text>" | null,    # optional, default null
          "missing": ["<criterion>", ...],# optional, default []
          "details": {...} | null,        # optional, default null
          "grader": "device:<name>",
          "made_at": "<ISO 8601 WITH an offset>"   # when it was answered on the phone
        }
      ]
    }

Errors for the whole request (nothing is recorded): 401 no session; 422 a body that does not
match the shape above (a missing or unknown field, a bad UUID, a `made_at` without an offset,
a verdict or confidence not in the list, no items or more than 500, the same client_id twice);
409 a concurrent upload stored the same answer first (retry: it then comes back
"already_accepted"). Any other failure is a 5xx and records nothing (one transaction).

Otherwise 200, one result per answer, in request order. Each answer is checked in this order;
the first rule it breaks rejects it with that `reason` (nothing is recorded for it):

1. `client_id` was accepted before: status "already_accepted" (nothing again); unless that was
   for another item or another response: `client_id_conflict`.
2. `made_at_in_future`: more than 5 minutes after the server's clock.
3. `unknown_item`: no review item `item_id` (its page was deleted).
4. `not_in_queue`: its page left the review queue (regenerated, or not ready).
5. `already_answered`: the item has no presentation now: it was answered since the bundle was
   made (on the web, the phone online, or an earlier upload), and that answer counts.
6. `presentation_changed`: the item is presented with another token now, or the variant it was
   shown in has been rebuilt since: the phone asked something the server no longer has.
7. `not_due`: `made_at` is more than 5 minutes before the item's `due_at`.
8. `invalid_grader`: `grader` does not start with "device:" or names nothing after it.
9. `invalid_grade`: the phone's grade breaks the rules every grade keeps (`missing` non-empty
   exactly for "partial", each a non-blank string; `details` an object of plain JSON or null);
   for a blurb also: each `missing` entry a required criterion of the form's rubric word for
   word, none twice, and a "partial" leaves at least one required criterion met.
10. `malformed_response`: the response is malformed for its question (see "Grading").

Answered both online and offline: the first answer to reach the server wins; the other is
rejected (`already_answered`, or `presentation_changed` when the question was shown again
since), records nothing and does not change the schedule. Show the reader that it was answered
elsewhere.

An accepted answer is stored like an online one (`ReviewManager.answer`), as made at
`made_at`: the attempt's time is `made_at` (it counts for that day's cap and the phone
summary's daily answers) and the item is rescheduled from `made_at` (its next due time is
`made_at` + the new interval). A multiple choice is graded again on the server (the
deterministic grader above) and the server's grade is stored; when the phone's verdict differs,
`verdict_mismatch` says so. A blurb keeps the phone's grade. The phone's grader and verdict are
kept in `offline_review_answers` (revision 0019) with the client_id.

200 body (`OfflineReviewUploadOut`):

    {
      "accepted": 2, "already_accepted": 0, "rejected": 1,
      "results": [
        {
          "client_id": "<canonical UUID>", "item_id": 17,
          "status": "accepted" | "already_accepted" | "rejected",
          "reason": null | "<one of the reasons above>",   # set exactly when rejected
          "detail": null | "<message for people>",        # set exactly when rejected
          "device_verdict": "failed",
          "regraded": true,            # graded again on the server (multiple choice)
          "verdict_mismatch": false,   # regraded and the server's verdict differs
          "recorded": {                # the stored answer; null when rejected
            "attempt_id": 88, "verdict": "passed", "passed": true, "feedback": "...",
            "missing": [], "confidence": "medium", "details": null, "grader": "...",
            "form": "original" | "variant", "variant": 1 | null,
            "interval_days": 2.2,      # the interval after this answer
            "due_at": "<ISO>",         # when the question was due next after this answer
            "made_at": "<ISO>"
          }
        }
      ]
    }

After an upload, GET the bundle again: the answered items are rescheduled.
"""

from __future__ import annotations

import random
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from teacher.blocks import private_content
from teacher.config import Settings
from teacher.db.models import (
    DEVICE_GRADER_PREFIX,
    ArtifactBlock,
    OfflineReviewAnswer,
    ReviewAttempt,
    ReviewItem,
)
from teacher.grading import (
    DEFAULT_MIN_WORDS,
    NO_OPTION_FEEDBACK,
    PREDICTION_FEEDBACK,
    Confidence,
    GradeResult,
    MultipleChoiceGrader,
    PredictionGrader,
    Verdict,
)
from teacher.judge import _answer_text
from teacher.managers import NotFoundError
from teacher.managers.artifacts import _is_unique_violation
from teacher.managers.errors import DuplicateError
from teacher.managers.review import (
    DEFAULT_DAILY_CAP,
    MAX_DAILY_CAP,
    ItemView,
    ReviewConflict,
    ReviewManager,
    shown_private,
)
from teacher.review_schedule import FIRST_INTERVAL, GROWTH, MAX_INTERVAL
from teacher.web.common import to_iso
from teacher.web.deps import SessionDep, SettingsDep
from teacher.web.routers.offline import (
    MAX_CLOCK_SKEW,
    MAX_UPLOAD_ITEMS,
    MULTIPLE_CHOICE_WRONG_PREFIX,
    OfflineGradingOut,
    _check_blurb_grade,
    _check_json,
    _Rejected,
)
from teacher.web.routers.reading import MaxAttemptsDep
from teacher.web.routers.review import ClockDep, ReviewItemOut, day_start, item_out

router = APIRouter(prefix="/api/offline", tags=["offline"])

REVIEW_BUNDLE_FORMAT = "teacher-offline-review-bundle"
REVIEW_BUNDLE_FORMAT_VERSION = 1
DEFAULT_DAYS = 3
MAX_DAYS = 14

ReviewRejectionReason = Literal[
    "client_id_conflict",
    "made_at_in_future",
    "unknown_item",
    "not_in_queue",
    "already_answered",
    "presentation_changed",
    "not_due",
    "invalid_grader",
    "invalid_grade",
    "malformed_response",
]


# -- models ---------------------------------------------------------------------------------


class ScheduleOut(BaseModel):
    first_interval_days: float
    growth: float
    max_interval_days: float


class OfflineReviewItemOut(ReviewItemOut):
    due_now: bool
    last_answered_at: str
    private: dict[str, Any] | None


class OfflineReviewBundleOut(BaseModel):
    format: str
    format_version: int
    bundled_at: str
    tz: str
    day_start: str
    cap: int
    done_today: int
    remaining_today: int
    days: int
    until: str
    due: int
    upcoming: int
    default_min_words: int
    grading: OfflineGradingOut
    schedule: ScheduleOut
    items: list[OfflineReviewItemOut]


class OfflineReviewAnswerIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_id: uuid.UUID
    item_id: int
    presentation: str
    response: Any
    confidence: Confidence
    verdict: Verdict
    feedback: str | None = None
    missing: list[str] = Field(default_factory=list)
    details: dict[str, Any] | None = None
    grader: str
    made_at: AwareDatetime


class OfflineReviewUploadIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answers: list[OfflineReviewAnswerIn] = Field(min_length=1, max_length=MAX_UPLOAD_ITEMS)

    @model_validator(mode="after")
    def _distinct_client_ids(self) -> OfflineReviewUploadIn:
        seen: set[uuid.UUID] = set()
        for item in self.answers:
            if item.client_id in seen:
                raise ValueError(f"client_id {item.client_id} is given twice in one upload")
            seen.add(item.client_id)
        return self


class RecordedReviewOut(BaseModel):
    attempt_id: int
    verdict: Verdict
    passed: bool
    feedback: str | None
    missing: list[str]
    confidence: Confidence
    details: dict[str, Any] | None
    grader: str
    form: Literal["original", "variant"]
    variant: int | None
    interval_days: float
    due_at: str
    made_at: str


class OfflineReviewResultOut(BaseModel):
    client_id: str
    item_id: int
    status: Literal["accepted", "already_accepted", "rejected"]
    reason: ReviewRejectionReason | None
    detail: str | None
    device_verdict: Verdict
    regraded: bool
    verdict_mismatch: bool
    recorded: RecordedReviewOut | None


class OfflineReviewUploadOut(BaseModel):
    accepted: int
    already_accepted: int
    rejected: int
    results: list[OfflineReviewResultOut]


# -- the bundle -----------------------------------------------------------------------------


def _bundle_item(
    session: Session, settings: Settings, view: ItemView, rng: random.Random, now: datetime
) -> OfflineReviewItemOut:
    shown = ReviewManager(session, settings).present(view, rng, now=now)
    base = item_out(session, settings, view, shown, now)
    return OfflineReviewItemOut(
        **base.model_dump(),
        due_now=view.item.due_at <= now,
        last_answered_at=to_iso(view.item.last_answered_at) or "",
        private=shown_private(private_content(shown.content), shown.order),
    )


@router.get("/review")
def get_review_bundle(
    session: SessionDep,
    settings: SettingsDep,
    max_attempts: MaxAttemptsDep,
    clock: ClockDep,
    tz: Annotated[str, Query(min_length=1)],
    days: Annotated[int, Query(ge=0, le=MAX_DAYS)] = DEFAULT_DAYS,
    cap: Annotated[int, Query(ge=1, le=MAX_DAILY_CAP)] = DEFAULT_DAILY_CAP,
) -> OfflineReviewBundleOut:
    """The review questions due now and in the next `days` days, to ask and grade offline."""
    now = clock()
    start = day_start(now, tz)
    manager = ReviewManager(session, settings)
    manager.sync(max_attempts=max_attempts)
    queue = manager.queue(now=now, day_start=start, cap=cap)
    until = now + timedelta(days=days)
    upcoming = [v for v in queue.waiting if v.item.due_at <= until]
    rng = random.SystemRandom()
    items = [_bundle_item(session, settings, v, rng, now) for v in (*queue.ordered, *upcoming)]
    return OfflineReviewBundleOut(
        format=REVIEW_BUNDLE_FORMAT,
        format_version=REVIEW_BUNDLE_FORMAT_VERSION,
        bundled_at=to_iso(now) or "",
        tz=tz,
        day_start=to_iso(start) or "",
        cap=cap,
        done_today=queue.done_today,
        remaining_today=queue.remaining_today,
        days=days,
        until=to_iso(until) or "",
        due=len(queue.ordered),
        upcoming=len(upcoming),
        default_min_words=DEFAULT_MIN_WORDS,
        grading=OfflineGradingOut(
            multiple_choice_grader=MultipleChoiceGrader.name,
            multiple_choice_wrong_prefix=MULTIPLE_CHOICE_WRONG_PREFIX,
            no_option_feedback=NO_OPTION_FEEDBACK,
            prediction_grader=PredictionGrader.name,
            prediction_feedback=PREDICTION_FEEDBACK,
            device_grader_prefix=DEVICE_GRADER_PREFIX,
        ),
        schedule=ScheduleOut(
            first_interval_days=FIRST_INTERVAL, growth=GROWTH, max_interval_days=MAX_INTERVAL
        ),
        items=items,
    )


# -- uploading answers ----------------------------------------------------------------------


class _DeviceBlurbGrade:
    """The phone's grade of a blurb, as a grader for `ReviewManager.answer`: checks it against
    the form's rubric and the answer's shape, then returns it."""

    def __init__(self, result: GradeResult) -> None:
        self.result = result

    def grade(self, content: dict[str, Any], response: dict[str, Any]) -> GradeResult:
        _check_blurb_grade(content, self.result)
        try:
            _answer_text(content, response)
        except ValueError as err:
            raise _Rejected("malformed_response", str(err)) from err  # type: ignore[arg-type]
        return self.result


def _device_grade(item: OfflineReviewAnswerIn) -> GradeResult:
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


def _recorded(attempt: ReviewAttempt) -> RecordedReviewOut:
    return RecordedReviewOut(
        attempt_id=attempt.id,
        verdict=attempt.verdict,  # type: ignore[arg-type]
        passed=attempt.verdict == "passed",
        feedback=attempt.feedback,
        missing=list(attempt.missing),
        confidence=attempt.confidence,  # type: ignore[arg-type]
        details=attempt.details,
        grader=attempt.grader,
        form=attempt.form,  # type: ignore[arg-type]
        variant=attempt.variant_position,
        interval_days=attempt.interval_after,
        due_at=to_iso(attempt.due_after) or "",
        made_at=to_iso(attempt.created_at) or "",
    )


def _as_graded(attempt: ReviewAttempt, response: Any) -> Any:
    """`response` (in the order shown) as the attempt stored it (in the form's own option
    order), to compare a re-sent answer with the stored one; unchanged when it cannot be
    mapped (it then differs from the stored response)."""
    order = attempt.option_order
    if order is None or not isinstance(response, dict):
        return response
    choice = response.get("choice")
    if isinstance(choice, bool) or not isinstance(choice, int) or not 0 <= choice < len(order):
        return response
    return {**response, "choice": order[choice]}


def _rejection(reason: ReviewRejectionReason, detail: str) -> _Rejected:
    return _Rejected(reason, detail)  # type: ignore[arg-type]


def _apply(
    session: Session, settings: Settings, item: OfflineReviewAnswerIn, now: datetime
) -> OfflineReviewResultOut:
    """Check one uploaded review answer and, if it may apply, store it."""
    client_id = str(item.client_id)
    base = {"client_id": client_id, "item_id": item.item_id, "device_verdict": item.verdict}
    manager = ReviewManager(session, settings)

    existing = session.scalar(
        select(OfflineReviewAnswer).where(OfflineReviewAnswer.client_id == client_id)
    )
    if existing is not None:
        attempt = session.get(ReviewAttempt, existing.attempt_id)
        assert attempt is not None  # the foreign key: the record goes with its attempt
        stored_item = session.get(ReviewItem, attempt.item_id)
        assert stored_item is not None  # likewise
        block = session.get(ArtifactBlock, stored_item.block_id)
        assert block is not None  # likewise
        if attempt.item_id != item.item_id or _as_graded(attempt, item.response) != attempt.response:
            raise _rejection(
                "client_id_conflict",
                f"client_id {client_id} was accepted before for "
                f"{'another item' if attempt.item_id != item.item_id else 'another response'}; "
                "a client_id names one answer",
            )
        regraded = block.type == "multiple_choice"
        return OfflineReviewResultOut(
            **base,
            status="already_accepted",
            reason=None,
            detail=None,
            regraded=regraded,
            verdict_mismatch=regraded and existing.device_verdict != attempt.verdict,
            recorded=_recorded(attempt),
        )

    made_at = item.made_at.astimezone(UTC).replace(tzinfo=None)
    if made_at > now + MAX_CLOCK_SKEW:
        raise _rejection(
            "made_at_in_future",
            f"made_at {item.made_at.isoformat()} is later than the server's clock (by more "
            f"than {int(MAX_CLOCK_SKEW.total_seconds() // 60)} minutes): check the phone's clock",
        )
    try:
        view = manager.get(item.item_id)
    except NotFoundError as err:
        raise _rejection("unknown_item", str(err)) from err
    except ReviewConflict as err:
        raise _rejection("not_in_queue", str(err)) from err
    review_item = view.item
    if review_item.presentation is None:
        raise _rejection(
            "already_answered",
            f"review item {item.item_id} was answered since this was saved (online or in "
            "another upload); that answer counts",
        )
    if review_item.presentation != item.presentation:
        raise _rejection(
            "presentation_changed",
            f"review item {item.item_id} is asked in another form now (it was shown again "
            "since this was saved); this answer cannot count",
        )
    try:
        manager.presented_content(view)
    except ReviewConflict as err:
        raise _rejection("presentation_changed", str(err)) from err
    if made_at < review_item.due_at - MAX_CLOCK_SKEW:
        raise _rejection(
            "not_due",
            f"review item {item.item_id} was answered at {item.made_at.isoformat()}, before it "
            f"was due ({to_iso(review_item.due_at)})",
        )
    if not item.grader.startswith(DEVICE_GRADER_PREFIX) or not item.grader[
        len(DEVICE_GRADER_PREFIX):
    ].strip():
        raise _rejection(
            "invalid_grader",
            f"grader must be {DEVICE_GRADER_PREFIX!r} followed by the on-device grader's name, "
            f"got {item.grader!r}",
        )
    device = _device_grade(item)
    question = view.block.type
    graders = {"multiple_choice": MultipleChoiceGrader(), "blurb": _DeviceBlurbGrade(device)}
    if question not in graders:
        raise ValueError(f"a {question} cannot be reviewed")  # REVIEW_QUESTION_TYPES has none
    try:
        outcome = manager.answer(
            item.item_id,
            presentation=item.presentation,
            response=item.response,
            confidence=item.confidence,
            graders=graders,
            now=made_at,
        )
    except ReviewConflict as err:
        raise _rejection("presentation_changed", str(err)) from err
    except ValueError as err:
        raise _rejection("malformed_response", str(err)) from err
    attempt = outcome.attempt
    record = OfflineReviewAnswer(
        client_id=client_id,
        attempt_id=attempt.id,
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
                f"review answer {client_id} was stored by a concurrent upload; send it again"
            ) from err
        raise
    regraded = question == "multiple_choice"
    return OfflineReviewResultOut(
        **base,
        status="accepted",
        reason=None,
        detail=None,
        regraded=regraded,
        verdict_mismatch=regraded and outcome.result.verdict != item.verdict,
        recorded=_recorded(attempt),
    )


@router.post("/review/answers")
def upload_review_answers(
    body: OfflineReviewUploadIn, session: SessionDep, settings: SettingsDep, clock: ClockDep
) -> OfflineReviewUploadOut:
    """Apply review answers made offline, in order; one result per answer."""
    now = clock()
    results: list[OfflineReviewResultOut] = []
    for item in body.answers:
        try:
            results.append(_apply(session, settings, item, now))
        except _Rejected as rejected:
            results.append(
                OfflineReviewResultOut(
                    client_id=str(item.client_id),
                    item_id=item.item_id,
                    status="rejected",
                    reason=rejected.reason,  # type: ignore[arg-type]
                    detail=rejected.detail,
                    device_verdict=item.verdict,
                    regraded=False,
                    verdict_mismatch=False,
                    recorded=None,
                )
            )
    counts = {
        status: sum(1 for r in results if r.status == status)
        for status in ("accepted", "already_accepted", "rejected")
    }
    return OfflineReviewUploadOut(**counts, results=results)


__all__ = [
    "DEFAULT_DAYS",
    "MAX_DAYS",
    "REVIEW_BUNDLE_FORMAT",
    "REVIEW_BUNDLE_FORMAT_VERSION",
    "OfflineReviewAnswerIn",
    "OfflineReviewBundleOut",
    "OfflineReviewItemOut",
    "OfflineReviewResultOut",
    "OfflineReviewUploadIn",
    "OfflineReviewUploadOut",
    "router",
]
