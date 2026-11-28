"""Enriching a completed exploration: its approved sources ingested into the library (with concept
extraction only), its outline written to the concept graph, and its concepts' grounding checked.
Nothing here runs except when the reader asks for it and confirms what it will do.

    preview = enrichment_preview(session, settings, exploration)       # reads only
    enrichment_id = start_enrichment(settings, enricher, exploration_id, owner=None,
                                     digest=preview["digest"])         # the confirmation
    run_enrichment(settings, enrichment_id, enricher)                  # never raises

The preview (`enrichment_preview`) says what an enrichment would do now: the approved candidates
linked to a concept that is not dropped (`ExplorationManager.approved_candidates`), each to be
added or, when the library already holds it, linked; the outline concepts that are not dropped,
each used as matched or to be looked up again (and created if still absent); and every drafted
prerequisite link between two of them, with what is expected of it (added, already present,
skipped and why, as far as the graph tells before anything is written). Its `digest` covers the
exploration's decisions and edits; `start_enrichment` refuses a digest that no longer matches
(StateConflictError: the plan changed since it was shown), so what runs is what was confirmed.

Starting (`start_enrichment`) checks that the run can work at all (Claude credentials and the
embedder's key: EnrichmentUnavailableError, HTTP 503) and creates the enrichment (`running`),
committed, refused (StateConflictError) unless the exploration is completed and no enrichment of
it runs (the database refuses a second running one too). While it runs, the reader's edits of
the exploration are refused. `run_enrichment` runs it as a background job (the web app) or in
the foreground (`teacher explore --enrich`); like `run_exploration` it uses short sessions,
committed at each step, never holds one while a model or a fetch runs, and never raises: a
failure of the whole job marks the enrichment `failed` with the error (an ExplorationError's
message as it is; anything else, a bug, as "<class>: <message>", logged with its traceback).

1. `ingest` (progress: candidates done). For each approved candidate, in the order found:
   - held already? A source it was stored as by an earlier attempt (`knowledge_source_id`), or
     one the library catalog matches by arXiv id, DOI or address (`teacher.discovery.
     LibraryCatalog`, as discovery marks candidates); after fetching, one with the same content
     or carrying its arXiv id or DOI. A held candidate is linked, never added twice.
   - otherwise it is added exactly as the Add page adds an address (`teacher.source_ingest`):
     fetched by the app's URL fetcher (`teacher.url_sources`, with its checks: no private
     address, size limits, readable text), its details detected (a PDF's by the metadata
     extractor, a page's from its own tags), stored as the source's file with the same
     metadata, plus `metadata.exploration` (the exploration, candidate and enrichment). A
     detail the detection did not find is the search result's (title, authors, year, summary,
     arXiv id, DOI), which the reader saw when ticking it; `ingest_detail.details` says which
     came from where. The slug is the title's; a slug taken by another source gets the first
     free `-2`, `-3`, ... suffix (said in `slug_note`), so a title shared by two works does not
     block the second. The source is logged in the study history (`via: exploration`) and the
     stored references are re-matched, as for any added source; its own references are not
     extracted (that runs on demand from its page).
   - then concept extraction only, when it has not run for the source (it has no
     `source_concepts` links yet): `ConceptExtractor.run`, as `teacher concepts extract` does.
     No study page is generated.
   - `mark_ingested` (with `ingest_detail`). A candidate that fails (fetch, details, storing,
     extraction) gets the error (`ingest_error`) and stays `approved`, so the next enrichment
     tries it again; the others go on. If it was stored before extraction failed, it keeps the
     source (`knowledge_source_id`) and the next attempt only links it and extracts.
2. `graph`. Each outline concept that is not dropped gets its graph concept: the one it
   matched (`matched`; the graph match's, or an earlier enrichment's); else it is re-resolved
   now (an approved source's extraction may just have created it): exactly by its name or its
   drafted name when every name and alias hit is that one concept (`found`), then with the
   graph match's resolver (`GraphMatcher.match`: shortlist and one judge call; the embedding
   index is brought up to date first, as extraction does); only if still absent it is created
   (`created`): origin `model`, unreviewed (`reviewed_at` NULL), the outline's name, aliases
   (the drafted name too when renamed) and summary, the first free slug, and its embedding.
   Then every drafted prerequisite between two concepts that are not dropped is written as
   `builds_on` (from = the dependent, to = the prerequisite) with
   `ConceptRelationManager.add` (origin `model`, unreviewed), so its rules hold: a pair already
   related the same way is `present`; a pair related otherwise (one relation per pair) or a
   link that would close a cycle is `skipped` with the reason (codes `related`, `cycle`; also
   `same_concept` when both ends resolved to one concept, `dropped` for a link to a dropped
   concept). Nothing existing is changed or deleted: no concept, alias or relation.
3. `grounding`. Each concept is checked against the rule in `teacher.exploration.grounding`
   (a library source introduces its graph concept): grounded ones are marked (`mark_grounded`),
   one no longer grounded goes back (`mark_ungrounded`), and every concept stores its sources.

The enrichment ends `completed` with its `summary` (sources added, held, failed; concepts
matched, found, created; relations added, present, skipped with reasons; concepts grounded and
still ungrounded). Running it again is idempotent: ingested candidates are not touched again,
a concept that has a graph concept keeps it, a relation that exists is `present`, and no source,
concept, alias or relation is made twice.

Costs: every API call (the metadata extractor's, concept extraction's, the resolver's judge,
embeddings) is recorded in the cost ledger as `exploration`, with the exploration and the
enrichment (`teacher.costs.cost_scope`; extraction's calls also carry their source).

Interruption: an enrichment cannot survive a restart; at startup the app marks every one still
`running` as failed (`fail_interrupted_enrichments`, `INTERRUPTED_ENRICHMENT_ERROR` naming the
stage); it can then be started again. A job that finds its enrichment no longer running stops
instead of writing on.
"""

from __future__ import annotations

import hashlib
import json
import logging
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from teacher.concepts import BUILDS_ON, find_cycle, normalize_name
from teacher.config import Settings
from teacher.costs import cost_scope
from teacher.db.engine import session_scope
from teacher.db.models import (
    Concept,
    Exploration,
    ExplorationCandidate,
    ExplorationConcept,
    ExplorationEnrichment,
    KnowledgeSource,
    SourceConcept,
)
from teacher.discovery.registry import LibraryCatalog, MergedCandidate
from teacher.embeddings import EmbeddingError, check_vectors, concept_text
from teacher.exploration.common import (
    ERROR_PREFIX,
    ExplorationError,
    ExplorationUnavailableError,
)
from teacher.exploration.grounding import grounding_of, is_grounded
from teacher.exploration.planner import DraftConcept
from teacher.exploration.view import ENRICHMENT_STAGE_LABELS as STAGE_LABELS
from teacher.files import sha256_file, slugify
from teacher.managers import (
    ConceptEmbeddingManager,
    ConceptManager,
    ConceptRelationManager,
    CycleError,
    ExplorationManager,
    KnowledgeSourceManager,
    ManagerError,
    RelationConflictError,
    SourceReferenceManager,
    StateConflictError,
    StudyHistoryManager,
)
from teacher.managers.concept_embeddings import text_of
from teacher.managers.concept_relations import describe, slugs_by_id
from teacher.source_ingest import detect_details, store_fetched, write_fetched

logger = logging.getLogger(__name__)

PURPOSE = "exploration"
ENRICHMENT_ERROR_PREFIX = "The enrichment failed: "
INTERRUPTED_ENRICHMENT_ERROR = (
    "interrupted: the server stopped while the {stage} stage of the enrichment ran"
)
HISTORY_VIA = "exploration"
RATIONALE = "The outline of exploration {id} drafted this as a direct prerequisite."
MODEL_CALLS_NOTE = (
    "Adding the sources calls models: one call to read each PDF's details, and concept "
    "extraction (one or two Claude calls and embeddings) for each source whose concepts were "
    "never extracted. Concepts that are new are looked up again with one judge call and the new "
    "ones embedded. There is no price estimate: what it spent is shown once it has run. No "
    "study page is generated."
)
_METHOD_ORDER = {"arxiv": 0, "doi": 1, "url": 2}


class EnrichmentError(ExplorationError):
    """An enrichment failed; the message says why (no secret in it)."""


class EnrichmentUnavailableError(ExplorationUnavailableError):
    """Something an enrichment needs is missing (HTTP 503)."""


class _Stopped(EnrichmentError):
    """The enrichment is no longer running (marked failed meanwhile, e.g. by a restart)."""


def _failed(reason: str) -> EnrichmentError:
    return EnrichmentError(f"{ENRICHMENT_ERROR_PREFIX}{reason}")


def _unavailable(reason: str) -> EnrichmentUnavailableError:
    return EnrichmentUnavailableError(f"{ENRICHMENT_ERROR_PREFIX}{reason}")


def _without_prefix(exc: BaseException) -> str:
    text = str(exc)
    for prefix in (ERROR_PREFIX, ENRICHMENT_ERROR_PREFIX, "Concepts could not be extracted: "):
        text = text.removeprefix(prefix)
    return text


# -- what a run needs -----------------------------------------------------------------------


class Enricher:
    """What an enrichment needs: the app's URL `fetcher` and `metadata_extractor` (the Add
    page's), a `concept_extractor` (`teacher.concept_extraction.ConceptExtractor`) and the graph
    match's `matcher` (`GraphMatcher`), whose embedder embeds the concepts it creates; both must
    use the same embedding model (the graph has one index)."""

    def __init__(
        self, *, fetcher: Any, metadata_extractor: Any, concept_extractor: Any, matcher: Any
    ) -> None:
        missing = [
            name
            for name, value in (
                ("fetcher", fetcher), ("metadata_extractor", metadata_extractor),
                ("concept_extractor", concept_extractor), ("matcher", matcher),
            )  # fmt: skip
            if value is None
        ]
        if missing:
            raise ValueError(f"an Enricher needs {', '.join(missing)}")
        extractor_model = getattr(getattr(concept_extractor, "embedder", None), "model", None)
        matcher_model = getattr(getattr(matcher, "embedder", None), "model", None)
        if extractor_model != matcher_model:
            raise ValueError(
                f"the concept extractor embeds with {extractor_model!r} and the graph matcher "
                f"with {matcher_model!r}; the graph has one embedding index, so they must agree"
            )
        self.fetcher = fetcher
        self.metadata_extractor = metadata_extractor
        self.concept_extractor = concept_extractor
        self.matcher = matcher

    @property
    def embedder(self) -> Any:
        return self.matcher.embedder

    def check_available(self) -> None:
        """EnrichmentUnavailableError naming every problem when no Claude client can be made
        or the embedder has no key; no request is made."""
        from teacher.concept_extraction import ConceptExtractionError

        problems: list[str] = []
        try:
            self.concept_extractor.check_available()
        except ConceptExtractionError as exc:
            problems.append(_without_prefix(exc))
        try:
            self.matcher.check_available()
        except ExplorationUnavailableError as exc:
            text = _without_prefix(exc)
            if text not in problems:
                problems.append(text)
        if problems:
            raise _unavailable("; ".join(problems))


def build_enricher(
    *, fetcher: Any, metadata_extractor: Any, concept_extractor: Any, matcher: Any
) -> Enricher:
    return Enricher(
        fetcher=fetcher, metadata_extractor=metadata_extractor,
        concept_extractor=concept_extractor, matcher=matcher,
    )  # fmt: skip


# -- what is held already ---------------------------------------------------------------------


def _held(
    session: Session, catalog: LibraryCatalog, candidate: ExplorationCandidate
) -> tuple[KnowledgeSource, str] | None:
    """The library source `candidate` already is, and how that was found, or None."""
    if candidate.knowledge_source_id is not None:
        source = session.get(KnowledgeSource, candidate.knowledge_source_id)
        if source is not None:
            return source, "earlier attempt"
    merged = MergedCandidate(
        id=candidate.candidate_key, key=candidate.candidate_key, url=candidate.url,
        landing_url=candidate.landing_url, title=candidate.title, found_by=[],
        arxiv_id=candidate.arxiv_id, doi=candidate.doi,
        addresses=[a for a in (candidate.url, candidate.landing_url) if a],
    )  # fmt: skip
    matches = catalog.match(merged)
    if not matches:
        return None
    best = min(matches, key=lambda m: (_METHOD_ORDER.get(m.method, 9), m.slug))
    source = session.scalar(select(KnowledgeSource).where(KnowledgeSource.slug == best.slug))
    if source is None:  # the catalog was read in this session: cannot happen
        raise _failed(f"the library source {best.slug!r} disappeared while it was read")
    return source, best.method


def _extracted(session: Session, source_id: int) -> bool:
    """Whether concept extraction has run for the source: it has concept links (a run writes
    at least one, all at once)."""
    return bool(
        session.scalar(
            select(func.count(SourceConcept.id)).where(SourceConcept.source_id == source_id)
        )
    )


# -- the preview ------------------------------------------------------------------------------


def _digest(exploration: Exploration, rows: list[ExplorationConcept], approved: list[int]) -> str:
    state = {
        "exploration": exploration.id,
        "candidates": sorted(approved),
        "concepts": [
            [r.id, r.status == "dropped", r.name, r.concept_id, list(r.prerequisites)]
            for r in rows
        ],
    }
    blob = json.dumps(state, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:32]


def _blocked(manager: ExplorationManager, exploration: Exploration) -> str | None:
    if exploration.status != "completed":
        return f"The exploration is {exploration.status}: only a completed exploration can be enriched."
    running = manager.running_enrichment(exploration)
    if running is not None:
        return f"An enrichment of this exploration is running (enrichment {running.id})."
    return None


def _expected_links(
    session: Session, settings: Settings, rows: list[ExplorationConcept]
) -> list[dict[str, Any]]:
    """Every drafted prerequisite link of the concepts that are not dropped, with what is
    expected of it before anything is written (see the module docstring)."""
    from teacher.managers.concept_relations import builds_on_edges

    by_key = {r.key: r for r in rows}
    live = {
        r.key: session.get(Concept, r.concept_id)
        for r in rows
        if r.status != "dropped" and r.concept_id is not None
    }
    relations = ConceptRelationManager(session, settings)
    edges = builds_on_edges(session)
    out = []
    for row in rows:
        if row.status == "dropped":
            continue
        for key in row.prerequisites:
            prereq = by_key.get(key)
            if prereq is None:
                continue
            item: dict[str, Any] = {
                "from_key": row.key, "from": row.name, "to_key": key, "to": prereq.name,
                "expected": "add", "code": None, "reason": None,
            }  # fmt: skip
            if prereq.status == "dropped":
                item.update(expected="skip", code="dropped",
                            reason=f"{prereq.name!r} is dropped from the plan")  # fmt: skip
                out.append(item)
                continue
            a, b = live.get(row.key), live.get(key)
            if a is None or b is None:
                item["reason"] = "added once the new concept is in the graph"
                out.append(item)
                continue
            if a.id == b.id:
                item.update(expected="skip", code="same_concept",
                            reason=f"both are the graph concept {a.slug!r}")  # fmt: skip
            else:
                existing = relations.between(a, b)
                if existing is not None:
                    if (existing.type, existing.from_id, existing.to_id) == (BUILDS_ON, a.id, b.id):
                        item.update(expected="present", reason="already in the graph")
                    else:
                        slugs = slugs_by_id(session, (existing.from_id, existing.to_id))
                        item.update(expected="skip", code="related",
                                    reason=f"already related as {describe(existing, slugs)}")  # fmt: skip
                else:
                    trial = {k: set(v) for k, v in edges.items()}
                    trial.setdefault(a.id, set()).add(b.id)
                    if find_cycle(trial) is not None:
                        item.update(expected="skip", code="cycle",
                                    reason="it would make builds_on cyclic")  # fmt: skip
                    else:
                        edges = trial
            out.append(item)
    return out


def enrichment_preview(
    session: Session, settings: Settings, exploration: Exploration
) -> dict[str, Any]:
    """What an enrichment of `exploration` would do now (see the module docstring). Reads
    only."""
    manager = ExplorationManager(session, settings, owner=exploration.user_id)
    rows = manager.concepts(exploration)
    approved = manager.approved_candidates(exploration)
    catalog = LibraryCatalog.load(session)
    live_rows = [r for r in rows if r.status != "dropped"]
    names = {r.key: r.name for r in rows}

    sources = []
    for candidate in approved:
        held = _held(session, catalog, candidate)
        source = held[0] if held else None
        sources.append(
            {
                "candidate_id": candidate.id,
                "title": candidate.title,
                "url": candidate.url,
                "action": "link" if held else "add",
                "held": (
                    {"slug": source.slug, "title": source.title, "method": held[1]}
                    if held and source is not None
                    else None
                ),
                "extraction": (
                    "done_before" if source is not None and _extracted(session, source.id) else "run"
                ),
                "concepts": [
                    names[c.key] for c in manager.concepts_of(candidate) if c.status != "dropped"
                ],
                "last_error": candidate.ingest_error,
            }
        )
    concepts = []
    for row in live_rows:
        concept = session.get(Concept, row.concept_id) if row.concept_id is not None else None
        concepts.append(
            {
                "id": row.id,
                "key": row.key,
                "name": row.name,
                "status": row.status,
                "action": "use" if concept is not None else "resolve",
                "concept": (
                    {"slug": concept.slug, "name": concept.name} if concept is not None else None
                ),
            }
        )
    links = _expected_links(session, settings, rows)
    blocked = _blocked(manager, exploration)
    return {
        "exploration_id": exploration.id,
        "digest": _digest(exploration, rows, [c.id for c in approved]),
        "can_start": blocked is None,
        "blocked": blocked,
        "sources": {
            "total": len(sources),
            "to_add": sum(1 for s in sources if s["action"] == "add"),
            "to_link": sum(1 for s in sources if s["action"] == "link"),
            "items": sources,
        },
        "concepts": {
            "total": len(concepts),
            "matched": sum(1 for c in concepts if c["action"] == "use"),
            "to_resolve": sum(1 for c in concepts if c["action"] == "resolve"),
            "items": concepts,
        },
        "links": {
            "total": len(links),
            "to_add": sum(1 for link in links if link["expected"] == "add"),
            "present": sum(1 for link in links if link["expected"] == "present"),
            "to_skip": sum(1 for link in links if link["expected"] == "skip"),
            "items": links,
        },
        "dropped": [r.name for r in rows if r.status == "dropped"],
        "model_calls": MODEL_CALLS_NOTE,
    }


def _plan(preview: dict[str, Any]) -> dict[str, Any]:
    """What is stored as the confirmed plan."""
    return {
        "digest": preview["digest"],
        "candidates": [s["candidate_id"] for s in preview["sources"]["items"]],
        "concepts": [c["id"] for c in preview["concepts"]["items"]],
        "links": [[link["from_key"], link["to_key"]] for link in preview["links"]["items"]],
        "counts": {
            "sources": preview["sources"]["total"],
            "concepts": preview["concepts"]["total"],
            "links": preview["links"]["total"],
        },
    }


# -- starting ---------------------------------------------------------------------------------


def start_enrichment(
    settings: Settings,
    enricher: Enricher,
    exploration_id: int,
    *,
    owner: int | None,
    digest: str,
) -> int:
    """Check the enricher, check that `digest` is the current preview's (the reader confirmed
    this plan) and create the enrichment (committed); its id. NotFoundError for another
    owner's exploration, StateConflictError for a changed plan, an exploration that is not
    completed or one being enriched, EnrichmentUnavailableError when it cannot run."""
    if not isinstance(digest, str) or not digest.strip():
        raise ValueError("confirm the enrichment with the digest of the plan it showed")
    with session_scope(settings) as session:
        manager = ExplorationManager(session, settings, owner=owner)
        row = manager.get(exploration_id)
        blocked = _blocked(manager, row)
        if blocked is not None:
            raise StateConflictError(blocked)
    enricher.check_available()
    with session_scope(settings) as session:
        manager = ExplorationManager(session, settings, owner=owner)
        row = manager.get(exploration_id)
        preview = enrichment_preview(session, settings, row)
        if preview["digest"] != digest.strip():
            raise StateConflictError(
                "The plan changed since it was shown (a source was ticked or a concept edited "
                "meanwhile): look at what the enrichment will do again, then confirm."
            )
        return manager.create_enrichment(row, _plan(preview)).id


# -- the job -----------------------------------------------------------------------------------


def _error_text(exc: BaseException) -> str:
    if isinstance(exc, ExplorationError):
        return str(exc)
    message = str(exc).strip()
    return f"{exc.__class__.__name__}: {message}" if message else exc.__class__.__name__


class _Job:
    """One enrichment's bookkeeping: short sessions on it, by id."""

    def __init__(
        self, settings: Settings, enrichment_id: int, on_stage: Callable[[str], None] | None
    ) -> None:
        self.settings = settings
        self.id = enrichment_id
        self.on_stage = on_stage

    @contextmanager
    def edit(self, *, running: bool = True) -> Iterator[tuple[ExplorationManager, Exploration, ExplorationEnrichment, Session]]:  # fmt: skip
        with session_scope(self.settings) as session:
            enrichment = session.get(ExplorationEnrichment, self.id)
            if enrichment is None:
                raise _failed(f"enrichment {self.id} no longer exists")
            exploration = session.get(Exploration, enrichment.exploration_id)
            assert exploration is not None  # the foreign key cascades
            if running and enrichment.status != "running":
                raise _Stopped(
                    f"{ENRICHMENT_ERROR_PREFIX}enrichment {self.id} is no longer running"
                    + (f" ({enrichment.error})" if enrichment.error else "")
                )
            manager = ExplorationManager(session, self.settings, owner=exploration.user_id)
            yield manager, exploration, enrichment, session

    @contextmanager
    def stage(self, name: str, *, total: int = 0) -> Iterator[None]:
        with self.edit() as (manager, _row, enrichment, _session):
            manager.begin_enrichment_stage(enrichment, name, total=total)
        if self.on_stage is not None:
            self.on_stage(name)
        yield
        with self.edit() as (manager, _row, enrichment, _session):
            manager.end_enrichment_stage(enrichment)

    def advance(self, done: int = 1) -> None:
        with self.edit() as (manager, _row, enrichment, _session):
            manager.advance_enrichment(enrichment, done)


def run_enrichment(
    settings: Settings,
    enrichment_id: int,
    enricher: Enricher,
    *,
    on_stage: Callable[[str], None] | None = None,
) -> None:
    """Run the enrichment's stages (see the module docstring). Never raises: a failure is
    stored on the enrichment. `on_stage(name)` is called as each stage starts."""
    job = _Job(settings, enrichment_id, on_stage)
    try:
        with job.edit(running=False) as (_manager, exploration, enrichment, _session):
            exploration_id = exploration.id
            if enrichment.status != "running" or enrichment.stage is not None:
                raise _failed(f"enrichment {enrichment_id} has already run ({enrichment.status})")
        with cost_scope(
            settings, purpose=PURPOSE, exploration_id=exploration_id,
            exploration_enrichment_id=enrichment_id,
        ):  # fmt: skip
            _stages(job, enricher)
    except BaseException as exc:
        error = _error_text(exc)
        if not isinstance(exc, ExplorationError):
            logger.exception("enrichment %s failed", enrichment_id)
        try:
            with job.edit(running=False) as (manager, _row, enrichment, _session):
                if enrichment.status == "running":
                    manager.fail_enrichment(enrichment, error)
        except Exception:  # the failure itself could not be stored: say so in the log
            logger.exception("enrichment %s: its failure could not be recorded", enrichment_id)
        if not isinstance(exc, Exception):
            raise


def _stages(job: _Job, enricher: Enricher) -> None:
    with job.edit() as (_manager, _row, enrichment, _session):
        plan = dict(enrichment.plan)
    candidate_ids: list[int] = list(plan["candidates"])
    concept_ids: list[int] = list(plan["concepts"])

    sources: list[dict[str, Any]] = []
    with job.stage("ingest", total=len(candidate_ids)):
        for candidate_id in candidate_ids:
            sources.append(_ingest(job, enricher, candidate_id))
            job.advance()

    with job.stage("graph", total=len(concept_ids)):
        concepts = _write_concepts(job, enricher, concept_ids)
        job.advance(len(concept_ids))
        relations = _write_relations(job, concept_ids)

    with job.stage("grounding", total=len(concept_ids)):
        grounding = _check_grounding(job, concept_ids)
        job.advance(len(concept_ids))

    summary = {
        "sources": {
            "added": sum(1 for s in sources if s["outcome"] == "added"),
            "held": sum(1 for s in sources if s["outcome"] == "held"),
            "failed": sum(1 for s in sources if s["outcome"] == "failed"),
            "items": sources,
        },
        "concepts": {
            "matched": sum(1 for c in concepts if c["outcome"] == "matched"),
            "found": sum(1 for c in concepts if c["outcome"] == "found"),
            "created": sum(1 for c in concepts if c["outcome"] == "created"),
            "items": concepts,
        },
        "relations": {
            "added": sum(1 for r in relations if r["outcome"] == "added"),
            "present": sum(1 for r in relations if r["outcome"] == "present"),
            "skipped": sum(1 for r in relations if r["outcome"] == "skipped"),
            "items": relations,
        },
        "grounding": {
            "grounded": sum(1 for g in grounding if g["grounded"]),
            "ungrounded": sum(1 for g in grounding if not g["grounded"]),
            "items": grounding,
        },
    }
    with job.edit() as (manager, _row, enrichment, _session):
        manager.complete_enrichment(enrichment, summary)


# -- 1. ingest ----------------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _free_slug(session: Session, settings: Settings, base: str) -> str:
    slug, n = base, 1
    while (
        session.scalar(select(KnowledgeSource.id).where(KnowledgeSource.slug == slug)) is not None
        or (settings.sources_dir / slug).exists()
    ):
        n += 1
        slug = f"{base}-{n}"
    return slug


def _expected_errors() -> tuple[type[BaseException], ...]:
    from teacher.concept_extraction import ConceptExtractionError
    from teacher.metadata import MetadataUnavailableError

    return (
        ValueError, OSError, ManagerError, MetadataUnavailableError, ConceptExtractionError,
        EmbeddingError, ExplorationError,
    )  # fmt: skip


def _ingest(job: _Job, enricher: Enricher, candidate_id: int) -> dict[str, Any]:
    """Ingest one approved candidate (see the module docstring); what became of it. A failure
    of this candidate is stored on it and returned; only the job's own failure raises."""
    settings = job.settings
    with job.edit() as (manager, exploration, enrichment, session):
        candidate = manager.candidate(exploration, candidate_id)
        item: dict[str, Any] = {
            "candidate_id": candidate.id, "title": candidate.title, "url": candidate.url,
            "outcome": None, "slug": None, "error": None,
        }  # fmt: skip
        if candidate.decision != "approved":
            item.update(outcome="skipped", error=f"it is {candidate.decision}, no longer approved")
            return item
        snapshot = {
            "url": candidate.url, "title": candidate.title, "authors": list(candidate.authors),
            "year": candidate.year, "summary": candidate.summary,
            "arxiv_id": candidate.arxiv_id, "doi": candidate.doi,
        }  # fmt: skip
        held = _held(session, LibraryCatalog.load(session), candidate)
        source_id = held[0].id if held else None
        exploration_id, enrichment_id = exploration.id, enrichment.id
    detail: dict[str, Any] = {"enrichment_id": enrichment_id, "attempted_at": _now()}
    if held:
        detail.update(outcome="held", method=held[1])
    state = {"step": "fetching it"}
    try:
        if source_id is None:
            source_id = _add(job, enricher, candidate_id, snapshot, detail, state,
                             exploration_id=exploration_id, enrichment_id=enrichment_id)  # fmt: skip
        state["step"] = "concept extraction"
        with session_scope(settings) as session:  # reads only
            source = session.get(KnowledgeSource, source_id)
            if source is None:
                raise _failed(f"the library source {source_id} it became was deleted meanwhile")
            slug = source.slug
            extracted = _extracted(session, source_id)
        item["slug"] = detail["slug"] = slug
        if extracted:
            detail["extraction"] = "done_before"
        else:
            from teacher.concept_extraction import source_info

            with session_scope(settings) as session:  # reads only
                info = source_info(session, settings, slug)
            run = enricher.concept_extractor.run(settings, info)
            created = len(run.by_resolution("created"))
            detail["extraction"] = "ran"
            detail["extracted"] = {
                "links": len(run.links), "created": created, "matched": len(run.links) - created,
            }  # fmt: skip
        with job.edit() as (manager, exploration, _enrichment, session):
            candidate = manager.candidate(exploration, candidate_id)
            source = session.get(KnowledgeSource, source_id)
            if source is None:
                raise _failed(f"the library source {source_id} it became was deleted meanwhile")
            manager.record_ingest(candidate, source, detail)
        item["outcome"] = detail["outcome"]
        return item
    except _Stopped:
        raise
    except Exception as exc:
        if isinstance(exc, _expected_errors()):
            error = f"{state['step']}: {_without_prefix(exc)}"
        else:
            logger.exception("enrichment: candidate %s failed", candidate_id)
            error = f"{state['step']}: {_error_text(exc)}"
        if state["step"] == "concept extraction":
            detail["extraction"] = "failed"
        detail.update(outcome="failed", failed_at=state["step"])
        with job.edit() as (manager, exploration, _enrichment, session):
            candidate = manager.candidate(exploration, candidate_id)
            source = session.get(KnowledgeSource, source_id) if source_id is not None else None
            manager.record_ingest_failure(candidate, error, detail, source=source)
        item.update(outcome="failed", error=error)
        return item


def _pick(detected: Any, name: str, fallback: Any, origins: dict[str, str]) -> Any:
    value = getattr(detected, name, None) if detected is not None else None
    if name == "authors":
        value = list(value) if value else None
    if value:
        origins[name] = "detected"
        return value
    if fallback:
        origins[name] = "search result"
        return fallback
    return None


def _add(
    job: _Job,
    enricher: Enricher,
    candidate_id: int,
    snapshot: dict[str, Any],
    detail: dict[str, Any],
    state: dict[str, str],
    *,
    exploration_id: int,
    enrichment_id: int,
) -> int:
    """Fetch the candidate and add it (or find it held after all); the library source's id.
    Raises for the candidate's failures, with `state["step"]` saying what failed."""
    settings = job.settings
    fetched = enricher.fetcher.fetch(snapshot["url"])
    with tempfile.TemporaryDirectory(prefix="teacher-enrich-") as tmp:
        path = write_fetched(fetched, tmp)
        sha = sha256_file(path)
        with session_scope(settings) as session:  # reads only
            same = KnowledgeSourceManager(session, settings).find_by_content(sha)
            if same is not None:
                detail.update(outcome="held", method="content")
                return same.id
        state["step"] = "reading its details"
        detected = detect_details(fetched, path, enricher.metadata_extractor)
    state["step"] = "adding it to the library"

    origins: dict[str, str] = {}
    title = _pick(detected, "title", snapshot["title"], origins)
    creators = _pick(detected, "authors", snapshot["authors"], origins) or []
    year = _pick(detected, "year", snapshot["year"], origins)
    summary = _pick(detected, "summary", snapshot["summary"], origins)
    arxiv_id = _pick(detected, "arxiv_id", snapshot["arxiv_id"], origins)
    doi = _pick(detected, "doi", snapshot["doi"], origins)
    identifiers = [(s, v) for s, v in (("arxiv", arxiv_id), ("doi", doi)) if v]
    detail["details"] = origins
    with session_scope(settings) as session:
        manager = KnowledgeSourceManager(session, settings)
        for scheme, value in identifiers:
            owner = manager.find_by_identifier(scheme, value)
            if owner is not None:
                detail.update(outcome="held", method=scheme)
                return owner.id
        base = slugify(title)
        slug = _free_slug(session, settings, base)
        if slug != base:
            detail["slug_note"] = (
                f"slug {base!r} belongs to another source; added as {slug!r}"
            )
        source = store_fetched(
            manager,
            fetched,
            slug=slug,
            title=title,
            creators=creators,
            originated_on=str(year) if year else None,
            summary=summary,
            identifiers=identifiers,
            metadata={
                "exploration": {
                    "id": exploration_id, "candidate_id": candidate_id,
                    "enrichment_id": enrichment_id,
                }
            },  # fmt: skip
        )
        StudyHistoryManager(session, settings).log(
            "source_added", source=source, payload={"via": HISTORY_VIA}
        )
        SourceReferenceManager(session, settings).rematch_all()
        # The candidate keeps its source in the same transaction, so an attempt that stops
        # after this (an extraction failure, a restart) links it next time.
        candidate = session.get(ExplorationCandidate, candidate_id)
        assert candidate is not None
        candidate.knowledge_source_id = source.id
        detail.update(outcome="added", method=None)
        return source.id


# -- 2. the graph -------------------------------------------------------------------------------


def _live_rows(manager: ExplorationManager, exploration: Exploration, ids: list[int]) -> list[ExplorationConcept]:  # fmt: skip
    wanted = set(ids)
    return [r for r in manager.concepts(exploration) if r.id in wanted and r.status != "dropped"]


def _clean_aliases(name: str, aliases: list[str]) -> list[str]:
    taken = {normalize_name(name)}
    out = []
    for alias in aliases:
        text = " ".join(str(alias).split())
        if not text:
            continue
        norm = normalize_name(text, what="alias")
        if norm in taken:
            continue
        taken.add(norm)
        out.append(text)
    return out


def _new_aliases(row: ExplorationConcept) -> list[str]:
    extra = [row.drafted_name] if row.drafted_name != row.name else []
    return _clean_aliases(row.name, [*row.aliases, *extra])


def _exact(concepts: ConceptManager, row: ExplorationConcept) -> Concept | None:
    """The concept the row names exactly: found by its name (or drafted name) with every name
    and alias hit one and the same concept, as concept extraction's exact rule."""
    names = [row.name] + ([row.drafted_name] if row.drafted_name != row.name else [])
    name_hits = {c.id: c for n in names for c in concepts.find_by_name(n)}
    alias_hits = {c.id: c for a in row.aliases for c in concepts.find_by_name(a)}
    if name_hits and len(set(name_hits) | set(alias_hits)) == 1:
        return next(iter(name_hits.values()))
    return None


def _write_concepts(job: _Job, enricher: Enricher, concept_ids: list[int]) -> list[dict[str, Any]]:
    """Give every concept of the plan its graph concept (see the module docstring)."""
    settings = job.settings
    outcomes: dict[int, dict[str, Any]] = {}
    to_resolve: list[int] = []
    with job.edit() as (manager, exploration, _enrichment, session):
        rows = _live_rows(manager, exploration, concept_ids)
        order = [r.id for r in rows]
        for row in rows:
            concept = session.get(Concept, row.concept_id) if row.concept_id is not None else None
            if concept is None:
                to_resolve.append(row.id)
                continue
            if row.graph_outcome is None:
                manager.store_graph(row, concept, outcome="matched", reason=row.match_reason)
            outcomes[row.id] = {
                "key": row.key, "name": row.name, "outcome": row.graph_outcome,
                "slug": concept.slug, "reason": row.graph_reason,
            }  # fmt: skip

    if to_resolve:
        _refresh_index(settings, enricher)
        exact: dict[int, int] = {}
        drafts: list[DraftConcept] = []
        with session_scope(settings) as session:  # reads only
            concepts = ConceptManager(session, settings)
            for row_id in to_resolve:
                row = session.get(ExplorationConcept, row_id)
                assert row is not None
                hit = _exact(concepts, row)
                if hit is not None:
                    exact[row_id] = hit.id
                else:
                    drafts.append(
                        DraftConcept(
                            key=row.key, position=row.position, step=row.step, name=row.name,
                            aliases=tuple(_new_aliases(row)), summary=row.summary,
                            prerequisites=tuple(row.prerequisites), search_query=row.search_query,
                        )  # fmt: skip
                    )
        matches = {}
        if drafts:
            try:
                found = enricher.matcher.match(settings, drafts)
            except ExplorationError as exc:
                raise _failed(f"the new concepts could not be looked up: {_without_prefix(exc)}") from exc
            matches = {draft.key: match for draft, match in zip(drafts, found, strict=True)}
        to_create = [d for d in drafts if matches[d.key].concept_id is None]
        vectors: list[list[float]] = []
        texts: list[str] = []
        if to_create:
            texts = [concept_text(d.name, list(d.aliases), d.summary) for d in to_create]
            try:
                vectors = check_vectors(
                    enricher.embedder.embed(list(texts)), count=len(texts),
                    model=enricher.embedder.model,
                )  # fmt: skip
            except EmbeddingError as exc:
                raise _unavailable(f"the new concepts could not be embedded: {exc}") from exc
        vector_of = {d.key: (t, v) for d, t, v in zip(to_create, texts, vectors, strict=True)}

        with job.edit() as (manager, exploration, _enrichment, session):
            concepts = ConceptManager(session, settings)
            embeddings: list[tuple[Concept, str, list[float]]] = []
            for row in _live_rows(manager, exploration, to_resolve):
                if row.id in exact:
                    concept = concepts.get(exact[row.id])
                    manager.store_graph(row, concept, outcome="found",
                                        reason=f"exact name match: {concept.name!r}")  # fmt: skip
                else:
                    match = matches[row.key]
                    if match.concept_id is not None:
                        concept = concepts.get(match.concept_id)
                        reason = (
                            f"exact name match: {concept.name!r}" if match.verdict == "exact"
                            else f"judged the same: {match.reason}"
                        )  # fmt: skip
                        manager.store_graph(row, concept, outcome="found", reason=reason)
                    else:
                        concept, created = _create(session, concepts, row, match.reason)
                        if created:
                            text, vector = vector_of[row.key]
                            embeddings.append((concept, text, vector))
                            manager.store_graph(row, concept, outcome="created",
                                                reason=match.reason or "no concept of the graph is the same")  # fmt: skip
                        else:
                            manager.store_graph(row, concept, outcome="found",
                                                reason="it appeared in the graph meanwhile")  # fmt: skip
                outcomes[row.id] = {
                    "key": row.key, "name": row.name, "outcome": row.graph_outcome,
                    "slug": concept.slug, "reason": row.graph_reason,
                }  # fmt: skip
            for concept, text, _vector in embeddings:
                if text_of(concept) != text:  # a guard: the hash must be the text's
                    raise _failed(
                        f"concept {concept.slug!r} was stored with another text than the one "
                        "embedded"
                    )
            ConceptEmbeddingManager(session, settings).store(embeddings, enricher.embedder.model)
    return [outcomes[row_id] for row_id in order if row_id in outcomes]


def _refresh_index(settings: Settings, enricher: Enricher) -> None:
    """Bring the embedding index up to date (as concept extraction does), so the resolver
    searches the whole graph."""
    try:
        with session_scope(settings) as session:
            if session.scalar(select(func.count(Concept.id))):
                ConceptEmbeddingManager(session, settings).embed_stale(enricher.embedder)
    except EmbeddingError as exc:
        raise _unavailable(f"the graph's embeddings could not be brought up to date: {exc}") from exc


def _create(
    session: Session, concepts: ConceptManager, row: ExplorationConcept, reason: str | None
) -> tuple[Concept, bool]:
    """The concept `row` becomes: a new one (origin model, unreviewed), or the one named
    exactly like it that appeared meanwhile; (concept, created)."""
    now = concepts.find_by_name(row.name)
    if len(now) == 1:
        return now[0], False
    if now:
        raise _failed(
            f"the concept {row.name!r} cannot be created: {len(now)} concepts of the graph are "
            f"named so now ({', '.join(c.slug for c in now)})"
        )
    base = slugify(row.name)
    slug, n = base, 1
    while session.scalar(select(Concept.id).where(Concept.slug == slug)) is not None:
        n += 1
        slug = f"{base}-{n}"
    concept = concepts.create(row.name, row.summary, "model", aliases=_new_aliases(row), slug=slug)
    return concept, True


def _write_relations(job: _Job, concept_ids: list[int]) -> list[dict[str, Any]]:
    """Write each drafted prerequisite link as builds_on (see the module docstring)."""
    out: list[dict[str, Any]] = []
    with job.edit() as (manager, exploration, _enrichment, session):
        relations = ConceptRelationManager(session, job.settings)
        all_rows = manager.concepts(exploration)
        by_key = {r.key: r for r in all_rows}
        wanted = set(concept_ids)
        rationale = RATIONALE.format(id=exploration.id)
        for row in all_rows:
            if row.id not in wanted or row.status == "dropped":
                continue
            links = []
            for key in row.prerequisites:
                prereq = by_key.get(key)
                if prereq is None:
                    continue
                link: dict[str, Any] = {
                    "key": key, "outcome": "skipped", "code": None, "reason": None,
                    "relation_id": None,
                }  # fmt: skip
                a = session.get(Concept, row.concept_id) if row.concept_id is not None else None
                b = session.get(Concept, prereq.concept_id) if prereq.concept_id is not None else None
                if prereq.status == "dropped" or prereq.id not in wanted:
                    link.update(code="dropped", reason=f"{prereq.name!r} is dropped from the plan")
                elif a is None or b is None:
                    link.update(code="no_concept", reason="one end has no graph concept")
                elif a.id == b.id:
                    link.update(code="same_concept", reason=f"both are the graph concept {a.slug!r}")
                else:
                    existing = relations.between(a, b)
                    if existing is not None:
                        if (existing.type, existing.from_id, existing.to_id) == (BUILDS_ON, a.id, b.id):
                            link.update(outcome="present", relation_id=existing.id,
                                        reason="already in the graph")  # fmt: skip
                        else:
                            slugs = slugs_by_id(session, (existing.from_id, existing.to_id))
                            link.update(code="related",
                                        reason=f"already related as {describe(existing, slugs)}")  # fmt: skip
                    else:
                        try:
                            relation = relations.add(BUILDS_ON, a, b, rationale, "model")
                        except CycleError as exc:
                            link.update(code="cycle", reason=str(exc))
                        except RelationConflictError as exc:
                            link.update(code="related", reason=str(exc))
                        else:
                            link.update(outcome="added", relation_id=relation.id)
                links.append(link)
                out.append(
                    {
                        "from_key": row.key, "from": row.name, "to_key": key, "to": prereq.name,
                        **{k: link[k] for k in ("outcome", "code", "reason", "relation_id")},
                    }  # fmt: skip
                )
            manager.store_prerequisite_links(row, links)
    return out


# -- 3. grounding -------------------------------------------------------------------------------


def _check_grounding(job: _Job, concept_ids: list[int]) -> list[dict[str, Any]]:
    out = []
    with job.edit() as (manager, exploration, _enrichment, session):
        for row in _live_rows(manager, exploration, concept_ids):
            concept = session.get(Concept, row.concept_id) if row.concept_id is not None else None
            sources = grounding_of(session, concept.id, outline_concept_id=row.id) if concept else []
            manager.store_grounding(row, [s.to_dict() for s in sources])
            grounded = is_grounded(sources)
            if grounded and row.status in ("planned", "known"):
                assert concept is not None
                manager.mark_grounded(row, concept)
            elif not grounded and row.status == "grounded":
                manager.mark_ungrounded(row)
            out.append(
                {
                    "key": row.key,
                    "name": row.name,
                    "grounded": grounded,
                    "sources": [s.slug for s in sources if s.grounds],
                    "proposed": sum(
                        1 for c in _candidates_of(manager, exploration, row) if c.decision == "proposed"
                    ),
                }
            )
    return out


def _candidates_of(
    manager: ExplorationManager, exploration: Exploration, row: ExplorationConcept
) -> list[ExplorationCandidate]:
    ids = {
        link.candidate_id for link in manager.links(exploration) if link.outline_concept_id == row.id
    }
    return [c for c in manager.candidates(exploration) if c.id in ids]


# -- interruption -------------------------------------------------------------------------------


def fail_interrupted_enrichments(settings: Settings) -> int:
    """Mark every enrichment still `running` as failed (`INTERRUPTED_ENRICHMENT_ERROR`); how
    many. Run at startup, when none can be running."""
    with session_scope(settings) as session:
        stale = list(
            session.scalars(
                select(ExplorationEnrichment).where(ExplorationEnrichment.status == "running")
            )
        )
        for enrichment in stale:
            exploration = session.get(Exploration, enrichment.exploration_id)
            assert exploration is not None
            manager = ExplorationManager(session, settings, owner=exploration.user_id)
            manager.fail_enrichment(
                enrichment, INTERRUPTED_ENRICHMENT_ERROR.format(stage=enrichment.stage or "first")
            )
        return len(stale)


__all__ = [
    "ENRICHMENT_ERROR_PREFIX",
    "INTERRUPTED_ENRICHMENT_ERROR",
    "MODEL_CALLS_NOTE",
    "PURPOSE",
    "STAGE_LABELS",
    "Enricher",
    "EnrichmentError",
    "EnrichmentUnavailableError",
    "build_enricher",
    "enrichment_preview",
    "fail_interrupted_enrichments",
    "run_enrichment",
    "start_enrichment",
]
