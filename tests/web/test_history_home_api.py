"""The study history endpoints behind the home page: `GET /api/history/summary` (today and this
week in the reader's time zone, the newest sessions) and `GET /api/history/continue` (the pages
studied most recently, with their progress)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from teacher.config import Settings
from teacher.db.engine import session_scope
from teacher.db.models import BlockResponse, utcnow
from teacher.generation import StubGenerator
from teacher.grading import MultipleChoiceGrader, WordCountBlurbGrader
from teacher.lab_generator import StubLabGenerator
from teacher.web.app import create_app

Json = dict[str, Any]


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    return create_app(
        settings,
        generator=StubGenerator(),
        lab_generator=StubLabGenerator(),
        graders={"multiple_choice": MultipleChoiceGrader(), "blurb": WordCountBlurbGrader()},
    )


@pytest.fixture
def make_page(client: TestClient, upload_source: Callable[..., Json]) -> Callable[[str], Json]:
    def make(name: str) -> Json:
        upload_source(title=name, slug=name.lower())
        created = client.post("/api/artifacts", json={"sources": [name.lower()]})
        assert created.status_code == 202, created.text
        return client.get(f"/api/artifacts/{created.json()['slug']}").json()

    return make


def summary(client: TestClient, **params: Any) -> Json:
    response = client.get("/api/history/summary", params={"tz": "UTC", **params})
    assert response.status_code == 200, response.text
    return response.json()


def answer(client: TestClient, slug: str, choice: int) -> Json:
    response = client.post(f"/api/artifacts/{slug}/blocks/b02/responses", json={"response": {"choice": choice}})
    assert response.status_code == 201, response.text
    return response.json()


# -- summary ------------------------------------------------------------------------------------


def test_summary_needs_a_known_time_zone(client: TestClient) -> None:
    missing = client.get("/api/history/summary")
    assert missing.status_code == 422 and "tz" in missing.json()["detail"]
    unknown = client.get("/api/history/summary", params={"tz": "Nowhere/Town"})
    assert unknown.status_code == 422
    assert "unknown time zone 'Nowhere/Town'" in unknown.json()["detail"]
    assert client.get("/api/history/summary", params={"tz": "UTC", "sessions": 0}).status_code == 422


def test_summary_of_nothing(client: TestClient) -> None:
    data = summary(client)
    assert set(data) == {"tz", "now", "today", "week", "total_sessions", "recent_sessions"}
    empty = {"active_seconds": 0.0, "sessions": 0, "answers": 0, "questions": 0,
             "first_attempts": 0, "first_attempts_passed": 0, "first_attempt_pass_rate": None}
    assert {k: v for k, v in data["today"].items() if k != "since"} == empty
    assert {k: v for k, v in data["week"].items() if k != "since"} == empty
    assert data["total_sessions"] == 0 and data["recent_sessions"] == []


@pytest.mark.parametrize("zone", ["UTC", "Pacific/Auckland", "America/Los_Angeles"])
def test_today_and_week_start_in_the_readers_time_zone(client: TestClient, zone: str) -> None:
    data = summary(client, tz=zone)
    tz = ZoneInfo(zone)
    local = datetime.fromisoformat(data["now"]).astimezone(tz)
    day = datetime.combine(local.date(), time.min, tzinfo=tz)
    week = datetime.combine(local.date() - timedelta(days=local.weekday()), time.min, tzinfo=tz)
    assert datetime.fromisoformat(data["today"]["since"]) == day
    assert datetime.fromisoformat(data["week"]["since"]) == week
    assert datetime.fromisoformat(data["week"]["since"]).astimezone(tz).weekday() == 0  # Monday
    assert data["tz"] == zone


def test_summary_counts_answers_and_sessions(
    client: TestClient, make_page: Callable[[str], Json], settings: Settings
) -> None:
    page = make_page("Optics")
    client.post("/api/history/heartbeat", json={"surface": "study_page", "slug": page["slug"]})
    answer(client, page["slug"], 1)  # wrong at the first attempt
    answer(client, page["slug"], 0)  # right at the second
    data = summary(client)
    for period in ("today", "week"):
        p = data[period]
        assert (p["answers"], p["questions"], p["first_attempts"], p["first_attempts_passed"]) == (2, 1, 1, 0)
        assert p["first_attempt_pass_rate"] == 0.0 and p["sessions"] == 1
    assert data["total_sessions"] == 1
    (recent,) = data["recent_sessions"]
    assert [a["slug"] for a in recent["artifacts"]] == [page["slug"]]
    assert recent["answers"] == 2 and recent["answers_passed"] == 1

    # An answer from long ago counts in neither period.
    with session_scope(settings) as session:
        first = session.scalar(select(BlockResponse).where(BlockResponse.attempt == 1))
        session.execute(
            update(BlockResponse).where(BlockResponse.id == first.id)
            .values(created_at=utcnow() - timedelta(days=30))
        )
    week = summary(client)["week"]
    assert (week["answers"], week["first_attempts"], week["first_attempt_pass_rate"]) == (1, 0, None)


def test_summary_lists_the_newest_sessions(client: TestClient, make_page: Callable[[str], Json]) -> None:
    make_page("Optics")
    data = summary(client, sessions=1)
    assert len(data["recent_sessions"]) == 1 and data["total_sessions"] == 1


# -- continue studying ---------------------------------------------------------------------------


def test_continue_lists_the_pages_studied_last(client: TestClient, make_page: Callable[[str], Json]) -> None:
    assert client.get("/api/history/continue").json() == {"items": []}
    optics = make_page("Optics")
    acoustics = make_page("Acoustics")
    # Making a page is not studying it.
    assert client.get("/api/history/continue").json() == {"items": []}

    assert client.get(f"/artifacts/{optics['slug']}").status_code == 200  # opening it is
    answer(client, optics["slug"], 0)
    assert client.get(f"/artifacts/{acoustics['slug']}").status_code == 200
    items = client.get("/api/history/continue").json()["items"]
    assert [i["artifact"]["slug"] for i in items] == [acoustics["slug"], optics["slug"]]
    first = items[1]
    assert set(first) == {"artifact", "url", "last_studied_at", "active_seconds"}
    assert first["url"] == f"/artifacts/{optics['slug']}"
    assert first["artifact"]["progress"]["gates_cleared"] == 1
    assert first["artifact"]["progress"]["gates_total"] == optics["progress"]["gates_total"] > 1
    assert first["active_seconds"] == 0.0
    assert datetime.fromisoformat(first["last_studied_at"]).tzinfo is not None

    limited = client.get("/api/history/continue", params={"limit": 1}).json()["items"]
    assert [i["artifact"]["slug"] for i in limited] == [acoustics["slug"]]
    for bad in (0, 21):
        assert client.get("/api/history/continue", params={"limit": bad}).status_code == 422


def test_continue_includes_labs(client: TestClient, make_page: Callable[[str], Json]) -> None:
    page = make_page("Optics")
    lab = client.post(f"/api/artifacts/{page['slug']}/labs")
    assert lab.status_code == 202, lab.text
    slug = lab.json()["slug"]
    assert client.get(f"/labs/{slug}").status_code == 200
    (item,) = client.get("/api/history/continue").json()["items"]
    assert item["url"] == f"/labs/{slug}" and item["artifact"]["kind"] == "lab"
    assert datetime.fromisoformat(item["last_studied_at"]) <= datetime.now(UTC)


def test_continue_lists_one_version_of_a_page(
    client: TestClient, make_page: Callable[[str], Json]
) -> None:
    old = make_page("Optics")
    regenerated = client.post(f"/api/artifacts/{old['slug']}/regenerate")
    assert regenerated.status_code == 202, regenerated.text
    new = regenerated.json()
    assert client.get(f"/artifacts/{old['slug']}").status_code == 200
    assert client.get(f"/artifacts/{new['slug']}").status_code == 200
    items = client.get("/api/history/continue").json()["items"]
    assert [i["artifact"]["slug"] for i in items] == [new["slug"]]
