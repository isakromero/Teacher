"""Exploration: a topic and a level become an ordered outline of concepts, each matched against
the concept graph and, where it is needed, with ranked candidate sources, for the reader to
review. The run ingests nothing and writes nothing to the concept graph; only an enrichment
does, when the reader asks for it and confirms its plan (`enrichment`).

    explorer = build_explorer(ModelSettings.from_env(), finder=build_source_finder(models),
                              embedder=OpenAIEmbedder(models.embedding))
    exploration_id = start_exploration(settings, explorer, "Diffie-Hellman", "intro", owner=None)
    run_exploration(settings, exploration_id, explorer)   # never raises; see runner

The stages (`runner`): the brief and the outline (`planner`, the generation model), the graph
match (`matching`, concept extraction's resolver without its writes) and discovery (`sources`,
milestone 1's `SourceFinder` for every concept that is new or thinly covered, with at most one
follow-up search and shared deduplication). The caps and the errors are in `common`; the
stored rows in `teacher.db.models` (`Exploration`, `ExplorationConcept`, `ExplorationSearch`,
`ExplorationCandidate`, `ExplorationLink`) and their manager
`teacher.managers.explorations.ExplorationManager`, which also holds the reader's edits and the
hooks for ingesting approved candidates (milestone 3). `view` is the JSON of an exploration.

Enrichment (`enrichment`): on a completed exploration, its approved candidates ingested (concept
extraction only, no study page), its outline concepts and prerequisite links written to the
graph, and each concept's grounding checked (`grounding`: a library source introduces it):

    preview = enrichment_preview(session, settings, exploration)
    enrichment_id = start_enrichment(settings, enricher, exploration.id, owner=None,
                                     digest=preview["digest"])
    run_enrichment(settings, enrichment_id, enricher)   # never raises

`grounding.exploration_roadmap(session, settings, exploration)` is the exploration's steps as
the roadmap reads them: each concept with its graph concept, grounding sources and a
recommended primary source.

Roadmap (`roadmap`): a completed exploration's steps in order, each concept with the reader's
standing, its primary source (recommended, or the reader's choice) and that source's study
page, which concepts and steps are done (computed when read) and the one next action:

    view = roadmap_view(session, settings, exploration, max_attempts=2)

Surfaces: `teacher explore "<topic>"`, `teacher explore --enrich <id>` and `teacher explore
--roadmap <id>` (`teacher.cli`), `/api/explorations` and the Explore pages
(`teacher.web.routers.explorations`).
"""

from teacher.exploration.common import (
    LIMITS,
    MAX_CANDIDATES_PER_CONCEPT,
    MAX_CONCEPTS,
    MAX_SEARCHES_PER_CONCEPT,
    ExplorationError,
    ExplorationOutputError,
    ExplorationUnavailableError,
    GraphMatchError,
)
from teacher.exploration.enrichment import (
    INTERRUPTED_ENRICHMENT_ERROR,
    Enricher,
    EnrichmentError,
    EnrichmentUnavailableError,
    build_enricher,
    enrichment_preview,
    fail_interrupted_enrichments,
    run_enrichment,
    start_enrichment,
)
from teacher.exploration.grounding import (
    GROUNDING_ROLES,
    GroundingSource,
    RoadmapConcept,
    RoadmapStep,
    exploration_roadmap,
    grounding_of,
)
from teacher.exploration.matching import ConceptMatch, GraphMatcher
from teacher.exploration.planner import Brief, DraftConcept, DraftOutline, Planner
from teacher.exploration.roadmap import RULES as ROADMAP_RULES
from teacher.exploration.roadmap import roadmap_summary, roadmap_view
from teacher.exploration.runner import (
    INTERRUPTED_ERROR,
    Explorer,
    build_explorer,
    fail_interrupted_explorations,
    run_exploration,
    start_exploration,
)
from teacher.exploration.view import exploration_detail, exploration_summary

__all__ = [
    "GROUNDING_ROLES",
    "INTERRUPTED_ENRICHMENT_ERROR",
    "INTERRUPTED_ERROR",
    "LIMITS",
    "MAX_CANDIDATES_PER_CONCEPT",
    "MAX_CONCEPTS",
    "MAX_SEARCHES_PER_CONCEPT",
    "ROADMAP_RULES",
    "Brief",
    "ConceptMatch",
    "DraftConcept",
    "DraftOutline",
    "Enricher",
    "EnrichmentError",
    "EnrichmentUnavailableError",
    "ExplorationError",
    "ExplorationOutputError",
    "ExplorationUnavailableError",
    "Explorer",
    "GraphMatchError",
    "GraphMatcher",
    "GroundingSource",
    "Planner",
    "RoadmapConcept",
    "RoadmapStep",
    "build_enricher",
    "build_explorer",
    "enrichment_preview",
    "exploration_detail",
    "exploration_roadmap",
    "exploration_summary",
    "fail_interrupted_enrichments",
    "fail_interrupted_explorations",
    "grounding_of",
    "run_enrichment",
    "roadmap_summary",
    "roadmap_view",
    "run_exploration",
    "start_enrichment",
    "start_exploration",
]
