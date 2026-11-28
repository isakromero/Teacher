"""An exploration as JSON: what `GET /api/explorations/{id}` returns and `teacher explore
--json` prints (`exploration_detail`), and one line of the list (`exploration_summary`).

The detail is read at request time: a known concept's standing, the study pages tagged with it
and the library sources linked to it are the graph's as they are now (`teacher.managers.
concept_standing`, `source_concepts`), not a copy made when the run matched it. The cost is the
cost ledger's (`teacher.costs.exploration_cost`). Reads only.

The enrichment (`teacher.exploration.enrichment`): `enrichment` is the latest one
(`enrichment_view`: status, stage, progress, error, stage log, the confirmed plan's counts, its
summary once completed, and its cost), `enriching` whether one runs now (then nothing is
`editable`), `can_enrich` whether one can start. Each candidate carries its last ingest attempt
(`ingest_error`, `ingest`) and each concept what an enrichment wrote for it (`graph`: how it got
its graph concept; `grounding`: its library sources when last checked, each with whether it
grounds it; `links`: what became of each drafted prerequisite).
"""

from __future__ import annotations

import copy
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from teacher.config import Settings
from teacher.costs import enrichment_cost, exploration_cost
from teacher.db.models import (
    Concept,
    Exploration,
    ExplorationCandidate,
    ExplorationConcept,
    ExplorationEnrichment,
    ExplorationLink,
    KnowledgeSource,
)
from teacher.exploration.common import LEVEL_LABELS, LIMITS
from teacher.managers import ConceptStandingManager, ExplorationManager, SourceConceptManager

STAGE_LABELS = {
    "brief": "Writing the brief",
    "outline": "Outlining the concepts",
    "graph_match": "Matching the concepts against the graph",
    "discovery": "Finding sources",
}
ENRICHMENT_STAGE_LABELS = {
    "ingest": "Adding the sources to your library",
    "graph": "Writing the outline to your concept graph",
    "grounding": "Checking which concepts are grounded",
}


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return (value.replace(tzinfo=UTC) if value.tzinfo is None else value).isoformat()


def exploration_summary(row: Exploration, *, concepts: int, candidates: int) -> dict[str, Any]:
    return {
        "id": row.id,
        "prompt": row.prompt,
        "level": row.level,
        "level_label": LEVEL_LABELS[row.level],
        "status": row.status,
        "stage": row.stage,
        "stage_label": STAGE_LABELS.get(row.stage or "", None),
        "progress": {"done": row.progress_done, "total": row.progress_total},
        "error": row.error,
        "concepts": concepts,
        "candidates": candidates,
        "created_at": _iso(row.created_at),
        "finished_at": _iso(row.finished_at),
    }


def _candidate(candidate: ExplorationCandidate, link: ExplorationLink, keys: list[str],
               source: KnowledgeSource | None) -> dict[str, Any]:  # fmt: skip
    return {
        "id": candidate.id,
        "rank": link.rank,
        "attempt": link.attempt,
        "fits": link.fits,
        "triage": link.triage,
        "overall": link.overall,
        "url": candidate.url,
        "landing_url": candidate.landing_url,
        "title": candidate.title,
        "authors": list(candidate.authors),
        "year": candidate.year,
        "arxiv_id": candidate.arxiv_id,
        "doi": candidate.doi,
        "summary": candidate.summary,
        "found_by": list(candidate.found_by),
        "verdict": candidate.verdict,
        "verdict_reason": candidate.verdict_reason,
        "readable": candidate.readable,
        "probe": dict(candidate.probe),
        "library": list(candidate.library),
        "decision": candidate.decision,
        "decided_at": _iso(candidate.decided_at),
        "ingest_error": candidate.ingest_error,
        "ingest": dict(candidate.ingest_detail) if candidate.ingest_detail is not None else None,
        "knowledge_source": (
            {"slug": source.slug, "title": source.title} if source is not None else None
        ),
        "concepts": keys,
    }


def _known(session: Session, settings: Settings, concept: Concept) -> dict[str, Any]:
    standing = ConceptStandingManager(session, settings).standing(concept)
    links = SourceConceptManager(session, settings).for_concept(concept)
    return {
        "concept": {"id": concept.id, "slug": concept.slug, "name": concept.name},
        "standing": {
            "status": standing.status,
            "questions_answered": standing.questions_answered,
            "first_attempt_passed": standing.first_attempt_passed,
            "first_attempt_pass_rate": standing.first_attempt_pass_rate,
            "last_studied_at": _iso(standing.last_studied_at),
        },
        "pages": [
            {"slug": p.artifact.slug, "title": p.artifact.title, "kind": p.artifact.kind,
             "role": p.role, "status": p.artifact.status}
            for p in standing.pages
        ],  # fmt: skip
        "sources": [
            {"slug": link.source.slug, "title": link.source.title, "role": link.role}
            for link in links
        ],
    }


def enrichment_view(session: Session, row: ExplorationEnrichment) -> dict[str, Any]:
    """One enrichment as JSON (see the module docstring)."""
    plan = row.plan or {}
    return {
        "id": row.id,
        "status": row.status,
        "stage": row.stage,
        "stage_label": ENRICHMENT_STAGE_LABELS.get(row.stage or "", None),
        "progress": {"done": row.progress_done, "total": row.progress_total},
        "error": row.error,
        "stages": [dict(stage) for stage in row.stages],
        "planned": dict(plan.get("counts", {})),
        "summary": copy.deepcopy(row.summary),  # a copy: the caller may edit what it sends
        "created_at": _iso(row.created_at),
        "finished_at": _iso(row.finished_at),
        "cost": enrichment_cost(session, row.id).to_dict(),
    }


def exploration_detail(session: Session, settings: Settings, row: Exploration) -> dict[str, Any]:
    """The whole exploration (see the module docstring)."""
    manager = ExplorationManager(session, settings, owner=row.user_id)
    concepts = manager.concepts(row)
    candidates = {c.id: c for c in manager.candidates(row)}
    links = manager.links(row)
    by_key = {c.key: c for c in concepts}
    keys_of: dict[int, list[str]] = defaultdict(list)
    concept_of = {c.id: c for c in concepts}
    for link in links:
        keys_of[link.candidate_id].append(concept_of[link.outline_concept_id].key)
    sources = {
        s.id: s
        for s in (
            session.get(KnowledgeSource, c.knowledge_source_id)
            for c in candidates.values()
            if c.knowledge_source_id is not None
        )
        if s is not None
    }
    links_by_concept: dict[int, list[ExplorationLink]] = defaultdict(list)
    for link in links:
        links_by_concept[link.outline_concept_id].append(link)

    def concept_out(c: ExplorationConcept) -> dict[str, Any]:
        matched = session.get(Concept, c.concept_id) if c.concept_id is not None else None
        out: dict[str, Any] = {
            "id": c.id,
            "key": c.key,
            "position": c.position,
            "step": c.step,
            "name": c.name,
            "drafted_name": c.drafted_name,
            "renamed": c.name != c.drafted_name,
            "aliases": list(c.aliases),
            "summary": c.summary,
            "prerequisites": [
                {"key": key, "name": by_key[key].name, "dropped": by_key[key].status == "dropped"}
                for key in c.prerequisites
                if key in by_key
            ],
            "search_query": c.search_query,
            "status": c.status,
            "match": None,
            "coverage": c.coverage,
            "known": None,
            "discovery": {
                "status": c.discovery_status,
                "error": c.discovery_error,
                "log": list(c.discovery_log),
            },
            "candidates": [
                _candidate(
                    candidates[link.candidate_id], link, keys_of[link.candidate_id],
                    sources.get(candidates[link.candidate_id].knowledge_source_id or 0),
                )  # fmt: skip
                for link in links_by_concept[c.id]
            ],
            "graph": (
                {"outcome": c.graph_outcome, "reason": c.graph_reason}
                if c.graph_outcome is not None
                else None
            ),
            "grounding": [dict(g) for g in c.grounding],
            "links": [
                {**dict(link), "name": by_key[link["key"]].name if link["key"] in by_key else link["key"]}
                for link in c.prerequisite_links
            ],
        }
        if c.match_method is not None:
            out["match"] = {
                "method": c.match_method,
                "reason": c.match_reason,
                "shortlist": list((c.match_detail or {}).get("shortlist", [])),
                # A matched concept deleted since: the match is kept, its concept is gone.
                "concept_missing": c.match_method != "new" and matched is None,
            }
        if matched is not None:
            out["known"] = _known(session, settings, matched)
        return out

    steps = []
    for number, title in enumerate(row.steps, start=1):
        steps.append(
            {
                "number": number,
                "title": title,
                "concepts": [concept_out(c) for c in concepts if c.step == number],
            }
        )
    cost = exploration_cost(session, row.id)
    latest = manager.latest_enrichment(row)
    enriching = latest is not None and latest.status == "running"
    return {
        **exploration_summary(row, concepts=len(concepts), candidates=len(candidates)),
        "stages": list(row.stages),
        "notes": list(row.notes),
        "limits": LIMITS,
        "brief": row.brief,
        "questions": row.questions,
        "steps": steps,
        "approved": sum(1 for c in candidates.values() if c.decision == "approved"),
        "searches": [
            {
                "concept": concept_of[s.outline_concept_id].key,
                "attempt": s.attempt,
                "query": s.query,
                "provider": s.provider,
                "outcome": s.outcome,
                "hits": s.hits,
                "error": s.error,
                "warnings": list(s.warnings),
                "seconds": round(s.seconds, 2),
            }
            for s in manager.searches(row)
        ],
        "cost": cost.to_dict(),
        "editable": row.status == "completed" and not enriching,
        "enriching": enriching,
        "can_enrich": row.status == "completed" and not enriching,
        "enrichment": enrichment_view(session, latest) if latest is not None else None,
        "enrichments": len(manager.enrichments(row)),
    }


__all__ = [
    "ENRICHMENT_STAGE_LABELS",
    "STAGE_LABELS",
    "enrichment_view",
    "exploration_detail",
    "exploration_summary",
]
