"""`teacher explore --enrich <id> [--yes] [--json]` with an enricher of fakes (`cli.build_enricher`
and `cli.confirm` replaced): the plan printed before anything changes, the confirmation (asked,
declined, skipped with --yes, no terminal), the run in the foreground with its stages on stderr,
the summary and JSON output, a failed source as a warning, and the errors that give status 1."""

from __future__ import annotations

import json
from typing import Any

import pytest

from teacher import cli
from teacher.config import Settings
from teacher.db.engine import session_scope
from teacher.db.models import ExplorationEnrichment, KnowledgeSource
from tests.enrichment_fakes import approve, cand, candidate, completed_exploration, enricher, outline, page

URL = "https://a.example/dlog"
BROKEN = "https://a.example/broken"


@pytest.fixture
def eid(settings: Settings, settings_env: Any, monkeypatch: pytest.MonkeyPatch,
        fake_anthropic: Any, fake_embedder: Any) -> int:  # fmt: skip
    settings_env(settings)
    exploration_id = completed_exploration(
        settings,
        [outline("ma", "Modular arithmetic", 1), outline("dl", "Discrete logarithm", 2, ["ma"])],
        {"dl": [candidate("c_dl", URL, title="Discrete logs"),
                candidate("c_broken", BROKEN, title="Broken page")]},
    )  # fmt: skip
    approve(settings, exploration_id, "c_dl", "c_broken")
    extract = fake_anthropic(fake_anthropic.concepts_reply([cand("Discrete logarithm")]))
    the_enricher, _ = enricher(pages={URL: page(URL, "Discrete logarithms in groups.")},
                               extract_client=extract, judge_client=fake_anthropic(),
                               embedder=fake_embedder())  # fmt: skip
    monkeypatch.setattr(cli, "build_enricher", lambda models: the_enricher)
    return exploration_id


def enrichments(settings: Settings) -> int:
    with session_scope(settings) as session:
        return len(list(session.scalars(ExplorationEnrichment.__table__.select())))


def test_enrich_asks_then_runs_and_prints_the_summary(
    settings: Settings, eid: int, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    asked: list[str] = []
    monkeypatch.setattr(cli, "confirm", lambda prompt: asked.append(prompt) or True)
    assert cli.main(["explore", "--enrich", str(eid)]) == 0
    out, err = capsys.readouterr()
    assert asked == ["Go ahead? [y/N] "]
    assert f"Enriching exploration {eid} will:" in out
    assert "add 2 source(s) to the library and link 0" in out
    assert "- Discrete logs <https://a.example/dlog>: add" in out
    assert "- Discrete logarithm builds on Modular arithmetic: add" in out
    assert "no price estimate" in out
    for stage in ("ingest", "graph", "grounding"):
        assert f": {stage}" in err
    assert "Enrichment 1: completed." in out
    assert "Sources: 1 added, 0 already held, 1 failed." in out
    assert "FAILED Broken page <https://a.example/broken>: fetching it:" in out
    assert "Relations: 1 added, 0 already present, 0 skipped." in out
    assert "grounded: Discrete logarithm by discrete-logs" in out
    assert "NOT GROUNDED: Modular arithmetic" in out
    assert "warning: 'Broken page' was not ingested: fetching it:" in err
    assert "recorded in the cost ledger." in out


def test_declined_or_unconfirmable_changes_nothing(
    settings: Settings, eid: int, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "confirm", lambda prompt: False)
    assert cli.main(["explore", "--enrich", str(eid)]) == 1
    assert "nothing was changed: the enrichment was not confirmed" in capsys.readouterr().err

    def no_terminal(prompt: str) -> bool:
        raise cli.PromptError("no terminal to ask on; pass --yes to go ahead without asking")

    monkeypatch.setattr(cli, "confirm", no_terminal)
    assert cli.main(["explore", "--enrich", str(eid)]) == 1
    assert "pass --yes" in capsys.readouterr().err
    assert enrichments(settings) == 0
    with session_scope(settings) as session:
        assert list(session.scalars(KnowledgeSource.__table__.select())) == []


def test_yes_and_json(settings: Settings, eid: int, monkeypatch: pytest.MonkeyPatch,
                      capsys: pytest.CaptureFixture[str]) -> None:  # fmt: skip
    monkeypatch.setattr(cli, "confirm", lambda prompt: pytest.fail("asked despite --yes"))
    assert cli.main(["explore", "--enrich", str(eid), "--yes", "--json"]) == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["enrichment"]["status"] == "completed"
    assert data["enrichment"]["summary"]["sources"]["added"] == 1
    assert "Enriching exploration" in captured.err  # the plan goes to stderr with --json


def test_errors_give_status_1(settings: Settings, eid: int, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["explore", "--enrich", "999", "--yes"]) == 1
    assert "no exploration 999" in capsys.readouterr().err
    assert cli.main(["explore", "--enrich", str(eid), "--level", "intro"]) == 1
    assert "--level is for exploring a topic" in capsys.readouterr().err
    assert cli.main(["explore", "Topic", "--yes"]) == 1
    assert "--yes is only used with --enrich" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        cli.main(["explore", "Topic", "--enrich", str(eid)])
    with session_scope(settings) as session:
        from teacher.managers import ExplorationManager

        m = ExplorationManager(session, settings, owner=None)
        m.create_enrichment(m.get(eid), {"digest": "x"})
    assert cli.main(["explore", "--enrich", str(eid), "--yes"]) == 1
    assert "An enrichment of this exploration is running" in capsys.readouterr().err
