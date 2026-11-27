"""Offline review (`teacher.web.routers.offline_review`): the bundle of due and upcoming review
questions with what grading them on the phone needs, and the upload of answers made there."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from teacher.config import Settings
from teacher.db.engine import session_scope
from teacher.db.models import OfflineReviewAnswer, ReviewAttempt
from teacher.managers import ArtifactManager
from teacher.review_bank import StubReviewBankBuilder
from teacher.review_schedule import GROWTH
from tests.web.test_review_api import (
    LONG_ANSWER,
    TZ,
    Clock,
    answer,
    clear_page,
    generate,
    make_client,
    queue,
)

BUNDLE = "/api/offline/review"
UPLOAD = "/api/offline/review/answers"
DEVICE = "device:test-model"
REQUIRED = ["States the central idea", "Mentions supporting evidence"]


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def client(settings: Settings, clock: Clock) -> Iterator[TestClient]:
    yield from make_client(settings, clock, review_bank_builder=StubReviewBankBuilder())


@pytest.fixture
def page(client: TestClient) -> str:
    """A generated page whose two questions were cleared (first review due in three days)."""
    slug = generate(client)
    clear_page(client, slug)
    return slug


def bundle(client: TestClient, **params: Any) -> dict[str, Any]:
    r = client.get(BUNDLE, params={"tz": TZ, **params})
    assert r.status_code == 200, r.text
    return r.json()


def iso(value: datetime) -> str:
    return value.replace(tzinfo=UTC).isoformat()


def by_type(items: list[dict[str, Any]], type_: str) -> dict[str, Any]:
    return next(i for i in items if i["block"]["type"] == type_)


def answer_in(
    item: dict[str, Any], response: Any, *, made_at: datetime, verdict: str = "passed", **kw: Any
) -> dict[str, Any]:
    out = {
        "client_id": str(uuid.uuid4()),
        "item_id": item["id"],
        "presentation": item["presentation"],
        "response": response,
        "confidence": "medium",
        "verdict": verdict,
        "grader": DEVICE,
        "made_at": iso(made_at),
    }
    out.update(kw)
    return out


def upload(client: TestClient, answers: list[dict[str, Any]]) -> dict[str, Any]:
    r = client.post(UPLOAD, json={"answers": answers})
    assert r.status_code == 200, r.text
    return r.json()


def attempts_in_db(settings: Settings) -> int:
    with session_scope(settings) as session:
        return len(list(session.scalars(select(ReviewAttempt))))


# -- the bundle -----------------------------------------------------------------------------


def test_the_bundle_lists_due_and_upcoming_items(
    client: TestClient, page: str, clock: Clock
) -> None:
    out = bundle(client)
    assert set(out) == {
        "format", "format_version", "bundled_at", "tz", "day_start", "cap", "done_today",
        "remaining_today", "days", "until", "due", "upcoming", "default_min_words", "grading",
        "schedule", "items",
    }
    assert (out["format"], out["format_version"]) == ("teacher-offline-review-bundle", 1)
    assert out["bundled_at"] == iso(clock.now) and out["until"] == iso(clock.now + timedelta(days=3))
    assert (out["tz"], out["days"], out["cap"], out["remaining_today"]) == (TZ, 3, 20, 20)
    assert out["schedule"] == {"first_interval_days": 1.0, "growth": GROWTH, "max_interval_days": 180.0}
    assert out["grading"]["device_grader_prefix"] == "device:"
    assert out["grading"]["multiple_choice_wrong_prefix"] == "Not quite. "
    assert out["default_min_words"] == 20
    # cleared (after the clock was set) with clean first passes: first due a little more than
    # three days from the clock's now
    assert (out["due"], out["upcoming"], out["items"]) == (0, 0, [])
    assert bundle(client, days=0)["items"] == []
    week = bundle(client, days=4)
    assert (week["due"], week["upcoming"], len(week["items"])) == (0, 2, 2)
    assert [i["due_now"] for i in week["items"]] == [False, False]
    assert [i["due_at"] for i in week["items"]] == sorted(i["due_at"] for i in week["items"])

    clock.advance(days=4)
    out = bundle(client)
    assert (out["due"], out["upcoming"]) == (2, 0)
    q = queue(client, limit=5)
    # the same items in the same order, presented the same way (one token for both)
    assert [i["id"] for i in out["items"]] == [i["id"] for i in q["items"]]
    for mine, served in zip(out["items"], q["items"], strict=True):
        assert mine["presentation"] == served["presentation"]
        assert {k: v for k, v in mine.items() if k not in ("due_now", "last_answered_at", "private")} == served
        assert mine["due_now"] is True and mine["last_answered_at"].endswith("+00:00")
    mc = by_type(out["items"], "multiple_choice")
    shown = mc["block"]["content"]["options"]
    private = mc["private"]
    # the answer key is in the order shown
    assert private["correct_index"] in range(len(shown))
    assert len(private["option_explanations"]) == len(shown)
    assert "private" not in mc["block"]["content"]
    blurb = by_type(out["items"], "blurb")
    assert [c["criterion"] for c in blurb["private"]["rubric"] if c.get("required", True)] == REQUIRED
    assert blurb["private"]["model_answer"]
    # bundling again keeps the presentation
    assert [i["presentation"] for i in bundle(client)["items"]] == [
        i["presentation"] for i in out["items"]
    ]


@pytest.mark.parametrize(
    "params", [{}, {"tz": "Nowhere/City"}, {"tz": TZ, "days": -1}, {"tz": TZ, "days": 15},
               {"tz": TZ, "cap": 0}]
)
def test_bundle_parameters_are_checked(client: TestClient, params: dict) -> None:
    assert client.get(BUNDLE, params=params).status_code == 422


# -- uploading ------------------------------------------------------------------------------


def due_bundle(client: TestClient, clock: Clock) -> dict[str, Any]:
    clock.advance(days=4)
    return bundle(client)


def test_a_multiple_choice_answer_is_regraded_and_rescheduled(
    client: TestClient, page: str, clock: Clock, settings: Settings
) -> None:
    items = due_bundle(client, clock)["items"]
    mc = by_type(items, "multiple_choice")
    right = mc["private"]["correct_index"]
    made_at = clock.now - timedelta(hours=1)
    sent = answer_in(mc, {"choice": right}, made_at=made_at, verdict="failed")
    out = upload(client, [sent])
    assert (out["accepted"], out["already_accepted"], out["rejected"]) == (1, 0, 0)
    (result,) = out["results"]
    assert set(result) == {
        "client_id", "item_id", "status", "reason", "detail", "device_verdict", "regraded",
        "verdict_mismatch", "recorded",
    }
    assert (result["status"], result["regraded"], result["verdict_mismatch"]) == (
        "accepted", True, True
    )
    assert result["device_verdict"] == "failed"
    recorded = result["recorded"]
    assert recorded["verdict"] == "passed" and recorded["passed"] is True
    assert recorded["grader"] == "multiple-choice-v2" and recorded["confidence"] == "medium"
    assert recorded["made_at"] == iso(made_at)
    assert recorded["interval_days"] == pytest.approx(mc["interval_days"] * GROWTH)
    assert recorded["due_at"] == iso(made_at + timedelta(days=recorded["interval_days"]))
    assert (recorded["form"], recorded["variant"]) == (mc["form"], mc["variant"])
    # the queue sees it: answered, rescheduled from made_at, one review done today
    # (made_at is an hour before the clock, which just after local midnight is still yesterday)
    zone = ZoneInfo(TZ)
    assert clock.now.tzinfo is None  # the app's clock is naive UTC
    local_day = lambda t: t.replace(tzinfo=UTC).astimezone(zone).date()  # noqa: E731
    same_day = local_day(made_at) == local_day(clock.now)
    q = queue(client, limit=5)
    assert q["due"] == 1 and q["done_today"] == (1 if same_day else 0)
    assert mc["id"] not in [i["id"] for i in q["items"]]
    with session_scope(settings) as session:
        (record,) = session.scalars(select(OfflineReviewAnswer))
        assert (record.client_id, record.device_grader, record.device_verdict) == (
            sent["client_id"], DEVICE, "failed"
        )
        attempt = session.get(ReviewAttempt, record.attempt_id)
        assert attempt.created_at == made_at

    # sent again: nothing new, the same record back
    again = upload(client, [sent])
    assert again["already_accepted"] == 1 and again["results"][0]["recorded"] == recorded
    assert again["results"][0]["verdict_mismatch"] is True
    assert attempts_in_db(settings) == 1
    # the same client_id with another answer is a conflict
    other = {**sent, "response": {"choice": (right + 1) % 3}}
    assert upload(client, [other])["results"][0]["reason"] == "client_id_conflict"
    # the phone summary counts it on the day it was made
    summary = client.get("/api/phone/summary", params={"tz": TZ}).json()
    assert summary["due_now"] == 1
    assert sum(d["review_answers"] for d in summary["daily_answers"]["items"]) == 1


def test_a_blurb_keeps_the_phones_grade(
    client: TestClient, page: str, clock: Clock, settings: Settings
) -> None:
    blurb = by_type(due_bundle(client, clock)["items"], "blurb")
    made_at = clock.now - timedelta(minutes=5)
    bad = [
        answer_in(blurb, {"text": LONG_ANSWER}, made_at=made_at, verdict="partial",
                  missing=["Not a criterion"]),
        answer_in(blurb, {"text": LONG_ANSWER}, made_at=made_at, verdict="partial",
                  missing=REQUIRED),
        answer_in(blurb, {"text": LONG_ANSWER}, made_at=made_at, verdict="partial"),
        answer_in(blurb, {"text": "too short"}, made_at=made_at, verdict="failed"),
        answer_in(blurb, ["not", "an", "object"], made_at=made_at, verdict="failed"),
    ]
    out = upload(client, bad)
    assert [r["reason"] for r in out["results"]] == [
        "invalid_grade", "invalid_grade", "invalid_grade", "malformed_response",
        "malformed_response",
    ]
    assert attempts_in_db(settings) == 0
    good = answer_in(
        blurb, {"text": LONG_ANSWER}, made_at=made_at, verdict="partial", missing=REQUIRED[1:],
        feedback="Add the evidence.", details={"model": "on-device", "score": 0.5},
        confidence="high",
    )
    (result,) = upload(client, [good])["results"]
    assert (result["status"], result["regraded"], result["verdict_mismatch"]) == (
        "accepted", False, False
    )
    recorded = result["recorded"]
    assert (recorded["verdict"], recorded["missing"], recorded["grader"]) == (
        "partial", REQUIRED[1:], DEVICE
    )
    assert recorded["feedback"] == "Add the evidence." and recorded["confidence"] == "high"
    assert recorded["details"] == {"model": "on-device", "score": 0.5}
    assert recorded["interval_days"] == blurb["interval_days"]  # a partial keeps it


def test_answered_online_and_offline_the_first_one_counts(
    client: TestClient, page: str, clock: Clock, settings: Settings
) -> None:
    items = due_bundle(client, clock)["items"]
    mc = by_type(items, "multiple_choice")
    blurb = by_type(items, "blurb")
    # online first: the offline answer to the same presentation is refused
    q = queue(client, limit=5)
    served = next(i for i in q["items"] if i["id"] == mc["id"])
    assert answer(client, served, {"choice": 0}).status_code == 201
    late = answer_in(mc, {"choice": 0}, made_at=clock.now - timedelta(hours=2))
    (result,) = upload(client, [late])["results"]
    assert (result["status"], result["reason"]) == ("rejected", "already_answered")
    assert result["recorded"] is None
    # offline first: the online answer to the same presentation is a 409
    offline = answer_in(blurb, {"text": LONG_ANSWER}, made_at=clock.now - timedelta(minutes=1))
    assert upload(client, [offline])["accepted"] == 1
    served = next(i for i in q["items"] if i["id"] == blurb["id"])
    assert answer(client, served, {"text": LONG_ANSWER}).status_code == 409
    assert attempts_in_db(settings) == 2


def test_upload_rejections(client: TestClient, page: str, clock: Clock, settings: Settings) -> None:
    week = bundle(client, days=4)  # both upcoming, not due yet
    mc = by_type(week["items"], "multiple_choice")
    blurb = by_type(week["items"], "blurb")
    now = clock.now
    answers = [
        answer_in(mc, {"choice": 0}, made_at=now + timedelta(minutes=6)),
        answer_in({**mc, "id": 999999}, {"choice": 0}, made_at=now),
        answer_in(mc, {"choice": 0}, made_at=now, presentation="another-token"),
        answer_in(mc, {"choice": 0}, made_at=now),  # before it is due
    ]
    out = upload(client, answers)
    assert [r["reason"] for r in out["results"]] == [
        "made_at_in_future", "unknown_item", "presentation_changed", "not_due",
    ]
    clock.advance(days=4)
    later = clock.now
    answers = [
        answer_in(mc, {"choice": 0}, made_at=later, grader="judge"),
        answer_in(mc, {"choice": 0}, made_at=later, grader="device:  "),
        answer_in(mc, {"choice": 0}, made_at=later, verdict="partial"),
        answer_in(mc, {"choice": 0}, made_at=later, missing=[" "], verdict="partial"),
        answer_in(mc, {"choice": 7}, made_at=later),
        answer_in(mc, {"choice": True}, made_at=later),
        answer_in(mc, {"text": "words"}, made_at=later),
    ]
    out = upload(client, answers)
    assert [r["reason"] for r in out["results"]] == [
        "invalid_grader", "invalid_grader", "invalid_grade", "invalid_grade",
        "malformed_response", "malformed_response", "malformed_response",
    ]
    assert attempts_in_db(settings) == 0
    # a page that leaves the queue takes its items with it
    with session_scope(settings) as session:
        artifacts = ArtifactManager(session, settings)
        artifacts.set_status(artifacts.get_by_slug(page), "failed")
    (result,) = upload(client, [answer_in(blurb, {"text": LONG_ANSWER}, made_at=later)])["results"]
    assert result["reason"] == "not_in_queue"
    for result in out["results"]:
        assert result["status"] == "rejected" and result["detail"] and result["recorded"] is None


@pytest.mark.parametrize(
    "change",
    [
        {"confidence": None},
        {"confidence": "sure"},
        {"made_at": "2026-11-26T10:00:00"},
        {"verdict": "maybe"},
        {"client_id": "nope"},
        {"extra": 1},
        {"item_id": "x"},
    ],
)
def test_upload_refuses_a_malformed_request(
    client: TestClient, page: str, clock: Clock, settings: Settings, change: dict
) -> None:
    items = due_bundle(client, clock)["items"]
    mc = by_type(items, "multiple_choice")
    good = answer_in(mc, {"choice": 0}, made_at=clock.now)
    bad = {**answer_in(by_type(items, "blurb"), {"text": LONG_ANSWER}, made_at=clock.now), **change}
    r = client.post(UPLOAD, json={"answers": [good, bad]})
    assert r.status_code == 422, r.text
    assert attempts_in_db(settings) == 0


def test_upload_refuses_empty_and_repeated(client: TestClient, page: str, clock: Clock) -> None:
    assert client.post(UPLOAD, json={"answers": []}).status_code == 422
    mc = by_type(due_bundle(client, clock)["items"], "multiple_choice")
    once = answer_in(mc, {"choice": 0}, made_at=clock.now)
    r = client.post(UPLOAD, json={"answers": [once, once]})
    assert r.status_code == 422 and "twice" in r.json()["detail"]
