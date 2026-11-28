"""teacher.exploration.runner: a whole run (brief, outline, graph match, discovery) ending
`completed`, with each stage's progress and log; a concept whose search failed while the others
did not; every model call in the cost ledger tied to the exploration; failures stored on the run
(a refused brief, an outline rejected twice, a bug); the check before a run starts; and an
interrupted run marked failed, never left running. Claude, the embedder and the source finder are
fakes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select

from teacher.config import Settings
from teacher.db.engine import session_scope
from teacher.db.models import ApiCall, Exploration, SourceConcept
from teacher.discovery import ProviderError, SourceFinder, Triager, WebSearchProvider
from teacher.exploration import (
    INTERRUPTED_ERROR,
    ExplorationUnavailableError,
    exploration_detail,
    fail_interrupted_explorations,
    run_exploration,
    start_exploration,
)
from teacher.managers import ExplorationManager, KnowledgeSourceManager
from tests.discovery_fakes import FakeFetcher, score, triage_reply, web_page, web_reply
from tests.exploration_fakes import (
    brief_reply,
    concept,
    explorer,
    finder,
    graph,
    hit,
    outline_reply,
    step,
)

STEPS = [
    step("Foundations", concept("Modular arithmetic"), concept("Group")),
    step("The protocol", concept("Diffie-Hellman key exchange", aliases=["DH"],
                                 prerequisites=["Modular arithmetic", "Group"])),  # fmt: skip
]
GROUP_URL = "https://a.example/groups"


@pytest.fixture
def embedder(fake_embedder: Any) -> Any:
    return fake_embedder()


@pytest.fixture
def library(settings: Settings, session: Any, embedder: Any, tmp_path: Path) -> dict[str, Any]:
    """"Modular arithmetic" introduced by a library source (covered); "Group" only used by one
    (thinly covered)."""
    made = graph(session, settings, embedder, "Modular arithmetic", "Group")
    path = tmp_path / "notes.md"
    path.write_text("# Notes\n", encoding="utf-8")
    source = KnowledgeSourceManager(session, settings).add(path, slug="notes", kind="web_page", title="Notes")
    for name, role in (("Modular arithmetic", "introduces"), ("Group", "uses")):
        session.add(SourceConcept(source_id=source.id, concept_id=made[name].id, role=role,
                                  resolution="exact", candidate_name=name))  # fmt: skip
    session.commit()
    return made


def start(settings: Settings, the_explorer: Any, topic: str = "Diffie-Hellman", level: str = "intro") -> int:
    return start_exploration(settings, the_explorer, topic, level, owner=None)


def detail(settings: Settings, exploration_id: int) -> dict[str, Any]:
    with session_scope(settings) as session:
        row = ExplorationManager(session, settings, owner=None).get(exploration_id)
        return exploration_detail(session, settings, row)


def concepts_by_key(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {c["key"]: c for s in data["steps"] for c in s["concepts"]}


def test_a_whole_run_completes_with_a_partial_discovery_failure(
    settings: Settings, library: dict[str, Any], embedder: Any, fake_anthropic: Any
) -> None:
    client = fake_anthropic(brief_reply(fake_anthropic), outline_reply(fake_anthropic, STEPS))
    src, provider = finder({
        "Group": [hit("web", 1, GROUP_URL, "Groups for beginners")],
        "Diffie-Hellman key exchange": ProviderError("arXiv answered with HTTP 503"),
    })  # fmt: skip
    the_explorer = explorer(client, embedder, src)
    exploration_id = start(settings, the_explorer)
    stages: list[str] = []
    run_exploration(settings, exploration_id, the_explorer, on_stage=stages.append)

    assert stages == ["brief", "outline", "graph_match", "discovery"]
    data = detail(settings, exploration_id)
    assert data["status"] == "completed" and data["error"] is None
    assert data["stage"] == "discovery" and data["progress"] == {"done": 2, "total": 2}
    assert [(s["name"], s["status"]) for s in data["stages"]] == [
        ("brief", "succeeded"), ("outline", "succeeded"), ("graph_match", "succeeded"),
        ("discovery", "succeeded"),
    ]  # fmt: skip
    assert data["brief"]["out_of_scope"] == ["elliptic curves"] and data["brief"]["studied"] == []
    assert [q["perspective"] for q in data["questions"]] == ["a practitioner", "a theorist"]
    assert [(s["number"], s["title"]) for s in data["steps"]] == [(1, "Foundations"), (2, "The protocol")]

    by_key = concepts_by_key(data)
    ma, group, dh = by_key["modular-arithmetic"], by_key["group"], by_key["diffie-hellman-key-exchange"]
    # Known and covered (a library source introduces it): not searched.
    assert (ma["status"], ma["match"]["method"], ma["coverage"], ma["discovery"]["status"]) == (
        "known", "exact", "covered", "not_needed")
    assert ma["known"]["concept"]["slug"] == "modular-arithmetic"
    assert ma["known"]["standing"]["status"] == "unstudied"
    assert ma["known"]["sources"] == [{"slug": "notes", "title": "Notes", "role": "introduces"}]
    # Known but thin (only used by a source): searched, with its candidate.
    assert (group["status"], group["coverage"], group["discovery"]["status"]) == ("known", "thin", "done")
    assert [(c["url"], c["rank"], c["fits"], c["verdict"]) for c in group["candidates"]] == [
        (GROUP_URL, 1, True, "readable")]
    # New, and its search failed: the run still completed and the error is on the concept.
    assert (dh["status"], dh["coverage"], dh["discovery"]["status"]) == ("planned", "new", "failed")
    assert "every provider failed (web: arXiv answered with HTTP 503)" in dh["discovery"]["error"]
    assert dh["candidates"] == []
    assert [p["key"] for p in dh["prerequisites"]] == ["modular-arithmetic", "group"]
    assert sorted(provider.topics) == ["Diffie-Hellman key exchange", "Group"]  # no follow-ups
    assert [(s["concept"], s["outcome"]) for s in sorted(data["searches"], key=lambda s: s["concept"])] == [
        ("diffie-hellman-key-exchange", "failed"), ("group", "ok")]
    assert data["editable"] is True and data["limits"]["max_concepts"] == 12
    # The two Claude calls, tied to the exploration.
    with session_scope(settings) as session:
        rows = [(r.purpose, r.step, r.exploration_id) for r in session.scalars(select(ApiCall).order_by(ApiCall.id))]
    assert rows == [("exploration", "brief", exploration_id), ("exploration", "outline", exploration_id)]
    assert data["cost"]["calls"] == 2 and data["cost"]["web_search_requests"] == 0


def test_every_model_call_of_a_run_is_in_the_ledger_with_the_exploration(
    settings: Settings, embedder: Any, fake_anthropic: Any
) -> None:
    """Web search (in a discovery thread, then a provider thread) and triage calls too, with
    the web search's count of searches, unpriced."""
    planner_client = fake_anthropic(
        brief_reply(fake_anthropic),
        outline_reply(fake_anthropic, [step("Only", concept("Diffie-Hellman key exchange"))]),
    )
    url = "https://a.example/dh"
    search_client = fake_anthropic(
        web_reply([{"url": url, "title": "DH", "reason": "A tutorial."}], searches=3),
        triage_reply([score("C1")]),
    )
    src = SourceFinder([WebSearchProvider(search_client)], fetcher=FakeFetcher({url: web_page(url)}),
                       triager=Triager(search_client))  # fmt: skip
    the_explorer = explorer(planner_client, embedder, src)
    exploration_id = start(settings, the_explorer)
    run_exploration(settings, exploration_id, the_explorer)
    data = detail(settings, exploration_id)
    assert data["status"] == "completed", data["error"]
    with session_scope(settings) as session:
        rows = [(r.purpose, r.step, r.exploration_id, r.web_search_requests, r.cost_usd is not None)
                for r in session.scalars(select(ApiCall).order_by(ApiCall.id))]  # fmt: skip
    assert rows == [
        ("exploration", "brief", exploration_id, 0, True),
        ("exploration", "outline", exploration_id, 0, True),
        ("exploration", "web_search", exploration_id, 3, True),
        ("exploration", "triage", exploration_id, 0, True),
    ]
    cost = data["cost"]
    assert cost["calls"] == 4 and cost["web_search_requests"] == 3
    assert cost["web_search_fees_priced"] is False
    assert {p["key"] for p in cost["parts"]} == {"brief", "outline", "web_search", "triage"}
    assert data["steps"][0]["concepts"][0]["discovery"]["log"][0]["web_search_requests"] == 3


def test_a_refused_brief_fails_the_run_with_the_error(
    settings: Settings, embedder: Any, fake_anthropic: Any
) -> None:
    client = fake_anthropic(fake_anthropic.reply(text="x", stop_reason="refusal", model="claude-sonnet-5-5"))
    the_explorer = explorer(client, embedder, finder({})[0])
    exploration_id = start(settings, the_explorer)
    run_exploration(settings, exploration_id, the_explorer)
    data = detail(settings, exploration_id)
    assert data["status"] == "failed" and data["finished_at"] is not None
    assert data["error"].startswith("The exploration failed: Claude declined the brief step")
    assert [(s["name"], s["status"], s["error"]) for s in data["stages"]] == [("brief", "failed", data["error"])]
    assert data["steps"] == [] and data["brief"] is None and data["editable"] is False
    assert data["cost"]["calls"] == 1  # the refused call is in the ledger


def test_an_outline_rejected_every_time_fails_the_run(
    settings: Settings, embedder: Any, fake_anthropic: Any
) -> None:
    cyclic = [step("One", concept("A", prerequisites=["B"]), concept("B", prerequisites=["A"]))]
    client = fake_anthropic(brief_reply(fake_anthropic), outline_reply(fake_anthropic, cyclic),
                            outline_reply(fake_anthropic, cyclic),
                            outline_reply(fake_anthropic, cyclic))  # fmt: skip
    the_explorer = explorer(client, embedder, finder({})[0])
    exploration_id = start(settings, the_explorer)
    run_exploration(settings, exploration_id, the_explorer)
    data = detail(settings, exploration_id)
    assert data["status"] == "failed" and data["stage"] == "outline"
    assert "the prerequisites form a cycle" in data["error"] and "attempt 3 of 3" in data["error"]
    assert data["brief"] is not None and data["steps"] == []


def test_a_bug_fails_the_run_with_its_class_and_message(
    settings: Settings, embedder: Any, fake_anthropic: Any
) -> None:
    class Broken:
        def find(self, query: Any, *, library: Any) -> Any:
            raise RuntimeError("a bug in the finder")

    client = fake_anthropic(brief_reply(fake_anthropic), outline_reply(fake_anthropic))
    the_explorer = explorer(client, embedder, Broken())
    exploration_id = start(settings, the_explorer)
    run_exploration(settings, exploration_id, the_explorer)
    data = detail(settings, exploration_id)
    assert data["status"] == "failed" and data["stage"] == "discovery"
    assert data["error"] == "RuntimeError: a bug in the finder"
    assert data["stages"][-1] == {**data["stages"][-1], "name": "discovery", "status": "failed"}


def test_the_check_before_a_run_starts(
    settings: Settings, session: Any, embedder: Any, fake_anthropic: Any
) -> None:
    the_explorer = explorer(fake_anthropic(), embedder, finder({})[0])
    for topic, level in (("  ", "intro"), ("x" * 301, "intro"), ("DH", "expert")):
        with pytest.raises(ValueError):
            start(settings, the_explorer, topic, level)
    graph(session, settings, embedder, "Group")
    from teacher.managers import ConceptManager

    ConceptManager(session, settings).create("Ring", "A ring.", "user")
    session.commit()
    with pytest.raises(ExplorationUnavailableError, match="teacher concepts reindex"):
        start(settings, the_explorer)
    with session_scope(settings) as s:
        assert s.scalar(select(func.count(Exploration.id))) == 0  # nothing was created


def test_an_interrupted_run_is_marked_failed_not_left_running(settings: Settings) -> None:
    with session_scope(settings) as session:
        manager = ExplorationManager(session, settings, owner=None)
        row = manager.create("DH", "intro")
        manager.begin_stage(row, "discovery", total=4)
        manager.advance(row)
        other = manager.create("Groups", "intro")  # interrupted before its first stage
        done = manager.create("Done", "intro")
        manager.complete(done)
        ids = (row.id, other.id, done.id)
    assert fail_interrupted_explorations(settings) == 2
    first, second, third = (detail(settings, i) for i in ids)
    assert first["status"] == "failed"
    assert first["error"] == INTERRUPTED_ERROR.format(stage="discovery")
    assert first["stages"][-1]["status"] == "failed" and first["progress"] == {"done": 1, "total": 4}
    assert second["error"] == "interrupted: the server stopped while the first stage ran"
    assert third["status"] == "completed"
    assert fail_interrupted_explorations(settings) == 0


def test_a_run_marked_failed_meanwhile_stops_instead_of_writing_on(
    settings: Settings, embedder: Any, fake_anthropic: Any
) -> None:
    client = fake_anthropic(brief_reply(fake_anthropic), outline_reply(fake_anthropic))
    the_explorer = explorer(client, embedder, finder({})[0])
    exploration_id = start(settings, the_explorer)

    def restart_sweep(stage: str) -> None:
        if stage == "outline":
            fail_interrupted_explorations(settings)

    run_exploration(settings, exploration_id, the_explorer, on_stage=restart_sweep)
    data = detail(settings, exploration_id)
    assert data["status"] == "failed"
    assert data["error"] == INTERRUPTED_ERROR.format(stage="outline")
    assert data["steps"] == []  # the outline was not stored after the sweep


def test_the_app_marks_interrupted_explorations_failed_at_startup(settings: Settings) -> None:
    from fastapi.testclient import TestClient

    from teacher.generation import StubGenerator
    from teacher.lab_generator import StubLabGenerator
    from teacher.web.app import create_app

    with session_scope(settings) as session:
        exploration_id = ExplorationManager(session, settings, owner=None).create("DH", "intro").id
    app = create_app(settings, generator=StubGenerator(), lab_generator=StubLabGenerator())
    with TestClient(app) as client:
        data = client.get(f"/api/explorations/{exploration_id}").json()
    assert data["status"] == "failed" and data["error"].startswith("interrupted:")


def test_an_embedder_that_does_not_answer_stops_the_run_before_anything_is_spent(
    settings: Settings, embedder: Any, fake_anthropic: Any
) -> None:
    embedder.fail_on = {1}  # the probe of the check before a run
    client = fake_anthropic(brief_reply(fake_anthropic))
    the_explorer = explorer(client, embedder, finder({})[0])
    with pytest.raises(ExplorationUnavailableError, match="the embedder does not answer"):
        start(settings, the_explorer)
    assert client.calls == [] and len(embedder.calls) == 1
    with session_scope(settings) as s:
        assert s.scalar(select(func.count(Exploration.id))) == 0  # nothing was created
