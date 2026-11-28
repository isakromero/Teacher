"""The finder: providers in parallel, then the registry, the probe and triage, ranked.

`SourceFinder.find(query, library=catalog)` runs one discovery and returns a `DiscoveryResult`:

1. Every provider searches at the same time (`provider_deadline` seconds at most). A provider
   that raises `ProviderError` or does not finish in time is recorded with its error in
   `result.providers`, next to the others; if every provider failed, `find` raises
   `AllProvidersFailedError` naming each error. Any other exception is a bug and is raised.
2. The hits are merged (`registry.merge_hits`; hits are taken rank by rank across the
   providers, so the order of first sighting interleaves them) and `max_candidates` are kept
   (`registry.select_candidates`); the rest are listed in `result.not_kept`, never dropped
   unseen. Each kept candidate is matched against the library (`LibraryCatalog.match`): a
   candidate already in the library is marked, not removed.
3. Every kept candidate is probed (`probe.probe_all`).
4. The readable ones are scored in one triage call (`triage.Triager`), skipped (with a note)
   when none is readable. A triage failure fails the whole call (`TriageError`).
5. Ranking: the scored candidates by `overall` score (highest first; ties by the number of
   providers that found them, then their best provider rank), then the unreadable ones in the
   order they were kept. `rank` counts from 1.

Nothing is ingested: adding a candidate is the existing add-from-URL flow, started by the
reader. Every model call is recorded in the cost ledger by its call site, under the caller's
cost scope (`teacher.costs`; the providers run in threads of their own, each in a copy of the
caller's context, so they record to the caller's meter); the run's calls are also in
`result.usage` (`teacher.discovery.common`), and a failure's message states what was spent. A
failure after the providers ran carries their statuses (`DiscoveryError.providers`).

There is no estimate of what a full study page from a candidate would cost: Teacher has no
such estimate before a generation runs (`teacher.costs` totals what was spent), so the result
leaves it out (`DiscoveryResult.page_cost_estimate` is always None).

`find_sources(settings, finder, query)` loads the library in a short session of its own and
calls `find` (no session is open while the providers and models run). `concept_query(settings,
slug, level)` builds the query for a concept of the graph. `build_source_finder(models)` builds
the default finder: `WebSearchProvider` with the generation model, `ArxivProvider`, a
`UrlFetcher` with `probe.PROBE_FETCH_TIMEOUT`, and `Triager` with the metadata model.
"""

from __future__ import annotations

import contextvars
import time
from collections.abc import Sequence
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Any

from teacher.config import ModelSettings, Settings
from teacher.db.engine import session_scope
from teacher.discovery.common import (
    AllProvidersFailedError,
    DiscoveryError,
    DiscoveryQuery,
    DiscoveryUsage,
    ModelCall,
    ProviderError,
)
from teacher.discovery.probe import (
    PROBE_DEADLINE,
    PROBE_FETCH_TIMEOUT,
    PROBE_WORKERS,
    Probe,
    probe_all,
)
from teacher.discovery.providers import ProviderOutput, SourceProvider
from teacher.discovery.registry import (
    FoundBy,
    LibraryCatalog,
    LibraryMatch,
    MergedCandidate,
    ProviderHit,
    merge_hits,
    select_candidates,
)
from teacher.discovery.triage import TriageError, TriageItem, Triager, TriageScore

MAX_CANDIDATES = 8
PROVIDER_DEADLINE = 300.0


@dataclass(frozen=True)
class ProviderStatus:
    """How one provider did: `ok` with its `hits`, or the `error` that stopped it. `calls` are
    its model calls (in the run's usage too)."""

    name: str
    ok: bool
    hits: int
    error: str | None
    warnings: tuple[str, ...]
    detail: str
    seconds: float
    calls: tuple[ModelCall, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "hits": self.hits,
            "error": self.error,
            "warnings": list(self.warnings),
            "detail": self.detail,
            "seconds": round(self.seconds, 2),
        }


@dataclass(frozen=True)
class Candidate:
    """One ranked candidate. `url` is what the probe fetched and what adding it would fetch;
    `landing_url` the address a provider gave when it was rewritten to its full text. `found_by`
    lists each provider with its reason; `library` the library sources it already is (empty:
    not in the library); `triage` is None when it was not readable (see `probe`)."""

    id: str
    rank: int
    url: str
    landing_url: str | None
    title: str
    found_by: tuple[FoundBy, ...]
    arxiv_id: str | None
    doi: str | None
    authors: tuple[str, ...]
    year: int | None
    summary: str | None
    library: tuple[LibraryMatch, ...]
    probe: Probe
    triage: TriageScore | None

    @property
    def providers(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(f.provider for f in self.found_by))

    @property
    def in_library(self) -> bool:
        return bool(self.library)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "rank": self.rank,
            "url": self.url,
            "landing_url": self.landing_url,
            "title": self.title,
            "providers": list(self.providers),
            "found_by": [f.to_dict() for f in self.found_by],
            "arxiv_id": self.arxiv_id,
            "doi": self.doi,
            "authors": list(self.authors),
            "year": self.year,
            "summary": self.summary,
            "in_library": self.in_library,
            "library": [m.to_dict() for m in self.library],
            "probe": self.probe.to_dict(),
            "triage": self.triage.to_dict() if self.triage is not None else None,
        }


@dataclass(frozen=True)
class NotKept:
    """A distinct candidate beyond `max_candidates`: neither probed nor scored."""

    id: str
    url: str
    title: str
    providers: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "url": self.url, "title": self.title, "providers": list(self.providers)}


@dataclass(frozen=True)
class TriageStatus:
    ran: bool
    model: str | None
    scored: int
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"ran": self.ran, "model": self.model, "scored": self.scored, "note": self.note}


@dataclass(frozen=True)
class DiscoveryResult:
    """One discovery run (see the module docstring)."""

    query: DiscoveryQuery
    candidates: tuple[Candidate, ...]
    providers: tuple[ProviderStatus, ...]
    hits: int
    distinct: int
    not_kept: tuple[NotKept, ...]
    triage: TriageStatus
    usage: DiscoveryUsage
    seconds: float
    page_cost_estimate: None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query.to_dict(),
            "candidates": [c.to_dict() for c in self.candidates],
            "providers": [p.to_dict() for p in self.providers],
            "hits": self.hits,
            "distinct": self.distinct,
            "not_kept": [n.to_dict() for n in self.not_kept],
            "triage": self.triage.to_dict(),
            "usage": self.usage.to_dict(),
            "seconds": round(self.seconds, 2),
            "page_cost_estimate": None,
        }

    def summary_lines(self) -> list[str]:
        """The result as text (`teacher discover`)."""
        q = self.query
        lines = [
            f"Sources for {q.topic!r} (level {q.level}): {len(self.candidates)} candidate(s) from "
            f"{self.hits} hit(s), {self.distinct} distinct"
            + (f", {len(self.not_kept)} not kept" if self.not_kept else "")
            + "."
        ]
        for status in self.providers:
            if status.ok:
                text = f"  {status.name}: {status.hits} hit(s)" + (f" ({status.detail})" if status.detail else "")
            else:
                text = f"  {status.name}: FAILED: {status.error}"
            lines.append(text)
            lines.extend(f"    warning: {w}" for w in status.warnings)
        if self.triage.note:
            lines.append(f"  triage: {self.triage.note}")
        for c in self.candidates:
            score = f"[{c.triage.overall:.1f}] " if c.triage is not None else "[--] "
            lines.append(f"{c.rank:2}. {score}{c.title}")
            lines.append(f"    {c.url}" + (f" (from {c.landing_url})" if c.landing_url else ""))
            lines.append(f"    {c.probe.verdict}: {c.probe.reason}")
            if c.triage is not None:
                t = c.triage
                lines.append(
                    f"    {t.treatment} (fit {t.topic_fit}, level {t.level_match}, teaching "
                    f"{t.teaching}): {t.reason}"
                )
            for found in c.found_by:
                lines.append(f"    {found.provider} #{found.rank}: {found.reason}")
            if c.library:
                held = ", ".join(f"{m.slug} (by {m.method})" for m in c.library)
                lines.append(f"    already in the library: {held}")
        for n in self.not_kept:
            lines.append(f"  not kept: {n.title} <{n.url}> ({', '.join(n.providers)})")
        usage = self.usage
        cost = f"Cost: ${usage.total_usd:.4f} in {len(usage.calls)} model call(s)"
        if usage.unpriced_models:
            cost += f", plus calls with no known price ({', '.join(usage.unpriced_models)})"
        if usage.web_search_requests:
            cost += f"; {usage.web_search_requests} web search(es), not priced"
        lines.append(cost + "; recorded in the cost ledger.")
        return lines


def spent(calls: Sequence[ModelCall]) -> str:
    """What the calls cost, for an error message."""
    if not calls:
        return "no model call was made"
    usage = DiscoveryUsage.of(calls)
    text = f"{len(calls)} model call(s) costing ${usage.total_usd:.4f}"
    if usage.unpriced_models:
        text += f" plus calls with no known price ({', '.join(usage.unpriced_models)})"
    if usage.web_search_requests:
        text += f" and {usage.web_search_requests} web search(es), not priced"
    return text + "; recorded in the cost ledger"


class SourceFinder:
    """Finds candidate sources for a query (see the module docstring). `fetcher` is what the
    probe fetches with (`teacher.url_sources.UrlFetcher` or a fake with `fetch(url)`), `triager`
    a `Triager` or a fake with `triage(query, items) -> TriageOutcome`."""

    def __init__(
        self,
        providers: Sequence[SourceProvider],
        *,
        fetcher: Any,
        triager: Any,
        max_candidates: int = MAX_CANDIDATES,
        probe_workers: int = PROBE_WORKERS,
        probe_deadline: float = PROBE_DEADLINE,
        provider_deadline: float = PROVIDER_DEADLINE,
    ) -> None:
        if not providers:
            raise ValueError("a SourceFinder needs at least one provider")
        names = [p.name for p in providers]
        if len(set(names)) != len(names):
            raise ValueError(f"provider names must be distinct, got {names}")
        if max_candidates < 1:
            raise ValueError("max_candidates must be at least 1")
        self.providers = tuple(providers)
        self.fetcher = fetcher
        self.triager = triager
        self.max_candidates = max_candidates
        self.probe_workers = probe_workers
        self.probe_deadline = probe_deadline
        self.provider_deadline = provider_deadline

    # -- 1. the providers -------------------------------------------------------------------

    def _run_providers(self, query: DiscoveryQuery) -> list[tuple[ProviderStatus, ProviderOutput | None]]:
        pool = ThreadPoolExecutor(max_workers=len(self.providers), thread_name_prefix="provider")
        try:
            # Each provider runs in a copy of this context, so its calls are recorded by the
            # caller's cost meter (`teacher.costs`); a Context cannot be shared by two threads.
            futures: list[Future[tuple[ProviderOutput | None, ProviderError | None, float]]] = [
                pool.submit(contextvars.copy_context().run, _search, provider, query)
                for provider in self.providers
            ]
            wait(futures, timeout=self.provider_deadline)
            results: list[tuple[ProviderStatus, ProviderOutput | None]] = []
            for provider, future in zip(self.providers, futures, strict=True):
                if not future.done():
                    future.cancel()
                    results.append(
                        (
                            ProviderStatus(
                                provider.name, False, 0,
                                f"did not finish within {self.provider_deadline:g} seconds",
                                (), "", self.provider_deadline,
                            ),
                            None,
                        )
                    )  # fmt: skip
                    continue
                output, error, seconds = future.result()  # anything but ProviderError: raised
                if error is not None:
                    results.append(
                        (
                            ProviderStatus(
                                provider.name, False, 0, str(error), (), "", seconds, error.calls
                            ),
                            None,
                        )
                    )
                    continue
                assert output is not None
                results.append(
                    (
                        ProviderStatus(
                            provider.name, True, len(output.hits), None, output.warnings,
                            output.detail, seconds, output.calls,
                        ),
                        output,
                    )
                )  # fmt: skip
            return results
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

    # -- the whole run ----------------------------------------------------------------------

    def find(self, query: DiscoveryQuery, *, library: LibraryCatalog) -> DiscoveryResult:
        started = time.monotonic()
        ran = self._run_providers(query)
        statuses = tuple(status for status, _ in ran)
        calls: list[ModelCall] = [call for status in statuses for call in status.calls]
        if not any(status.ok for status in statuses):
            failures = "; ".join(f"{s.name}: {s.error}" for s in statuses)
            raise AllProvidersFailedError(
                f"No sources could be found: every provider failed ({failures}). Spent: "
                f"{spent(calls)}.",
                calls=calls,
                providers=statuses,
            )

        outputs = [output for _, output in ran if output is not None]
        hits: list[ProviderHit] = sorted(
            (hit for output in outputs for hit in output.hits),
            key=lambda h: (h.rank, [p.name for p in self.providers].index(h.provider)),
        )
        merged = merge_hits(hits)
        kept, rest = select_candidates(merged, self.max_candidates)
        matches = [library.match(candidate) for candidate in kept]
        probes = probe_all(
            self.fetcher,
            [candidate.url for candidate in kept],
            workers=self.probe_workers,
            deadline=self.probe_deadline,
        )

        readable = [i for i, p in enumerate(probes) if p.readable]
        scores: dict[int, TriageScore] = {}
        if readable:
            items = [
                TriageItem(
                    label=f"C{n}",
                    url=kept[i].url,
                    title=kept[i].title,
                    kind=probes[i].kind or "web_page",
                    words=probes[i].words or 0,
                    opening=probes[i].opening or "",
                )
                for n, i in enumerate(readable, start=1)
            ]
            try:
                outcome = self.triager.triage(query, items)
            except TriageError as exc:
                calls.extend(exc.calls)
                raise type(exc)(
                    f"{exc} Spent: {spent(calls)}.", calls=calls, providers=statuses
                ) from exc
            calls.append(outcome.call)
            for n, i in enumerate(readable, start=1):
                scores[i] = outcome.scores[f"C{n}"]
            triage = TriageStatus(True, outcome.call.model, len(scores))
        else:
            triage = TriageStatus(
                False, None, 0,
                "No candidate was readable, so none was scored." if kept else "Nothing was found to score.",
            )  # fmt: skip

        def order(i: int) -> tuple[Any, ...]:
            candidate = kept[i]
            if i in scores:
                return (0, -scores[i].overall, -len(candidate.providers), candidate.best_rank, i)
            return (1, i)

        ranked = sorted(range(len(kept)), key=order)
        candidates = tuple(
            _candidate(kept[i], rank, matches[i], probes[i], scores.get(i))
            for rank, i in enumerate(ranked, start=1)
        )
        return DiscoveryResult(
            query=query,
            candidates=candidates,
            providers=statuses,
            hits=len(hits),
            distinct=len(merged),
            not_kept=tuple(NotKept(c.id, c.url, c.title, c.providers) for c in rest),
            triage=triage,
            usage=DiscoveryUsage.of(calls),
            seconds=time.monotonic() - started,
        )


def _search(
    provider: SourceProvider, query: DiscoveryQuery
) -> tuple[ProviderOutput | None, ProviderError | None, float]:
    """(output, None, seconds) or (None, the ProviderError, seconds)."""
    started = time.monotonic()
    try:
        return provider.search(query), None, time.monotonic() - started
    except ProviderError as exc:
        return None, exc, time.monotonic() - started


def _candidate(
    merged: MergedCandidate,
    rank: int,
    library: tuple[LibraryMatch, ...],
    probe: Probe,
    triage: TriageScore | None,
) -> Candidate:
    return Candidate(
        id=merged.id,
        rank=rank,
        url=merged.url,
        landing_url=merged.landing_url,
        title=merged.title,
        found_by=tuple(merged.found_by),
        arxiv_id=merged.arxiv_id,
        doi=merged.doi,
        authors=merged.authors,
        year=merged.year,
        summary=merged.summary,
        library=library,
        probe=probe,
        triage=triage,
    )


# -- entry points -----------------------------------------------------------------------------


def find_sources(settings: Settings, finder: SourceFinder, query: DiscoveryQuery) -> DiscoveryResult:
    """Load the library (a short session of its own), then `finder.find(query, ...)`."""
    with session_scope(settings) as session:
        catalog = LibraryCatalog.load(session)
    return finder.find(query, library=catalog)


def concept_query(settings: Settings, slug: str, level: str) -> DiscoveryQuery:
    """The query for the concept `slug` (NotFoundError when there is none)."""
    from teacher.managers import ConceptManager

    with session_scope(settings) as session:
        concept = ConceptManager(session, settings).get_by_slug(slug)
        return DiscoveryQuery.for_concept(concept, level)


def build_source_finder(
    models: ModelSettings,
    *,
    fetcher: Any = None,
    client: Any = None,
    arxiv_client: Any = None,
) -> SourceFinder:
    """The default finder (see the module docstring). `client` is the Anthropic client of both
    model calls (default: created on first use); `fetcher` the probe's fetcher (default: a
    `UrlFetcher` with `PROBE_FETCH_TIMEOUT`); `arxiv_client` arXiv's HTTP client."""
    from teacher.discovery.providers import ArxivProvider, WebSearchProvider
    from teacher.url_sources import UrlFetcher

    return SourceFinder(
        [WebSearchProvider(client, model=models.generation), ArxivProvider(arxiv_client)],
        fetcher=fetcher if fetcher is not None else UrlFetcher(timeout=PROBE_FETCH_TIMEOUT),
        triager=Triager(client, model=models.metadata),
    )


__all__ = [
    "MAX_CANDIDATES",
    "Candidate",
    "DiscoveryError",
    "DiscoveryResult",
    "NotKept",
    "ProviderStatus",
    "SourceFinder",
    "TriageStatus",
    "build_source_finder",
    "concept_query",
    "find_sources",
    "spent",
]
