"""The roadmap of a completed exploration: its steps in order, each concept with the reader's
standing, its grounding, its primary source and that source's study page, and the progress made.
Computed when read; only the reader's two choices per concept are stored (revision 0022:
`ExplorationConcept.primary_source_id` and `marked_known_at`, set by `teacher.managers.
explorations.ExplorationManager.set_primary_source` / `set_marked_known`).

    view = roadmap_view(session, settings, exploration, max_attempts=2)

The steps and concepts are `teacher.exploration.grounding.exploration_roadmap`'s: study order,
dropped concepts left out (and listed in `dropped`), a step whose concepts were all dropped left
out. A roadmap is an ordered container of ordinary study pages; nothing here generates one.

Per concept:
- `standing`: the reader's standing on its graph concept (`teacher.managers.concept_standing`;
  null without a graph concept). Standings, pages and the review queue are the library's, which
  every account shares (accounts own only their explorations and highlights).
- `grounded`, `sources` (its grounding sources: the choices for its primary source, each marked
  `recommended` and `chosen`), `recommended_source` (`grounding.primary_source`) and
  `primary_source`: the chosen one when the reader chose one and it still grounds the concept,
  else the recommended one. A chosen source that no longer grounds it (its concepts were
  extracted again with another role) is not replaced quietly: `primary_source` is null and
  `choice_problem` says why, until the reader chooses again.
- `page`: the primary source's study page, if it has one: the newest study page (kind
  study_page, not a lab) linked to the source that is generating or ready and not superseded by
  another generating or ready page; with its gate progress and whether it is `complete` (ready,
  every gate cleared: `teacher.reading.ReadingState.complete`, as `progress.complete` of `GET
  /api/artifacts/{slug}`). `failed_page`: the newest study page of the source when it is newer
  than `page` and failed. One source may be the primary source of several concepts: they share
  its one page (`shared_with` names the others).
- `done` and `done_by` (see the rules below), `state`, and `can_generate`: it has a primary
  source and that source has no page (also when the concept is done: the reader may still want
  the page).

The rules (`RULES`, also in the JSON):
- A concept is done when the reader marked it "I know this" (`marked_known`), or their standing
  on its graph concept is `strong`, or its primary source's study page is complete. `done_by`
  lists which of these hold, in that order.
- A step is done when every concept of it that is not dropped is done. The current step is the
  first step that is not done (null when every step is done: the roadmap is `complete`); the
  steps before it are `done`, the ones after it `upcoming` (a later step whose concepts are all
  done is `done` too). Upcoming steps are not locked: every concept can be studied at any time.
- `state` of a concept that is not done: `page` (its page exists), `choose_source` (the chosen
  source no longer grounds it), `generate` (grounded, no page yet) or
  `ungrounded` (no library source introduces it; it may not even be in the graph yet).
- The one next action (`next_action`), the first that applies:
  1. `open_page` / `continue_page`: the first concept of the current step (in study order) that
     is not done and has a page: open it (generating, or no gate cleared yet) or continue it;
  2. `review`: a concept with a `weak` standing, not marked known, in the current step or an
     earlier one (any step once the roadmap is complete), with questions due now in the review
     queue (`teacher.managers.review`); the lowest first-attempt pass rate first, then study
     order: review it there (`/review?concept=<slug>`);
  3. `choose_source`: the first concept of the current step whose chosen source no longer
     grounds it;
  4. `generate_page`: the first grounded concept of the current step without a page: generate
     a study page from its primary source (one click, `POST .../page`; never automatic);
  5. `enrich`: the first concept of the current step that is not grounded: find and tick sources
     for it on the review page and enrich;
  6. `done`: every step is done and nothing is due.

The review counts are the review queue's items as `ReviewManager.sync` last made them; the API
syncs first, as the suggestions do. Reads only.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from teacher.config import Settings
from teacher.db.models import Artifact, Concept, Exploration, KnowledgeSource, utcnow
from teacher.exploration.common import LEVEL_LABELS
from teacher.exploration.grounding import GroundingSource, RoadmapConcept, exploration_roadmap
from teacher.labs import STUDY_PAGE_KIND
from teacher.managers import ArtifactManager, ConceptStandingManager, ExplorationManager
from teacher.managers.concept_standing import STRONG, WEAK, ConceptStanding
from teacher.managers.errors import StateConflictError
from teacher.managers.review import ReviewManager
from teacher.reading import reading_state

#: The reasons a concept is done, in the order `done_by` lists them.
DONE_REASONS: tuple[str, ...] = ("marked_known", "strong", "page_complete")
#: The next actions, in the order they are chosen.
NEXT_ACTIONS: tuple[str, ...] = (
    "open_page", "continue_page", "review", "choose_source", "generate_page", "enrich", "done",
)  # fmt: skip
#: The study page statuses that count as "the source has a page" (a failed one does not).
LIVE_PAGE_STATUSES: tuple[str, ...] = ("generating", "ready")

RULES: dict[str, Any] = {
    "concept_done": (
        "A concept is done when you marked it “I know this”, your standing on it is strong, or "
        "the study page of its primary source is complete (every gate cleared)."
    ),
    "step_done": "A step is done when every concept of it that is not dropped is done.",
    "current_step": (
        "The current step is the first step that is not done. Later steps are upcoming, not "
        "locked: you may study them at any time."
    ),
    "next_action": [
        "Open or continue the study page of the first concept of the current step that is not "
        "done and has one.",
        "Review a concept you are weak on (in the current step or an earlier one) whose "
        "questions are due in the review queue.",
        "Choose a source again for a concept whose chosen source no longer introduces it.",
        "Generate a study page from the primary source of the first concept of the current step "
        "that has none.",
        "Find and enrich sources for the first concept of the current step that no source "
        "introduces.",
    ],
    "done_reasons": list(DONE_REASONS),
    "next_actions": list(NEXT_ACTIONS),
}


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return (value.replace(tzinfo=UTC) if value.tzinfo is None else value).isoformat()


def _plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


@dataclass(frozen=True)
class SourcePages:
    """A source's study page (`page`, None when it has none) and its newest study page when
    that one is newer and failed (`failed`)."""

    page: Artifact | None
    failed: Artifact | None


def study_pages_of(session: Session, settings: Settings, source: KnowledgeSource) -> SourcePages:
    """The source's study page as the roadmap counts it (see the module docstring)."""
    pages = ArtifactManager(session, settings).list(kind=STUDY_PAGE_KIND, source=source)
    live = [p for p in pages if p.status in LIVE_PAGE_STATUSES]
    live_ids = {p.id for p in live}
    current = [p for p in live if not any(s.id in live_ids for s in p.superseded_by)]
    page = current[-1] if current else None  # `list` is in creation order: the newest last
    newest = pages[-1] if pages else None
    failed = (
        newest
        if newest is not None and newest.status == "failed"
        and (page is None or pages.index(newest) > pages.index(page))
        else None
    )
    return SourcePages(page, failed)


def page_view(
    session: Session, settings: Settings, page: Artifact, *, max_attempts: int
) -> dict[str, Any]:
    """A study page as the roadmap shows it: its gate progress and whether it is complete."""
    summary = reading_state(session, settings, page, max_attempts=max_attempts).summary()
    return {
        "slug": page.slug,
        "title": page.title,
        "status": page.status,
        "url": f"/artifacts/{page.slug}",
        "gates_total": summary["gates_total"],
        "gates_cleared": summary["gates_cleared"],
        "started": summary["gates_cleared"] > 0,
        "complete": page.status == "ready" and bool(summary["complete"]),
    }


def _standing_view(s: ConceptStanding) -> dict[str, Any]:
    return {
        "status": s.status,
        "questions_answered": s.questions_answered,
        "first_attempt_passed": s.first_attempt_passed,
        "first_attempt_pass_rate": s.first_attempt_pass_rate,
        "last_studied_at": _iso(s.last_studied_at),
    }


def _source_ref(s: GroundingSource) -> dict[str, Any]:
    return {"slug": s.slug, "title": s.title}


@dataclass
class _Item:
    """One concept while the roadmap is assembled."""

    rc: RoadmapConcept
    step: int
    view: dict[str, Any]
    primary: GroundingSource | None
    standing: ConceptStanding | None
    review: tuple[int, int, datetime | None] | None


def _next_action(
    exploration: Exploration,
    steps: Sequence[dict[str, Any]],
    items: Sequence[_Item],
    current: int | None,
) -> dict[str, Any]:
    """The one next action (see the module docstring for the order)."""

    def ref(item: _Item) -> dict[str, Any]:
        return {"id": item.rc.outline_concept_id, "key": item.rc.key, "name": item.rc.name}

    def action(kind: str, text: str, item: _Item | None = None, **extra: Any) -> dict[str, Any]:
        return {
            "kind": kind,
            "text": text,
            "step": item.step if item is not None else None,
            "concept": ref(item) if item is not None else None,
            "url": extra.pop("url", None),
            "page": extra.pop("page", None),
            "source": extra.pop("source", None),
            "review": extra.pop("review", None),
        }

    open_items = [i for i in items if i.step == current and not i.view["done"]]
    for item in open_items:
        page = item.view["page"]
        if page is None:
            continue
        name = item.rc.name
        if page["status"] == "generating":
            text = f"“{page['title']}” is being generated for {name}: open it to watch its progress."
            return action("open_page", text, item, url=page["url"], page=page)
        if page["started"]:
            text = (
                f"Continue “{page['title']}” to study {name} ({page['gates_cleared']} of "
                f"{_plural(page['gates_total'], 'gate')} cleared)."
            )
            return action("continue_page", text, item, url=page["url"], page=page)
        return action("open_page", f"Open “{page['title']}” to study {name}.", item,
                      url=page["url"], page=page)  # fmt: skip

    reach = current if current is not None else max((s["number"] for s in steps), default=0)
    weak = [
        i for i in items
        if i.step <= reach and i.standing is not None and i.standing.status == WEAK
        and not i.view["marked_known"] and i.review is not None and i.review[1] > 0
    ]  # fmt: skip
    if weak:
        item = min(weak, key=lambda i: (i.standing.first_attempt_pass_rate or 0.0, i.rc.position))  # type: ignore[union-attr]
        items_n, due, _next = item.review  # type: ignore[misc]
        rate = item.standing.first_attempt_pass_rate  # type: ignore[union-attr]
        pct = "-" if rate is None else f"{round(rate * 100)}%"
        slug = item.view["concept"]["slug"]
        text = (
            f"Review {item.rc.name} in the review queue: your standing on it is weak ({pct} right "
            f"at the first try) and {_plural(due, 'question')} {'is' if due == 1 else 'are'} due "
            "now."
        )
        url = f"/review?concept={slug}"
        return action("review", text, item, url=url,
                      review={"items": items_n, "due": due, "url": url})  # fmt: skip

    for kind, state in (("choose_source", "choose_source"), ("generate_page", "generate"),
                        ("enrich", "ungrounded")):  # fmt: skip
        for item in open_items:
            if item.view["state"] != state:
                continue
            name = item.rc.name
            if kind == "choose_source":
                return action("choose_source", f"Choose a source to study {name} from: "
                              f"{item.view['choice_problem']}", item)  # fmt: skip
            if kind == "generate_page":
                source = item.view["primary_source"]
                text = f"Generate a study page from “{source['title']}” to study {name}."
                if item.view["shared_with"]:
                    text += f" It is also the source for {', '.join(item.view['shared_with'])}."
                return action("generate_page", text, item,
                              source={"slug": source["slug"], "title": source["title"]})  # fmt: skip
            text = (
                f"No source in your library introduces {name} yet: find and tick sources for it "
                "on the review page, then enrich."
            )
            return action("enrich", text, item,
                          url=f"/explore/{exploration.id}#concept-{item.rc.key}")  # fmt: skip
    return action("done", "Every step is done.")


def roadmap_view(
    session: Session,
    settings: Settings,
    exploration: Exploration,
    *,
    max_attempts: int,
    now: datetime | None = None,
    review_counts: Mapping[int, tuple[int, int, datetime | None]] | None = None,
) -> dict[str, Any]:
    """The roadmap of a completed exploration as JSON (see the module docstring).
    StateConflictError for an exploration that is not completed. `now` (naive UTC) decides
    which review questions are due; `review_counts` (by concept id, as `ReviewManager.
    concept_counts`) may be passed when already computed."""
    if exploration.status != "completed":
        raise StateConflictError(
            f"exploration {exploration.id} is {exploration.status}: only a completed "
            "exploration has a roadmap"
        )
    now = utcnow() if now is None else now
    if review_counts is None:
        review_counts = ReviewManager(session, settings).concept_counts(now=now)
    manager = ExplorationManager(session, settings, owner=exploration.user_id)
    rows = {r.id: r for r in manager.concepts(exploration)}
    names = {r.key: r.name for r in rows.values()}
    standings = ConceptStandingManager(session, settings)
    pages_of: dict[int, SourcePages] = {}

    def pages_for(source_id: int) -> SourcePages:
        if source_id not in pages_of:
            source = session.get(KnowledgeSource, source_id)
            if source is None:
                raise StateConflictError(f"source {source_id} vanished while the roadmap was read")
            pages_of[source_id] = study_pages_of(session, settings, source)
        return pages_of[source_id]

    page_views: dict[int, dict[str, Any]] = {}
    items: list[_Item] = []
    plan = exploration_roadmap(session, settings, exploration)
    for step in plan:
        for rc in step.concepts:
            row = rows[rc.outline_concept_id]
            grounding = [s for s in rc.sources if s.grounds]
            chosen_id = row.primary_source_id
            chosen = next((s for s in grounding if s.source_id == chosen_id), None)
            problem = None
            if chosen_id is not None and chosen is None:
                gone = session.get(KnowledgeSource, chosen_id)
                title = gone.title if gone is not None else f"source {chosen_id}"
                problem = (
                    f"the source you chose, “{title}”, no longer introduces it. Choose another "
                    "of its sources, or the recommended one."
                    if grounding
                    else f"the source you chose, “{title}”, no longer introduces it, and no "
                    "other source does."
                )
                primary = None
            else:
                primary = chosen or rc.primary_source
            concept = session.get(Concept, rc.concept_id) if rc.concept_id is not None else None
            standing = standings.standing(concept) if concept is not None else None
            page = failed = None
            if primary is not None:
                found = pages_for(primary.source_id)
                if found.page is not None:
                    if found.page.id not in page_views:
                        page_views[found.page.id] = page_view(
                            session, settings, found.page, max_attempts=max_attempts
                        )
                    page = page_views[found.page.id]
                if found.failed is not None:
                    failed = {"slug": found.failed.slug, "title": found.failed.title,
                              "url": f"/artifacts/{found.failed.slug}"}  # fmt: skip
            marked = row.marked_known_at is not None
            done_by = [
                reason
                for reason, holds in (
                    ("marked_known", marked),
                    ("strong", standing is not None and standing.status == STRONG),
                    ("page_complete", page is not None and page["complete"]),
                )
                if holds
            ]
            if done_by:
                state = "done"
            elif page is not None:
                state = "page"
            elif problem is not None:
                state = "choose_source"
            elif primary is not None:
                state = "generate"
            else:
                state = "ungrounded"
            counts = review_counts.get(concept.id) if concept is not None else None
            view = {
                "id": rc.outline_concept_id,
                "key": rc.key,
                "name": rc.name,
                "summary": rc.summary,
                "position": rc.position,
                "step": step.number,
                "prerequisites": [{"key": k, "name": names[k]} for k in rc.prerequisites],
                "concept": (
                    {"id": concept.id, "slug": concept.slug, "name": concept.name}
                    if concept is not None
                    else None
                ),
                "standing": _standing_view(standing) if standing is not None else None,
                "grounded": rc.grounded,
                "sources": [
                    {
                        **_source_ref(s),
                        "recommended": rc.primary_source is not None
                        and s.source_id == rc.primary_source.source_id,
                        "chosen": s.source_id == chosen_id,
                    }
                    for s in grounding
                ],
                "recommended_source": (
                    _source_ref(rc.primary_source) if rc.primary_source is not None else None
                ),
                "primary_source": (
                    {**_source_ref(primary), "chosen": chosen is not None}
                    if primary is not None
                    else None
                ),
                "choice_problem": problem,
                "page": page,
                "failed_page": failed,
                "shared_with": [],
                "marked_known": marked,
                "marked_known_at": _iso(row.marked_known_at),
                "done": bool(done_by),
                "done_by": done_by,
                "state": state,
                "can_generate": primary is not None and page is None,
                "review": (
                    {
                        "items": counts[0],
                        "due": counts[1],
                        "next_due_at": _iso(counts[2]),
                        "url": f"/review?concept={concept.slug}",  # type: ignore[union-attr]
                    }
                    if counts is not None
                    else None
                ),
            }
            items.append(_Item(rc, step.number, view, primary, standing, counts))

    by_source: dict[int, list[_Item]] = defaultdict(list)
    for item in items:
        if item.primary is not None:
            by_source[item.primary.source_id].append(item)
    for sharing in by_source.values():
        for item in sharing:
            item.view["shared_with"] = [o.rc.name for o in sharing if o is not item]

    steps = []
    current: int | None = None
    for step in plan:
        concepts = [i.view for i in items if i.step == step.number]
        done = sum(1 for c in concepts if c["done"])
        finished = done == len(concepts)
        if not finished and current is None:
            current = step.number
        steps.append(
            {
                "number": step.number,
                "title": step.title,
                "state": "done" if finished else ("current" if current == step.number else "upcoming"),
                "done": done,
                "total": len(concepts),
                "concepts": concepts,
            }
        )
    concepts_done = sum(1 for i in items if i.view["done"])
    steps_done = sum(1 for s in steps if s["state"] == "done")
    return {
        "exploration": {
            "id": exploration.id,
            "prompt": exploration.prompt,
            "level": exploration.level,
            "level_label": LEVEL_LABELS[exploration.level],
            "status": exploration.status,
            "enriching": manager.running_enrichment(exploration) is not None,
        },
        "url": f"/explore/{exploration.id}/roadmap",
        "review_url": f"/explore/{exploration.id}",
        "rules": RULES,
        "steps": steps,
        "dropped": [
            {"key": r.key, "name": r.name} for r in rows.values() if r.status == "dropped"
        ],
        "progress": {
            "concepts_done": concepts_done,
            "concepts_total": len(items),
            "steps_done": steps_done,
            "steps_total": len(steps),
            "complete": current is None,
        },
        "current_step": current,
        "next_action": _next_action(exploration, steps, items, current),
    }


def roadmap_summary(view: Mapping[str, Any]) -> dict[str, Any]:
    """What the explorations list and the home page show of a roadmap: its progress, its
    current step and its next action."""
    current = view["current_step"]
    title = next((s["title"] for s in view["steps"] if s["number"] == current), None)
    return {
        "url": view["url"],
        "progress": dict(view["progress"]),
        "current_step": current,
        "current_step_title": title,
        "next_action": dict(view["next_action"]),
    }


__all__ = [
    "DONE_REASONS",
    "LIVE_PAGE_STATUSES",
    "NEXT_ACTIONS",
    "RULES",
    "SourcePages",
    "page_view",
    "roadmap_summary",
    "roadmap_view",
    "study_pages_of",
]
