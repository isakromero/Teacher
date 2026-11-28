"""The discovery stage of an exploration: candidate sources for the concepts that need them,
with milestone 1's source finder (`teacher.discovery.SourceFinder`).

Which concepts (`needs_sources`, the thin-coverage rule): a concept that is `new` to the graph,
and a `known` one that is thinly covered, i.e. no source of the library is linked to its graph
concept with role `introduces` (`source_concepts`, written by concept extraction: a source
"introduces" a concept it explains as its own new idea; one that only uses or assumes it does
not teach it). A known concept that some library source introduces is `covered` and is not
searched.

Per concept (`search_target`), in a thread of its own (at most `DISCOVERY_WORKERS` at a time,
each in a copy of the caller's context, so every model call is recorded under the exploration's
cost scope):

1. The first search: the concept's name, aliases and one-line summary at the reader's level
   (`first_query`).
2. If that search completed and no candidate it found fits (`fits`: readable, triaged as
   teaching the topic, with a topic fit of at least `FIT_MIN_TOPIC`), one follow-up search
   with the outline's differently worded `search_query` (`followup_query`); never more
   (`MAX_SEARCHES_PER_CONCEPT`). A search that failed (every provider failing, triage failing)
   is not retried: it is the concept's error, shown with it, and the run goes on.
3. The candidates of both searches, the same work once (`work_keys`), ranked: the ones that fit
   first, then the other scored ones, both by their overall triage score, then the unreadable
   ones in the order found; at most `MAX_CANDIDATES_PER_CONCEPT` are kept. The ones left out
   are counted in the concept's log, never dropped unseen.

Deduplication across the whole exploration happens when each concept's outcome is stored
(`teacher.managers.explorations.ExplorationManager.record_discovery`): the same work found for
two concepts is one candidate linked to both.

The searched and fetched text stays inside milestone 1: the web search reads it in its own
tool loop, triage reads it fenced as untrusted data in its user turn
(`teacher.discovery.untrusted`), and nothing of it reaches any other prompt; the follow-up query
comes from the outline, not from what the first search found.
"""

from __future__ import annotations

import contextvars
import time
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any

from teacher.discovery import (
    Candidate,
    DiscoveryError,
    DiscoveryQuery,
    DiscoveryResult,
    LibraryCatalog,
    ProviderStatus,
    SourceFinder,
)
from teacher.discovery.registry import canonical_url
from teacher.exploration.common import MAX_CANDIDATES_PER_CONCEPT, MAX_SEARCHES_PER_CONCEPT
from teacher.managers.explorations import work_keys

FIT_MIN_TOPIC = 3
DISCOVERY_WORKERS = 3
INTRODUCES = "introduces"


def coverage_of(concept_id: int | None, introducing_sources: int) -> str:
    """`new` (no graph concept), `thin` (no library source introduces it) or `covered`."""
    if concept_id is None:
        return "new"
    return "covered" if introducing_sources > 0 else "thin"


def needs_sources(coverage: str) -> bool:
    return coverage in ("new", "thin")


def fits(candidate: Candidate) -> bool:
    """Whether a candidate fits its concept: readable and triaged as teaching it, with a topic
    fit of at least `FIT_MIN_TOPIC`."""
    t = candidate.triage
    return (
        candidate.probe.readable
        and t is not None
        and t.treatment == "teaches"
        and t.topic_fit >= FIT_MIN_TOPIC
    )


@dataclass(frozen=True)
class SearchTarget:
    """A concept to find sources for: its outline key, name, aliases, summary, the outline's
    follow-up query, and why it is searched (`coverage`: new or thin)."""

    key: str
    name: str
    aliases: tuple[str, ...]
    summary: str
    search_query: str
    coverage: str


def first_query(target: SearchTarget, level: str) -> DiscoveryQuery:
    return DiscoveryQuery(
        topic=target.name, level=level, aliases=target.aliases, summary=target.summary
    )


def followup_query(target: SearchTarget, level: str) -> DiscoveryQuery:
    return DiscoveryQuery(
        topic=target.search_query,
        level=level,
        summary=f"{target.name}: {target.summary}",
    )


@dataclass(frozen=True)
class Attempt:
    """One search of a concept: its number (1 or 2), the query, the result or the error, the
    providers' statuses (from either), why it ran (a follow-up's reason) and how long it took."""

    number: int
    query: DiscoveryQuery
    result: DiscoveryResult | None
    error: str | None
    providers: tuple[ProviderStatus, ...]
    reason: str
    seconds: float

    def log(self, kept: int) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "attempt": self.number,
            "query": self.query.topic,
            "reason": self.reason,
            "error": self.error,
            "seconds": round(self.seconds, 2),
            "providers": [p.to_dict() for p in self.providers],
        }
        if self.result is not None:
            r = self.result
            entry.update(
                hits=r.hits,
                distinct=r.distinct,
                candidates=len(r.candidates),
                fitting=sum(1 for c in r.candidates if fits(c)),
                kept=kept,
                not_kept=[n.to_dict() for n in r.not_kept],
                triage_note=r.triage.note,
                cost_usd=round(r.usage.total_usd, 6),
                web_search_requests=r.usage.web_search_requests,
            )
        return entry


@dataclass(frozen=True)
class Ranked:
    candidate: Candidate
    attempt: int
    fits: bool


@dataclass(frozen=True)
class TargetOutcome:
    """What discovery did for one concept: its attempts, its ranked candidates (at most
    `MAX_CANDIDATES_PER_CONCEPT`), how many more were found and left out, and its error (the
    failed search's; None when every search it ran completed)."""

    target: SearchTarget
    attempts: tuple[Attempt, ...]
    ranked: tuple[Ranked, ...]
    left_out: int
    error: str | None

    def searches(self) -> list[dict[str, Any]]:
        """Its provider calls, as `ExplorationManager.record_discovery` takes them."""
        rows = []
        for attempt in self.attempts:
            for status in attempt.providers:
                rows.append(
                    {
                        "attempt": attempt.number,
                        "query": attempt.query.topic,
                        "query_detail": attempt.query.to_dict(),
                        "provider": status.name,
                        "outcome": "ok" if status.ok else "failed",
                        "hits": status.hits,
                        "error": None if status.ok else (status.error or "failed"),
                        "warnings": list(status.warnings),
                        "seconds": status.seconds,
                    }
                )
        return rows

    def candidates(self) -> list[dict[str, Any]]:
        """Its ranked candidates, as `ExplorationManager.record_discovery` takes them."""
        return [_candidate_record(r) for r in self.ranked]

    def log(self) -> list[dict[str, Any]]:
        kept = {a.number: sum(1 for r in self.ranked if r.attempt == a.number) for a in self.attempts}
        entries = [a.log(kept[a.number]) for a in self.attempts]
        if self.left_out:
            entries.append({"left_out": self.left_out,
                            "note": f"{self.left_out} more candidate(s) were found and ranked "
                                    f"below the {MAX_CANDIDATES_PER_CONCEPT} kept."})  # fmt: skip
        return entries


def candidate_urls(candidate: Candidate) -> list[str]:
    """Its canonical addresses: the fetched one first, then the landing page's."""
    urls = [canonical_url(candidate.url)]
    if candidate.landing_url:
        landing = canonical_url(candidate.landing_url)
        if landing not in urls:
            urls.append(landing)
    return urls


def _keys(candidate: Candidate) -> set[str]:
    return work_keys(
        candidate_key=candidate.id, arxiv_id=candidate.arxiv_id, doi=candidate.doi,
        urls=candidate_urls(candidate),
    )  # fmt: skip


def _candidate_record(ranked: Ranked) -> dict[str, Any]:
    c = ranked.candidate
    return {
        "candidate_key": c.id,
        "url": c.url,
        "canonical_urls": candidate_urls(c),
        "landing_url": c.landing_url,
        "arxiv_id": c.arxiv_id,
        "doi": c.doi,
        "title": c.title,
        "authors": list(c.authors),
        "year": c.year,
        "summary": c.summary,
        "found_by": [f.to_dict() for f in c.found_by],
        "verdict": c.probe.verdict,
        "verdict_reason": c.probe.reason,
        "probe": c.probe.to_dict(),
        "library": [m.to_dict() for m in c.library],
        "attempt": ranked.attempt,
        "triage": c.triage.to_dict() if c.triage is not None else None,
        "overall": c.triage.overall if c.triage is not None else None,
        "fits": ranked.fits,
    }


def _run_attempt(
    finder: SourceFinder,
    catalog: LibraryCatalog,
    number: int,
    query: DiscoveryQuery,
    reason: str,
) -> Attempt:
    started = time.monotonic()
    try:
        result = finder.find(query, library=catalog)
    except DiscoveryError as exc:
        return Attempt(
            number, query, None, str(exc), tuple(exc.providers), reason,
            time.monotonic() - started,
        )  # fmt: skip
    return Attempt(number, query, result, None, result.providers, reason, time.monotonic() - started)


def rank(attempts: Sequence[Attempt]) -> tuple[list[Ranked], int]:
    """The candidates of `attempts`, each work once (first sighting kept), ranked (see the
    module docstring) and cut to `MAX_CANDIDATES_PER_CONCEPT`; and how many were cut."""
    seen: list[tuple[set[str], Ranked]] = []
    for attempt in attempts:
        if attempt.result is None:
            continue
        for candidate in attempt.result.candidates:
            keys = _keys(candidate)
            if any(keys & other for other, _ in seen):
                continue
            seen.append((keys, Ranked(candidate, attempt.number, fits(candidate))))
    items = [ranked for _, ranked in seen]

    def order(i: int) -> tuple[Any, ...]:
        r = items[i]
        t = r.candidate.triage
        if r.fits:
            return (0, -t.overall, i)  # type: ignore[union-attr]
        if t is not None:
            return (1, -t.overall, i)
        return (2, i)

    ordered = [items[i] for i in sorted(range(len(items)), key=order)]
    kept = ordered[:MAX_CANDIDATES_PER_CONCEPT]
    return kept, len(ordered) - len(kept)


def search_target(
    finder: SourceFinder, catalog: LibraryCatalog, target: SearchTarget, level: str
) -> TargetOutcome:
    """The first search, at most one follow-up, and the ranked candidates (module docstring)."""
    why = "new to the concept graph" if target.coverage == "new" else (
        "in the graph, but no library source introduces it"
    )
    attempts = [_run_attempt(finder, catalog, 1, first_query(target, level), f"first search: {why}")]
    first = attempts[0]
    error = first.error
    if (
        first.result is not None
        and MAX_SEARCHES_PER_CONCEPT > 1
        and not any(fits(c) for c in first.result.candidates)
    ):
        readable = sum(1 for c in first.result.candidates if c.probe.readable)
        reason = (
            f"follow-up: the first search found {len(first.result.candidates)} candidate(s), "
            f"{readable} readable, none of them fitting (readable, teaching the concept, topic "
            f"fit at least {FIT_MIN_TOPIC} of 5)"
        )
        second = _run_attempt(finder, catalog, 2, followup_query(target, level), reason)
        attempts.append(second)
        if second.error is not None:
            error = f"the follow-up search failed: {second.error}"
    elif first.error is not None:
        error = f"the search failed: {first.error}"
    ranked, left_out = rank(attempts)
    return TargetOutcome(target, tuple(attempts), tuple(ranked), left_out, error)


def discover(
    finder: SourceFinder,
    catalog: LibraryCatalog,
    targets: Sequence[SearchTarget],
    level: str,
    *,
    on_done: Callable[[TargetOutcome], None],
    workers: int = DISCOVERY_WORKERS,
) -> list[TargetOutcome]:
    """Search every target (in threads, at most `workers` at once) and call `on_done` with each
    outcome as it finishes, in this thread (so the caller stores it). An exception other than a
    search's `DiscoveryError` (a bug) cancels the targets not yet started and is raised."""
    if not targets:
        return []
    outcomes: list[TargetOutcome] = []
    pool = ThreadPoolExecutor(max_workers=max(1, min(workers, len(targets))),
                              thread_name_prefix="explore-discovery")  # fmt: skip
    try:
        futures: list[Future[TargetOutcome]] = [
            pool.submit(contextvars.copy_context().run, search_target, finder, catalog, t, level)
            for t in targets
        ]
        for future in as_completed(futures):
            outcome = future.result()
            on_done(outcome)
            outcomes.append(outcome)
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
    return outcomes


__all__ = [
    "DISCOVERY_WORKERS",
    "FIT_MIN_TOPIC",
    "INTRODUCES",
    "Attempt",
    "Ranked",
    "SearchTarget",
    "TargetOutcome",
    "candidate_urls",
    "coverage_of",
    "discover",
    "first_query",
    "fits",
    "followup_query",
    "needs_sources",
    "rank",
    "search_target",
]
