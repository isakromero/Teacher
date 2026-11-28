"""The roadmap of a completed exploration (`teacher.exploration.roadmap`) and the reader's two
choices on it (`ExplorationManager.set_primary_source`, `set_marked_known`): the assembly (study
order, dropped concepts, a source shared by two concepts with its one page, an existing page,
ungrounded concepts), the done rules for concepts and steps, the next action in each case, and
the choices with their undo and validation. Built from managers only (tests/roadmap_fakes.py);
no model call."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from teacher.config import Settings
from teacher.db.engine import session_scope
from teacher.db.models import utcnow
from teacher.exploration import roadmap_summary, roadmap_view
from teacher.managers import (
    ArtifactManager,
    ConceptManager,
    ExplorationManager,
    KnowledgeSourceManager,
    LinkSpec,
    NotFoundError,
    SourceConceptManager,
)
from teacher.managers.errors import StateConflictError
from teacher.managers.review import ReviewManager
from tests.enrichment_fakes import completed_exploration, outline
from tests.roadmap_fakes import World, answer, build_world, study_page


@pytest.fixture
def world(settings: Settings, tmp_path: Path) -> World:
    return build_world(settings, tmp_path)


def view(settings: Settings, eid: int, *, now: Any = None, owner: int | None = None) -> dict[str, Any]:
    with session_scope(settings) as session:
        row = ExplorationManager(session, settings, owner=owner).get(eid)
        return roadmap_view(session, settings, row, max_attempts=2, now=now)


def concepts(v: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {c["key"]: c for step in v["steps"] for c in step["concepts"]}


def choose(settings: Settings, world: World, key: str, source: str | None) -> None:
    with session_scope(settings) as session:
        m = ExplorationManager(session, settings, owner=None)
        row = m.get(world.eid)
        found = KnowledgeSourceManager(session, settings).get_by_slug(source) if source else None
        m.set_primary_source(row, m.concept(row, world.rows[key]), found)


def mark(settings: Settings, world: World, key: str, known: bool) -> None:
    with session_scope(settings) as session:
        m = ExplorationManager(session, settings, owner=None)
        row = m.get(world.eid)
        m.set_marked_known(row, m.concept(row, world.rows[key]), known)


# -- assembly -------------------------------------------------------------------------------------


def test_the_roadmap_lists_the_steps_in_order_without_the_dropped_concepts(
    settings: Settings, world: World
) -> None:
    v = view(settings, world.eid)
    assert [(s["number"], s["title"], [c["key"] for c in s["concepts"]]) for s in v["steps"]] == [
        (1, "Foundations", ["ma", "gr"]),
        (2, "Next", ["dl"]),
        (3, "Last", ["dh", "ec"]),
    ]
    assert v["dropped"] == [{"key": "xx", "name": "Extra"}]
    c = concepts(v)
    assert c["dl"]["prerequisites"] == [{"key": "ma", "name": "Modular arithmetic"}]
    # dl has two grounding sources: the older is recommended; nothing is chosen.
    assert [(s["slug"], s["recommended"], s["chosen"]) for s in c["dl"]["sources"]] == [
        ("dlog-notes", True, False), ("dlog-paper", False, False)]
    assert c["dl"]["primary_source"] == {"slug": "dlog-notes", "title": "Discrete log notes", "chosen": False}
    assert c["dl"]["standing"]["status"] == "unstudied"
    assert c["dl"]["state"] == "generate" and c["dl"]["can_generate"] is True
    # dh: its only source uses it, so it is not grounded; ec is not in the graph at all.
    assert c["dh"]["grounded"] is False and c["dh"]["primary_source"] is None and c["dh"]["sources"] == []
    assert c["dh"]["state"] == "ungrounded" and c["dh"]["can_generate"] is False
    assert c["ec"]["concept"] is None and c["ec"]["standing"] is None and c["ec"]["state"] == "ungrounded"
    assert v["progress"] == {"concepts_done": 0, "concepts_total": 5, "steps_done": 0,
                             "steps_total": 3, "complete": False}  # fmt: skip
    assert v["current_step"] == 1
    assert [s["state"] for s in v["steps"]] == ["current", "upcoming", "upcoming"]
    assert v["url"] == f"/explore/{world.eid}/roadmap" and v["review_url"] == f"/explore/{world.eid}"


def test_a_source_shared_by_two_concepts_has_one_page_shown_under_both(
    settings: Settings, world: World
) -> None:
    c = concepts(view(settings, world.eid))
    assert c["ma"]["primary_source"]["slug"] == c["gr"]["primary_source"]["slug"] == "number-theory"
    assert c["ma"]["shared_with"] == ["Group"] and c["gr"]["shared_with"] == ["Modular arithmetic"]
    study_page(settings, "number-theory", "nt-page")
    c = concepts(view(settings, world.eid))
    assert c["ma"]["page"] == c["gr"]["page"]
    assert c["ma"]["page"]["slug"] == "nt-page" and c["ma"]["page"]["url"] == "/artifacts/nt-page"
    assert c["ma"]["state"] == c["gr"]["state"] == "page"
    assert c["ma"]["can_generate"] is False


def test_which_page_of_a_source_counts(settings: Settings, world: World) -> None:
    # A failed one does not; it is shown as the last attempt.
    study_page(settings, "dlog-notes", "dl-failed", status="failed")
    c = concepts(view(settings, world.eid))["dl"]
    assert c["page"] is None and c["failed_page"]["slug"] == "dl-failed" and c["can_generate"] is True
    # A generating one does, and is not complete.
    study_page(settings, "dlog-notes", "dl-generating", status="generating")
    c = concepts(view(settings, world.eid))["dl"]
    assert c["page"]["slug"] == "dl-generating" and c["page"]["complete"] is False
    assert c["failed_page"] is None  # the failure is older than the page
    # A ready page superseded by a failed regeneration is still the page; a lab never is.
    with session_scope(settings) as session:
        artifacts = ArtifactManager(session, settings)
        artifacts.delete(artifacts.get_by_slug("dl-generating"))
    study_page(settings, "dlog-notes", "dl-ready")
    with session_scope(settings) as session:
        artifacts = ArtifactManager(session, settings)
        old = artifacts.get_by_slug("dl-ready")
        new = artifacts.create(slug="dl-ready-2", kind="study_page", title="Study again",
                               generator="test", sources=[old.sources[0]], supersedes=old)  # fmt: skip
        artifacts.mark_failed(new)
        lab = artifacts.create(slug="dl-lab", kind="lab", title="Lab", generator="test",
                               sources=[old.sources[0]], params={"study_page": "dl-ready"})  # fmt: skip
        artifacts.mark_ready(lab)
    c = concepts(view(settings, world.eid))["dl"]
    assert c["page"]["slug"] == "dl-ready" and c["failed_page"]["slug"] == "dl-ready-2"


def test_the_roadmap_needs_a_completed_exploration(settings: Settings) -> None:
    with session_scope(settings) as session:
        m = ExplorationManager(session, settings, owner=None)
        row = m.create("Topic", "intro")
        with pytest.raises(StateConflictError, match="only a completed exploration has a roadmap"):
            roadmap_view(session, settings, row, max_attempts=2)


# -- the done rules ---------------------------------------------------------------------------------


def test_a_concept_is_done_when_marked_known_strong_or_its_page_is_complete(
    settings: Settings, world: World
) -> None:
    # Marked "I know this".
    mark(settings, world, "ec", True)
    c = concepts(view(settings, world.eid))
    assert c["ec"]["done"] is True and c["ec"]["done_by"] == ["marked_known"]
    assert c["ec"]["marked_known"] is True and c["ec"]["marked_known_at"] is not None
    # Strong standing, from questions on another source's page tagged with it.
    study_page(settings, "dlog-paper", "dl-other", tag="discrete-logarithm")
    answer(settings, "dl-other", [True, True])
    c = concepts(view(settings, world.eid))["dl"]
    assert c["standing"]["status"] == "strong" and c["done_by"] == ["strong"]
    assert c["page"] is None  # its primary source (the notes) has no page: still done
    # The primary source's page complete (every gate cleared), with no question tagged.
    study_page(settings, "number-theory", "nt-page")
    answer(settings, "nt-page", [True, False])
    c = concepts(view(settings, world.eid))
    assert c["ma"]["done"] is False and c["ma"]["page"]["gates_cleared"] == 1
    answer(settings, "nt-page", [None, False])  # the second attempt on gate 2 reveals it: cleared
    c = concepts(view(settings, world.eid))
    assert c["ma"]["page"]["complete"] is True
    assert c["ma"]["done_by"] == c["gr"]["done_by"] == ["page_complete"]  # one page, both done
    # Every reason that holds is listed.
    mark(settings, world, "dl", True)
    assert concepts(view(settings, world.eid))["dl"]["done_by"] == ["marked_known", "strong"]


def test_a_step_is_done_when_all_its_concepts_are_and_the_current_step_is_the_first_open_one(
    settings: Settings, world: World
) -> None:
    mark(settings, world, "ma", True)
    v = view(settings, world.eid)
    assert [(s["state"], s["done"], s["total"]) for s in v["steps"]] == [
        ("current", 1, 2), ("upcoming", 0, 1), ("upcoming", 0, 2)]
    # A later step done before the current one is done; the current one stays the first open.
    mark(settings, world, "dh", True)
    mark(settings, world, "ec", True)
    v = view(settings, world.eid)
    assert [s["state"] for s in v["steps"]] == ["current", "upcoming", "done"]
    assert v["current_step"] == 1 and v["progress"]["steps_done"] == 1
    mark(settings, world, "gr", True)
    v = view(settings, world.eid)
    assert [s["state"] for s in v["steps"]] == ["done", "current", "done"] and v["current_step"] == 2
    mark(settings, world, "dl", True)
    v = view(settings, world.eid)
    assert v["current_step"] is None and v["progress"] == {
        "concepts_done": 5, "concepts_total": 5, "steps_done": 3, "steps_total": 3, "complete": True}
    assert v["next_action"]["kind"] == "done" and v["next_action"]["text"] == "Every step is done."
    # The dropped concept does not count; taking it back (on the review page) makes step 3 open.
    with session_scope(settings) as session:
        m = ExplorationManager(session, settings, owner=None)
        row = m.get(world.eid)
        m.set_dropped(row, m.concept(row, world.rows["xx"]), False)
    v = view(settings, world.eid)
    assert v["current_step"] == 3 and v["steps"][2]["total"] == 3


# -- the next action ------------------------------------------------------------------------------


def test_the_next_action_in_each_case(settings: Settings, world: World) -> None:
    # Nothing yet: the first concept of step 1 has a grounding source and no page.
    a = view(settings, world.eid)["next_action"]
    assert a["kind"] == "generate_page" and a["step"] == 1
    assert a["concept"] == {"id": world.rows["ma"], "key": "ma", "name": "Modular arithmetic"}
    assert a["source"] == {"slug": "number-theory", "title": "Number theory"}
    assert a["text"] == ("Generate a study page from “Number theory” to study Modular arithmetic. "
                         "It is also the source for Group.")  # fmt: skip
    # A page being generated: open it to watch.
    study_page(settings, "number-theory", "nt-page", status="generating")
    a = view(settings, world.eid)["next_action"]
    assert a["kind"] == "open_page" and a["url"] == "/artifacts/nt-page" and "being generated" in a["text"]
    # Ready, nothing cleared: open it; one gate cleared: continue it.
    with session_scope(settings) as session:
        ArtifactManager(session, settings).mark_ready(ArtifactManager(session, settings).get_by_slug("nt-page"))
    a = view(settings, world.eid)["next_action"]
    assert a["kind"] == "open_page" and a["text"] == "Open “Study: nt-page” to study Modular arithmetic."
    answer(settings, "nt-page", [True])
    a = view(settings, world.eid)["next_action"]
    assert a["kind"] == "continue_page" and a["page"]["gates_cleared"] == 1
    assert a["text"] == "Continue “Study: nt-page” to study Modular arithmetic (1 of 2 gates cleared)."
    # Step 1 done: step 2's concept has a source but no page.
    answer(settings, "nt-page", [None, True])
    a = view(settings, world.eid)["next_action"]
    assert a["kind"] == "generate_page" and a["step"] == 2 and a["concept"]["key"] == "dl"
    # Step 3: dh is not grounded: enrich, on the review page.
    mark(settings, world, "dl", True)
    a = view(settings, world.eid)["next_action"]
    assert a["kind"] == "enrich" and a["concept"]["key"] == "dh"
    assert a["url"] == f"/explore/{world.eid}#concept-dh"


def test_a_weak_concept_with_due_reviews_comes_before_a_new_page(settings: Settings, world: World) -> None:
    study_page(settings, "number-theory", "nt-page", tag="modular-arithmetic")
    answer(settings, "nt-page", [False, False])
    answer(settings, "nt-page", [False, False])  # both revealed: the page is complete
    with session_scope(settings) as session:
        assert ReviewManager(session, settings).sync(max_attempts=2) == 2
    later = utcnow() + timedelta(days=2)
    v = view(settings, world.eid, now=later)
    c = concepts(v)
    assert c["ma"]["standing"]["status"] == "weak" and c["ma"]["done_by"] == ["page_complete"]
    assert c["ma"]["review"]["due"] == 2 and c["ma"]["review"]["url"] == "/review?concept=modular-arithmetic"
    assert v["current_step"] == 2
    a = v["next_action"]
    assert a["kind"] == "review" and a["concept"]["key"] == "ma" and a["step"] == 1
    assert a["url"] == "/review?concept=modular-arithmetic" and a["review"]["due"] == 2
    assert a["text"] == ("Review Modular arithmetic in the review queue: your standing on it is weak "
                         "(0% right at the first try) and 2 questions are due now.")  # fmt: skip
    # Nothing due yet: no review; the new page comes next.
    assert view(settings, world.eid)["next_action"]["kind"] == "generate_page"
    # Marked known: it is not asked for again.
    mark(settings, world, "ma", True)
    assert view(settings, world.eid, now=later)["next_action"]["kind"] == "generate_page"


def test_a_chosen_source_that_no_longer_grounds_the_concept_is_said_not_replaced(
    settings: Settings, world: World
) -> None:
    choose(settings, world, "dl", "dlog-paper")
    with session_scope(settings) as session:  # its concepts extracted again: now it only uses it
        source = KnowledgeSourceManager(session, settings).get_by_slug("dlog-paper")
        dlog = ConceptManager(session, settings).get_by_slug("discrete-logarithm")
        SourceConceptManager(session, settings).replace(
            source, [LinkSpec(dlog, "uses", "created", "Discrete logarithm", evidence="in passing")]
        )
    mark(settings, world, "ma", True)
    mark(settings, world, "gr", True)
    v = view(settings, world.eid)
    c = concepts(v)["dl"]
    assert c["primary_source"] is None and c["state"] == "choose_source" and c["can_generate"] is False
    assert c["choice_problem"] == ("the source you chose, “Discrete log paper”, no longer introduces it. "
                                   "Choose another of its sources, or the recommended one.")  # fmt: skip
    assert [(s["slug"], s["chosen"]) for s in c["sources"]] == [("dlog-notes", False)]
    assert v["next_action"]["kind"] == "choose_source"
    choose(settings, world, "dl", None)
    c = concepts(view(settings, world.eid))["dl"]
    assert c["primary_source"]["slug"] == "dlog-notes" and c["choice_problem"] is None


def test_the_summary_for_the_list(settings: Settings, world: World) -> None:
    s = roadmap_summary(view(settings, world.eid))
    assert s["url"] == f"/explore/{world.eid}/roadmap"
    assert s["current_step"] == 1 and s["current_step_title"] == "Foundations"
    assert s["progress"]["concepts_total"] == 5 and s["next_action"]["kind"] == "generate_page"


# -- the reader's choices -------------------------------------------------------------------------


def test_choosing_another_grounding_source_and_going_back(settings: Settings, world: World) -> None:
    study_page(settings, "dlog-paper", "paper-page")
    choose(settings, world, "dl", "dlog-paper")
    c = concepts(view(settings, world.eid))["dl"]
    assert c["primary_source"] == {"slug": "dlog-paper", "title": "Discrete log paper", "chosen": True}
    assert [(s["slug"], s["recommended"], s["chosen"]) for s in c["sources"]] == [
        ("dlog-notes", True, False), ("dlog-paper", False, True)]
    assert c["recommended_source"]["slug"] == "dlog-notes"
    assert c["page"]["slug"] == "paper-page"  # the chosen source's page
    choose(settings, world, "dl", None)  # undo: the recommended one again
    c = concepts(view(settings, world.eid))["dl"]
    assert c["primary_source"]["chosen"] is False and c["primary_source"]["slug"] == "dlog-notes"
    assert c["page"] is None


def test_a_chosen_source_must_ground_the_concept(settings: Settings, world: World) -> None:
    with pytest.raises(ValueError, match=r"'dlog-notes' does not ground concept 'Group'.*choose one of its grounding sources: number-theory"):
        choose(settings, world, "gr", "dlog-notes")
    with pytest.raises(ValueError, match="'dh-blog' does not ground .* grounding sources: none"):
        choose(settings, world, "dh", "dh-blog")  # it only uses it
    with pytest.raises(ValueError, match="not in your concept graph yet"):
        choose(settings, world, "ec", "number-theory")
    with pytest.raises(StateConflictError, match="is dropped"):
        choose(settings, world, "xx", None)
    with pytest.raises(StateConflictError, match="is dropped"):
        mark(settings, world, "xx", True)
    assert concepts(view(settings, world.eid))["gr"]["primary_source"]["chosen"] is False


def test_the_choices_need_a_completed_exploration_of_the_owner(settings: Settings, world: World) -> None:
    other = completed_exploration(settings, [outline("a", "A", 1)])
    with session_scope(settings) as session:
        m = ExplorationManager(session, settings, owner=None)
        row = m.get(world.eid)
        foreign = m.concepts(m.get(other))[0]
        with pytest.raises(NotFoundError):
            m.set_marked_known(row, foreign, True)
        with pytest.raises(NotFoundError):
            m.set_primary_source(row, foreign, None)
        with pytest.raises(ValueError, match="true or false"):
            m.set_marked_known(row, m.concept(row, world.rows["ma"]), "yes")  # type: ignore[arg-type]
        running = m.create("Running", "intro")
        rows = m.store_outline(running, questions=[], steps=["S"], concepts=[
            {**outline("b", "B", 1), "position": 1}])  # fmt: skip
        with pytest.raises(StateConflictError, match="only a completed exploration has a roadmap"):
            m.set_marked_known(running, rows[0], True)


def test_marking_known_and_taking_it_back(settings: Settings, world: World) -> None:
    mark(settings, world, "ma", True)
    with session_scope(settings) as session:
        first = ExplorationManager(session, settings, owner=None).concept(
            ExplorationManager(session, settings, owner=None).get(world.eid), world.rows["ma"]
        ).marked_known_at
    mark(settings, world, "ma", True)  # again: the first time is kept
    c = concepts(view(settings, world.eid))["ma"]
    assert c["marked_known"] is True and c["marked_known_at"].startswith(first.isoformat())
    mark(settings, world, "ma", False)
    c = concepts(view(settings, world.eid))["ma"]
    assert c["marked_known"] is False and c["marked_known_at"] is None and c["done"] is False
    # The concept's status (known: matched to the graph) is not touched by the mark.
    with session_scope(settings) as session:
        m = ExplorationManager(session, settings, owner=None)
        assert m.concept(m.get(world.eid), world.rows["ma"]).status == "known"


def test_deleting_the_chosen_source_clears_the_choice(settings: Settings, world: World) -> None:
    choose(settings, world, "dl", "dlog-paper")
    with session_scope(settings) as session:
        sources = KnowledgeSourceManager(session, settings)
        sources.delete(sources.get_by_slug("dlog-paper"))
    with session_scope(settings) as session:
        m = ExplorationManager(session, settings, owner=None)
        assert m.concept(m.get(world.eid), world.rows["dl"]).primary_source_id is None
    c = concepts(view(settings, world.eid))["dl"]
    assert c["primary_source"]["slug"] == "dlog-notes" and c["choice_problem"] is None
