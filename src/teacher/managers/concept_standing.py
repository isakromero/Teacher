"""ConceptStandingManager: how well the reader stands on each concept, computed on read (nothing
is stored) from the answers to, and the time spent on, study-page blocks tagged with it
(`block_concepts`, `teacher.block_tagging`).

Evidence. A *question* is a block of a question type (multiple_choice or blurb,
`teacher.blocks.is_question_type`); a prediction is a gate but not a question, so it never
counts. For each concept, the questions tagged with it as `main` are the ones that test it;
their answers decide the status:
- `questions_answered`: main-tagged questions with at least one attempt on the page;
- `first_attempt_passed`: those whose first attempt passed, and `first_attempt_partial` those
  whose first attempt was partly right (verdict `partial`: some required criteria met). Only the
  first attempt on the page counts: later attempts come after the grader's feedback (and the
  last one allowed reveals the answer), so they say more about the feedback than about what the
  reader knew. All attempts are still counted (`attempts`), and so are the questions passed at
  any attempt (`eventually_passed`) and the ones never passed (`unresolved`);
- `reviews`, `reviews_passed`, `reviews_partial`: the review queue's answers to those questions
  (`teacher.managers.review`; one attempt each, often in a fresh form). Each review is one more
  first-try observation, so the standing can rise and fall after the page is finished;
- `first_attempt_pass_rate` = (first-try passes + half the first-try partial answers) /
  first tries, over the first attempts on the page and every review (None without answers):
  a partial answer counts as half a pass.
Incidental tags never change the status (a question that only mentions a concept does not tell
whether the reader understands it); their answers are reported apart
(`incidental_questions_answered`, `incidental_first_attempt_passed`), and so is the time on
incidental blocks (`incidental_seconds`).

Time. `active_seconds` is the attention-span time (`attention_spans`, surface `study_page`)
on every block tagged `main` with the concept, any block type: a block's time is credited in
full to each of its main concepts (not split between them), so the concepts of a page do not
add up to the page's time. `last_studied_at` is the latest answer to, or attention span on, a
main-tagged block (reviews included); `first_studied_at` the earliest; `last_answered_at` the
latest answer to a main-tagged question, on the page or in a review. `seen` is true once anything (main or incidental, answer or time) touched
the concept.

Status (from the main-tagged questions only, thresholds below):
- `unstudied`: no main-tagged question answered yet (whatever time was spent reading);
- `weak`: first-attempt pass rate below WEAK_BELOW;
- `strong`: at least STRONG_MIN_QUESTIONS distinct questions answered (reviews of one question
  do not make it several) and a first-attempt pass rate of at least STRONG_FROM;
- `developing`: everything else (answered, not weak, not yet strong).
`developing` and `strong` are good standing (`GOOD_STANDING`).

Answers to the tutor's chat exercises are not tagged and do not count. Answers on pages that
were regenerated since (superseded) still count: they are what the reader did.

Reads only; like every manager it never commits.
"""

from __future__ import annotations

import builtins
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from teacher.blocks import BLOCK_TYPES, is_question_type
from teacher.config import Settings
from teacher.db.models import (
    Artifact,
    ArtifactBlock,
    AttentionSpan,
    BlockConcept,
    BlockResponse,
    Concept,
    ReviewAttempt,
    ReviewItem,
)
from teacher.managers.artifacts import _require_persistent

UNSTUDIED = "unstudied"
WEAK = "weak"
DEVELOPING = "developing"
STRONG = "strong"
STANDING_STATUSES: tuple[str, ...] = (UNSTUDIED, WEAK, DEVELOPING, STRONG)
GOOD_STANDING = frozenset({DEVELOPING, STRONG})

WEAK_BELOW = 0.5  # a first-attempt pass rate below this is weak
STRONG_FROM = 0.8  # ... at least this, with enough questions, is strong
STRONG_MIN_QUESTIONS = 2  # one question passed is not enough to call a concept strong
# The block types that are questions (multiple_choice, blurb): not a prediction.
QUESTION_TYPES: tuple[str, ...] = tuple(sorted(n for n, t in BLOCK_TYPES.items() if t.is_question))


def status_of(
    questions_answered: int, first_attempt_passed: float, first_tries: int | None = None
) -> str:
    """The status for that evidence (see the module docstring): `first_attempt_passed` is the
    pass credit (a partial answer is half) over `first_tries` first tries (default
    `questions_answered`; with reviews, more)."""
    tries = questions_answered if first_tries is None else first_tries
    if questions_answered < 0 or tries < questions_answered or not 0 <= first_attempt_passed <= tries:
        raise ValueError(
            f"impossible evidence: {first_attempt_passed} of {tries} first tries passed, "
            f"over {questions_answered} questions"
        )
    if questions_answered == 0:
        return UNSTUDIED
    rate = first_attempt_passed / tries
    if rate < WEAK_BELOW:
        return WEAK
    if questions_answered >= STRONG_MIN_QUESTIONS and rate >= STRONG_FROM:
        return STRONG
    return DEVELOPING


def thresholds() -> dict[str, Any]:
    """The status rule's constants, as JSON (shown with the standing)."""
    return {
        "weak_below": WEAK_BELOW,
        "strong_from": STRONG_FROM,
        "strong_min_questions": STRONG_MIN_QUESTIONS,
        "statuses": list(STANDING_STATUSES),
        "good_standing": sorted(GOOD_STANDING),
    }


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat() + "+00:00" if value.tzinfo is None else value.isoformat()


@dataclass(frozen=True, slots=True)
class BlockEvidence:
    """What the reader did on one tagged block: its answers (questions only) and time."""

    block_id: int
    artifact_id: int
    key: str
    type: str
    position: int
    is_question: bool
    attempts: int = 0
    first_passed: bool | None = None  # None: never answered
    first_verdict: str | None = None  # None: never answered
    eventually_passed: bool = False
    reviews: int = 0
    reviews_passed: int = 0
    reviews_partial: int = 0
    first_answered_at: datetime | None = None
    last_answered_at: datetime | None = None
    seconds: float = 0.0
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None

    @property
    def answered(self) -> bool:
        return self.attempts > 0

    @property
    def first_tries(self) -> int:
        """The first attempt on the page (if any) and every review."""
        return int(self.answered) + self.reviews

    @property
    def first_try_credit(self) -> float:
        """Passes among the first tries, a partial answer counting half."""
        first = {"passed": 1.0, "partial": 0.5}.get(self.first_verdict or "", 0.0)
        return first + self.reviews_passed + 0.5 * self.reviews_partial

    @property
    def first_at(self) -> datetime | None:
        times = [t for t in (self.first_answered_at, self.first_seen_at) if t is not None]
        return min(times) if times else None

    @property
    def last_at(self) -> datetime | None:
        times = [t for t in (self.last_answered_at, self.last_seen_at) if t is not None]
        return max(times) if times else None


@dataclass(frozen=True, slots=True)
class PageCoverage:
    """A study page with blocks tagged with the concept: how many as `main` (and how many of
    those are questions), and how many as `incidental`."""

    artifact: Artifact
    main_blocks: int
    main_questions: int
    incidental_blocks: int

    @property
    def role(self) -> str:
        return "main" if self.main_blocks else "incidental"


@dataclass(frozen=True)
class ConceptStanding:
    """One concept's standing (see the module docstring for every number)."""

    concept: Concept
    status: str
    questions: int = 0
    questions_answered: int = 0
    first_attempt_passed: int = 0
    first_attempt_partial: int = 0
    reviews: int = 0
    reviews_passed: int = 0
    reviews_partial: int = 0
    first_try_credit: float = 0.0
    attempts: int = 0
    eventually_passed: int = 0
    incidental_questions: int = 0
    incidental_questions_answered: int = 0
    incidental_first_attempt_passed: int = 0
    blocks: int = 0
    incidental_blocks: int = 0
    active_seconds: float = 0.0
    incidental_seconds: float = 0.0
    first_studied_at: datetime | None = None
    last_studied_at: datetime | None = None
    last_answered_at: datetime | None = None
    seen: bool = False
    pages: tuple[PageCoverage, ...] = field(default_factory=tuple)

    @property
    def first_tries(self) -> int:
        return self.questions_answered + self.reviews

    @property
    def first_attempt_pass_rate(self) -> float | None:
        if not self.first_tries:
            return None
        return self.first_try_credit / self.first_tries

    @property
    def unresolved(self) -> int:
        return self.questions_answered - self.eventually_passed

    @property
    def good(self) -> bool:
        return self.status in GOOD_STANDING

    def record(self) -> dict[str, Any]:
        """As JSON."""
        return {
            "concept": {"id": self.concept.id, "slug": self.concept.slug, "name": self.concept.name},
            "status": self.status,
            "seen": self.seen,
            "questions": self.questions,
            "questions_answered": self.questions_answered,
            "first_attempt_passed": self.first_attempt_passed,
            "first_attempt_partial": self.first_attempt_partial,
            "reviews": self.reviews,
            "reviews_passed": self.reviews_passed,
            "reviews_partial": self.reviews_partial,
            "first_tries": self.first_tries,
            "first_attempt_pass_rate": self.first_attempt_pass_rate,
            "attempts": self.attempts,
            "eventually_passed": self.eventually_passed,
            "unresolved": self.unresolved,
            "incidental_questions": self.incidental_questions,
            "incidental_questions_answered": self.incidental_questions_answered,
            "incidental_first_attempt_passed": self.incidental_first_attempt_passed,
            "blocks": self.blocks,
            "incidental_blocks": self.incidental_blocks,
            "active_seconds": self.active_seconds,
            "incidental_seconds": self.incidental_seconds,
            "first_studied_at": _iso(self.first_studied_at),
            "last_studied_at": _iso(self.last_studied_at),
            "last_answered_at": _iso(self.last_answered_at),
            "pages": [
                {
                    "slug": p.artifact.slug,
                    "title": p.artifact.title,
                    "kind": p.artifact.kind,
                    "role": p.role,
                    "main_blocks": p.main_blocks,
                    "main_questions": p.main_questions,
                    "incidental_blocks": p.incidental_blocks,
                }
                for p in self.pages
            ],
        }


def _standing(
    concept: Concept,
    tags: Iterable[tuple[str, BlockEvidence]],
    artifacts: Mapping[int, Artifact],
) -> ConceptStanding:
    main = [ev for role, ev in tags if role == "main"]
    incidental = [ev for role, ev in tags if role == "incidental"]
    main_questions = [ev for ev in main if ev.is_question]
    answered = [ev for ev in main_questions if ev.answered]
    passed_first = sum(1 for ev in answered if ev.first_passed)
    credit = sum(ev.first_try_credit for ev in answered)
    tries = sum(ev.first_tries for ev in answered)
    inc_answered = [ev for ev in incidental if ev.is_question and ev.answered]
    firsts = [ev.first_at for ev in main if ev.first_at is not None]
    lasts = [ev.last_at for ev in main if ev.last_at is not None]
    answered_at = [ev.last_answered_at for ev in answered if ev.last_answered_at is not None]
    per_page: dict[int, list[int]] = defaultdict(lambda: [0, 0, 0])
    for ev in main:
        per_page[ev.artifact_id][0] += 1
        per_page[ev.artifact_id][1] += int(ev.is_question)
    for ev in incidental:
        per_page[ev.artifact_id][2] += 1
    pages = sorted(
        (
            PageCoverage(artifacts[artifact_id], counts[0], counts[1], counts[2])
            for artifact_id, counts in per_page.items()
        ),
        key=lambda p: (p.main_blocks == 0, -p.main_questions, -p.artifact.id),
    )
    every = main + incidental
    return ConceptStanding(
        concept=concept,
        status=status_of(len(answered), credit, tries),
        questions=len(main_questions),
        questions_answered=len(answered),
        first_attempt_passed=passed_first,
        first_attempt_partial=sum(1 for ev in answered if ev.first_verdict == "partial"),
        reviews=sum(ev.reviews for ev in answered),
        reviews_passed=sum(ev.reviews_passed for ev in answered),
        reviews_partial=sum(ev.reviews_partial for ev in answered),
        first_try_credit=credit,
        attempts=sum(ev.attempts for ev in main_questions),
        eventually_passed=sum(1 for ev in answered if ev.eventually_passed),
        incidental_questions=sum(1 for ev in incidental if ev.is_question),
        incidental_questions_answered=len(inc_answered),
        incidental_first_attempt_passed=sum(1 for ev in inc_answered if ev.first_passed),
        blocks=len(main),
        incidental_blocks=len(incidental),
        active_seconds=float(sum(ev.seconds for ev in main)),
        incidental_seconds=float(sum(ev.seconds for ev in incidental)),
        first_studied_at=min(firsts) if firsts else None,
        last_studied_at=max(lasts) if lasts else None,
        last_answered_at=max(answered_at) if answered_at else None,
        seen=any(ev.answered or ev.last_seen_at is not None for ev in every),
        pages=tuple(pages),
    )


class ConceptStandingManager:
    """Compute the standing of every concept, or of one, and the questions that test one."""

    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings

    def _evidence(self, block_ids: set[int]) -> dict[int, BlockEvidence]:
        """Per tagged block id: its answers, reviews and attention time (four queries)."""
        if not block_ids:
            return {}
        ids = sorted(block_ids)
        blocks = {
            b.id: b
            for b in self.session.scalars(select(ArtifactBlock).where(ArtifactBlock.id.in_(ids)))
        }
        answers: dict[int, list[tuple[int, bool, datetime]]] = defaultdict(list)
        first_verdicts: dict[int, str] = {}
        for block_id, attempt, passed, verdict, created_at in self.session.execute(
            select(
                BlockResponse.block_id,
                BlockResponse.attempt,
                BlockResponse.passed,
                BlockResponse.verdict,
                BlockResponse.created_at,
            )
            .where(BlockResponse.block_id.in_(ids))
            .order_by(BlockResponse.block_id, BlockResponse.attempt)
        ):
            if block_id not in answers:
                # NULL only for a row written outside the managers (migration 0012 filled in
                # the older ones), which says no more than `passed` does.
                first_verdicts[block_id] = verdict or ("passed" if passed else "failed")
            answers[block_id].append((attempt, bool(passed), created_at))
        reviews: dict[int, list[tuple[str, datetime]]] = defaultdict(list)
        for block_id, verdict, created_at in self.session.execute(
            select(ReviewItem.block_id, ReviewAttempt.verdict, ReviewAttempt.created_at)
            .join(ReviewAttempt, ReviewAttempt.item_id == ReviewItem.id)
            .where(ReviewItem.block_id.in_(ids))
        ):
            reviews[block_id].append((verdict, created_at))
        spans = {
            block_id: (float(seconds or 0.0), first, last)
            for block_id, seconds, first, last in self.session.execute(
                select(
                    AttentionSpan.block_id,
                    func.sum(AttentionSpan.active_seconds),
                    func.min(AttentionSpan.started_at),
                    func.max(AttentionSpan.last_seen_at),
                )
                .where(AttentionSpan.block_id.in_(ids), AttentionSpan.surface == "study_page")
                .group_by(AttentionSpan.block_id)
            )
        }
        out: dict[int, BlockEvidence] = {}
        for block_id, block in blocks.items():
            rows = answers.get(block_id, [])
            reviewed = reviews.get(block_id, [])
            answer_times = [t for _a, _p, t in rows] + [t for _v, t in reviewed]
            seconds, first_seen, last_seen = spans.get(block_id, (0.0, None, None))
            out[block_id] = BlockEvidence(
                block_id=block_id,
                artifact_id=block.artifact_id,
                key=block.key,
                type=block.type,
                position=block.position,
                is_question=is_question_type(block.type),
                attempts=len(rows),
                first_passed=rows[0][1] if rows else None,
                first_verdict=first_verdicts.get(block_id),
                eventually_passed=any(passed for _a, passed, _t in rows),
                reviews=len(reviewed),
                reviews_passed=sum(1 for v, _t in reviewed if v == "passed"),
                reviews_partial=sum(1 for v, _t in reviewed if v == "partial"),
                first_answered_at=rows[0][2] if rows else None,
                last_answered_at=max(answer_times) if answer_times else None,
                seconds=seconds,
                first_seen_at=first_seen,
                last_seen_at=last_seen,
            )
        return out

    def _compute(self, concepts: builtins.list[Concept]) -> dict[int, ConceptStanding]:
        ids = [c.id for c in concepts]
        if not ids:
            return {}
        tags = list(
            self.session.execute(
                select(BlockConcept.concept_id, BlockConcept.role, BlockConcept.block_id).where(
                    BlockConcept.concept_id.in_(ids)
                )
            )
        )
        evidence = self._evidence({block_id for _c, _r, block_id in tags})
        artifacts = {
            a.id: a
            for a in self.session.scalars(
                select(Artifact).where(
                    Artifact.id.in_({ev.artifact_id for ev in evidence.values()})
                )
            )
        }
        by_concept: dict[int, list[tuple[str, BlockEvidence]]] = defaultdict(list)
        for concept_id, role, block_id in tags:
            by_concept[concept_id].append((role, evidence[block_id]))
        return {c.id: _standing(c, by_concept.get(c.id, []), artifacts) for c in concepts}

    def standings(self) -> dict[int, ConceptStanding]:
        """Every concept's standing, by concept id (in slug order)."""
        concepts = list(self.session.scalars(select(Concept).order_by(Concept.slug)))
        return self._compute(concepts)

    def standing(self, concept: Concept) -> ConceptStanding:
        _require_persistent(concept, "concept")
        return self._compute([concept])[concept.id]

    def tagged_questions(
        self, concept: Concept
    ) -> builtins.list[tuple[str, str, BlockEvidence, Artifact]]:
        """The questions tagged with `concept` as (role, rationale, evidence, page): main
        first, then newest page first, then in reading order."""
        _require_persistent(concept, "concept")
        rows = list(
            self.session.execute(
                select(BlockConcept.role, BlockConcept.rationale, ArtifactBlock.id)
                .join(ArtifactBlock, ArtifactBlock.id == BlockConcept.block_id)
                .where(
                    BlockConcept.concept_id == concept.id,
                    ArtifactBlock.type.in_(QUESTION_TYPES),
                )
            )
        )
        evidence = self._evidence({block_id for _r, _why, block_id in rows})
        artifacts = {
            a.id: a
            for a in self.session.scalars(
                select(Artifact).where(
                    Artifact.id.in_({ev.artifact_id for ev in evidence.values()})
                )
            )
        }
        out = [
            (role, rationale or "", evidence[block_id], artifacts[evidence[block_id].artifact_id])
            for role, rationale, block_id in rows
        ]
        return sorted(out, key=lambda r: (r[0] != "main", -r[3].id, r[2].position))


__all__ = [
    "DEVELOPING",
    "GOOD_STANDING",
    "STANDING_STATUSES",
    "STRONG",
    "STRONG_FROM",
    "STRONG_MIN_QUESTIONS",
    "UNSTUDIED",
    "WEAK",
    "WEAK_BELOW",
    "BlockEvidence",
    "ConceptStanding",
    "ConceptStandingManager",
    "PageCoverage",
    "status_of",
    "thresholds",
]
