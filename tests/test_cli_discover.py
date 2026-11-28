"""`teacher discover "<topic>" | --concept SLUG [--level ...] [--json]` with a finder of fakes
(`cli.build_source_finder` replaced): the text and JSON output, a failed provider on stderr, and
the errors that give status 1."""

from __future__ import annotations

import json
from typing import Any

import pytest

from teacher import cli
from teacher.config import Settings
from teacher.db.engine import session_scope
from teacher.discovery import ProviderError, SourceFinder
from teacher.managers import ConceptManager
from tests.discovery_fakes import FakeFetcher, FakeProvider, FakeTriager, hit, model_call, web_page

URL = "https://example.org/dh"


@pytest.fixture
def providers(settings: Settings, settings_env: Any, monkeypatch: pytest.MonkeyPatch) -> list[FakeProvider]:
    settings_env(settings)
    found = [
        FakeProvider("web", [hit("web", 1, URL, "DH explained", reason="A tutorial")],
                     calls=[model_call(cost=0.05, searches=3)]),
        FakeProvider("arxiv", error=ProviderError("arXiv answered with HTTP 503")),
    ]  # fmt: skip
    finder = SourceFinder(found, fetcher=FakeFetcher({URL: web_page(URL)}), triager=FakeTriager())
    monkeypatch.setattr(cli, "build_source_finder", lambda models: finder)
    return found


def test_discover_prints_the_ranked_sources(providers: list[FakeProvider], capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["discover", "Diffie-Hellman key exchange", "--level", "intro"]) == 0
    captured = capsys.readouterr()
    assert "Sources for 'Diffie-Hellman key exchange' (level intro): 1 candidate(s)" in captured.out
    assert "  arxiv: FAILED: arXiv answered with HTTP 503" in captured.out
    assert " 1. [4.0] DH explained" in captured.out
    assert "    readable: readable: 800 words of text" in captured.out
    assert "    web #1: A tutorial" in captured.out
    assert "Cost: $0.0600 in 2 model call(s); 3 web search(es), not priced" in captured.out
    assert "warning: the arxiv provider failed: arXiv answered with HTTP 503" in captured.err
    assert providers[0].queries[0].level == "intro"


def test_discover_json(providers: list[FakeProvider], capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["discover", "Diffie-Hellman key exchange", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["query"]["level"] == "intermediate"
    assert [c["url"] for c in data["candidates"]] == [URL]
    assert [(p["name"], p["ok"]) for p in data["providers"]] == [("web", True), ("arxiv", False)]
    assert "recorded_in_ledger" not in data["usage"]


def test_discover_a_concept(
    providers: list[FakeProvider], settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    with session_scope(settings) as session:
        concept = ConceptManager(session, settings).create(
            "Diffie-Hellman key exchange", "Agreeing on a key in public.", "user", aliases=["DH"]
        )
        slug = concept.slug
    assert cli.main(["discover", "--concept", slug, "--json"]) == 0
    query = providers[0].queries[0]
    assert (query.topic, query.aliases, query.concept_slug) == ("Diffie-Hellman key exchange", ("DH",), slug)
    assert cli.main(["discover", "--concept", "no-such-concept"]) == 1
    assert "no-such-concept" in capsys.readouterr().err


def test_discover_errors_give_status_1(
    providers: list[FakeProvider], capsys: pytest.CaptureFixture[str]
) -> None:
    providers[0].error = ProviderError("the web search call failed: boom")
    assert cli.main(["discover", "anything"]) == 1
    err = capsys.readouterr().err
    assert "error: No sources could be found: every provider failed" in err
    assert "web: the web search call failed: boom" in err
    assert cli.main(["discover", "   "]) == 1
    assert "the topic is empty" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        cli.main(["discover", "x", "--level", "expert"])  # argparse refuses an unknown level
