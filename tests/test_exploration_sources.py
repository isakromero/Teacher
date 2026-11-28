"""teacher.exploration.sources: the thin-coverage rule, what fits, the single follow-up search
(only when nothing readable fits, never after a failed search), the per-concept cap and ranking,
deduplication within a concept and across the whole exploration (one candidate linked to two
concepts), and failures that stay with their concept. The source finder is made of fakes."""

from __future__ import annotations

from typing import Any

import pytest

from teacher.db.engine import session_scope
from teacher.discovery import DiscoveryQuery, LibraryCatalog, ProviderError, TriageError
from teacher.exploration.sources import (
    FIT_MIN_TOPIC,
    SearchTarget,
    coverage_of,
    discover,
    fits,
    needs_sources,
    search_target,
)
from teacher.managers import ExplorationManager
from tests.discovery_fakes import FakeTriager, hit, web_page
from tests.exploration_fakes import finder

EMPTY = LibraryCatalog()


def target(name: str = "Modular arithmetic", **kw: Any) -> SearchTarget:
    return SearchTarget(
        key=kw.pop("key", name.lower().replace(" ", "-")), name=name, aliases=kw.pop("aliases", ()),
        summary=f"What {name} is.", search_query=kw.pop("search_query", f"{name} lecture notes"),
        coverage=kw.pop("coverage", "new"),
    )  # fmt: skip


def test_the_thin_coverage_rule() -> None:
    assert coverage_of(None, 0) == "new"
    assert coverage_of(7, 0) == "thin"  # in the graph, no library source introduces it
    assert coverage_of(7, 2) == "covered"
    assert [needs_sources(c) for c in ("new", "thin", "covered")] == [True, True, False]


def test_what_fits() -> None:
    url = "https://a.example/1"

    def found(**kw: Any) -> Any:
        src, _ = finder({"T": [hit("web", 1, url)]}, **kw)
        (c,) = src.find(DiscoveryQuery("T"), library=EMPTY).candidates
        return c

    assert fits(found(score=lambda item: (FIT_MIN_TOPIC, 1, 1)))
    assert not fits(found(score=lambda item: (FIT_MIN_TOPIC - 1, 5, 5)))
    unreadable = found(pages={url: web_page(url, 50)})
    assert not unreadable.probe.readable and not fits(unreadable)


def test_no_follow_up_when_a_candidate_fits() -> None:
    src, provider = finder({"Modular arithmetic": [hit("web", 1, "https://a.example/ma")]})
    outcome = search_target(src, EMPTY, target(), "intro")
    assert provider.topics == ["Modular arithmetic"]
    assert len(outcome.attempts) == 1 and outcome.error is None
    assert [(r.candidate.url, r.attempt, r.fits) for r in outcome.ranked] == [("https://a.example/ma", 1, True)]
    q = provider.queries[0]
    assert (q.level, q.summary) == ("intro", "What Modular arithmetic is.")


def test_one_follow_up_with_the_reformulated_query_when_nothing_readable_fits() -> None:
    thin, good = "https://a.example/thin", "https://b.example/good"
    src, provider = finder(
        {"Modular arithmetic": [hit("web", 1, thin)], "Modular arithmetic lecture notes": [hit("web", 1, good)]},
        pages={thin: web_page(thin, 50), good: web_page(good)},
    )
    outcome = search_target(src, EMPTY, target(), "intro")
    assert provider.topics == ["Modular arithmetic", "Modular arithmetic lecture notes"]
    assert [a.number for a in outcome.attempts] == [1, 2]
    assert outcome.attempts[1].reason.startswith("follow-up: the first search found 1 candidate(s), 0 readable")
    # The fitting one first, then the unreadable one.
    assert [(r.candidate.url, r.attempt, r.fits) for r in outcome.ranked] == [(good, 2, True), (thin, 1, False)]
    follow = provider.queries[1]
    assert follow.summary == "Modular arithmetic: What Modular arithmetic is." and follow.aliases == ()
    log = outcome.log()
    assert [(e["attempt"], e["candidates"], e["fitting"]) for e in log] == [(1, 1, 0), (2, 1, 1)]


def test_never_more_than_one_follow_up() -> None:
    src, provider = finder({}, pages={})  # nothing found by either search
    outcome = search_target(src, EMPTY, target(), "intro")
    assert len(provider.queries) == 2 and outcome.ranked == () and outcome.error is None


def test_a_failed_search_is_the_concepts_error_and_is_not_retried() -> None:
    src, provider = finder({"Modular arithmetic": ProviderError("HTTP 503")})
    outcome = search_target(src, EMPTY, target(), "intro")
    assert provider.topics == ["Modular arithmetic"]
    assert outcome.error.startswith("the search failed: No sources could be found: every provider failed (web: HTTP 503)")
    assert [(s["provider"], s["outcome"], s["error"]) for s in outcome.searches()] == [("web", "failed", "HTTP 503")]


def test_a_failed_follow_up_keeps_the_first_candidates() -> None:
    thin = "https://a.example/thin"
    src, _ = finder(
        {"Modular arithmetic": [hit("web", 1, thin)], "Modular arithmetic lecture notes": ProviderError("boom")},
        pages={thin: web_page(thin, 50)},
    )
    outcome = search_target(src, EMPTY, target(), "intro")
    assert outcome.error.startswith("the follow-up search failed: No sources could be found")
    assert [r.candidate.url for r in outcome.ranked] == [thin]
    assert [(s["attempt"], s["outcome"]) for s in outcome.searches()] == [(1, "ok"), (2, "failed")]


def test_a_triage_failure_is_the_concepts_error_with_the_providers_recorded() -> None:
    url = "https://a.example/1"
    src, _ = finder({"Modular arithmetic": [hit("web", 1, url)]},
                    triager=FakeTriager(error=TriageError("Could not rank the sources: x")))  # fmt: skip
    outcome = search_target(src, EMPTY, target(), "intro")
    assert "Could not rank the sources: x" in outcome.error
    assert [(s["provider"], s["outcome"], s["hits"]) for s in outcome.searches()] == [("web", "ok", 1)]


def test_at_most_five_candidates_per_concept_ranked_by_score() -> None:
    urls = [f"https://a.example/{n}" for n in range(8)]
    scores = {u: (5, 5, n % 5 + 1) for n, u in enumerate(urls)}
    src, _ = finder({"Modular arithmetic": [hit("web", n + 1, u) for n, u in enumerate(urls)]},
                    score=lambda item: scores[item.url])  # fmt: skip
    outcome = search_target(src, EMPTY, target(), "intro")
    assert len(outcome.ranked) == 5 and outcome.left_out == 3
    overall = [r.candidate.triage.overall for r in outcome.ranked]
    assert overall == sorted(overall, reverse=True)
    assert outcome.log()[-1]["left_out"] == 3


def test_the_same_work_in_both_searches_is_one_candidate_of_the_concept() -> None:
    abs_url, pdf = "https://arxiv.org/abs/2101.00001", "https://arxiv.org/pdf/2101.00001"
    src, _ = finder(
        {"Modular arithmetic": [hit("web", 1, abs_url)], "Modular arithmetic lecture notes": [hit("web", 1, pdf)]},
        pages={pdf: web_page(pdf, 50)},  # unreadable: the follow-up runs and finds it again
    )
    outcome = search_target(src, EMPTY, target(), "intro")
    assert len(outcome.attempts) == 2
    assert [(r.candidate.url, r.attempt) for r in outcome.ranked] == [(pdf, 1)]


def test_discovery_fans_out_and_shares_deduplication_across_the_exploration(settings: Any) -> None:
    shared, own = "https://a.example/shared", "https://b.example/own"
    src, provider = finder({"Group": [hit("web", 1, shared)], "Ring": [hit("web", 1, shared), hit("web", 2, own)]})
    targets = [target("Group"), target("Ring", coverage="thin")]
    with session_scope(settings) as session:
        manager = ExplorationManager(session, settings, owner=None)
        row = manager.create("Algebra", "intro")
        manager.store_outline(row, questions=[], steps=["S"], concepts=[
            {"key": t.key, "position": n, "step": 1, "name": t.name, "aliases": [], "summary": t.summary,
             "prerequisites": [], "search_query": t.search_query}
            for n, t in enumerate(targets, start=1)
        ])  # fmt: skip
        exploration_id = row.id
    done: list[str] = []

    def store(outcome: Any) -> None:
        with session_scope(settings) as session:
            manager = ExplorationManager(session, settings, owner=None)
            row = manager.get(exploration_id)
            concept_row = next(c for c in manager.concepts(row) if c.key == outcome.target.key)
            manager.record_discovery(row, concept_row, searches=outcome.searches(),
                                     candidates=outcome.candidates(), log=outcome.log(),
                                     error=outcome.error)  # fmt: skip
        done.append(outcome.target.key)

    outcomes = discover(src, EMPTY, targets, "intro", on_done=store, workers=2)
    assert sorted(done) == ["group", "ring"] and len(outcomes) == 2
    assert sorted(provider.topics) == ["Group", "Ring"]
    with session_scope(settings) as session:
        manager = ExplorationManager(session, settings, owner=None)
        row = manager.get(exploration_id)
        candidates = manager.candidates(row)
        assert sorted(c.url for c in candidates) == [shared, own]
        by_url = {c.url: c for c in candidates}
        assert sorted(c.key for c in manager.concepts_of(by_url[shared])) == ["group", "ring"]
        assert [c.key for c in manager.concepts_of(by_url[own])] == ["ring"]
        links = {(l.outline_concept_id, l.candidate_id): l.rank for l in manager.links(row)}
        assert len(links) == 3
        assert [c.discovery_status for c in manager.concepts(row)] == ["done", "done"]
        assert len(manager.searches(row)) == 2


@pytest.mark.parametrize("workers", [1, 3])
def test_a_bug_in_a_search_is_raised(workers: int) -> None:
    class Broken:
        def find(self, query: Any, *, library: Any) -> Any:
            raise RuntimeError("a bug")

    with pytest.raises(RuntimeError, match="a bug"):
        discover(Broken(), EMPTY, [target("A"), target("B")], "intro", on_done=lambda o: None,  # type: ignore[arg-type]
                 workers=workers)  # fmt: skip
