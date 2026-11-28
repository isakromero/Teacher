"""teacher.exploration.matching: the graph match reuses concept extraction's resolver (exact name,
shortlist, judge) and writes nothing: every table's row count and every concept row are the same
afterwards. A stale embedding index is an error before anything is spent. Claude and the
embedder are fakes."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import func, select

from teacher.config import Settings
from teacher.db.base import Base
from teacher.db.engine import session_scope
from teacher.db.models import Concept, ConceptAlias
from teacher.exploration import ExplorationOutputError, ExplorationUnavailableError, GraphMatcher
from teacher.exploration.planner import DraftConcept
from teacher.managers import ConceptManager
from tests.exploration_fakes import MODEL, graph

pytestmark = pytest.mark.usefixtures("cost_meter")


def draft(position: int, name: str, *, aliases: tuple[str, ...] = (), summary: str | None = None) -> DraftConcept:
    from teacher.files import slugify

    return DraftConcept(
        key=slugify(name), position=position, step=1, name=name, aliases=aliases,
        summary=summary or f"What {name} is.", prerequisites=(), search_query=f"{name} intro",
    )  # fmt: skip


def snapshot(settings: Settings) -> dict[str, Any]:
    """Every table's row count, and every concept and alias row as it is."""
    with session_scope(settings) as session:
        counts = {
            t.name: session.scalar(select(func.count()).select_from(t))
            for t in Base.metadata.sorted_tables
        }
        concepts = [
            (c.id, c.slug, c.name, c.summary, c.updated_at, c.reviewed_at)
            for c in session.scalars(select(Concept).order_by(Concept.id))
        ]
        aliases = [(a.concept_id, a.alias) for a in session.scalars(select(ConceptAlias).order_by(ConceptAlias.id))]
    return {"counts": counts, "concepts": concepts, "aliases": aliases}


@pytest.fixture
def embedder(fake_embedder: Any) -> Any:
    # "DH key exchange" lands next to "Diffie-Hellman key exchange"; everything else apart.
    return fake_embedder({"diffie-hellman key exchange": [1.0, 0.1], "dh key exchange": [1.0, 0.0]})


def test_exact_judged_and_new_matches_write_nothing(
    settings: Settings, session: Any, embedder: Any, fake_anthropic: Any, cost_meter: Any
) -> None:
    made = graph(session, settings, embedder, "Modular arithmetic",
                 ("Diffie-Hellman key exchange", ["Diffie Hellman"]))  # fmt: skip
    before = snapshot(settings)
    fake = fake_anthropic(
        fake_anthropic.judge_reply(
            [{"candidate": 2, "verdict": "same_as", "concept_id": made["Diffie-Hellman key exchange"].id,
              "reason": "The same protocol."}]  # fmt: skip
        )
    )
    drafts = [
        draft(1, "Modular arithmetic"),
        draft(2, "DH key exchange", aliases=("DHKE",)),
        draft(3, "Forward secrecy"),
    ]
    matches = GraphMatcher(fake, embedder=embedder, model=MODEL).match(settings, drafts)
    assert [(m.key, m.verdict, m.concept_id) for m in matches] == [
        ("modular-arithmetic", "exact", made["Modular arithmetic"].id),
        ("dh-key-exchange", "judged", made["Diffie-Hellman key exchange"].id),
        ("forward-secrecy", "new", None),
    ]
    assert matches[1].reason == "The same protocol."
    assert matches[1].shortlist and matches[1].shortlist[0]["slug"] == "diffie-hellman-key-exchange"
    # One judge call for the one concept with a shortlist (the reused prompt and schema).
    (call,) = fake.calls
    assert "tools" not in call
    assert '"name": "DH key exchange"' in call["messages"][0]["content"][0]["text"]
    # Read-only: no row anywhere was added, removed or changed. Extraction would have added
    # "DH key exchange" and "DHKE" as aliases of the judged match; the match does not.
    assert snapshot(settings) == before
    with session_scope(settings) as s:
        aliases = {a.alias for a in ConceptManager(s, settings).get_by_slug("diffie-hellman-key-exchange").aliases}
    assert aliases == {"Diffie Hellman"}
    # The calls: one embedding request for the drafts, one judge call.
    assert [(e.purpose, e.step) for e in cost_meter.entries] == [("exploration", "concept judge")]
    assert len(embedder.calls) == 2  # the graph's own embedding (in `graph`), then the drafts


def test_an_empty_graph_makes_every_concept_new_without_any_call(
    settings: Settings, fake_embedder: Any, fake_anthropic: Any
) -> None:
    embedder = fake_embedder()
    fake = fake_anthropic()
    before = snapshot(settings)
    matches = GraphMatcher(fake, embedder=embedder).match(settings, [draft(1, "A"), draft(2, "B")])
    assert [m.verdict for m in matches] == ["new", "new"]
    assert fake.calls == [] and embedder.calls == []
    assert snapshot(settings) == before


def test_a_stale_index_is_an_error_before_anything_is_spent(
    settings: Settings, session: Any, embedder: Any, fake_anthropic: Any
) -> None:
    graph(session, settings, embedder, "Modular arithmetic")
    ConceptManager(session, settings).create("Group", "A set with an operation.", "user")
    session.commit()  # "group" has no embedding yet
    before = snapshot(settings)
    embedder.calls.clear()
    fake = fake_anthropic()
    matcher = GraphMatcher(fake, embedder=embedder)
    with pytest.raises(ExplorationUnavailableError, match="1 of 2 concepts of the graph have no up-to-date embedding .*teacher concepts reindex"):
        matcher.match(settings, [draft(1, "Group")])
    with pytest.raises(ExplorationUnavailableError, match="no up-to-date embedding"):
        matcher.check_index(settings)
    assert fake.calls == [] and embedder.calls == []
    assert snapshot(settings) == before  # the stale concept was NOT embedded


def test_an_invalid_judge_reply_is_an_error(
    settings: Settings, session: Any, embedder: Any, fake_anthropic: Any
) -> None:
    made = graph(session, settings, embedder, "Diffie-Hellman key exchange", "Modular arithmetic")
    for judgements, expected in [
        ([], "the judge gave no answer for candidate"),
        ([{"candidate": 1, "verdict": "same_as", "concept_id": made["Modular arithmetic"].id,
           "reason": "x"}], "which is not on its shortlist"),  # fmt: skip
    ]:
        fake = fake_anthropic(fake_anthropic.judge_reply(judgements))
        with pytest.raises(ExplorationOutputError, match=expected):
            GraphMatcher(fake, embedder=embedder).match(settings, [draft(1, "DH key exchange")])
    refused = fake_anthropic(fake_anthropic.judge_reply([], stop_reason="refusal"))
    with pytest.raises(ExplorationOutputError, match="declined"):
        GraphMatcher(refused, embedder=embedder).match(settings, [draft(1, "DH key exchange")])


def test_without_keys_the_matcher_names_every_problem(fake_embedder: Any) -> None:
    embedder = fake_embedder()
    embedder.available = False
    with pytest.raises(ExplorationUnavailableError) as info:
        GraphMatcher(embedder=embedder).check_available()
    assert "credentials" in str(info.value) and "no OpenAI API key" in str(info.value)
