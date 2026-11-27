"""Offline API: the bundle of a study page, and uploading attempts graded on the phone.

`Phone` below implements the contract in `teacher.web.routers.offline`'s docstring from the
bundle alone; the tests check that it reproduces the reading endpoints exactly.
"""

from __future__ import annotations

import copy
import os
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from teacher.blocks import is_gate_type, validate_block_content
from teacher.config import Settings
from teacher.db.engine import session_scope
from teacher.db.models import BlockResponse, OfflineAttempt, StudyEvent, StudySession
from teacher.files import sha256_bytes
from teacher.generation import StubGenerator
from teacher.grading import (
    GradeResult,
    MultipleChoiceGrader,
    PredictionGrader,
)
from teacher.judge import _answer_text
from teacher.lab_generator import StubLabGenerator
from teacher.managers import (
    ArtifactBlockManager,
    ArtifactManager,
    KnowledgeSourceManager,
    SourceSegmentManager,
)
from teacher.web.app import create_app

DEVICE = "device:test-model"
R1, R2, R3 = "Names the cause.", "Gives an example.", "Mentions a limit."

PAGE: list[tuple[str, dict[str, Any]]] = [
    ("prose", {"markdown": "# Intro\n\nOpening text."}),
    (
        "multiple_choice",
        {
            "question": "Which letter?",
            "options": ["alpha", "beta", "gamma"],
            "private": {
                "correct_index": 1,
                "explanation": "Beta is right.",
                "option_explanations": ["Alpha is first.", "Beta is right.", "Gamma is last."],
            },
        },
    ),
    ("prose", {"markdown": "Locked prose three."}),
    (
        "blurb",
        {
            "prompt": "Summarise the text.",
            "min_words": 5,
            "private": {
                "model_answer": "The model answer.",
                "rubric": [
                    {"criterion": R1},
                    {"criterion": R2},
                    {"criterion": R3, "required": False},
                ],
            },
        },
    ),
    (
        "prediction",
        {
            "prompt": "Will it rise?",
            "options": ["up", "down"],
            "private": {"correct_index": 0, "resolution": "It rises."},
        },
    ),
    ("prose", {"markdown": "Locked prose six."}),
]
LONG = "this answer has well over five words in it"


class ScriptedBlurbGrader:
    """The web page's blurb grader in these tests: checks the answer like the judge, then
    returns the next scripted grade (what the phone's on-device grader decided)."""

    def __init__(self) -> None:
        self.script: list[GradeResult] = []

    def grade(self, content: dict[str, Any], response: dict[str, Any]) -> GradeResult:
        _answer_text(content, response)
        return self.script.pop(0)


@pytest.fixture
def blurb_grader() -> ScriptedBlurbGrader:
    return ScriptedBlurbGrader()


@pytest.fixture
def app(settings: Settings, blurb_grader: ScriptedBlurbGrader) -> FastAPI:
    graders = {
        "multiple_choice": MultipleChoiceGrader(),
        "prediction": PredictionGrader(),
        "blurb": blurb_grader,
    }
    return create_app(
        settings, generator=StubGenerator(), lab_generator=StubLabGenerator(), graders=graders
    )


MakeArtifact = Callable[..., str]


@pytest.fixture
def make_artifact(settings: Settings, tmp_path: Path) -> MakeArtifact:
    """Build and commit a page through the managers; return its slug."""
    counter = iter(range(1, 1000))

    def make(
        slug: str = "study",
        *,
        status: str = "ready",
        kind: str = "study_page",
        page: list[tuple[str, dict]] = PAGE,
    ) -> str:
        n = next(counter)
        src = tmp_path / f"source-{n}.txt"
        src.write_text(f"Source text number {n}.\n")
        with session_scope(settings) as session:
            source = KnowledgeSourceManager(session, settings).add(
                src, slug=f"src-{n}", kind="note", title=f"Source {n}"
            )
            segment = SourceSegmentManager(session, settings).add(
                source,
                kind="section",
                label="intro",
                title="Intro",
                locator={"type": "text_span", "start_char": 0, "end_char": 6},
            )
            artifacts = ArtifactManager(session, settings)
            artifact = artifacts.create(
                slug=slug, kind=kind, title="Study", generator="test", sources=[source]
            )
            blocks = ArtifactBlockManager(session, settings)
            for i, (type_, content) in enumerate(page):
                blocks.add(
                    artifact,
                    type=type_,
                    content=validate_block_content(type_, content),
                    is_gate=is_gate_type(type_),
                    segments=[segment] if i == 0 else (),
                )
            artifacts.set_status(artifact, status)
        return slug

    return make


@pytest.fixture
def study(make_artifact: MakeArtifact, client: TestClient) -> str:
    return make_artifact()


# -- helpers --------------------------------------------------------------------------------


def bundle_url(slug: str) -> str:
    return f"/api/offline/artifacts/{slug}"


def upload_url(slug: str) -> str:
    return f"/api/offline/artifacts/{slug}/attempts"


def blocks_url(slug: str) -> str:
    return f"/api/artifacts/{slug}/blocks"


def responses_url(slug: str, key: str) -> str:
    return f"/api/artifacts/{slug}/blocks/{key}/responses"


def get_bundle(client: TestClient, slug: str) -> dict[str, Any]:
    r = client.get(bundle_url(slug))
    assert r.status_code == 200, r.text
    return r.json()


def iso_utc(value: datetime) -> str:
    """What the server shows for a stored time (`teacher.web.common.to_iso`)."""
    return value.astimezone(UTC).replace(tzinfo=UTC).isoformat()


def ago(minutes: float) -> datetime:
    return datetime.now(UTC) - timedelta(minutes=minutes)


def item(
    key: str,
    response: Any,
    *,
    verdict: str = "passed",
    made_at: datetime | None = None,
    **fields: Any,
) -> dict[str, Any]:
    """One upload item; fields override the defaults."""
    out = {
        "client_id": str(uuid.uuid4()),
        "key": key,
        "response": response,
        "verdict": verdict,
        "grader": DEVICE,
        "made_at": (made_at or ago(10)).isoformat(),
    }
    out.update(fields)
    return out


def upload(client: TestClient, slug: str, items: list[dict[str, Any]], version: str | None = None):
    if version is None:
        version = get_bundle(client, slug)["version"]
    return client.post(upload_url(slug), json={"version": version, "attempts": items})


def upload_ok(client: TestClient, slug: str, items: list[dict[str, Any]], **kw: Any) -> dict:
    r = upload(client, slug, items, **kw)
    assert r.status_code == 200, r.text
    return r.json()


def responses_in_db(settings: Settings) -> int:
    with session_scope(settings) as session:
        return len(list(session.scalars(select(BlockResponse))))


# -- the phone: the contract, implemented from the bundle alone -----------------------------


BLOCK_OUT_FIELDS = (
    "key", "position", "type", "is_gate", "content", "state", "attempts", "revealed",
    "segments", "concepts",
)


class Malformed(Exception):
    pass


class Phone:
    """A reader on the phone: the bundle, the attempts made since, and the reading endpoints
    computed from them exactly as the offline docstring says."""

    def __init__(self, bundle: dict[str, Any]) -> None:
        self.bundle = copy.deepcopy(bundle)
        self.blocks = self.bundle["blocks"]
        self.pending: list[dict[str, Any]] = []

    def block(self, key: str) -> dict[str, Any]:
        return next(b for b in self.blocks if b["key"] == key)

    def states(self) -> list[dict[str, Any]]:
        out, visible = [], True
        for b in self.blocks:
            attempts = len(b["responses"])
            passed = any(r["passed"] for r in b["responses"])
            if not b["is_gate"]:
                state = None
            elif passed:
                state = "passed"
            elif attempts >= self.bundle["max_attempts"]:
                state = "revealed"
            else:
                state = "open"
            cleared = state in ("passed", "revealed")
            view = {
                **{f: b[f] for f in BLOCK_OUT_FIELDS},
                "state": state,
                "attempts": attempts,
                "revealed": b["private"] if cleared else None,
            }
            out.append({"view": view, "visible": visible, "cleared": cleared, "state": state})
            if state == "open":
                visible = False
        return out

    def blocks_view(self) -> dict[str, Any]:
        states = self.states()
        gates = [s for s in states if s["view"]["is_gate"]]
        return {
            "artifact": self.bundle["artifact"],
            "total_blocks": len(states),
            "locked_blocks": sum(1 for s in states if not s["visible"]),
            "complete": all(s["cleared"] for s in gates),
            "blocks": [s["view"] for s in states if s["visible"]],
        }

    def responses_view(self, key: str) -> list[dict[str, Any]]:
        index = next(i for i, b in enumerate(self.blocks) if b["key"] == key)
        state = self.states()[index]
        assert state["visible"]
        return [
            {**r, "details": r["details"] if state["cleared"] else None}
            for r in self.block(key)["responses"]
        ]

    def grade(self, block: dict[str, Any], response: Any, blurb: GradeResult | None) -> dict:
        grading = self.bundle["grading"]
        private = block["private"]
        if block["type"] in ("multiple_choice", "prediction"):
            if not isinstance(response, dict) or set(response) != {"choice"}:
                raise Malformed
            choice = response["choice"]
            if isinstance(choice, bool) or not isinstance(choice, int):
                raise Malformed
            if not 0 <= choice < len(block["content"]["options"]):
                raise Malformed
            if block["type"] == "prediction":
                return {
                    "verdict": "passed", "feedback": grading["prediction_feedback"],
                    "missing": [], "details": {"correct": choice == private["correct_index"]},
                    "grader": grading["prediction_grader"],
                }
            if choice == private["correct_index"]:
                feedback, verdict = private["explanation"], "passed"
            elif private.get("option_explanations") is None:
                feedback, verdict = grading["no_option_feedback"], "failed"
            else:
                feedback = grading["multiple_choice_wrong_prefix"] + (
                    private["option_explanations"][choice]
                )
                verdict = "failed"
            return {
                "verdict": verdict, "feedback": feedback, "missing": [], "details": None,
                "grader": grading["multiple_choice_grader"],
            }
        assert block["type"] == "blurb"
        if not isinstance(response, dict) or set(response) != {"text"}:
            raise Malformed
        text = response["text"]
        min_words = block["content"].get("min_words", self.bundle["default_min_words"])
        if not isinstance(text, str) or not text.strip() or len(text.split()) < min_words:
            raise Malformed
        assert blurb is not None
        return {
            "verdict": blurb.verdict, "feedback": blurb.feedback, "missing": list(blurb.missing),
            "details": blurb.details, "grader": blurb.grader,
        }

    def submit(
        self,
        key: str,
        response: Any,
        *,
        confidence: str | None = None,
        blurb: GradeResult | None = None,
        made_at: datetime | None = None,
    ) -> dict[str, Any]:
        """What POST .../responses would answer; queues the upload item."""
        before = {s["view"]["key"]: s for s in self.states()}
        block = self.block(key)
        assert before[key]["visible"] and before[key]["state"] == "open"
        grade = self.grade(block, response, blurb)
        made_at = made_at or datetime.now(UTC)
        row = {
            "attempt": len(block["responses"]) + 1,
            "response": response,
            "passed": grade["verdict"] == "passed",
            "verdict": grade["verdict"],
            "feedback": grade["feedback"],
            "missing": grade["missing"],
            "confidence": confidence,
            "grader": grade["grader"],
            "details": grade["details"],
            "created_at": iso_utc(made_at),
        }
        block["responses"].append(row)
        self.pending.append(
            {
                "client_id": str(uuid.uuid4()), "key": key, "response": response,
                "confidence": confidence, "verdict": grade["verdict"],
                "feedback": grade["feedback"], "missing": grade["missing"],
                "details": grade["details"],
                "grader": grade["grader"] if grade["grader"].startswith("device:") else DEVICE,
                "made_at": made_at.isoformat(),
            }
        )
        after = self.states()
        now = next(s for s in after if s["view"]["key"] == key)
        return {
            "attempt": row["attempt"],
            "passed": row["passed"],
            "verdict": row["verdict"],
            "feedback": row["feedback"],
            "missing": row["missing"],
            "confidence": confidence,
            "details": row["details"] if now["cleared"] else None,
            "state": now["state"],
            "revealed": now["view"]["revealed"],
            "unlocked": [
                s["view"] for s in after
                if s["visible"] and not before[s["view"]["key"]]["visible"]
            ],
            "locked_blocks": sum(1 for s in after if not s["visible"]),
            "complete": all(s["cleared"] for s in after if s["view"]["is_gate"]),
        }


def assert_phone_matches(client: TestClient, slug: str, phone: Phone, *, times: bool) -> None:
    """The phone's GET .../blocks and GET .../responses are the server's (created_at only
    when `times`: on a twin page the attempts were made at other moments)."""
    view = client.get(blocks_url(slug)).json()
    assert phone.blocks_view() == view
    for block in view["blocks"]:
        real = client.get(responses_url(slug, block["key"])).json()
        mine = phone.responses_view(block["key"])
        if not times:
            real = [{**r, "created_at": None} for r in real]
            mine = [{**r, "created_at": None} for r in mine]
        assert mine == real, block["key"]


# -- the bundle -----------------------------------------------------------------------------


def test_bundle_shape(client: TestClient, study: str, settings: Settings) -> None:
    bundle = get_bundle(client, study)
    assert set(bundle) == {
        "format", "format_version", "artifact", "artifact_id", "title", "generated_at",
        "version", "superseded_by", "bundled_at", "max_attempts", "default_min_words",
        "grading", "total_blocks", "gates_total", "gates_cleared", "locked_blocks",
        "complete", "blocks", "assets", "highlights",
    }
    assert bundle["highlights"] == []
    assert bundle["format"] == "teacher-offline-bundle" and bundle["format_version"] == 1
    assert bundle["artifact"] == study and bundle["title"] == "Study"
    assert bundle["max_attempts"] == 2 and bundle["default_min_words"] == 20
    assert len(bundle["version"]) == 64 and bundle["superseded_by"] is None
    assert bundle["generated_at"].endswith("+00:00") and bundle["bundled_at"].endswith("+00:00")
    assert (bundle["total_blocks"], bundle["gates_total"], bundle["gates_cleared"]) == (6, 3, 0)
    assert (bundle["locked_blocks"], bundle["complete"]) == (4, False)
    assert bundle["grading"] == {
        "multiple_choice_grader": "multiple-choice-v2",
        "multiple_choice_wrong_prefix": "Not quite. ",
        "no_option_feedback": bundle["grading"]["no_option_feedback"],
        "prediction_grader": "prediction-v1",
        "prediction_feedback": bundle["grading"]["prediction_feedback"],
        "device_grader_prefix": "device:",
    }
    # the wrong-answer prefix is the grader's own
    wrong = MultipleChoiceGrader().grade(PAGE[1][1], {"choice": 0})
    assert wrong.feedback == "Not quite. Alpha is first."

    blocks = bundle["blocks"]
    assert [b["key"] for b in blocks] == ["b01", "b02", "b03", "b04", "b05", "b06"]
    assert [b["visible"] for b in blocks] == [True, True, False, False, False, False]
    assert [b["is_question"] for b in blocks] == [False, True, False, True, False, False]
    assert [b["state"] for b in blocks] == [None, "open", None, "open", "open", None]
    for b, (type_, content) in zip(blocks, PAGE, strict=True):
        stored = validate_block_content(type_, content)
        assert b["private"] == stored.get("private")
        assert "private" not in b["content"]
        assert b["responses"] == [] and b["passed"] is False and b["revealed"] is None
    assert blocks[0]["segments"][0]["label"] == "intro"
    assert blocks[2]["content"] == {"markdown": "Locked prose three."}  # locked, still sent


def test_bundle_matches_the_reading_endpoints_as_the_page_is_read(
    client: TestClient, make_artifact: MakeArtifact, blurb_grader: ScriptedBlurbGrader
) -> None:
    """On a twin page, each POST the phone predicts is the server's reply, and its views are
    the server's after every step."""
    twin = make_artifact("twin")
    phone = Phone(get_bundle(client, twin))
    assert_phone_matches(client, twin, phone, times=False)

    partial = GradeResult(
        "partial", feedback="Add an example.", grader=DEVICE,
        details={"criteria": [{"criterion": R1, "met": True}]}, missing=(R2,),
    )
    passed = GradeResult("passed", feedback="Good.", grader=DEVICE, details={"score": 1.0})
    steps: list[tuple[str, Any, dict[str, Any]]] = [
        ("b02", {"choice": 0}, {"confidence": "low"}),
        ("b02", {"choice": 1}, {"confidence": "high"}),
        ("b04", {"text": LONG}, {"blurb": partial}),
        ("b04", {"text": LONG + " again"}, {"blurb": passed, "confidence": "medium"}),
        ("b05", {"choice": 1}, {}),
    ]
    for key, response, extra in steps:
        if "blurb" in extra:
            blurb_grader.script.append(extra["blurb"])
        predicted = phone.submit(key, response, **extra)
        body = {"response": response}
        if extra.get("confidence"):
            body["confidence"] = extra["confidence"]
        real = client.post(responses_url(twin, key), json=body)
        assert real.status_code == 201, real.text
        assert predicted == real.json(), key
        assert_phone_matches(client, twin, phone, times=False)

    # a malformed answer is no attempt, on the phone as on the server
    with pytest.raises(Malformed):
        Phone(get_bundle(client, make_artifact("third"))).submit("b02", {"choice": 3})
    assert client.get(blocks_url(twin)).json()["complete"] is True


def test_attempts_made_offline_and_uploaded_match_the_phone(
    client: TestClient, study: str, settings: Settings
) -> None:
    phone = Phone(get_bundle(client, study))
    start = ago(30)
    phone.submit("b02", {"choice": 2}, confidence="low", made_at=start)
    phone.submit("b02", {"choice": 1}, made_at=start + timedelta(minutes=1))
    phone.submit(
        "b04", {"text": LONG},
        blurb=GradeResult("failed", feedback="Not yet.", grader=DEVICE),
        made_at=start + timedelta(minutes=2),
    )
    phone.submit(
        "b04", {"text": LONG},
        blurb=GradeResult("partial", feedback="Almost.", grader=DEVICE, missing=(R1,)),
        made_at=start + timedelta(minutes=3),
    )
    # the gate is revealed on the phone now; then the prediction
    phone.submit("b05", {"choice": 0}, made_at=start + timedelta(minutes=4))

    body = upload_ok(client, study, phone.pending, version=phone.bundle["version"])
    assert (body["accepted"], body["already_accepted"], body["rejected"]) == (5, 0, 0)
    assert body["version_matched"] is True
    assert [r["attempt"] for r in body["results"]] == [1, 2, 1, 2, 1]
    assert [r["state"] for r in body["results"]] == ["open", "passed", "open", "revealed", "passed"]
    assert [r["unlocked"] for r in body["results"]] == [
        [], ["b03", "b04"], [], ["b05"], ["b06"]
    ]
    assert [r["gate_cleared"] for r in body["results"]] == [False, True, False, True, True]
    assert [r["regraded"] for r in body["results"]] == [True, True, False, False, True]
    assert not any(r["verdict_mismatch"] for r in body["results"])
    # the phone's views, times included, are now the server's
    assert_phone_matches(client, study, phone, times=True)
    # and the fresh bundle is what the phone holds
    fresh = body["bundle"]
    assert [b["responses"] for b in fresh["blocks"]] == [b["responses"] for b in phone.blocks]
    assert fresh["complete"] is True and fresh["locked_blocks"] == 0
    assert body["results"][0]["recorded"] == phone.block("b02")["responses"][0]


def test_bundle_responses_carry_details_before_the_gate_is_cleared(
    client: TestClient, study: str, blurb_grader: ScriptedBlurbGrader
) -> None:
    assert client.post(responses_url(study, "b02"), json={"response": {"choice": 1}}).status_code == 201
    blurb_grader.script.append(
        GradeResult("failed", feedback="No.", grader="judge", details={"secret": "rubric"})
    )
    assert client.post(responses_url(study, "b04"), json={"response": {"text": LONG}}).status_code == 201
    assert client.get(responses_url(study, "b04")).json()[0]["details"] is None
    blurb = get_bundle(client, study)["blocks"][3]
    assert blurb["responses"][0]["details"] == {"secret": "rubric"}
    assert blurb["state"] == "open" and blurb["revealed"] is None and blurb["private"] is not None


def test_bundle_lists_assets(client: TestClient, study: str, settings: Settings, tmp_path: Path) -> None:
    folder = settings.artifacts_dir / study / "assets"
    (folder / "img").mkdir()
    (folder / "img" / "fig one.png").write_bytes(b"\x89PNG-bytes")
    (folder / "notes.txt").write_text("asset text")
    outside = tmp_path / "outside.txt"
    outside.write_text("SECRET")
    os.symlink(outside, folder / "link.txt")
    assets = get_bundle(client, study)["assets"]
    assert assets == [
        {
            "path": "img/fig one.png",
            "url": f"/api/artifacts/{study}/assets/img/fig%20one.png",
            "bytes": 10,
            "sha256": sha256_bytes(b"\x89PNG-bytes"),
        },
        {
            "path": "notes.txt",
            "url": f"/api/artifacts/{study}/assets/notes.txt",
            "bytes": 10,
            "sha256": sha256_bytes(b"asset text"),
        },
    ]
    for asset in assets:
        r = client.get(asset["url"])
        assert r.status_code == 200 and len(r.content) == asset["bytes"]


def test_version_is_stable_and_names_the_page(
    client: TestClient, study: str, settings: Settings, make_artifact: MakeArtifact
) -> None:
    first = get_bundle(client, study)
    assert client.post(responses_url(study, "b02"), json={"response": {"choice": 1}}).status_code == 201
    assert get_bundle(client, study)["version"] == first["version"]  # attempts do not change it
    assert get_bundle(client, make_artifact("other"))["version"] != first["version"]
    # the slug is reused by another page: its version differs, and old attempts are refused
    assert client.delete(f"/api/artifacts/{study}").status_code == 204
    make_artifact(study)
    again = get_bundle(client, study)
    assert again["version"] != first["version"]
    body = upload_ok(client, study, [item("b02", {"choice": 1})], version=first["version"])
    assert body["version_matched"] is False and body["version"] == again["version"]
    assert body["results"][0]["reason"] == "page_changed"
    assert body["bundle"]["version"] == again["version"]


def test_superseded_by(client: TestClient, study: str, settings: Settings) -> None:
    with session_scope(settings) as session:
        manager = ArtifactManager(session, settings)
        old = manager.get_by_slug(study)
        manager.create(slug="study-2", kind="study_page", title="Study", generator="t", supersedes=old)
    assert get_bundle(client, study)["superseded_by"] == "study-2"


@pytest.mark.parametrize("url", [bundle_url, upload_url])
def test_lab_unknown_and_not_ready(
    client: TestClient, make_artifact: MakeArtifact, url: Callable[[str], str]
) -> None:
    def call(slug: str):
        if url is bundle_url:
            return client.get(url(slug))
        return client.post(url(slug), json={"version": "x", "attempts": [item("b02", {"choice": 1})]})

    assert call("nope").status_code == 404
    lab = make_artifact("a-lab", kind="lab", page=[])
    r = call(lab)
    assert r.status_code == 422 and "lab" in r.json()["detail"]
    for status in ("generating", "failed"):
        slug = make_artifact(f"page-{status}", status=status)
        r = call(slug)
        assert r.status_code == 409 and status in r.json()["detail"]


# -- uploading: idempotency, re-grading, history -------------------------------------------


def test_upload_is_idempotent(client: TestClient, study: str, settings: Settings) -> None:
    attempt = item("b02", {"choice": 0}, verdict="failed")
    first = upload_ok(client, study, [attempt])
    assert first["results"][0]["status"] == "accepted"
    again = upload_ok(client, study, [attempt, item("b02", {"choice": 1})])
    assert [r["status"] for r in again["results"]] == ["already_accepted", "accepted"]
    assert again["results"][0]["recorded"] == first["results"][0]["recorded"]
    assert again["results"][0]["attempt"] == 1 and again["results"][1]["attempt"] == 2
    assert again["results"][0]["gate_cleared"] is False
    assert again["results"][0]["state"] == "open"  # the gate's state when the item was applied
    assert (again["accepted"], again["already_accepted"]) == (1, 1)
    assert responses_in_db(settings) == 2
    # an accepted attempt stays accepted even when the gate has moved on since
    third = upload_ok(client, study, [attempt])
    assert third["results"][0]["status"] == "already_accepted"
    assert responses_in_db(settings) == 2
    with session_scope(settings) as session:
        rows = list(session.scalars(select(OfflineAttempt).order_by(OfflineAttempt.id)))
        assert [r.client_id for r in rows][0] == attempt["client_id"]
        assert rows[0].device_grader == DEVICE and rows[0].device_verdict == "failed"


def test_client_id_is_canonical_and_reuse_for_another_attempt_is_rejected(
    client: TestClient, study: str
) -> None:
    attempt = item("b02", {"choice": 0}, verdict="failed")
    attempt["client_id"] = attempt["client_id"].upper()
    body = upload_ok(client, study, [attempt])
    assert body["results"][0]["client_id"] == attempt["client_id"].lower()
    for change in ({"response": {"choice": 2}}, {"key": "b04", "response": {"text": LONG}}):
        reused = {**attempt, **change}
        result = upload_ok(client, study, [reused])["results"][0]
        assert result["status"] == "rejected" and result["reason"] == "client_id_conflict"


def test_multiple_choice_is_graded_again_on_the_server(
    client: TestClient, study: str, settings: Settings
) -> None:
    wrong_but_claimed = item("b02", {"choice": 0}, verdict="passed", feedback="phone says yes",
                             details={"phone": True})
    body = upload_ok(client, study, [wrong_but_claimed])
    result = body["results"][0]
    assert result["status"] == "accepted" and result["regraded"] is True
    assert result["verdict_mismatch"] is True and result["device_verdict"] == "passed"
    recorded = result["recorded"]
    assert recorded["verdict"] == "failed" and recorded["passed"] is False
    assert recorded["feedback"] == "Not quite. Alpha is first."
    assert recorded["grader"] == "multiple-choice-v2" and recorded["details"] is None
    assert result["state"] == "open" and result["gate_cleared"] is False
    with session_scope(settings) as session:
        row = session.scalars(select(OfflineAttempt)).one()
        assert row.device_verdict == "passed"
    # a prediction too: the server's details, whatever the phone sent
    upload_ok(client, study, [item("b02", {"choice": 1})])
    phone = Phone(get_bundle(client, study))
    phone.submit("b04", {"text": LONG}, blurb=GradeResult("passed", feedback="ok", grader=DEVICE))
    upload_ok(client, study, phone.pending)
    prediction = upload_ok(
        client, study, [item("b05", {"choice": 1}, verdict="failed", details={"x": 1})]
    )["results"][0]
    assert prediction["verdict_mismatch"] is True
    assert prediction["recorded"]["details"] == {"correct": False}
    assert prediction["recorded"]["grader"] == "prediction-v1"


def test_blurb_keeps_the_phones_grade(client: TestClient, study: str) -> None:
    upload_ok(client, study, [item("b02", {"choice": 1})])
    partial = item(
        "b04", {"text": LONG}, verdict="partial", missing=[R2], feedback="Add an example.",
        details={"score": 0.5}, confidence="medium",
    )
    result = upload_ok(client, study, [partial])["results"][0]
    assert result["status"] == "accepted" and result["regraded"] is False
    recorded = result["recorded"]
    assert (recorded["verdict"], recorded["missing"], recorded["feedback"]) == (
        "partial", [R2], "Add an example."
    )
    assert recorded["grader"] == DEVICE and recorded["details"] == {"score": 0.5}
    assert recorded["confidence"] == "medium"
    assert result["state"] == "open"


def test_created_at_session_and_gate_cleared_come_from_the_phone(
    client: TestClient, study: str, settings: Settings
) -> None:
    made = ago(24 * 60)  # a day ago: its own study session
    body = upload_ok(client, study, [item("b02", {"choice": 1}, made_at=made)])
    assert body["results"][0]["gate_cleared"] is True
    assert body["results"][0]["recorded"]["created_at"] == iso_utc(made)
    with session_scope(settings) as session:
        response = session.scalars(select(BlockResponse)).one()
        events = list(session.scalars(select(StudyEvent).where(StudyEvent.kind == "gate_cleared")))
        assert len(events) == 1
        event = events[0]
        assert event.payload == {"state": "passed", "attempt": 1, "unlocked": ["b03", "b04"]}
        assert event.block_id == response.block_id
        naive = made.astimezone(UTC).replace(tzinfo=None)
        assert event.occurred_at == naive and response.created_at == naive
        assert event.session_id == response.session_id
        assert session.get(StudySession, response.session_id).started_at == naive
    # a failed attempt clears nothing and logs nothing
    upload_ok(client, study, [item("b04", {"text": LONG}, verdict="failed")])
    with session_scope(settings) as session:
        kinds = [e.kind for e in session.scalars(select(StudyEvent))]
        assert kinds.count("gate_cleared") == 1


def test_gate_cleared_by_running_out_of_attempts_is_logged(
    client: TestClient, study: str, settings: Settings
) -> None:
    body = upload_ok(
        client, study,
        [item("b02", {"choice": 0}, verdict="failed"), item("b02", {"choice": 2}, verdict="failed")],
    )
    assert [r["gate_cleared"] for r in body["results"]] == [False, True]
    assert body["results"][1]["state"] == "revealed"
    with session_scope(settings) as session:
        event = session.scalars(select(StudyEvent).where(StudyEvent.kind == "gate_cleared")).one()
        assert event.payload == {"state": "revealed", "attempt": 2, "unlocked": ["b03", "b04"]}


# -- uploading: rejections ------------------------------------------------------------------


def rejected(body: dict[str, Any], index: int = 0) -> tuple[str, str]:
    result = body["results"][index]
    assert result["status"] == "rejected", result
    assert result["attempt"] is None and result["recorded"] is None and result["state"] is None
    assert isinstance(result["detail"], str) and result["detail"]
    return result["reason"], result["detail"]


def test_rejections_by_the_servers_state(client: TestClient, study: str, settings: Settings) -> None:
    # locked: the blurb before its multiple choice is cleared, also later in the same upload
    body = upload_ok(client, study, [
        item("b04", {"text": LONG}),
        item("b01", {"choice": 1}),
        item("b99", {"choice": 1}),
        item("b02", {"choice": 1}),
        item("b04", {"text": LONG}),
        item("b02", {"choice": 1}),
    ])
    assert [r["reason"] for r in body["results"]] == [
        "block_locked", "not_a_gate", "unknown_block", None, None, "gate_already_passed"
    ]
    assert (body["accepted"], body["rejected"]) == (2, 4)
    assert responses_in_db(settings) == 2


def test_attempts_exhausted(client: TestClient, study: str, settings: Settings) -> None:
    for choice in (0, 2):  # two wrong answers on the web page: the gate is revealed
        r = client.post(responses_url(study, "b02"), json={"response": {"choice": choice}})
        assert r.json()["state"] == ("open" if choice == 0 else "revealed")
    reason, detail = rejected(upload_ok(client, study, [item("b02", {"choice": 1})]))
    assert reason == "attempts_exhausted" and "revealed" in detail
    assert responses_in_db(settings) == 2


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"grader": "llm-judge-v2"}, "invalid_grader"),
        ({"grader": "device:"}, "invalid_grader"),
        ({"grader": "device:  "}, "invalid_grader"),
        ({"verdict": "partial"}, "invalid_grade"),  # partial without missing
        ({"verdict": "failed", "missing": [R1]}, "invalid_grade"),  # missing on a failed one
        ({"verdict": "partial", "missing": ["Something else."]}, "invalid_grade"),
        ({"verdict": "partial", "missing": [R3]}, "invalid_grade"),  # optional criterion
        ({"verdict": "partial", "missing": [R1, R1]}, "invalid_grade"),
        ({"verdict": "partial", "missing": [R1, R2]}, "invalid_grade"),  # that is failed
        ({"verdict": "partial", "missing": [" "]}, "invalid_grade"),
        ({"response": {"text": "too short"}}, "malformed_response"),
        ({"response": {"text": LONG, "extra": 1}}, "malformed_response"),
        ({"response": None}, "malformed_response"),
        ({"response": "text"}, "malformed_response"),
        ({"made_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat()}, "made_at_in_future"),
    ],
)
def test_blurb_rejections(
    client: TestClient, study: str, settings: Settings, change: dict, reason: str
) -> None:
    upload_ok(client, study, [item("b02", {"choice": 1})])
    body = upload_ok(client, study, [{**item("b04", {"text": LONG}), **change}])
    assert rejected(body)[0] == reason
    assert responses_in_db(settings) == 1
    assert body["bundle"]["blocks"][3]["responses"] == []


@pytest.mark.parametrize(
    ("key", "response", "change", "reason"),
    [
        ("b02", {"choice": 3}, {}, "malformed_response"),
        ("b02", {"choice": True}, {}, "malformed_response"),
        ("b02", {"choice": "1"}, {}, "malformed_response"),
        ("b02", [1], {}, "malformed_response"),
        ("b02", {"choice": 1}, {"verdict": "partial"}, "invalid_grade"),
        ("b05", {"choice": 0}, {"confidence": "high"}, "confidence_not_allowed"),
    ],
)
def test_choice_rejections(
    client: TestClient, study: str, settings: Settings, key: str, response: Any,
    change: dict, reason: str,
) -> None:
    if key == "b05":
        upload_ok(client, study, [item("b02", {"choice": 1})])
        phone = Phone(get_bundle(client, study))
        phone.submit("b04", {"text": LONG}, blurb=GradeResult("passed", feedback="ok", grader=DEVICE))
        upload_ok(client, study, phone.pending)
    before = responses_in_db(settings)
    body = upload_ok(client, study, [{**item(key, response), **change}])
    assert rejected(body)[0] == reason
    assert responses_in_db(settings) == before


def test_a_rejected_item_does_not_stop_the_next(client: TestClient, study: str) -> None:
    body = upload_ok(client, study, [
        item("b02", {"choice": 9}),
        item("b02", {"choice": 1}),
    ])
    assert [r["status"] for r in body["results"]] == ["rejected", "accepted"]
    assert body["results"][1]["attempt"] == 1


@pytest.mark.parametrize(
    "mutate",
    [
        lambda body: body["attempts"].append(dict(body["attempts"][0])),  # same client_id twice
        lambda body: body["attempts"][0].update(made_at="2026-11-25T10:00:00"),  # no offset
        lambda body: body["attempts"][0].update(client_id="not-a-uuid"),
        lambda body: body["attempts"][0].update(verdict="maybe"),
        lambda body: body["attempts"][0].update(confidence="sure"),
        lambda body: body["attempts"][0].update(extra=1),
        lambda body: body["attempts"][0].pop("grader"),
        lambda body: body["attempts"][0].pop("made_at"),
        lambda body: body.update(attempts=[]),
        lambda body: body.pop("version"),
        lambda body: body.update(extra=1),
    ],
)
def test_malformed_request_is_422_and_records_nothing(
    client: TestClient, study: str, settings: Settings, mutate: Callable[[dict], Any]
) -> None:
    body = {"version": get_bundle(client, study)["version"], "attempts": [item("b02", {"choice": 1})]}
    mutate(body)
    r = client.post(upload_url(study), json=body)
    assert r.status_code == 422, r.text
    assert isinstance(r.json()["detail"], str)
    assert responses_in_db(settings) == 0


def test_too_many_items_is_422(client: TestClient, study: str) -> None:
    items = [item("b02", {"choice": 0}) for _ in range(501)]
    assert upload(client, study, items).status_code == 422


# -- the account gate -----------------------------------------------------------------------


@pytest.fixture
def gated(settings: Settings) -> Iterator[TestClient]:
    app = create_app(
        settings, generator=StubGenerator(), lab_generator=StubLabGenerator(),
        access_code="the-access-code",
    )
    with TestClient(app) as test_client:
        yield test_client


def test_behind_the_account_gate(gated: TestClient, make_artifact: MakeArtifact) -> None:
    slug = make_artifact()
    assert gated.get(bundle_url(slug)).status_code == 401
    r = gated.post(upload_url(slug), json={"version": "x", "attempts": [item("b02", {"choice": 1})]})
    assert r.status_code == 401
    registered = gated.post(
        "/api/auth/register",
        json={"email": "phone@example.com", "password": "a long password",
              "access_code": "the-access-code"},
    )
    assert registered.status_code == 201, registered.text
    gated.cookies.clear()
    headers = {"Authorization": f"Bearer {registered.json()['token']}"}
    bundle = gated.get(bundle_url(slug), headers=headers)
    assert bundle.status_code == 200
    r = gated.post(
        upload_url(slug), headers=headers,
        json={"version": bundle.json()["version"], "attempts": [item("b02", {"choice": 1})]},
    )
    assert r.status_code == 200 and r.json()["accepted"] == 1
