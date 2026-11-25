"""What to study next: rule-based suggestions from the reader's standing on each concept
(`teacher.managers.concept_standing`) and the concept graph's builds_on relations (from = the
dependent, to = the prerequisite). No model call.

The rules, each giving zero or more suggestions of one kind:
- `fill_gap`: for every concept the reader struggled with (status `weak`), the concepts on its
  roadmap (`ConceptGraph.roadmap(weak concept, known=the concepts in good standing)`, the weak
  concept itself left out): its builds_on prerequisites that are not in good standing (weak or
  unstudied), and theirs in turn, stopping at a concept in good standing.
- `review`: every `weak` concept; and every concept in good standing (`developing` or
  `strong`) last studied at least REVIEW_AFTER ago.
- `ready_next`: every `unstudied` concept whose direct builds_on prerequisites are all in good
  standing (a concept with no prerequisite qualifies: it can be started at once).
A concept gets one suggestion, of the first kind in `KINDS` that applies; the reasons of the
other kinds that apply to it are kept in `also`.

Ranking, in this order (`rank` counts from 1):
1. by kind: fill_gap, then review, then ready_next;
2. fill_gap: supporting more struggled-with concepts first; then fewer of the other gaps among
   its own prerequisites first (so a prerequisite comes before what builds on it); then slug;
3. review: weak before stale; weak ones by first-attempt pass rate (lowest first), then more
   questions answered first; stale ones longest unstudied first; then slug;
4. ready_next: concepts with at least one prerequisite (all met) before those with none; then
   the ones more concepts build on directly; then by what the library offers (a study page,
   then a source without one, then nothing); then slug.

Each suggestion says why in plain words (`reason`), the numbers behind it (`evidence`: the
concept's standing, plus the struggled-with concepts it supports, its prerequisites, or the
days since it was studied), and what to do (`action`): the study pages with blocks tagged with
it (`study_page`), else the sources linked to it whose study pages do not cover it (`source`:
none yet, not tagged yet, or tagged without it), else `none` (nothing in the library covers
it). A `review` suggestion for a concept with questions in the review queue
(`teacher.managers.review`: cleared questions tagged `main` with it, due or not) sends the
reader there instead (`review_queue`, with the pages too): its `review` says how many items it
has, how many are due now, when the next is due, and the queue's `url` for that concept.

The pass rate in the reasons is the standing's first-attempt pass rate, which counts the
reviews and half of every partly right first try (`teacher.managers.concept_standing`).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from teacher.concept_graph import ConceptGraph
from teacher.config import Settings
from teacher.db.models import (
    Artifact,
    ArtifactSource,
    Concept,
    KnowledgeSource,
    SourceConcept,
    by_code_point,
    utcnow,
)
from teacher.labs import LAB_KIND
from teacher.managers.concept_standing import (
    GOOD_STANDING,
    STRONG,
    UNSTUDIED,
    WEAK,
    ConceptStanding,
    ConceptStandingManager,
)
from teacher.managers.review import ReviewManager

FILL_GAP = "fill_gap"
REVIEW = "review"
READY_NEXT = "ready_next"
KINDS: tuple[str, ...] = (FILL_GAP, REVIEW, READY_NEXT)
REVIEW_AFTER = timedelta(days=14)


def rules() -> dict[str, Any]:
    """The rules' constants and order, as JSON (shown with the suggestions)."""
    return {
        "kinds": list(KINDS),
        "review_after_days": REVIEW_AFTER.total_seconds() / 86400,
        "good_standing": sorted(GOOD_STANDING),
    }


def _pct(rate: float | None) -> str:
    return "-" if rate is None else f"{round(rate * 100)}%"


def _names(names: Sequence[str]) -> str:
    """"a", "a and b", "a, b and c"."""
    if len(names) <= 1:
        return "".join(names)
    return f"{', '.join(names[:-1])} and {names[-1]}"


def _unresolved_text(s: ConceptStanding) -> str:
    """A sentence on the questions answered but never passed ("" when there are none)."""
    n, answered = s.unresolved, s.questions_answered
    if not n:
        return ""
    if n == answered:
        return " You never passed it." if n == 1 else " You never passed any of them."
    return " One of them you never passed." if n == 1 else f" {n} of them you never passed."


def _evidence_of(s: ConceptStanding) -> dict[str, Any]:
    return {
        "status": s.status,
        "questions": s.questions,
        "questions_answered": s.questions_answered,
        "first_attempt_passed": s.first_attempt_passed,
        "first_attempt_pass_rate": s.first_attempt_pass_rate,
        "attempts": s.attempts,
        "eventually_passed": s.eventually_passed,
        "active_seconds": s.active_seconds,
        "last_studied_at": s.record()["last_studied_at"],
    }


def _ref(s: ConceptStanding) -> dict[str, Any]:
    return {"id": s.concept.id, "slug": s.concept.slug, "name": s.concept.name}


def _passed_text(s: ConceptStanding) -> str:
    partly = f", {s.first_attempt_partial} partly" if s.first_attempt_partial else ""
    reviews = (
        f", and {s.reviews_passed} of {s.reviews} review{'' if s.reviews == 1 else 's'}"
        + (f" ({s.reviews_partial} partly)" if s.reviews_partial else "")
        if s.reviews else ""
    )
    return (
        f"you passed {s.first_attempt_passed} of {s.questions_answered} question"
        f"{'' if s.questions_answered == 1 else 's'} on it at the first attempt{partly}{reviews} "
        f"({_pct(s.first_attempt_pass_rate)})"
    )


@dataclass
class _Candidate:
    standing: ConceptStanding
    kind: str
    reason: str
    evidence: dict[str, Any]
    key: tuple[Any, ...]
    also: list[dict[str, str]] = field(default_factory=list)


# -- what the library offers ------------------------------------------------------------------


@dataclass(frozen=True)
class _Library:
    """Per concept id: the (source, role) links; per source id: its ready study pages."""

    links: Mapping[int, Sequence[tuple[KnowledgeSource, str]]]
    pages_of_source: Mapping[int, Sequence[Artifact]]

    @classmethod
    def load(cls, session: Session) -> _Library:
        links: dict[int, list[tuple[KnowledgeSource, str]]] = {}
        for concept_id, role, source in session.execute(
            select(SourceConcept.concept_id, SourceConcept.role, KnowledgeSource)
            .join(KnowledgeSource, KnowledgeSource.id == SourceConcept.source_id)
            .order_by(by_code_point(KnowledgeSource.title), KnowledgeSource.slug)
        ):
            links.setdefault(concept_id, []).append((source, role))
        pages: dict[int, list[Artifact]] = {}
        for source_id, artifact in session.execute(
            select(ArtifactSource.source_id, Artifact)
            .join(Artifact, Artifact.id == ArtifactSource.artifact_id)
            .where(Artifact.kind != LAB_KIND, Artifact.status == "ready")
            .order_by(Artifact.id.desc())
        ):
            pages.setdefault(source_id, []).append(artifact)
        return cls(links, pages)


@dataclass(frozen=True)
class ReviewCount:
    """A concept's items in the review queue: how many, how many due now, the next due time."""

    items: int
    due: int
    next_due_at: datetime | None


def review_action(
    s: ConceptStanding, count: ReviewCount, pages: list[dict[str, Any]]
) -> dict[str, Any]:
    """The `review_queue` action of a concept with review items."""
    if count.due:
        text = (
            f"Review it in the review queue: {count.due} question"
            f"{' is' if count.due == 1 else 's are'} due now ({count.items} in all)."
        )
    else:
        when = f" on {count.next_due_at:%Y-%m-%d}" if count.next_due_at is not None else ""
        text = (
            f"It is in the review queue ({count.items} question"
            f"{'' if count.items == 1 else 's'}): nothing is due now, the next one is due{when}."
        )
    return {
        "kind": "review_queue",
        "text": text,
        "pages": pages,
        "sources": [],
        "review": {
            "items": count.items,
            "due": count.due,
            "next_due_at": (
                count.next_due_at.isoformat() + "+00:00" if count.next_due_at is not None else None
            ),
            "url": f"/review?concept={s.concept.slug}",
        },
    }


def action_for(
    s: ConceptStanding, library: _Library, review: ReviewCount | None = None
) -> dict[str, Any]:
    """What the reader can do about the concept (see the module docstring); `review` is the
    concept's review queue count, given for a `review` suggestion only."""
    covering = [p for p in s.pages if p.artifact.status == "ready"]
    pages = [
        {
            "slug": p.artifact.slug,
            "title": p.artifact.title,
            "role": p.role,
            "questions": p.main_questions,
            "blocks": p.main_blocks + p.incidental_blocks,
            "superseded": bool(p.artifact.superseded_by),
        }
        for p in sorted(covering, key=lambda p: (p.role != "main", bool(p.artifact.superseded_by)))
    ]
    if review is not None and review.items:
        return review_action(s, review, pages)
    covered_ids = {p.artifact.id for p in covering}
    sources = []
    for source, role in library.links.get(s.concept.id, ()):
        source_pages = library.pages_of_source.get(source.id, ())
        if any(page.id in covered_ids for page in source_pages):
            continue  # one of its pages already teaches the concept: listed above
        untagged = [p for p in source_pages if p.block_concepts_tagged_at is None]
        if not source_pages:
            state = "no_study_page"
        elif untagged:
            state = "page_not_tagged"
        else:
            state = "page_does_not_cover"
        sources.append(
            {
                "slug": source.slug,
                "title": source.title,
                "role": role,
                "state": state,
                "pages": [{"slug": p.slug, "title": p.title} for p in (untagged or source_pages)],
            }
        )
    if pages:
        first = pages[0]
        questions = f" ({first['questions']} question{'s' if first['questions'] != 1 else ''})"
        text = f"Study it on “{first['title']}”{questions if first['questions'] else ''}."
        kind = "study_page"
    elif sources:
        first = sources[0]
        kind = "source"
        if first["state"] == "no_study_page":
            text = f"“{first['title']}” covers it but has no study page yet: make one."
        elif first["state"] == "page_not_tagged":
            text = (
                f"“{first['title']}” covers it; its study page is not tagged with concepts yet "
                "(teacher concepts tag-blocks)."
            )
        else:
            text = f"“{first['title']}” covers it, but its study page does not: make a new one."
    else:
        kind = "none"
        text = "Nothing in the library covers it yet: add a source about it."
    return {"kind": kind, "text": text, "pages": pages, "sources": sources}


# -- the rules ----------------------------------------------------------------------------------


def direct_prerequisites(graph: ConceptGraph, concept: Concept) -> list[Concept]:
    """The concepts `concept` builds on directly, by slug."""
    below = {c.id for c in graph.prerequisites(concept)}
    return [c for c, _type in graph.neighbours(concept, "builds_on") if c.id in below]


def _fill_gaps(
    standings: Mapping[int, ConceptStanding], graph: ConceptGraph
) -> list[_Candidate]:
    good = [s.concept for s in standings.values() if s.status in GOOD_STANDING]
    supports: dict[int, list[ConceptStanding]] = {}
    direct: dict[int, set[int]] = {}
    for s in standings.values():
        if s.status != WEAK:
            continue
        prerequisites = {c.id for c in direct_prerequisites(graph, s.concept)}
        for gap in graph.roadmap(s.concept, good):
            if gap.id == s.concept.id:
                continue
            supports.setdefault(gap.id, []).append(s)
            if gap.id in prerequisites:
                direct.setdefault(gap.id, set()).add(s.concept.id)
    gaps = set(supports)
    out = []
    for gap_id, weak in supports.items():
        gap = standings[gap_id]
        below = sum(1 for p in graph.prerequisites(gap.concept) if p.id in gaps)
        weak = sorted(weak, key=lambda w: w.concept.slug)
        names = _names([w.concept.name for w in weak])
        how = "a prerequisite" if direct.get(gap_id) else "an indirect prerequisite"
        state = (
            "you have not answered a question on it yet" if gap.status == UNSTUDIED
            else _passed_text(gap)
        )
        reason = (
            f"{gap.concept.name} is {how} of {names}, which you struggled with, and {state}. "
            "Filling this gap first should make the rest easier."
        )
        out.append(
            _Candidate(
                gap,
                FILL_GAP,
                reason,
                {
                    **_evidence_of(gap),
                    "supports": [
                        {
                            **_ref(w),
                            "status": w.status,
                            "questions_answered": w.questions_answered,
                            "first_attempt_passed": w.first_attempt_passed,
                            "first_attempt_pass_rate": w.first_attempt_pass_rate,
                            "direct": w.concept.id in direct.get(gap_id, set()),
                        }
                        for w in weak
                    ],
                },
                (-len(weak), below, gap.concept.slug),
            )
        )
    return out


def _reviews(standings: Mapping[int, ConceptStanding], now: datetime) -> list[_Candidate]:
    out = []
    for s in standings.values():
        if s.status == WEAK:
            reason = f"You struggled with {s.concept.name}: {_passed_text(s)}."
            reason += _unresolved_text(s)
            out.append(
                _Candidate(
                    s, REVIEW, reason, {**_evidence_of(s), "why": "weak"},
                    (0, s.first_attempt_pass_rate or 0.0, -s.questions_answered, 0.0,
                     s.concept.slug),
                )
            )
        elif s.status in GOOD_STANDING and s.last_studied_at is not None:
            age = now - s.last_studied_at
            if age >= REVIEW_AFTER:
                days = int(age.total_seconds() // 86400)
                word = "strong" if s.status == STRONG else "developing"
                reason = (
                    f"Your standing on {s.concept.name} is {word} ({_passed_text(s)}), but you "
                    f"last studied it {days} days ago: a review keeps it fresh."
                )
                out.append(
                    _Candidate(
                        s, REVIEW, reason,
                        {**_evidence_of(s), "why": "stale", "days_since": days},
                        (1, 0.0, 0, -age.total_seconds(), s.concept.slug),
                    )
                )
    return out


def _ready_next(
    standings: Mapping[int, ConceptStanding], graph: ConceptGraph, library: _Library
) -> list[_Candidate]:
    out = []
    for s in standings.values():
        if s.status != UNSTUDIED:
            continue
        prerequisites = [standings[c.id] for c in direct_prerequisites(graph, s.concept)]
        if not all(p.status in GOOD_STANDING for p in prerequisites):
            continue
        unlocks = len(graph.unlocks(s.concept))
        if prerequisites:
            listed = _names([f"{p.concept.name} ({p.status})" for p in prerequisites])
            reason = (
                f"You have not studied {s.concept.name} yet, and what it builds on is in good "
                f"standing: {listed}."
            )
        else:
            reason = (
                f"You have not studied {s.concept.name} yet, and it builds on nothing else in "
                "the graph, so you can start it now."
            )
        if unlocks:
            reason += f" {unlocks} concept{'s build' if unlocks != 1 else ' builds'} on it."
        offer = action_for(s, library)["kind"]
        out.append(
            _Candidate(
                s,
                READY_NEXT,
                reason,
                {
                    **_evidence_of(s),
                    "prerequisites": [
                        {**_ref(p), "status": p.status,
                         "first_attempt_pass_rate": p.first_attempt_pass_rate}
                        for p in prerequisites
                    ],
                    "unlocks": unlocks,
                },
                (not prerequisites, -unlocks, ("study_page", "source", "none").index(offer),
                 s.concept.slug),
            )
        )
    return out


def suggestions(
    session: Session,
    settings: Settings,
    *,
    now: datetime | None = None,
    standings: Mapping[int, ConceptStanding] | None = None,
    review_counts: Mapping[int, ReviewCount] | None = None,
) -> list[dict[str, Any]]:
    """Every suggestion, ranked (see the module docstring), as JSON. `now` is naive UTC
    (default the current time); `standings` may be passed when already computed, and so may
    `review_counts` (by concept id; by default the review items the queue has derived so far,
    so a caller with `max_attempts` runs `ReviewManager.sync` first)."""
    now = utcnow() if now is None else now
    if standings is None:
        standings = ConceptStandingManager(session, settings).standings()
    if review_counts is None:
        review_counts = {
            concept_id: ReviewCount(*counts)
            for concept_id, counts in ReviewManager(session, settings)
            .concept_counts(now=now)
            .items()
        }
    graph = ConceptGraph.load(session)
    library = _Library.load(session)
    by_kind = {
        FILL_GAP: _fill_gaps(standings, graph),
        REVIEW: _reviews(standings, now),
        READY_NEXT: _ready_next(standings, graph, library),
    }
    chosen: dict[int, _Candidate] = {}
    for kind in KINDS:
        for candidate in by_kind[kind]:
            concept_id = candidate.standing.concept.id
            if concept_id in chosen:
                chosen[concept_id].also.append({"kind": kind, "reason": candidate.reason})
            else:
                chosen[concept_id] = candidate
    ranked = sorted(chosen.values(), key=lambda c: (KINDS.index(c.kind), c.key))
    return [
        {
            "rank": rank,
            "kind": c.kind,
            "concept": _ref(c.standing),
            "status": c.standing.status,
            "reason": c.reason[:1].upper() + c.reason[1:],
            "evidence": c.evidence,
            "also": c.also,
            "action": action_for(
                c.standing,
                library,
                review_counts.get(c.standing.concept.id) if c.kind == REVIEW else None,
            ),
        }
        for rank, c in enumerate(ranked, start=1)
    ]


__all__ = [
    "FILL_GAP",
    "KINDS",
    "READY_NEXT",
    "REVIEW",
    "REVIEW_AFTER",
    "ReviewCount",
    "action_for",
    "direct_prerequisites",
    "rules",
    "suggestions",
]
