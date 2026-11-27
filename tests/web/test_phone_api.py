"""The phone summary (`GET /api/phone/summary`, `teacher.web.routers.phone`): it agrees with the
review queue, lists upcoming due times, counts answers per local day (offline ones by when they
were made) and names the page to resume."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from teacher.config import Settings
from teacher.review_bank import StubReviewBankBuilder
from teacher.web.routers import phone
from tests.web.test_review_api import (
    TZ,
    Clock,
    answer,
    clear_page,
    generate,
    make_client,
    queue,
)

URL = "/api/phone/summary"


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def client(settings: Settings, clock: Clock) -> Iterator[TestClient]:
    yield from make_client(settings, clock, review_bank_builder=StubReviewBankBuilder())


def summary(client: TestClient, tz: str = TZ) -> dict[str, Any]:
    r = client.get(URL, params={"tz": tz})
    assert r.status_code == 200, r.text
    return r.json()


def local_today(clock: Clock, tz: str) -> date:
    return clock.now.replace(tzinfo=UTC).astimezone(ZoneInfo(tz)).date()


@pytest.mark.parametrize("params", [{}, {"tz": ""}, {"tz": "Mars/Olympus"}, {"tz": "utc+1"}])
def test_the_time_zone_is_required_and_checked(client: TestClient, params: dict) -> None:
    r = client.get(URL, params=params)
    assert r.status_code == 422, r.text
    assert "tz" in r.json()["detail"] or "time zone" in r.json()["detail"]


def test_an_empty_library(client: TestClient, clock: Clock) -> None:
    out = summary(client)
    assert set(out) == {
        "now", "tz", "today", "due_now", "review", "upcoming_due", "daily_answers", "continue",
    }
    assert out["now"] == clock.now.replace(tzinfo=UTC).isoformat() and out["tz"] == TZ
    assert out["today"] == local_today(clock, TZ).isoformat()
    assert out["due_now"] == 0 and out["continue"] is None
    assert out["review"] == {
        "cap": 20, "done_today": 0, "remaining_today": 20, "items_total": 0, "next_due_at": None,
    }
    assert out["upcoming_due"]["total"] == 0 and out["upcoming_due"]["times"] == []
    assert out["upcoming_due"]["window_days"] == 7 and out["upcoming_due"]["limit"] == 100
    daily = out["daily_answers"]
    assert daily["days"] == 60 and len(daily["items"]) == 60 and daily["streak_days"] == 0
    assert daily["last_day"] == out["today"] == daily["items"][-1]["date"]
    assert daily["first_day"] == daily["items"][0]["date"]
    assert date.fromisoformat(daily["last_day"]) - date.fromisoformat(daily["first_day"]) == (
        timedelta(days=59)
    )
    assert all(
        d == {"date": d["date"], "page_answers": 0, "review_answers": 0, "total": 0}
        for d in daily["items"]
    )


def test_it_agrees_with_the_review_queue(client: TestClient, clock: Clock) -> None:
    slug = generate(client)
    clear_page(client, slug)  # three answers on the page, two questions for review
    out = summary(client)
    today = out["daily_answers"]["items"][-1]
    assert (today["page_answers"], today["review_answers"], today["total"]) == (3, 0, 3)
    assert out["daily_answers"]["streak_days"] == 1
    assert out["due_now"] == queue(client)["due"] == 0
    upcoming = out["upcoming_due"]
    assert upcoming["total"] == 2 and len(upcoming["times"]) == 2
    assert upcoming["times"] == sorted(upcoming["times"])
    assert upcoming["times"][0] == queue(client)["next_due_at"] == out["review"]["next_due_at"]
    resume = out["continue"]
    assert resume["artifact"]["slug"] == slug and resume["url"] == f"/artifacts/{slug}"
    assert resume == client.get("/api/history/continue", params={"limit": 1}).json()["items"][0]

    clock.advance(days=4)
    out = summary(client)
    q = queue(client, limit=1)
    assert out["due_now"] == q["due"] == 2
    assert out["upcoming_due"]["total"] == 0
    assert out["review"]["items_total"] == q["items_total"] == 2
    item = q["items"][0]
    response = {"choice": 0} if item["block"]["type"] == "multiple_choice" else {"text": "w " * 30}
    assert answer(client, item, response).status_code == 201
    out = summary(client)
    assert out["due_now"] == queue(client)["due"] == 1
    assert out["review"]["done_today"] == 1 and out["review"]["remaining_today"] == 19
    today = out["daily_answers"]["items"][-1]
    assert (today["page_answers"], today["review_answers"], today["total"]) == (0, 1, 1)
    # the page answers four days ago are still counted, on their day
    assert out["daily_answers"]["items"][-5]["page_answers"] == 3
    # the answered question comes back within the week: its time is listed
    assert out["upcoming_due"]["total"] == 1
    assert out["daily_answers"]["streak_days"] == 1  # days -4 .. -2 had none


def test_upcoming_times_are_capped(
    client: TestClient, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    slug = generate(client)
    clear_page(client, slug)
    monkeypatch.setattr(phone, "UPCOMING_LIMIT", 1)
    upcoming = summary(client)["upcoming_due"]
    assert (upcoming["total"], upcoming["limit"], len(upcoming["times"])) == (2, 1, 1)
    clock.advance(days=-5)  # both due more than a week from now
    upcoming = summary(client)["upcoming_due"]
    assert (upcoming["total"], upcoming["times"]) == (0, [])


def test_offline_answers_count_on_the_day_they_were_made(client: TestClient, clock: Clock) -> None:
    tz = "America/Los_Angeles"
    zone = ZoneInfo(tz)
    slug = generate(client)
    today = local_today(clock, tz)

    def local(day: date, hour: int, minute: int) -> str:
        return datetime.combine(day, time(hour, minute), tzinfo=zone).isoformat()

    bundle = client.get(f"/api/offline/artifacts/{slug}").json()
    attempts = [
        {"client_id": str(uuid.uuid4()), "key": "b02", "response": {"choice": 1},
         "verdict": "failed", "grader": "device:test",
         "made_at": local(today - timedelta(days=2), 23, 50)},
        {"client_id": str(uuid.uuid4()), "key": "b02", "response": {"choice": 0},
         "verdict": "passed", "grader": "device:test",
         "made_at": local(today - timedelta(days=1), 0, 10)},
    ]
    r = client.post(
        f"/api/offline/artifacts/{slug}/attempts",
        json={"version": bundle["version"], "attempts": attempts},
    )
    assert r.status_code == 200 and r.json()["accepted"] == 2, r.text
    items = summary(client, tz)["daily_answers"]["items"]
    assert [d["page_answers"] for d in items[-3:]] == [1, 1, 0]
    assert items[-3]["date"] == (today - timedelta(days=2)).isoformat()
    assert summary(client, tz)["daily_answers"]["streak_days"] == 2
    # in another zone the same two answers can fall on other days, but they are still two
    other = summary(client, "Asia/Tokyo")["daily_answers"]["items"]
    assert sum(d["page_answers"] for d in other) == 2
