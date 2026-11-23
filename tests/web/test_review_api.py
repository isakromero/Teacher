"""The review queue's API (`teacher.web.routers.review`): a page generated offline gets its
review bank in the background, its cleared questions become due as the (injected) clock moves
on, a review is graded once and rescheduled, the banks can be rebuilt, and reviews feed the
standing and the suggestions."""

from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from teacher.config import Settings
from teacher.db.engine import session_scope
from teacher.db.models import ArtifactBlock, utcnow
from teacher.generation import StubGenerator
from teacher.grading import offline_graders
from teacher.lab_generator import StubLabGenerator
from teacher.managers import ArtifactManager, BlockConceptManager, ConceptManager
from teacher.review_bank import REVIEW_BANK_STAGE, ClaudeReviewBankBuilder, StubReviewBankBuilder
from teacher.web.app import create_app
from tests.web.conftest import send_source

LONG_ANSWER = " ".join(["word"] * 25)
TZ = "Europe/Stockholm"


class Clock:
    def __init__(self) -> None:
        self.now = utcnow()

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


@pytest.fixture
def clock() -> Clock:
    return Clock()


def make_client(settings: Settings, clock: Clock, **kwargs: Any) -> Iterator[TestClient]:
    app = create_app(
        settings,
        generator=StubGenerator(),
        lab_generator=StubLabGenerator(),
        graders=offline_graders(),
        clock=clock,
        **kwargs,
    )
    with TestClient(app) as client:
        yield client


@pytest.fixture
def client(settings: Settings, clock: Clock) -> Iterator[TestClient]:
    yield from make_client(settings, clock, review_bank_builder=StubReviewBankBuilder())


def generate(client: TestClient, title: str = "Optics") -> str:
    source = send_source(client, title=title, slug=title.lower()).json()
    created = client.post("/api/artifacts", json={"sources": [source["slug"]]})
    assert created.status_code == 202, created.text
    slug = created.json()["slug"]
    for _ in range(50):  # the TestClient runs the background tasks before returning, normally
        if client.get(f"/api/artifacts/{slug}").json()["status"] == "ready":
            break
        time.sleep(0.05)
    return slug


def clear_page(client: TestClient, slug: str) -> None:
    """Answer the stub page's questions right the first time: the multiple choice (b02), the
    blurb (b04) and the prediction (b05)."""
    base = f"/api/artifacts/{slug}/blocks"
    r = client.post(f"{base}/b02/responses", json={"response": {"choice": 0}, "confidence": "medium"})
    assert r.json()["verdict"] == "passed", r.text
    r = client.post(f"{base}/b04/responses", json={"response": {"text": LONG_ANSWER}, "confidence": "low"})
    assert r.json()["verdict"] == "passed", r.text
    r = client.post(f"{base}/b05/responses", json={"response": {"choice": 1}})
    assert r.status_code == 201, r.text


def queue(client: TestClient, **params: Any) -> dict[str, Any]:
    r = client.get("/api/review/queue", params={"tz": TZ, **params})
    assert r.status_code == 200, r.text
    return r.json()


def answer(client: TestClient, item: dict[str, Any], response: dict, confidence: str = "medium"):
    return client.post(
        f"/api/review/items/{item['id']}/answers",
        json={"presentation": item["presentation"], "response": response, "confidence": confidence},
    )


def test_a_generated_page_gets_its_bank_and_is_reviewed(client: TestClient, clock: Clock) -> None:
    slug = generate(client)
    runs = client.get(f"/api/artifacts/{slug}/runs").json()
    assert (runs[-1]["stage"], runs[-1]["status"]) == (REVIEW_BANK_STAGE, "succeeded")
    bank = client.get(f"/api/artifacts/{slug}/review-bank").json()
    assert (bank["status"], bank["questions"], bank["variants"], bank["can_build"]) == ("built", 2, 4, True)

    empty = queue(client)
    assert (empty["items_total"], empty["due"], empty["items"]) == (0, 0, [])
    clear_page(client, slug)
    waiting = queue(client)
    assert (waiting["items_total"], waiting["due"], waiting["items"]) == (2, 0, [])
    assert waiting["next_due_at"] is not None  # three days after the clean first passes

    clock.advance(days=4)
    q = queue(client, limit=5)
    assert (q["due"], q["remaining_today"], len(q["items"])) == (2, 20, 2)
    mc = next(i for i in q["items"] if i["block"]["type"] == "multiple_choice")
    assert (mc["form"], mc["variant"], mc["variants_available"], mc["reviews"]) == ("variant", 1, 2, 0)
    assert mc["block"]["content"]["question"].startswith("(Review form 1) ")
    assert "private" not in mc["block"]["content"] and mc["block"]["revealed"] is None
    assert mc["artifact"]["slug"] == slug
    # a reload shows the same presentation
    assert queue(client, limit=5)["items"][0]["presentation"] in {i["presentation"] for i in q["items"]}

    options = mc["block"]["content"]["options"]
    right = options.index("Optics")  # the stub page's answer is the source's title
    r = answer(client, mc, {"choice": right}, confidence="high")
    assert r.status_code == 201, r.text
    got = r.json()
    assert (got["verdict"], got["state"], got["attempt"], got["form"]) == ("passed", "passed", 1, "variant")
    assert got["interval_days"] == pytest.approx(6.6)
    assert options[got["revealed"]["correct_index"]] == "Optics"
    assert len(got["revealed"]["option_explanations"]) == len(options)
    assert answer(client, mc, {"choice": right}).status_code == 409  # one attempt per review

    blurb = next(i for i in q["items"] if i["block"]["type"] == "blurb")
    r = answer(client, blurb, {"text": LONG_ANSWER}, confidence="low")
    assert r.json()["verdict"] == "passed" and r.json()["revealed"]["model_answer"]

    after = queue(client)
    assert (after["due"], after["done_today"], after["items"]) == (0, 2, [])
    clock.advance(days=7)
    again = queue(client)
    assert again["due"] == 2 and again["items"][0]["form"] == "original"  # review 2 asks the original


def test_the_cap_the_concept_filter_and_bad_requests(client: TestClient, clock: Clock) -> None:
    slug = generate(client)
    clear_page(client, slug)
    clock.advance(days=4)
    capped = queue(client, cap=1)
    (item,) = capped["items"]
    assert item["question_key"] == "b02"  # cleared first, so the most overdue
    assert answer(client, item, {"choice": 0}).status_code == 201
    done = queue(client, cap=1)
    assert (done["due"], done["remaining_today"], done["items"]) == (1, 0, [])
    assert len(queue(client, cap=2)["items"]) == 1  # "keep going"
    assert client.get("/api/review/queue", params={"tz": "Mars/Base"}).status_code == 422
    assert client.get("/api/review/queue").status_code == 422  # tz is required
    assert client.get("/api/review/queue", params={"tz": TZ, "concept": "nope"}).status_code == 404
    with session_scope(client.app.state.settings) as session:
        concept = ConceptManager(session, client.app.state.settings).create("light", "Light.", "user")
        page = ArtifactManager(session, client.app.state.settings).get_by_slug(slug)
        block = next(b for b in page.blocks if b.key == "b04")
        BlockConceptManager(session, client.app.state.settings).add(block, concept, "main", "user")
    scoped = queue(client, concept="light", cap=5)
    assert scoped["concept"] == {"slug": "light", "name": "light"}
    assert scoped["items_total"] == 1 and [i["question_key"] for i in scoped["items"]] == ["b04"]
    r = client.post("/api/review/items/999/answers",
                    json={"presentation": "x", "response": {"choice": 0}, "confidence": "low"})
    assert r.status_code == 404
    r = client.post("/api/review/items/1/answers", json={"presentation": "x", "response": {"choice": 0}})
    assert r.status_code == 422  # confidence is required


def test_a_regenerated_page_leaves_the_queue(client: TestClient, clock: Clock) -> None:
    slug = generate(client)
    clear_page(client, slug)
    clock.advance(days=4)
    item = queue(client)["items"][0]
    newer = client.post(f"/api/artifacts/{slug}/regenerate")
    assert newer.status_code == 202
    gone = queue(client)
    assert (gone["items_total"], gone["due"]) == (0, 0)
    r = answer(client, item, {"choice": 0})
    assert r.status_code == 409 and "no longer in the review queue" in r.json()["detail"]


def test_banks_can_be_rebuilt_and_say_why_they_are_missing(settings: Settings, clock: Clock) -> None:
    for client in make_client(settings, clock):  # no builder: an offline generator
        slug = generate(client)
        runs = client.get(f"/api/artifacts/{slug}/runs").json()
        assert REVIEW_BANK_STAGE not in [r["stage"] for r in runs]
        clear_page(client, slug)
        banks = client.get("/api/review/banks").json()
        assert banks["can_build"] is False and "no review bank builder" in banks["cannot_build_reason"]
        (page,) = banks["pages"]
        assert (page["status"], page["variants"]) == ("not_built", 0)
        assert "original questions only" in page["reason"]
        r = client.post(f"/api/artifacts/{slug}/review-bank")
        assert r.status_code == 503 and "no review bank builder" in r.json()["detail"]
        clock.advance(days=4)
        assert {i["form"] for i in queue(client, limit=5)["items"]} == {"original"}
    for client in make_client(settings, clock, review_bank_builder=ClaudeReviewBankBuilder()):
        r = client.post(f"/api/artifacts/{slug}/review-bank")  # no credentials in the tests
        assert r.status_code == 503 and "credentials" in r.json()["detail"]
        page = client.get(f"/api/artifacts/{slug}/review-bank").json()
        assert page["status"] == "failed" and "credentials" in page["run"]["error"]
        assert client.get(f"/api/artifacts/{slug}").json()["status"] == "ready"  # the page is fine
    for client in make_client(settings, clock, review_bank_builder=StubReviewBankBuilder()):
        r = client.post(f"/api/artifacts/{slug}/review-bank")
        assert r.status_code == 200, r.text
        assert (r.json()["status"], r.json()["variants"]) == ("built", 4)
        assert client.get("/api/review/banks").json()["pages"][0]["status"] == "built"
        assert client.post("/api/artifacts/nope/review-bank").status_code == 404


def test_reviews_feed_the_standing_and_the_suggestions(client: TestClient, clock: Clock) -> None:
    slug = generate(client)
    settings = client.app.state.settings
    with session_scope(settings) as session:
        concept = ConceptManager(session, settings).create("optics", "Light.", "user")
        page = ArtifactManager(session, settings).get_by_slug(slug)
        tags = BlockConceptManager(session, settings)
        for block in page.blocks:
            if block.key in ("b02", "b05"):  # the multiple choice, and the prediction
                tags.add(session.get(ArtifactBlock, block.id), concept, "main", "user")
    base = f"/api/artifacts/{slug}/blocks"
    for _ in range(2):  # wrong twice, certain: the answer is revealed, a confident error
        r = client.post(f"{base}/b02/responses", json={"response": {"choice": 1}, "confidence": "high"})
        assert r.json()["verdict"] == "failed"
    assert r.json()["state"] == "revealed"
    standing = client.get("/api/history/concepts/optics").json()
    # the prediction is tagged too, but it is not a question
    assert (standing["questions"], standing["questions_answered"], standing["status"]) == (1, 1, "weak")
    assert [q["block"] for q in standing["tested_by"]] == ["b02"]

    suggestion = next(s for s in client.get("/api/history/suggestions").json()["suggestions"]
                      if s["concept"]["slug"] == "optics")
    assert suggestion["kind"] == "review"
    action = suggestion["action"]
    assert action["kind"] == "review_queue"
    assert action["review"]["items"] == 1 and action["review"]["due"] == 0
    assert action["review"]["url"] == "/review?concept=optics"
    assert "nothing is due now" in action["text"]

    clock.advance(days=2)
    (item,) = [i for i in queue(client, limit=5)["items"] if i["question_key"] == "b02"]
    assert item["confident_error"] is True
    right = item["block"]["content"]["options"].index("Optics")
    assert answer(client, item, {"choice": right}).json()["verdict"] == "passed"
    standing = client.get("/api/history/concepts/optics").json()
    assert (standing["reviews"], standing["reviews_passed"], standing["first_tries"]) == (1, 1, 2)
    assert standing["first_attempt_pass_rate"] == 0.5 and standing["status"] == "developing"
    assert standing["last_answered_at"] == client.get("/api/review/queue", params={"tz": TZ}).json()["now"]
