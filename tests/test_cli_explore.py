"""`teacher explore "<topic>" [--level ...] [--json]` with an explorer of fakes
(`cli.build_explorer` replaced): the run in the foreground with its stages on stderr, the text
and JSON output, a failed concept search as a warning, and the errors that give status 1."""

from __future__ import annotations

import json
from typing import Any

import pytest

from teacher import cli
from teacher.config import Settings
from teacher.discovery import ProviderError
from tests.exploration_fakes import brief_reply, explorer, finder, hit, outline_reply

URL = "https://a.example/ma"


@pytest.fixture
def replies(settings: Settings, settings_env: Any, monkeypatch: pytest.MonkeyPatch,
            fake_anthropic: Any, fake_embedder: Any) -> list[Any]:  # fmt: skip
    """The queued Claude replies (a test may replace them before running the command)."""
    settings_env(settings)
    outcomes: list[Any] = [brief_reply(fake_anthropic), outline_reply(fake_anthropic)]
    client = fake_anthropic()
    client.outcomes = outcomes
    src, _ = finder({
        "Modular arithmetic": [hit("web", 1, URL, "Clock arithmetic notes")],
        "Diffie-Hellman key exchange": ProviderError("HTTP 503"),
    })  # fmt: skip
    monkeypatch.setattr(cli, "build_explorer", lambda models: explorer(client, fake_embedder(), src))
    return outcomes


def test_explore_prints_the_outline(replies: list[Any], capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["explore", "Diffie-Hellman", "--level", "intro"]) == 0
    captured = capsys.readouterr()
    out, err = captured.out, captured.err
    assert "Exploration 1: 'Diffie-Hellman' (level intro): completed." in out
    assert "Brief: How two parties agree" in out
    assert "Step 1: Foundations" in out and "Step 2: The protocol" in out
    assert "   1. Modular arithmetic [new; coverage new]" in out
    assert "      1. [4.0] Clock arithmetic notes <https://a.example/ma>" in out
    assert "      builds on: Modular arithmetic" in out
    assert "SOURCE SEARCH FAILED: the search failed: No sources could be found" in out
    assert "Cost: $" in out and "recorded in the cost ledger." in out
    assert "exploration 1: started. At most 12 concepts" in err  # the limits, before it runs
    assert "no reliable price estimate" in err
    for stage in ("brief", "outline", "graph_match", "discovery"):
        assert f"exploration 1: {stage}" in err
    assert "warning: the source search for 'Diffie-Hellman key exchange' failed" in err


def test_explore_json(replies: list[Any], capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["explore", "Diffie-Hellman", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "completed" and data["level"] == "intermediate"
    assert [c["key"] for s in data["steps"] for c in s["concepts"]] == [
        "modular-arithmetic", "diffie-hellman-key-exchange"]
    assert data["cost"]["calls"] == 2


def test_a_failed_run_gives_status_1(replies: list[Any], fake_anthropic: Any,
                                     capsys: pytest.CaptureFixture[str]) -> None:  # fmt: skip
    replies[:] = [fake_anthropic.reply(text="x", stop_reason="refusal", model="claude-sonnet-5-5")]
    assert cli.main(["explore", "Diffie-Hellman"]) == 1
    captured = capsys.readouterr()
    assert "error: The exploration failed: Claude declined the brief step" in captured.err
    assert "Exploration 1: 'Diffie-Hellman' (level intermediate): failed." in captured.out


def test_bad_input_gives_status_1(replies: list[Any], capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["explore", "   "]) == 1
    assert "the topic is empty" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        cli.main(["explore", "x", "--level", "expert"])
