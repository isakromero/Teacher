"""Costs in the API: an artifact's `cost` (generation by stage, and since), a chat thread's and
each reply's, and the recording done by the web app (the tutor's calls linked to their reply,
grading linked to the page, a call outside any closer scope still recorded)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import anthropic
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from teacher.blocks import is_gate_type, validate_block_content
from teacher.config import Settings
from teacher.db.engine import session_scope
from teacher.db.models import ApiCall, ChatMessage, GenerationRun
from teacher.generation import StubGenerator
from teacher.grading import MultipleChoiceGrader
from teacher.judge import LLMJudgeGrader
from teacher.managers import ArtifactBlockManager, ArtifactManager, KnowledgeSourceManager
from teacher.tutor import Tutor
from teacher.web.app import create_app

M = 1_000_000
BLURB = {
    "prompt": "Summarise the text.",
    "min_words": 3,
    "private": {"model_answer": "A summary.", "rubric": [{"criterion": "Summarises"}]},
}
COST_KEYS = {"total_usd", "calls", "unknown_price_calls", "unknown_price_models", "estimate",
             "estimate_calls", "parts"}


@pytest.fixture
def fake(fake_anthropic):
    return fake_anthropic()


@pytest.fixture
def app(settings: Settings, fake) -> FastAPI:
    graders = {"multiple_choice": MultipleChoiceGrader(), "blurb": LLMJudgeGrader(fake)}
    return create_app(settings, generator=StubGenerator(), graders=graders, tutor=Tutor(fake))


@pytest.fixture
def page(settings: Settings, tmp_path: Path) -> int:
    """A ready page with one blurb (graded by the fake judge); returns its id."""
    path = tmp_path / "paper.txt"
    path.write_text("Attention is computed from queries and keys.\n", encoding="utf-8")
    with session_scope(settings) as session:
        source = KnowledgeSourceManager(session, settings).add(
            path, slug="src", kind="paper", title="Paper"
        )
        artifacts = ArtifactManager(session, settings)
        artifact = artifacts.create(slug="study", kind="study_page", title="Study",
                                    generator="test", sources=[source])
        ArtifactBlockManager(session, settings).add(
            artifact, type="blurb", content=validate_block_content("blurb", BLURB),
            is_gate=is_gate_type("blurb"),
        )
        artifacts.set_status(artifact, "ready")
        return artifact.id


def ledger(settings: Settings) -> list[ApiCall]:
    with session_scope(settings) as session:
        return list(session.scalars(select(ApiCall).order_by(ApiCall.id)))


def test_an_artifact_without_recorded_calls_says_so(client: TestClient, page: int) -> None:
    cost = client.get("/api/artifacts/study").json()["cost"]
    assert set(cost) == COST_KEYS | {"generation", "since_generation"}
    assert cost["calls"] == 0 and cost["total_usd"] == 0
    assert cost["generation"] == {
        "total_usd": 0, "calls": 0, "unknown_price_calls": 0, "unknown_price_models": [],
        "estimate": False, "estimate_calls": 0, "parts": [],
    }
    listed = client.get("/api/artifacts").json()
    assert listed[0]["cost"] == cost and "tokens" in listed[0]  # existing fields stay


def test_the_artifact_cost_splits_generation_and_since(
    client: TestClient, settings: Settings, page: int
) -> None:
    def row(**kwargs: Any) -> ApiCall:
        return ApiCall(**{
            "purpose": "generation", "step": "write", "provider": "anthropic",
            "model": "claude-opus-5-5", "input_tokens": 1, "cache_write_tokens": 0,
            "cache_write_1h_tokens": 0, "cache_read_tokens": 0, "output_tokens": 1,
            "cost_usd": 0.5, "priced_on": "2026-11-24", "artifact_id": page, **kwargs,
        })  # fmt: skip

    with session_scope(settings) as session:
        run = GenerationRun(artifact_id=page, stage="write", model="m", status="succeeded")
        session.add(run)
        session.flush()
        session.add_all([
            row(generation_run_id=run.id, cost_usd=0.84),
            row(purpose="tutor", step="tutor reply", cost_usd=0.09),
            row(purpose="grading", step="judge", cost_usd=0.03, is_estimate=True),
            row(purpose="grading", step="judge", cost_usd=None, priced_on=None, model="x-1"),
        ])  # fmt: skip
    cost = client.get("/api/artifacts/study").json()["cost"]
    assert cost["total_usd"] == pytest.approx(0.96) and cost["calls"] == 4
    assert cost["generation"]["total_usd"] == pytest.approx(0.84)
    assert cost["generation"]["parts"] == [{
        "key": "write", "label": "writing", "total_usd": 0.84, "calls": 1,
        "unknown_price_calls": 0, "estimate": False,
    }]  # fmt: skip
    since = cost["since_generation"]
    assert since["total_usd"] == pytest.approx(0.12)
    assert (since["estimate"], since["estimate_calls"]) == (True, 1)
    assert (since["unknown_price_calls"], since["unknown_price_models"]) == (1, ["x-1"])
    assert [(p["key"], p["label"], p["calls"]) for p in since["parts"]] == [
        ("tutor", "tutor", 1), ("grading", "grading", 2)
    ]


def test_grading_an_answer_is_recorded_with_the_page(
    client: TestClient, settings: Settings, fake, page: int
) -> None:
    fake.outcomes.append(fake.reply([(True, "Met.")], input_tokens=500, output_tokens=60))
    response = client.post("/api/artifacts/study/blocks/b01/responses",
                           json={"response": {"text": "It summarises the text."}})
    assert response.status_code == 201, response.text
    [row] = ledger(settings)
    assert (row.purpose, row.step, row.artifact_id, row.model) == (
        "grading", "judge", page, "claude-haiku-5-5"
    )
    since = client.get("/api/artifacts/study").json()["cost"]["since_generation"]
    assert since["total_usd"] == pytest.approx((500 * 0.10 + 60 * 0.50) / M)


def parse_sse(body: str) -> list[tuple[str, Any]]:
    events = []
    for frame in body.strip().split("\n\n"):
        name, data = frame.split("\n")
        events.append((name.removeprefix("event: "), json.loads(data.removeprefix("data: "))))
    return events


def test_a_tutor_reply_has_its_cost_and_the_thread_its_total(
    client: TestClient, settings: Settings, fake, page: int
) -> None:
    thread = client.post("/api/artifacts/study/chat/threads").json()
    assert thread["cost"]["calls"] == 0
    reply = fake.reply(text="Attention mixes values.", model="claude-opus-5-5",
                       input_tokens=100, output_tokens=20, cache_creation_input_tokens=1_000,
                       cache_read_input_tokens=5)
    reply.usage.cache_creation = anthropic.types.CacheCreation(
        ephemeral_5m_input_tokens=0, ephemeral_1h_input_tokens=1_000
    )
    refused = fake.reply(text="", stop_reason="refusal", model="claude-opus-5-5",
                         input_tokens=7, output_tokens=1)
    fake.outcomes += [reply, refused]
    url = f"/api/artifacts/study/chat/threads/{thread['id']}/messages"
    events = parse_sse(client.post(url, json={"text": "What is attention?"}).text)
    done = events[-1][1]["message"]
    expected = (100 * 4 + 1_000 * 8 + 5 * 0.20 + 20 * 20) / M
    assert done["cost"]["total_usd"] == pytest.approx(expected) and done["cost"]["calls"] == 1
    assert events[0][1]["cost"] is None  # the reader's own message cost nothing

    # A refused reply is not stored, but its call cost money: it counts for the thread.
    events = parse_sse(client.post(url, json={"text": "And then?"}).text)
    assert events[-1][0] == "error"
    detail = client.get(f"/api/artifacts/study/chat/threads/{thread['id']}").json()
    assert detail["cost"]["calls"] == 2
    assert detail["cost"]["total_usd"] == pytest.approx(expected + (7 * 4 + 1 * 20) / M)
    assert [m["cost"] and m["cost"]["calls"] for m in detail["messages"]] == [None, 1, None]
    listed = client.get("/api/artifacts/study/chat/threads").json()
    assert listed[0]["cost"] == detail["cost"]

    rows = ledger(settings)
    with session_scope(settings) as session:
        stored = session.scalar(select(ChatMessage.id).where(ChatMessage.role == "assistant"))
    assert [(r.purpose, r.chat_thread_id, r.chat_message_id, r.artifact_id) for r in rows] == [
        ("tutor", thread["id"], stored, page), ("tutor", thread["id"], None, page)
    ]
    page_cost = client.get("/api/artifacts/study").json()["cost"]
    assert [p["key"] for p in page_cost["since_generation"]["parts"]] == ["tutor"]


def test_a_call_without_a_closer_scope_is_still_recorded(
    client: TestClient, settings: Settings, fake, tmp_path: Path, make_pdf
) -> None:
    """Every request runs under a base meter: detecting an upload's metadata (which belongs to
    no source yet) is recorded unlinked."""
    app = client.app
    from teacher.metadata import MetadataExtractor

    app.state.metadata_extractor = MetadataExtractor(fake)
    fake.outcomes.append(fake.metadata_reply())
    response = client.post(
        "/api/sources/inspect",
        files={"file": ("paper.pdf", make_pdf(1), "application/pdf")},
    )
    assert response.status_code == 200, response.text
    [row] = ledger(settings)
    assert (row.purpose, row.step, row.artifact_id, row.knowledge_source_id) == (
        "metadata", "metadata", None, None
    )


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_cost_wording_under_node() -> None:
    """cost.js: amounts ("<$0.01", two decimals), "at most" for estimates, unpriced calls."""
    result = subprocess.run(
        ["node", str(Path(__file__).parent / "js" / "cost_test.mjs")],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert "cost ok" in result.stdout
