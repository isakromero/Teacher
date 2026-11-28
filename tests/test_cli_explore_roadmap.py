"""`teacher explore --roadmap <id> [--json]` (`teacher.cli.explore_roadmap`): the roadmap as text
(progress, next action, each step and concept with its source and page) and as JSON, read-only
(nothing is written, nothing generated), and the errors that give status 1."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select

from teacher import cli
from teacher.config import Settings
from teacher.db.engine import session_scope
from teacher.db.models import Artifact, ExplorationConcept, ReviewItem
from teacher.managers import ExplorationManager
from tests.roadmap_fakes import World, answer, build_world, study_page


@pytest.fixture
def world(settings: Settings, settings_env: Any, tmp_path: Path) -> World:
    settings_env(settings)
    return build_world(settings, tmp_path)


def snapshot(settings: Settings) -> tuple[Any, ...]:
    with session_scope(settings) as session:
        rows = session.execute(
            select(ExplorationConcept.id, ExplorationConcept.primary_source_id,
                   ExplorationConcept.marked_known_at, ExplorationConcept.updated_at)  # fmt: skip
        ).all()
        return (
            tuple(rows),
            session.scalar(select(func.count()).select_from(Artifact)),
            session.scalar(select(func.count()).select_from(ReviewItem)),
        )


def test_the_roadmap_as_text(settings: Settings, world: World, capsys: pytest.CaptureFixture[str]) -> None:
    study_page(settings, "number-theory", "nt-page")
    answer(settings, "nt-page", [True])
    before = snapshot(settings)
    assert cli.main(["explore", "--roadmap", str(world.eid)]) == 0
    out = capsys.readouterr().out
    assert f"Roadmap of exploration {world.eid}: 'Diffie-Hellman' (level intro): step 1 of 3." in out
    assert "Progress: 0 of 5 concept(s) done, 0 of 3 step(s) done." in out
    assert "Next: Continue “Study: nt-page” to study Modular arithmetic (1 of 2 gates cleared). <" \
           "/artifacts/nt-page>" in out
    assert "Step 1: Foundations [current; 0 of 2 done]" in out
    assert "  [ ] Modular arithmetic (standing unstudied)" in out
    assert "      source (recommended): Number theory [number-theory]" in out
    assert "      page: Study: nt-page [nt-page]: 1 of 2 gate(s) cleared" in out
    assert "      its source is also the source of: Group" in out
    assert "      other grounding sources: dlog-paper" in out
    assert "      page: none yet (generate it on the roadmap page)" in out
    assert "  [ ] Elliptic curves (standing not in the graph)" in out
    assert "      NOT GROUNDED: no library source introduces it; enrich first" in out
    assert "Dropped (not counted): Extra" in out
    assert snapshot(settings) == before  # read-only


def test_the_roadmap_as_json(settings: Settings, world: World, capsys: pytest.CaptureFixture[str]) -> None:
    with session_scope(settings) as session:
        m = ExplorationManager(session, settings, owner=None)
        row = m.get(world.eid)
        m.set_marked_known(row, m.concept(row, world.rows["ma"]), True)
    assert cli.main(["explore", "--roadmap", str(world.eid), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["exploration"]["id"] == world.eid
    assert data["progress"]["concepts_done"] == 1
    assert data["steps"][0]["concepts"][0]["done_by"] == ["marked_known"]
    assert data["next_action"]["kind"] == "generate_page"


def test_errors_give_status_1(settings: Settings, world: World, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["explore", "--roadmap", "999"]) == 1
    assert "no exploration 999" in capsys.readouterr().err
    with session_scope(settings) as session:
        running = ExplorationManager(session, settings, owner=None).create("Running", "intro").id
    assert cli.main(["explore", "--roadmap", str(running)]) == 1
    assert "only a completed exploration has a roadmap" in capsys.readouterr().err
    assert cli.main(["explore", "--roadmap", str(world.eid), "--level", "intro"]) == 1
    assert "--level is for exploring a topic, not with --roadmap" in capsys.readouterr().err
    assert cli.main(["explore", "--roadmap", str(world.eid), "--yes"]) == 1
    assert "--yes is only used with --enrich" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        cli.main(["explore", "--roadmap", str(world.eid), "--enrich", str(world.eid)])
    with pytest.raises(SystemExit):
        cli.main(["explore", "Topic", "--roadmap", str(world.eid)])
