"""teacher.discovery.service.SourceFinder: providers in parallel with every failure visible, the
merged and capped list, library marks, the probe, triage and the ranking, and the run's cost.
Providers, the fetcher and the triager are fakes."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from teacher.config import ModelSettings, Settings
from teacher.db.engine import session_scope
from teacher.discovery import (
    AllProvidersFailedError,
    ArxivProvider,
    DiscoveryQuery,
    LibraryCatalog,
    ProviderError,
    SourceFinder,
    TriageError,
    WebSearchProvider,
    build_source_finder,
    concept_query,
    find_sources,
)
from teacher.managers import ConceptManager, KnowledgeSourceManager
from teacher.url_sources import UrlSourceError
from tests.discovery_fakes import (
    FakeFetcher,
    FakeProvider,
    FakeTriager,
    hit,
    model_call,
    web_page,
)

QUERY = DiscoveryQuery("Diffie-Hellman key exchange", level="intro")
EMPTY = LibraryCatalog()


def finder(providers: list[Any], fetcher: Any, triager: Any = None, **kw: Any) -> SourceFinder:
    return SourceFinder(providers, fetcher=fetcher, triager=triager or FakeTriager(), **kw)


def test_a_failed_provider_is_shown_next_to_the_others_results() -> None:
    web = FakeProvider("web", [hit("web", 1, "https://a.example/1"), hit("web", 2, "https://a.example/2")],
                       calls=[model_call(searches=2)])  # fmt: skip
    arxiv = FakeProvider("arxiv", error=ProviderError("arXiv answered with HTTP 503"))
    fetcher = FakeFetcher({u: web_page(u) for u in ("https://a.example/1", "https://a.example/2")})
    result = finder([web, arxiv], fetcher).find(QUERY, library=EMPTY)
    assert [(p.name, p.ok, p.hits, p.error) for p in result.providers] == [
        ("web", True, 2, None),
        ("arxiv", False, 0, "arXiv answered with HTTP 503"),
    ]
    assert len(result.candidates) == 2
    data = result.to_dict()
    assert data["providers"][1] == {
        "name": "arxiv", "ok": False, "hits": 0, "error": "arXiv answered with HTTP 503",
        "warnings": [], "detail": "", "seconds": data["providers"][1]["seconds"],
    }  # fmt: skip
    assert web.queries == [QUERY] and arxiv.queries == [QUERY]


def test_every_provider_failing_is_an_error_naming_each_and_what_was_spent() -> None:
    web = FakeProvider("web", error=ProviderError("the web search reply is unusable: x",
                                                   calls=[model_call(cost=0.0123, searches=4)]))  # fmt: skip
    arxiv = FakeProvider("arxiv", error=ProviderError("arXiv could not be reached"))
    with pytest.raises(AllProvidersFailedError) as info:
        finder([web, arxiv], FakeFetcher({})).find(QUERY, library=EMPTY)
    message = str(info.value)
    assert "web: the web search reply is unusable: x" in message
    assert "arxiv: arXiv could not be reached" in message
    assert "1 model call(s) costing $0.0123 and 4 web search(es), not priced" in message
    assert len(info.value.calls) == 1


def test_providers_run_in_parallel_and_a_slow_one_times_out() -> None:
    fast = FakeProvider("web", [hit("web", 1, "https://a.example/1")])
    slow = FakeProvider("arxiv", [hit("arxiv", 1, "https://arxiv.org/abs/2101.00001")], delay=1.0)
    started = time.monotonic()
    result = finder([fast, slow], FakeFetcher({"https://a.example/1": web_page("https://a.example/1")}),
                    provider_deadline=0.3).find(QUERY, library=EMPTY)  # fmt: skip
    assert time.monotonic() - started < 0.9
    assert [(p.name, p.ok) for p in result.providers] == [("web", True), ("arxiv", False)]
    assert "did not finish within 0.3 seconds" in result.providers[1].error


def test_merged_capped_probed_triaged_and_ranked() -> None:
    urls = [f"https://a.example/{n}" for n in range(1, 7)]
    web = FakeProvider("web", [hit("web", n, u) for n, u in enumerate(urls, start=1)])
    arxiv = FakeProvider("arxiv", [
        hit("arxiv", 1, "https://arxiv.org/abs/2101.00001", arxiv_id="2101.00001", title="Survey"),
        hit("arxiv", 2, "https://a.example/3", title="Also found by arXiv"),
    ])  # fmt: skip
    pages: dict[str, Any] = {u: web_page(u) for u in urls}
    pages["https://a.example/2"] = UrlSourceError("needs a login", reason="paywalled")
    pages["https://a.example/4"] = web_page("https://a.example/4", 100)  # too thin
    pages["https://arxiv.org/pdf/2101.00001"] = web_page("https://arxiv.org/pdf/2101.00001", 5000)
    fetcher = FakeFetcher(pages)

    def scorer(item: Any) -> tuple[int, int, int]:
        return {"https://a.example/5": (5, 5, 5), "https://arxiv.org/pdf/2101.00001": (5, 3, 5)}.get(
            item.url, (3, 3, 3)
        )

    triager = FakeTriager(scorer)
    result = finder([web, arxiv], fetcher, triager, max_candidates=5).find(QUERY, library=EMPTY)

    assert (result.hits, result.distinct) == (8, 7)
    kept = {c.url for c in result.candidates}
    assert kept == {
        "https://a.example/3",  # two providers
        "https://a.example/1",
        "https://arxiv.org/pdf/2101.00001",  # rewritten from the abs page
        "https://a.example/2",
        "https://a.example/4",
    }
    assert sorted(n.url for n in result.not_kept) == ["https://a.example/5", "https://a.example/6"]
    assert sorted(fetcher.calls) == sorted(kept)  # only the kept ones are fetched
    (items,) = triager.seen
    assert {i.url for i in items} == {"https://a.example/3", "https://a.example/1",
                                      "https://arxiv.org/pdf/2101.00001"}  # the readable ones
    ranked = [(c.rank, c.url, c.probe.verdict, c.triage.overall if c.triage else None)
              for c in result.candidates]  # fmt: skip
    assert ranked == [
        (1, "https://arxiv.org/pdf/2101.00001", "readable", 4.6),
        (2, "https://a.example/3", "readable", 3.0),  # ties: found by two providers first
        (3, "https://a.example/1", "readable", 3.0),
        (4, "https://a.example/2", "paywalled", None),
        (5, "https://a.example/4", "too_thin", None),
    ]
    first = result.candidates[0]
    assert first.landing_url == "https://arxiv.org/abs/2101.00001"
    assert result.candidates[1].providers == ("arxiv", "web")  # in order of first sighting
    assert result.triage.ran and result.triage.scored == 3
    assert result.usage.calls[-1].step == "triage"


def test_library_candidates_are_marked_not_dropped(settings: Settings, tmp_path: Path) -> None:
    path = tmp_path / "dh.md"
    path.write_text("# DH\n", encoding="utf-8")
    with session_scope(settings) as session:
        KnowledgeSourceManager(session, settings).add(
            path, slug="dh-notes", kind="web_page", title="DH notes",
            origin_url="https://a.example/dh",
        )  # fmt: skip
    web = FakeProvider("web", [hit("web", 1, "https://a.example/dh"), hit("web", 2, "https://a.example/new")])
    fetcher = FakeFetcher({u: web_page(u) for u in ("https://a.example/dh", "https://a.example/new")})
    result = find_sources(settings, finder([web], fetcher), QUERY)
    by_url = {c.url: c for c in result.candidates}
    assert by_url["https://a.example/dh"].in_library
    assert [m.to_dict() for m in by_url["https://a.example/dh"].library] == [
        {"slug": "dh-notes", "title": "DH notes", "method": "url"}
    ]
    assert not by_url["https://a.example/new"].in_library
    assert len(result.candidates) == 2


def test_no_readable_candidate_skips_triage_and_says_so() -> None:
    web = FakeProvider("web", [hit("web", 1, "https://a.example/1")])
    triager = FakeTriager()
    result = finder([web], FakeFetcher({}), triager).find(QUERY, library=EMPTY)
    assert triager.seen == []
    assert result.triage.to_dict() == {
        "ran": False, "model": None, "scored": 0,
        "note": "No candidate was readable, so none was scored.",
    }  # fmt: skip
    assert result.candidates[0].probe.verdict == "fetch_failed"


def test_nothing_found_is_an_empty_result_not_an_error() -> None:
    result = finder([FakeProvider("web")], FakeFetcher({})).find(QUERY, library=EMPTY)
    assert result.candidates == () and result.providers[0].ok
    assert result.triage.note == "Nothing was found to score."


def test_a_triage_failure_fails_the_run_with_what_was_spent() -> None:
    web = FakeProvider("web", [hit("web", 1, "https://a.example/1")], calls=[model_call(cost=0.02)])
    triager = FakeTriager(error=TriageError("Could not rank the sources: no score for C1",
                                            calls=[model_call("triage", cost=0.001)]))  # fmt: skip
    with pytest.raises(TriageError) as info:
        finder([web], FakeFetcher({"https://a.example/1": web_page("https://a.example/1")}), triager).find(
            QUERY, library=EMPTY
        )
    assert "no score for C1" in str(info.value)
    assert "2 model call(s) costing $0.0210" in str(info.value)


def test_the_result_json_and_text(tmp_path: Path) -> None:
    web = FakeProvider("web", [hit("web", 1, "https://arxiv.org/abs/2101.00001", reason="survey")],
                       calls=[model_call(cost=0.03, searches=2)])  # fmt: skip
    pdf = "https://arxiv.org/pdf/2101.00001"
    result = finder([web], FakeFetcher({pdf: web_page(pdf)})).find(QUERY, library=EMPTY)
    data = result.to_dict()
    assert data["query"] == {"topic": "Diffie-Hellman key exchange", "level": "intro", "aliases": [],
                             "summary": None, "concept_slug": None}  # fmt: skip
    (c,) = data["candidates"]
    assert set(c) == {
        "id", "rank", "url", "landing_url", "title", "providers", "found_by", "arxiv_id", "doi",
        "authors", "year", "summary", "in_library", "library", "probe", "triage",
    }
    assert c["found_by"] == [{"provider": "web", "rank": 1, "reason": "survey"}]
    assert c["probe"]["verdict"] == "readable" and c["triage"]["overall"] == 4.0
    assert data["usage"]["total_usd"] == pytest.approx(0.04)
    assert data["usage"]["web_search_requests"] == 2
    assert "recorded_in_ledger" not in data["usage"] and "note" not in data["usage"]
    assert data["usage"]["search_fees_priced"] is False
    assert data["page_cost_estimate"] is None
    text = "\n".join(result.summary_lines())
    assert "1 candidate(s) from 1 hit(s), 1 distinct." in text
    assert " 1. [4.0] Title of https://arxiv.org/abs/2101.00001" in text
    assert "(from https://arxiv.org/abs/2101.00001)" in text
    assert "Cost: $0.0400 in 2 model call(s); 2 web search(es), not priced" in text


def test_queries_validate_and_a_concept_gives_its_names(settings: Settings) -> None:
    with pytest.raises(ValueError, match="empty"):
        DiscoveryQuery("   ")
    with pytest.raises(ValueError, match="unknown level"):
        DiscoveryQuery("x", level="expert")
    with pytest.raises(ValueError, match="longer than"):
        DiscoveryQuery("x" * 301)
    q = DiscoveryQuery("  RSA  ", aliases=("rsa", "Rivest–Shamir–Adleman", " "))
    assert (q.topic, q.aliases) == ("RSA", ("Rivest–Shamir–Adleman",))
    with session_scope(settings) as session:
        concepts = ConceptManager(session, settings)
        concept = concepts.create("Diffie-Hellman key exchange", "Agreeing on a key in public.", "user")
        concepts.add_alias(concept, "DH")
        slug = concept.slug
    query = concept_query(settings, slug, "advanced")
    assert query.to_dict() == {
        "topic": "Diffie-Hellman key exchange", "level": "advanced", "aliases": ["DH"],
        "summary": "Agreeing on a key in public.", "concept_slug": slug,
    }  # fmt: skip


def test_the_default_finder_has_the_web_and_arxiv_providers() -> None:
    built = build_source_finder(ModelSettings(generation="claude-sonnet-5-5", metadata="claude-haiku-5-5"))
    web, arxiv = built.providers
    assert isinstance(web, WebSearchProvider) and web.model == "claude-sonnet-5-5"
    assert isinstance(arxiv, ArxivProvider)
    assert built.triager.model == "claude-haiku-5-5"
    assert built.max_candidates == 8
    with pytest.raises(ValueError, match="distinct"):
        SourceFinder([FakeProvider("web"), FakeProvider("web")], fetcher=None, triager=None)
