"""teacher.managers.explorations: owners only see their own explorations; the reader's edits
(rename, drop and restore, reorder steps within the prerequisites, approve/reject candidates)
only on a completed exploration; the hooks milestone 3 builds on (the approved candidates,
marking one ingested and a concept grounded); and the run's bookkeeping refusing an exploration
that is no longer running."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from teacher.config import Settings
from teacher.db.engine import session_scope
from teacher.managers import (
    ConceptManager,
    ExplorationManager,
    KnowledgeSourceManager,
    NotFoundError,
    StateConflictError,
    UserManager,
)


def outline_concept(key: str, position: int, step: int, prerequisites: tuple[str, ...] = ()) -> dict[str, Any]:
    return {"key": key, "position": position, "step": step, "name": key.title(), "aliases": [],
            "summary": f"{key}.", "prerequisites": list(prerequisites), "search_query": f"{key} q"}  # fmt: skip


def candidate(key: str, url: str, **kw: Any) -> dict[str, Any]:
    return {
        "candidate_key": key, "url": url, "canonical_urls": [url.replace("http://", "https://")],
        "landing_url": None, "arxiv_id": kw.get("arxiv_id"), "doi": None, "title": f"About {key}",
        "authors": [], "year": None, "summary": None, "found_by": [], "verdict": "readable",
        "verdict_reason": "readable", "probe": {"verdict": "readable"}, "library": [],
        "attempt": 1, "triage": {"overall": 4.0}, "overall": 4.0, "fits": True,
    }  # fmt: skip


def make(settings: Settings, *, owner: int | None = None, complete: bool = True) -> int:
    """An exploration with steps A (a), B (b, builds on a), C (c, builds on b; d), candidates
    for b and c (one shared)."""
    with session_scope(settings) as session:
        m = ExplorationManager(session, settings, owner=owner)
        row = m.create("Topic", "intro")
        rows = m.store_outline(row, questions=[], steps=["A", "B", "C"], concepts=[
            outline_concept("a", 1, 1), outline_concept("b", 2, 2, ("a",)),
            outline_concept("c", 3, 3, ("b",)), outline_concept("d", 4, 3),
        ])  # fmt: skip
        for r in rows:
            m.store_match(r, concept_id=None, method="new", reason=None, detail=None,
                          coverage="new", needs_sources=True)  # fmt: skip
        by_key = {r.key: r for r in rows}
        m.record_discovery(row, by_key["b"], searches=[], log=[], error=None,
                           candidates=[candidate("c_1", "https://x.example/1"),
                                       candidate("c_2", "https://x.example/2", arxiv_id="2101.00001")])  # fmt: skip
        # The same work for c: by arXiv id (another registry id and address) and by address.
        m.record_discovery(row, by_key["c"], searches=[], log=[], error="the search failed: x",
                           candidates=[candidate("c_9", "https://arxiv.org/pdf/2101.00001", arxiv_id="2101.00001"),
                                       candidate("c_8", "http://x.example/1")])  # fmt: skip
        if complete:
            m.complete(row)
        return row.id


def test_shared_candidates_are_stored_once(settings: Settings) -> None:
    eid = make(settings)
    with session_scope(settings) as session:
        m = ExplorationManager(session, settings, owner=None)
        row = m.get(eid)
        assert [c.candidate_key for c in m.candidates(row)] == ["c_1", "c_2"]
        assert {c.candidate_key: [k.key for k in m.concepts_of(c)] for c in m.candidates(row)} == {
            "c_1": ["b", "c"], "c_2": ["b", "c"]}
        c_row = next(c for c in m.concepts(row) if c.key == "c")
        assert (c_row.discovery_status, c_row.discovery_error) == ("failed", "the search failed: x")


def test_owners_see_only_their_own(settings: Settings) -> None:
    with session_scope(settings) as session:
        ann = UserManager(session, settings).create("ann@example.com", "a long password 1").id
        bob = UserManager(session, settings).create("bob@example.com", "a long password 2").id
    mine = make(settings, owner=ann)
    with session_scope(settings) as session:
        assert [r.id for r in ExplorationManager(session, settings, owner=ann).list()] == [mine]
        assert ExplorationManager(session, settings, owner=bob).list() == []
        assert ExplorationManager(session, settings, owner=None).list() == []
        for owner in (bob, None):
            with pytest.raises(NotFoundError):
                ExplorationManager(session, settings, owner=owner).get(mine)


def test_rename_and_drop(settings: Settings) -> None:
    eid = make(settings)
    with session_scope(settings) as session:
        m = ExplorationManager(session, settings, owner=None)
        row = m.get(eid)
        a, b = m.concepts(row)[:2]
        m.rename_concept(row, a, "  Modular   arithmetic ")
        assert (a.name, a.drafted_name, a.key) == ("Modular arithmetic", "A", "a")
        for bad, message in (("", "blank"), ("x" * 121, "longer"), ("b", "another concept")):
            with pytest.raises(ValueError, match=message):
                m.rename_concept(row, a, bad)
        m.set_dropped(row, b, True)
        assert b.status == "dropped"
        m.set_dropped(row, b, False)
        assert b.status == "planned"  # it was new
        b.match_method, b.status = "exact", "known"
        m.set_dropped(row, b, True)
        m.set_dropped(row, b, False)
        assert b.status == "known"


def test_reorder_steps_keeps_prerequisites_earlier(settings: Settings) -> None:
    eid = make(settings)
    with session_scope(settings) as session:
        m = ExplorationManager(session, settings, owner=None)
        row = m.get(eid)
        for order in ([1, 2], [1, 1, 2], [0, 1, 2], [1, 2, "3"], [True, 2, 3]):
            with pytest.raises(ValueError, match="each of the steps 1 to 3 exactly once"):
                m.reorder_steps(row, order)
        with pytest.raises(ValueError, match="'b' would be in step 1 but builds on 'a' in step 2"):
            m.reorder_steps(row, [2, 1, 3])
        # Dropping b frees c's step and a's: b's prerequisite is no longer checked.
        b = next(c for c in m.concepts(row) if c.key == "b")
        m.set_dropped(row, b, True)
        m.reorder_steps(row, [3, 1, 2])
        assert row.steps == ["C", "A", "B"]
        assert [(c.key, c.step, c.position) for c in m.concepts(row)] == [
            ("c", 1, 1), ("d", 1, 2), ("a", 2, 3), ("b", 3, 4)]


def test_decisions(settings: Settings, tmp_path: Path) -> None:
    eid = make(settings)
    with session_scope(settings) as session:
        m = ExplorationManager(session, settings, owner=None)
        row = m.get(eid)
        first, second = m.candidates(row)
        m.decide(row, first, "approved")
        assert first.decision == "approved" and first.decided_at is not None
        m.decide(row, second, "rejected")
        m.decide(row, second, "proposed")
        assert second.decision == "proposed"
        with pytest.raises(ValueError, match="unknown decision 'ingested'"):
            m.decide(row, first, "ingested")
        assert [c.id for c in m.approved_candidates(row)] == [first.id]
        # Milestone 3: ingesting and grounding.
        path = tmp_path / "s.md"
        path.write_text("# S\n", encoding="utf-8")
        source = KnowledgeSourceManager(session, settings).add(path, slug="s", kind="web_page", title="S")
        with pytest.raises(StateConflictError, match="only an approved candidate"):
            m.mark_ingested(second, source)
        m.mark_ingested(first, source)
        assert (first.decision, first.knowledge_source_id) == ("ingested", source.id)
        with pytest.raises(StateConflictError, match="already ingested"):
            m.decide(row, first, "proposed")
        assert m.approved_candidates(row) == []
        concept = ConceptManager(session, settings).create("B", "b.", "user")
        b = next(c for c in m.concepts(row) if c.key == "b")
        m.mark_grounded(b, concept)
        assert (b.status, b.concept_id) == ("grounded", concept.id)
        with pytest.raises(StateConflictError, match="grounded and cannot be dropped"):
            m.set_dropped(row, b, True)
        with pytest.raises(StateConflictError):
            m.mark_grounded(b, concept)


def test_approved_candidates_leave_out_those_of_dropped_concepts_only(settings: Settings) -> None:
    eid = make(settings)
    with session_scope(settings) as session:
        m = ExplorationManager(session, settings, owner=None)
        row = m.get(eid)
        first, _ = m.candidates(row)
        m.decide(row, first, "approved")
        by_key = {c.key: c for c in m.concepts(row)}
        m.set_dropped(row, by_key["b"], True)
        assert m.approved_candidates(row) == [first]  # still linked to c
        m.set_dropped(row, by_key["c"], True)
        assert m.approved_candidates(row) == []


def test_edits_only_on_a_completed_exploration(settings: Settings) -> None:
    eid = make(settings, complete=False)
    with session_scope(settings) as session:
        m = ExplorationManager(session, settings, owner=None)
        row = m.get(eid)
        concept, cand = m.concepts(row)[0], m.candidates(row)[0]
        for edit in (
            lambda: m.rename_concept(row, concept, "X"),
            lambda: m.set_dropped(row, concept, True),
            lambda: m.reorder_steps(row, [1, 2, 3]),
            lambda: m.decide(row, cand, "approved"),
        ):
            with pytest.raises(StateConflictError, match="is running: only a completed exploration"):
                edit()
        m.fail(row, "boom")
        with pytest.raises(StateConflictError, match="is failed, not running"):
            m.begin_stage(row, "brief")
        with pytest.raises(StateConflictError):
            m.fail(row, "again")


def test_a_prerequisite_may_share_its_step_but_not_follow_it() -> None:
    from teacher.managers.explorations import check_step_order

    check_step_order({"a": 1, "b": 1}, {"b": ["a"]})
    with pytest.raises(ValueError, match="must stay in an earlier step"):
        check_step_order({"a": 2, "b": 1}, {"b": ["a"]})
