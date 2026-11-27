"""ReviewManager: the review queue. Cleared study-page questions are asked again at growing
intervals, in fresh forms, so what was learned on a page is retrieved again after reading.

Items. Every question (multiple_choice, blurb; not a prediction) of a *reviewable* page becomes
a `ReviewItem` once the reader has cleared it there: passed it, or used up `max_attempts` so
the answer was revealed. A page is reviewable while it is a ready study page that no ready page
supersedes (a regeneration that is still running or failed does not take its questions out of
the queue). `sync(max_attempts=...)` creates the missing items from the page's answers, so
questions answered before the queue existed get their items the first time the queue is read;
it is idempotent and runs on every queue read. An item's first review is scheduled from the
page: `cleared_at` (the passing attempt, or the attempt that revealed the answer) plus
`teacher.review_schedule.first_interval` (3 days after a clean first pass, else 1). A page that
is superseded or no longer ready drops out of the queue; its items and attempts stay as history
(and come back if it is reviewable again). Deleting the page deletes them with its blocks.

Queue (`queue`). The due items (`due_at` <= now) in the order of `teacher.review_schedule`:
confident errors first, then the most overdue, interleaved across pages (the page of the latest
review counts as the previous one). A daily cap limits how many are served: `cap` minus the
reviews already done since `day_start` (the start of the reader's day); the reader may extend
it by asking with a higher cap. `concept` narrows the queue to items whose question is tagged
`main` with that concept.

Presenting (`present`). Each served item gets a presentation: the form it is asked in (the
original question or a variant from the review bank, `teacher.review_schedule.form_for`) and,
for a multiple choice, a fresh option order, chosen here on the server and stored with a token
until the item is answered, so a reload shows the same thing and the stored answer is
unambiguous. The reader's choice is an index into the order shown; it is mapped back to the
form's own option index before grading, and the revealed answer is mapped to the order shown.

Answering (`answer`). One attempt per review: it is graded by the same graders as the page,
stored as a `ReviewAttempt` (form, option order, response, verdict, missing criteria,
confidence, schedule before and after), and the item is rescheduled
(`teacher.review_schedule.reschedule`). The reader then sees the verdict and the explanation or
model answer. Confidence is required: the review page always asks.

Time. Every method that needs the time takes `now` (naive UTC): the API passes
`app.state.clock()`, so tests and an offline check can move time forward.

Not yet in the study history: review attempts join no study session and are not in the
history's timeline or totals. They do count for the reader's standing
(`teacher.managers.concept_standing`).

Like every manager it flushes and never commits.
"""

from __future__ import annotations

import builtins
import random
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import and_, exists, func, select
from sqlalchemy.orm import Session, aliased

from teacher.blocks import private_content, public_content
from teacher.config import Settings
from teacher.costs import cost_scope
from teacher.db.models import (
    REVIEW_QUESTION_TYPES,
    Artifact,
    ArtifactBlock,
    BlockConcept,
    BlockResponse,
    Concept,
    ReviewAttempt,
    ReviewItem,
    ReviewVariant,
)
from teacher.grading import CONFIDENCES, Grader, GradeResult
from teacher.labs import LAB_KIND
from teacher.managers.artifact_blocks import ArtifactBlockManager
from teacher.managers.block_responses import response_verdict
from teacher.managers.errors import NotFoundError
from teacher.review_schedule import (
    due_after,
    first_interval,
    form_for,
    interleave,
    is_confident_error,
    option_order,
    priority,
    reschedule,
)

DEFAULT_DAILY_CAP = 20
MAX_DAILY_CAP = 500


class ReviewConflict(Exception):
    """The answer does not fit the item's current state (HTTP 409): it was not served, it was
    served again since, its variant was rebuilt, or its page left the queue."""


def _reviewable_page(artifact: Any = Artifact) -> Any:
    """SQL: `artifact` is a ready study page that no ready page supersedes."""
    successor = aliased(Artifact)
    return and_(
        artifact.status == "ready",
        artifact.kind != LAB_KIND,
        ~exists().where(successor.supersedes_id == artifact.id, successor.status == "ready"),
    )


@dataclass(frozen=True)
class ItemView:
    """An item with what the queue needs to order and show it."""

    item: ReviewItem
    block: ArtifactBlock
    artifact: Artifact

    @property
    def confident_error(self) -> bool:
        return is_confident_error(self.item.last_verdict, self.item.last_confidence)


@dataclass(frozen=True)
class Queue:
    """A read of the queue at `now` (see the module docstring)."""

    now: datetime
    items_total: int  # items in scope (due or not)
    due: int  # due now, before the cap
    done_today: int  # reviews since day_start (whatever the scope)
    cap: int
    next_due_at: datetime | None  # the earliest due time of an item not due yet
    ordered: tuple[ItemView, ...]  # the due items in the order they are asked
    # The items in scope not due yet, soonest first (ties: by item id).
    waiting: tuple[ItemView, ...] = ()

    @property
    def remaining_today(self) -> int:
        return max(self.cap - self.done_today, 0)

    def served(self, limit: int) -> tuple[ItemView, ...]:
        return self.ordered[: min(limit, self.remaining_today)]


@dataclass(frozen=True)
class Presentation:
    """How an item is asked now: the form, its full content, the option order and the public
    content as shown (options reordered)."""

    token: str
    form: str  # "original" | "variant"
    variant_position: int | None
    content: dict[str, Any]
    order: list[int] | None
    shown: dict[str, Any]


@dataclass(frozen=True)
class AnswerOutcome:
    attempt: ReviewAttempt
    result: GradeResult
    revealed: dict[str, Any] | None  # the form's private part, in the order shown
    view: ItemView


def shown_content(public: Mapping[str, Any], order: Sequence[int] | None) -> dict[str, Any]:
    """Public content with the options in the order shown (`order[shown] = option index`)."""
    shown = dict(public)
    if order is not None:
        shown["options"] = [public["options"][i] for i in order]
    return shown


def shown_private(private: Mapping[str, Any] | None, order: Sequence[int] | None) -> dict[str, Any] | None:
    """A form's private part with `correct_index` and `option_explanations` in the order shown."""
    if private is None:
        return None
    out = dict(private)
    if order is not None:
        out["correct_index"] = list(order).index(private["correct_index"])
        if "option_explanations" in private:
            out["option_explanations"] = [private["option_explanations"][i] for i in order]
    return out


class ReviewManager:
    """Derive, order, present, answer and reschedule review items."""

    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings

    # -- items ------------------------------------------------------------------------------

    def sync(self, *, max_attempts: int) -> int:
        """Create the items of cleared questions on reviewable pages that have none; returns
        how many were created."""
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1:
            raise ValueError("max_attempts must be a positive integer")
        blocks = list(self.session.scalars(
            select(ArtifactBlock)
            .join(Artifact, Artifact.id == ArtifactBlock.artifact_id)
            .where(
                _reviewable_page(),
                ArtifactBlock.type.in_(REVIEW_QUESTION_TYPES),
                ~exists().where(ReviewItem.block_id == ArtifactBlock.id),
                exists().where(BlockResponse.block_id == ArtifactBlock.id),
            )
        ))
        if not blocks:
            return 0
        answers: dict[int, list[BlockResponse]] = {}
        for row in self.session.scalars(
            select(BlockResponse)
            .where(BlockResponse.block_id.in_([b.id for b in blocks]))
            .order_by(BlockResponse.block_id, BlockResponse.attempt)
        ):
            answers.setdefault(row.block_id, []).append(row)
        created = 0
        for block in blocks:
            rows = answers.get(block.id, [])
            cleared = next((r for r in rows if r.passed), None)
            if cleared is None and len(rows) >= max_attempts:
                cleared = rows[max_attempts - 1]  # the attempt that revealed the answer
            if cleared is None:
                continue
            first_clean = response_verdict(rows[0]) == "passed"
            interval = first_interval(first_clean)
            last = rows[-1]
            self.session.add(ReviewItem(
                block_id=block.id,
                cleared_at=cleared.created_at,
                first_clean=first_clean,
                interval_days=interval,
                due_at=due_after(cleared.created_at, interval),
                reviews=0,
                last_verdict=response_verdict(last),
                last_confidence=last.confidence,
                last_answered_at=last.created_at,
            ))
            created += 1
        self.session.flush()
        return created

    def _views(self, concept: Concept | None = None) -> builtins.list[ItemView]:
        stmt = (
            select(ReviewItem, ArtifactBlock, Artifact)
            .join(ArtifactBlock, ArtifactBlock.id == ReviewItem.block_id)
            .join(Artifact, Artifact.id == ArtifactBlock.artifact_id)
            .where(_reviewable_page())
        )
        if concept is not None:
            stmt = stmt.where(exists().where(
                BlockConcept.block_id == ArtifactBlock.id,
                BlockConcept.concept_id == concept.id,
                BlockConcept.role == "main",
            ))
        return [ItemView(i, b, a) for i, b, a in self.session.execute(stmt.order_by(ReviewItem.id))]

    def get(self, item_id: int) -> ItemView:
        """The item with `item_id` (NotFoundError), with its block and page; ReviewConflict if
        its page is not reviewable now."""
        row = self.session.execute(
            select(ReviewItem, ArtifactBlock, Artifact)
            .join(ArtifactBlock, ArtifactBlock.id == ReviewItem.block_id)
            .join(Artifact, Artifact.id == ArtifactBlock.artifact_id)
            .where(ReviewItem.id == item_id)
        ).one_or_none()
        if row is None:
            raise NotFoundError(f"no review item {item_id}")
        view = ItemView(*row)
        reviewable = self.session.scalar(
            select(exists().where(Artifact.id == view.artifact.id, _reviewable_page()))
        )
        if not reviewable:
            raise ReviewConflict(
                f"review item {item_id} is on {view.artifact.slug!r}, which is no longer in the "
                "review queue (it was regenerated, or is not ready)"
            )
        return view

    # -- the queue --------------------------------------------------------------------------

    def done_since(self, day_start: datetime) -> int:
        return self.session.scalar(
            select(func.count()).select_from(ReviewAttempt)
            .where(ReviewAttempt.created_at >= day_start)
        ) or 0

    def queue(
        self,
        *,
        now: datetime,
        day_start: datetime,
        cap: int = DEFAULT_DAILY_CAP,
        concept: Concept | None = None,
    ) -> Queue:
        """The queue at `now` (call `sync` first). `day_start` is when the reader's day began."""
        if isinstance(cap, bool) or not isinstance(cap, int) or not 1 <= cap <= MAX_DAILY_CAP:
            raise ValueError(f"cap must be an integer from 1 to {MAX_DAILY_CAP}")
        views = self._views(concept)
        due = [v for v in views if v.item.due_at <= now]
        waiting = sorted(
            (v for v in views if v.item.due_at > now), key=lambda v: (v.item.due_at, v.item.id)
        )
        due.sort(key=lambda v: (*priority(confident_error=v.confident_error, due_at=v.item.due_at), v.item.id))
        previous = self.session.scalar(
            select(ArtifactBlock.artifact_id)
            .join(ReviewItem, ReviewItem.block_id == ArtifactBlock.id)
            .join(ReviewAttempt, ReviewAttempt.item_id == ReviewItem.id)
            .order_by(ReviewAttempt.id.desc())
            .limit(1)
        )
        ordered = interleave(due, lambda v: v.artifact.id, previous=previous)
        return Queue(
            now=now,
            items_total=len(views),
            due=len(due),
            done_today=self.done_since(day_start),
            cap=cap,
            next_due_at=waiting[0].item.due_at if waiting else None,
            ordered=tuple(ordered),
            waiting=tuple(waiting),
        )

    def variants(self, block: ArtifactBlock) -> builtins.list[ReviewVariant]:
        return list(self.session.scalars(
            select(ReviewVariant)
            .where(ReviewVariant.block_id == block.id)
            .order_by(ReviewVariant.position)
        ))

    def _form(self, view: ItemView) -> tuple[str, ReviewVariant | None, dict[str, Any]]:
        """The item's presented form: ("original" | "variant", the variant, full content);
        ReviewConflict when its variant was removed by a rebuild since."""
        item = view.item
        if item.presented_form == "variant":
            variant = (
                self.session.get(ReviewVariant, item.presented_variant_id)
                if item.presented_variant_id is not None else None
            )
            if variant is None:
                raise ReviewConflict(
                    "the variant this question was shown in has been rebuilt since; reload the "
                    "review page to get it again"
                )
            return "variant", variant, dict(variant.content)
        content = ArtifactBlockManager(self.session, self.settings).read_content(view.block)
        return "original", None, content

    def presented_content(self, view: ItemView) -> tuple[str, ReviewVariant | None, dict[str, Any]]:
        """The full content of the form the item is presented in now: ("original" |
        "variant", the variant, content); ReviewConflict when its variant was rebuilt since.
        The item must have a presentation (`present`)."""
        if view.item.presentation is None:
            raise ValueError(f"review item {view.item.id} is not presented")
        return self._form(view)

    def present(self, view: ItemView, rng: random.Random, *, now: datetime) -> Presentation:
        """The item's current presentation, choosing (and storing) a new one if it has none or
        its variant is gone."""
        item = view.item
        if item.presentation is not None:
            try:
                form, variant, content = self._form(view)
            except ReviewConflict:
                pass  # its variant was rebuilt before the reader answered: choose again
            else:
                return self._presentation(item, form, variant, content)
        variants = self.variants(view.block)
        position = form_for(item.reviews, len(variants))
        variant = variants[position - 1] if position is not None else None
        content = (
            dict(variant.content) if variant is not None
            else ArtifactBlockManager(self.session, self.settings).read_content(view.block)
        )
        order = None
        if view.block.type == "multiple_choice":
            last = self.session.scalar(
                select(ReviewAttempt.option_order)
                .where(ReviewAttempt.item_id == item.id)
                .order_by(ReviewAttempt.id.desc())
                .limit(1)
            )
            order = option_order(len(content["options"]), rng, previous=last)
        item.presentation = secrets.token_urlsafe(12)
        item.presented_form = "variant" if variant is not None else "original"
        item.presented_variant_id = variant.id if variant is not None else None
        item.presented_order = order
        item.presented_at = now
        self.session.flush()
        return self._presentation(item, item.presented_form, variant, content)

    @staticmethod
    def _presentation(
        item: ReviewItem, form: str, variant: ReviewVariant | None, content: dict[str, Any]
    ) -> Presentation:
        order = list(item.presented_order) if item.presented_order is not None else None
        assert item.presentation is not None
        return Presentation(
            token=item.presentation,
            form=form,
            variant_position=variant.position if variant is not None else None,
            content=content,
            order=order,
            shown=shown_content(public_content(content), order),
        )

    # -- answering --------------------------------------------------------------------------

    def answer(
        self,
        item_id: int,
        *,
        presentation: str,
        response: dict[str, Any],
        confidence: str,
        graders: Mapping[str, Grader],
        now: datetime,
    ) -> AnswerOutcome:
        """Grade and record one review answer and reschedule the item. `response` is in the
        order shown (`{"choice": <shown position>}` or `{"text": ...}`).

        NotFoundError for an unknown item; ReviewConflict when the page left the queue, the
        item was not served with `presentation` (never served, answered already, or served
        again since) or its variant was rebuilt; ValueError for a bad confidence or a malformed
        response (nothing recorded); the grader may raise GradingUnavailableError."""
        if confidence not in CONFIDENCES:
            raise ValueError(f"confidence must be one of {list(CONFIDENCES)}, got {confidence!r}")
        view = self.get(item_id)
        item = view.item
        if item.presentation is None or item.presentation != presentation:
            raise ReviewConflict(
                "this question is not the one shown to you any more (it was answered or shown "
                "again elsewhere); reload the review page"
            )
        form, variant, content = self._form(view)
        order = list(item.presented_order) if item.presented_order is not None else None
        if not isinstance(response, dict):
            raise ValueError("response must be an object")
        graded = dict(response)
        if order is not None:
            choice = response.get("choice")
            if isinstance(choice, bool) or not isinstance(choice, int) or not 0 <= choice < len(order):
                raise ValueError(f"choice must be an option index from 0 to {len(order) - 1}")
            graded["choice"] = order[choice]
        grader = graders.get(view.block.type)
        if grader is None:
            raise ValueError(f"no grader for block type {view.block.type!r}")
        with cost_scope(self.settings, purpose="grading", artifact_id=view.block.artifact_id):
            result = grader.grade(content, graded)
        schedule = reschedule(item.interval_days, result.verdict, now)
        attempt = ReviewAttempt(
            item_id=item.id,
            form=form,
            variant_id=variant.id if variant is not None else None,
            variant_position=variant.position if variant is not None else None,
            asked=shown_content(public_content(content), order),
            option_order=order,
            response=graded,
            verdict=result.verdict,
            missing=list(result.missing),
            confidence=confidence,
            feedback=result.feedback,
            grader=result.grader,
            details=result.details,
            interval_before=item.interval_days,
            interval_after=schedule.interval_days,
            due_before=item.due_at,
            due_after=schedule.due_at,
            created_at=now,
        )
        self.session.add(attempt)
        item.interval_days = schedule.interval_days
        item.due_at = schedule.due_at
        item.reviews += 1
        item.last_verdict = result.verdict
        item.last_confidence = confidence
        item.last_answered_at = now
        item.presentation = None
        item.presented_form = None
        item.presented_variant_id = None
        item.presented_order = None
        item.presented_at = None
        self.session.flush()
        return AnswerOutcome(attempt, result, shown_private(private_content(content), order), view)

    # -- counts for other parts of the app ---------------------------------------------------

    def concept_counts(self, *, now: datetime) -> dict[int, tuple[int, int, datetime | None]]:
        """Per concept id: (items, due now, next due time of the rest) over the reviewable
        items whose question is tagged `main` with it. Counts the items `sync` created."""
        out: dict[int, list[Any]] = {}
        for concept_id, due_at in self.session.execute(
            select(BlockConcept.concept_id, ReviewItem.due_at)
            .join(ReviewItem, ReviewItem.block_id == BlockConcept.block_id)
            .join(ArtifactBlock, ArtifactBlock.id == ReviewItem.block_id)
            .join(Artifact, Artifact.id == ArtifactBlock.artifact_id)
            .where(BlockConcept.role == "main", _reviewable_page())
        ):
            entry = out.setdefault(concept_id, [0, 0, None])
            entry[0] += 1
            if due_at <= now:
                entry[1] += 1
            elif entry[2] is None or due_at < entry[2]:
                entry[2] = due_at
        return {k: (v[0], v[1], v[2]) for k, v in out.items()}

    def answer_times(self, since: datetime) -> builtins.list[datetime]:
        """When every review answer from `since` (naive UTC) on was made (`created_at`: for an
        answer uploaded from a phone, when it was made there), oldest first."""
        return list(self.session.scalars(
            select(ReviewAttempt.created_at)
            .where(ReviewAttempt.created_at >= since)
            .order_by(ReviewAttempt.created_at)
        ))

    def attempts(self, item: ReviewItem) -> builtins.list[ReviewAttempt]:
        return list(self.session.scalars(
            select(ReviewAttempt).where(ReviewAttempt.item_id == item.id).order_by(ReviewAttempt.id)
        ))

    def pages_with_items(self) -> builtins.list[tuple[Artifact, int]]:
        """The reviewable pages that have items, with how many, newest page first."""
        rows = self.session.execute(
            select(Artifact, func.count(ReviewItem.id))
            .join(ArtifactBlock, ArtifactBlock.artifact_id == Artifact.id)
            .join(ReviewItem, ReviewItem.block_id == ArtifactBlock.id)
            .where(_reviewable_page())
            .group_by(Artifact.id)
            .order_by(Artifact.id.desc())
        )
        return [(artifact, count) for artifact, count in rows]


__all__ = [
    "DEFAULT_DAILY_CAP",
    "MAX_DAILY_CAP",
    "AnswerOutcome",
    "ItemView",
    "Presentation",
    "Queue",
    "ReviewConflict",
    "ReviewManager",
    "shown_content",
    "shown_private",
]
