"""The FastAPI application factory.

`create_app()` stores everything the routers need on `app.state` (`settings`, `generator`,
`lab_generator` (writes labs; `teacher.lab_generator`), `lab_runner` (the lab generator's
runner, or None), `graders`, `metadata_extractor`, `tutor`, `glossary_builder`, `concept_extractor` (the
generator's concepts stage, or None), `concept_relater` (finds relations for one concept on
demand), `search_embedder` (embeds the graph page's search queries), `max_attempts`,
`chat_replies`, the registry of threads with a tutor reply in progress, and `relating`, the
registry of concepts with a relating run in progress), `review_bank_builder` (writes the review
queue's question variants, `teacher.review_bank`; None when the app has none),
`reference_extractor` (lists each source's references, `teacher.reference_extraction`; None
when the app has none), `source_finder` (finds candidate sources for a topic,
`teacher.discovery`; None when the app has none), `explorer` (runs explorations,
`teacher.exploration`; None when the app has none) and `clock` (the
review queue's time: a callable returning naive UTC) and `auth_gate` (the account gate,
`teacher.web.auth`, or None when the app has no access code: nobody signs in). Every request runs under a base cost
meter (`CostMeterMiddleware`, `teacher.costs`), so every API call the app makes is recorded. On startup the lifespan logs
which database it uses (the provider's `describe()`, never a password), migrates the
database to head and marks any
artifact left in status `generating` as `failed` (a generation job cannot survive a restart),
and likewise every generation run and every exploration still `running`.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from sqlalchemy import select

from teacher.config import ModelSettings, Settings
from teacher.costs import Meter, metering
from teacher.db.engine import session_scope
from teacher.db.migrate import upgrade_to_head
from teacher.db.providers import create_db_provider
from teacher.db.models import GenerationRun
from teacher.exploration import fail_interrupted_enrichments, fail_interrupted_explorations
from teacher.managers import ArtifactManager
from teacher.managers.generation_runs import GenerationRunManager
from teacher.reading import DEFAULT_MAX_ATTEMPTS
from teacher.url_sources import UrlFetcher
from teacher.web.auth import install_auth_gate
from teacher.web.errors import install_error_handlers
from teacher.web.routers import (
    artifacts,
    chat,
    concepts,
    discovery,
    explorations,
    glossary,
    highlights,
    history,
    labs,
    library_search,
    offline,
    offline_review,
    phone,
    pyodide,
    reading,
    references,
    review,
    source_graph,
    sources,
)


class CostMeterMiddleware:
    """Runs every HTTP request (its endpoint, a streamed body and its background tasks) under a
    base cost meter for `settings` with no links (`teacher.costs`), so an API call made without
    a closer scope is still recorded. A pure ASGI middleware: the meter is a context variable
    set in the request's task, which the threadpool running a sync endpoint, a streamed body
    or a background task copies."""

    def __init__(self, app: Any, *, settings: Settings) -> None:
        self.app = app
        self.settings = settings

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        with metering(Meter.for_settings(self.settings)):
            await self.app(scope, receive, send)


def fail_interrupted_generations(settings: Settings) -> list[str]:
    """Mark every artifact still `generating` as `failed`; return their slugs."""
    with session_scope(settings) as session:
        manager = ArtifactManager(session, settings)
        stale = manager.list(status="generating")
        for artifact in stale:
            manager.mark_failed(artifact)
        return [artifact.slug for artifact in stale]


INTERRUPTED_RUN_ERROR = "interrupted: the server stopped while this stage ran"


def fail_interrupted_runs(settings: Settings) -> int:
    """Mark every generation run still `running` as `failed`; return how many. Runs execute
    inside the server process, so at startup none can be running: one left `running` (for
    example a page's review variants, built after the page is ready) would otherwise be shown
    as in progress until it is old enough to count as stale."""
    with session_scope(settings) as session:
        runs = GenerationRunManager(session, settings)
        stale = list(session.scalars(select(GenerationRun).where(GenerationRun.status == "running")))
        for run in stale:
            runs.fail(run, INTERRUPTED_RUN_ERROR)
        return len(stale)


def create_app(
    settings: Settings | None = None,
    *,
    generator: Any = None,
    graders: Any = None,
    metadata_extractor: Any = None,
    tutor: Any = None,
    glossary_builder: Any = None,
    concept_extractor: Any = None,
    embedder: Any = None,
    block_tagger: Any = None,
    concept_relater: Any = None,
    search_embedder: Any = None,
    url_fetcher: Any = None,
    lab_generator: Any = None,
    lab_runner: Any = None,
    review_bank_builder: Any = None,
    reference_extractor: Any = None,
    source_finder: Any = None,
    explorer: Any = None,
    enricher: Any = None,
    clock: Any = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    access_code: str | None = None,
) -> FastAPI:
    """Build the API app. `settings` defaults to `Settings.from_env()`.

    `generator` defaults to `ClaudeGenerator`, `graders` to `default_graders()` (the LLM judge
    for blurbs), `metadata_extractor` to `MetadataExtractor` (detects an uploaded paper's
    details), `tutor` to `teacher.tutor.Tutor` (the side chat) and `glossary_builder` to
    `teacher.claude_generator.GlossaryBuilder` (builds the glossary of an existing page, with the
    generation model), with models from `ModelSettings.from_env()`. Pass
    `generator=teacher.generation.StubGenerator()` to work offline.

    `concept_extractor` and `embedder` only build the default generator: its concepts stage is
    `concept_extractor`, by default `teacher.concept_extraction.ConceptExtractor` with the
    concept model and `embedder` (by default `teacher.embeddings.OpenAIEmbedder` with the
    embedding model). A generator passed in brings its own concepts stage, so passing either
    with `generator` is a ValueError (it would be ignored), as is `embedder` with
    `concept_extractor`. `block_tagger` likewise only builds the default generator: its
    block_concepts stage, by default `teacher.block_tagging.BlockTagger` with the concept model.

    `concept_relater` (the "Find relations" button) defaults to
    `teacher.concept_relating.ConceptRelater` with the concept model and its own
    `teacher.embeddings.OpenAIEmbedder` with the embedding model; pass
    `teacher.concept_relating.stub_relater()` to work offline.

    `url_fetcher` fetches a web address for a source added from a URL; it defaults to
    `teacher.url_sources.UrlFetcher`.

    `lab_generator` writes labs (`kind = "lab"`); it defaults to
    `teacher.lab_generator.ClaudeLabGenerator` with the generation model and `lab_runner`,
    which defaults to `teacher.pyodide_runtime.PythonRunner(settings)` (it verifies each lab;
    nothing is checked until a lab is generated). `lab_runner` only builds the default lab
    generator, so passing it with `lab_generator` is a ValueError. Pass
    `lab_generator=teacher.lab_generator.StubLabGenerator()` to work offline.

    `search_embedder` embeds the graph search's queries (`GET /api/graph/search`). It defaults
    to the relater's `embedder` (the one the default relater is built with, or that of the
    relater passed in): both read the concept graph's one embedding index, which only matches
    queries embedded with the same model. A `concept_relater` without an `embedder` attribute
    and no `search_embedder` is a ValueError; a `search_embedder` without `model`, `embed` and
    `check_available` a TypeError.

    `review_bank_builder` writes the review queue's variants of a page's questions with the
    page's generation model, after each generation and on demand (`teacher.review_bank`). It
    defaults to `teacher.review_bank.ClaudeReviewBankBuilder` only together with the default
    generator: a generator passed in (such as the `StubGenerator`, whose model is "none") has
    no model a real builder could use, so then there is no builder unless one is passed (e.g.
    `teacher.review_bank.StubReviewBankBuilder()` offline), and the review page says variants
    cannot be built. `clock` (default `teacher.db.models.utcnow`) is the review queue's time.

    `reference_extractor` lists each source's references after it is added and on demand
    (`teacher.reference_extraction`). Like `review_bank_builder`, it defaults to
    `teacher.reference_extraction.ReferenceExtractor` (with the metadata model) only together
    with the default generator: an app given a generator (the offline `StubGenerator` in tests)
    makes no reference call unless an extractor is passed (e.g.
    `teacher.reference_extraction.StubReferenceExtractor()` offline); without one, extraction
    is reported as unavailable and every source's state stays "never run".

    `source_finder` finds candidate sources for a topic or concept (`POST
    /api/sources/discover`, `teacher.discovery`). Like `reference_extractor`, it defaults to
    `teacher.discovery.build_source_finder` (web search with the generation model, arXiv, and
    triage with the metadata model) only together with the default generator; an app given a
    generator has no finder unless one is passed, and the endpoint then answers 503.

    `explorer` runs explorations (`/api/explorations`, `teacher.exploration.Explorer`). It
    defaults to `teacher.exploration.build_explorer` (the generation model plans, the concept
    model judges the graph match, with the graph search's embedder `search_embedder` and the
    `source_finder`) only when the app has a source finder and no explorer is passed; without
    one, starting an exploration answers 503 (reading stored ones still works).

    `enricher` enriches a completed exploration (`teacher.exploration.Enricher`: its approved
    sources ingested with concept extraction, its outline written to the graph). It defaults to
    one built from the app's URL fetcher, metadata extractor, the generator's concept extractor
    and the explorer's graph matcher, when there is an explorer, the concept extractor has an
    embedder (the offline stub's has none) and no enricher is passed; the two must embed with
    the same model (otherwise there is none, and starting an enrichment answers 503 saying
    so). Without one, starting an enrichment answers 503.

    `access_code` puts the account gate in front of every route (`teacher.web.auth`): every
    page and API route then needs a signed-in user account, and registering an account needs
    this code (`teacher serve` passes `$TEACHER_PASSWORD`). None (the default): no gate, no
    accounts, the app is open. `GET /healthz` (an empty 200) exists either way.
    """
    if settings is None:
        settings = Settings.from_env()
    if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1:
        raise ValueError("max_attempts must be a positive integer")
    if generator is not None and (concept_extractor is not None or embedder is not None):
        raise ValueError(
            "concept_extractor and embedder only configure the default generator; pass them "
            "to the generator you pass instead"
        )
    if generator is not None and block_tagger is not None:
        raise ValueError(
            "block_tagger only configures the default generator; pass it to the generator you "
            "pass instead"
        )
    if lab_generator is not None and lab_runner is not None:
        raise ValueError(
            "lab_runner only configures the default lab generator; pass it to the "
            "lab_generator you pass instead"
        )
    if concept_extractor is not None and embedder is not None:
        raise ValueError(
            "embedder only builds the default concept extractor; give it to the "
            "concept_extractor you pass instead"
        )
    if (
        search_embedder is None
        and concept_relater is not None
        and getattr(concept_relater, "embedder", None) is None
    ):
        raise ValueError(
            "the concept_relater passed has no embedder for the graph search to share; pass "
            "search_embedder too"
        )
    if search_embedder is not None and not (
        isinstance(getattr(search_embedder, "model", None), str)
        and callable(getattr(search_embedder, "embed", None))
        and callable(getattr(search_embedder, "check_available", None))
    ):
        raise TypeError("search_embedder must have model, embed(texts) and check_available()")

    # Defaults need Anthropic credentials only when they call Claude (clients are lazy). The
    # models come from $TEACHER_GENERATION_MODEL, $TEACHER_JUDGE_MODEL,
    # $TEACHER_METADATA_MODEL, $TEACHER_TUTOR_MODEL, $TEACHER_CONCEPT_MODEL and
    # $TEACHER_EMBEDDING_MODEL (see ModelSettings).
    if review_bank_builder is None and generator is None:
        from teacher.review_bank import ClaudeReviewBankBuilder

        review_bank_builder = ClaudeReviewBankBuilder()
    if reference_extractor is None and generator is None:
        from teacher.reference_extraction import ReferenceExtractor

        reference_extractor = ReferenceExtractor(model=ModelSettings.from_env().metadata)
    if source_finder is None and generator is None:
        from teacher.discovery import build_source_finder

        source_finder = build_source_finder(ModelSettings.from_env())
    if clock is None:
        from teacher.db.models import utcnow

        clock = utcnow
    if not callable(clock):
        raise TypeError("clock must be a callable returning the current naive UTC datetime")

    if (
        generator is None
        or graders is None
        or metadata_extractor is None
        or tutor is None
        or glossary_builder is None
        or concept_relater is None
        or lab_generator is None
    ):
        models = ModelSettings.from_env()
        if generator is None:
            from teacher.claude_generator import ClaudeGenerator

            if concept_extractor is None:
                from teacher.concept_extraction import ConceptExtractor
                from teacher.embeddings import OpenAIEmbedder

                if embedder is None:
                    embedder = OpenAIEmbedder(models.embedding)
                concept_extractor = ConceptExtractor(model=models.concept, embedder=embedder)
            if block_tagger is None:
                from teacher.block_tagging import BlockTagger

                block_tagger = BlockTagger(model=models.concept)
            generator = ClaudeGenerator(
                model=models.generation,
                concept_extractor=concept_extractor,
                block_tagger=block_tagger,
                judge_model=models.judge,  # the question check grades as readers are graded
            )
        if graders is None:
            from teacher.grading import default_graders

            graders = default_graders(judge_model=models.judge)
        if metadata_extractor is None:
            from teacher.metadata import MetadataExtractor

            metadata_extractor = MetadataExtractor(model=models.metadata)
        if tutor is None:
            from teacher.tutor import Tutor

            tutor = Tutor(model=models.tutor)
        if glossary_builder is None:
            from teacher.claude_generator import GlossaryBuilder

            glossary_builder = GlossaryBuilder(model=models.generation)
        if concept_relater is None:
            from teacher.concept_relating import ConceptRelater
            from teacher.embeddings import OpenAIEmbedder

            concept_relater = ConceptRelater(
                model=models.concept, embedder=OpenAIEmbedder(models.embedding)
            )
        if lab_generator is None:
            from teacher.lab_generator import ClaudeLabGenerator

            if lab_runner is None:
                from teacher.pyodide_runtime import PythonRunner

                lab_runner = PythonRunner(settings)
            lab_generator = ClaudeLabGenerator(models.generation, runner=lab_runner)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # uvicorn's server logger, which prints its own startup lines at INFO.
        logging.getLogger("uvicorn.error").info(
            "Database: %s", create_db_provider(app.state.settings).describe()
        )
        upgrade_to_head(app.state.settings)
        fail_interrupted_generations(app.state.settings)
        fail_interrupted_runs(app.state.settings)
        fail_interrupted_explorations(app.state.settings)
        fail_interrupted_enrichments(app.state.settings)
        yield

    app = FastAPI(title="Teacher", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.generator = generator
    app.state.lab_generator = lab_generator
    app.state.lab_runner = getattr(lab_generator, "runner", None)
    app.state.graders = graders
    app.state.metadata_extractor = metadata_extractor
    app.state.tutor = tutor
    app.state.glossary_builder = glossary_builder
    app.state.concept_extractor = getattr(generator, "concept_extractor", None)
    app.state.max_attempts = max_attempts
    app.state.url_fetcher = url_fetcher if url_fetcher is not None else UrlFetcher()
    app.state.chat_replies = chat.ReplyRegistry()
    app.state.concept_relater = concept_relater
    app.state.search_embedder = (
        search_embedder if search_embedder is not None else concept_relater.embedder
    )
    app.state.relating = concepts.RunningSet()
    app.state.review_bank_builder = review_bank_builder
    app.state.reference_extractor = reference_extractor
    app.state.source_finder = source_finder
    if explorer is None and source_finder is not None:
        from teacher.exploration import build_explorer

        explorer = build_explorer(
            ModelSettings.from_env(), finder=source_finder, embedder=app.state.search_embedder
        )
    app.state.explorer = explorer
    app.state.enricher_unavailable = None
    if (
        enricher is None
        and explorer is not None
        and getattr(app.state.concept_extractor, "embedder", None) is not None
    ):
        from teacher.exploration import build_enricher

        try:
            enricher = build_enricher(
                fetcher=app.state.url_fetcher, metadata_extractor=metadata_extractor,
                concept_extractor=app.state.concept_extractor, matcher=explorer.matcher,
            )  # fmt: skip
        except ValueError as exc:  # e.g. two embedding models: say why on every start
            app.state.enricher_unavailable = str(exc)
    app.state.enricher = enricher
    app.state.clock = clock

    install_error_handlers(app)
    app.add_middleware(CostMeterMiddleware, settings=settings)

    @app.get("/api/health", tags=["meta"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(discovery.router)
    app.include_router(explorations.router)
    app.include_router(sources.router)
    app.include_router(artifacts.router)
    app.include_router(reading.router)
    app.include_router(offline.router)
    app.include_router(highlights.router)
    app.include_router(offline_review.router)
    app.include_router(phone.router)
    app.include_router(chat.router)
    app.include_router(glossary.router)
    app.include_router(concepts.router)
    # The runtime before the labs: its GET /api/labs/runtime must win over GET /api/labs/{slug}.
    app.include_router(pyodide.router)
    app.include_router(labs.router)
    app.include_router(history.router)
    app.include_router(library_search.router)
    app.include_router(review.router)
    app.include_router(source_graph.router)
    app.include_router(references.router)

    # --- pages: server-rendered HTML front end (page routes + /static) ---
    from teacher.web.routers import pages

    app.mount(pages.STATIC_URL, pages.static_files(), name="static")
    app.include_router(pages.router)
    app.include_router(review.page_router)
    # --- end pages ---

    # Last, so its middleware is the outermost: a request without a session goes no further.
    install_auth_gate(app, access_code, settings)

    return app


__all__ = ["create_app", "fail_interrupted_generations", "fail_interrupted_runs"]
