"""Record what the real server answers, for the iOS reading engine's conformance tests.

    uv run python ios/tools/make_offline_fixtures.py <scratch home> ios/AiristotleTests/Fixtures/offline_conformance.json

Builds a throwaway Teacher (SQLite in <scratch home>, which must not exist yet or be empty;
offline stubs only, no model call), makes study pages through the managers, and for each page
records its offline bundle and then a sequence of submissions to the real reading endpoints:
each reply (or error status and detail) and, after each successful step, `GET .../blocks` and
`GET .../responses` of every visible block. Written answers are graded on the server by a
scripted grader that applies `teacher.judge.blurb_verdict` to a recorded judge reply, the same
reply the Swift test hands its fake on-device judge: the Swift engine must then produce the
same JSON (times aside).

It also uploads a phone-style attempt list to a third page and records the upload's answer
and the reading endpoints after it, times included.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

os.environ["ANTHROPIC_BASE_URL"] = "http://127.0.0.1:9/unreachable"
os.environ["OPENAI_BASE_URL"] = "http://127.0.0.1:9/unreachable"
for _name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
    os.environ.pop(_name, None)

from fastapi.testclient import TestClient  # noqa: E402

from teacher.blocks import is_gate_type, validate_block_content  # noqa: E402
from teacher.config import Settings  # noqa: E402
from teacher.db.engine import session_scope  # noqa: E402
from teacher.db.migrate import upgrade_to_head  # noqa: E402
from teacher.generation import StubGenerator  # noqa: E402
from teacher.grading import GradeResult, MultipleChoiceGrader, PredictionGrader  # noqa: E402
from teacher.judge import _answer_text, blurb_verdict  # noqa: E402
from teacher.lab_generator import StubLabGenerator  # noqa: E402
from teacher.managers import ArtifactBlockManager, ArtifactManager, KnowledgeSourceManager  # noqa: E402
from teacher.web.app import create_app  # noqa: E402

DEVICE = "device:apple-foundation-models"
MODEL = "apple-on-device-system-language-model"
R1, R2, R3 = "Names the cause.", "Gives an example.", "Mentions a limit."
LONG = "this answer has well over five words in it"

PAGE: list[tuple[str, dict[str, Any]]] = [
    ("prose", {"markdown": "# Intro\n\nOpening text."}),
    ("multiple_choice", {
        "question": "Which letter?", "options": ["alpha", "beta", "gamma"],
        "private": {"correct_index": 1, "explanation": "Beta is right.",
                    "option_explanations": ["Alpha is first.", "Beta is right.", "Gamma is last."]},
    }),
    ("prose", {"markdown": "Locked prose three."}),
    ("blurb", {
        "prompt": "Summarise the text.", "min_words": 5,
        "private": {"model_answer": "The model answer.",
                    "rubric": [{"criterion": R1}, {"criterion": R2}, {"criterion": R3, "required": False}]},
    }),
    ("prediction", {"prompt": "Will it rise?", "options": ["up", "down"],
                    "private": {"correct_index": 0, "resolution": "It rises."}}),
    ("prose", {"markdown": "Locked prose six."}),
]

# A page without per-option explanations and a blurb without min_words (default 20).
OLD_PAGE: list[tuple[str, dict[str, Any]]] = [
    ("multiple_choice", {
        "question": "Pick one.", "options": ["one", "two"],
        "private": {"correct_index": 0, "explanation": "One it is."},
    }),
    ("blurb", {
        "prompt": "Explain.",
        "private": {"model_answer": "Because.", "rubric": [{"criterion": R1}, {"criterion": R2}]},
    }),
    ("prose", {"markdown": "The end."}),
]


def judge(*met: bool, feedback: str = "Feedback for you.") -> dict[str, Any]:
    return {
        "criteria": [
            {"number": i, "justification": f"Criterion {i} was {'met' if m else 'not met'}.", "met": m}
            for i, m in enumerate(met, start=1)
        ],
        "feedback": feedback,
    }


class ScriptedJudgeGrader:
    """The server's blurb grader here: validates like the judge, then decides the verdict from
    the next scripted judge reply with `blurb_verdict`, in the on-device grader's shape."""

    def __init__(self) -> None:
        self.script: list[dict[str, Any]] = []

    def grade(self, content: dict[str, Any], response: dict[str, Any]) -> GradeResult:
        _answer_text(content, response)
        reply = self.script.pop(0)
        rubric = content["private"]["rubric"]
        criteria = [
            {"criterion": item["criterion"], "required": item["required"], "met": j["met"],
             "justification": j["justification"]}
            for item, j in zip(rubric, reply["criteria"], strict=True)
        ]
        verdict, missing = blurb_verdict(criteria)
        score = sum(1 for c in criteria if c["met"]) / len(criteria)
        return GradeResult(verdict, feedback=reply["feedback"], grader=DEVICE,
                           details={"model": MODEL, "score": score, "criteria": criteria}, missing=missing)


def make_page(settings: Settings, home: Path, slug: str, page: list[tuple[str, dict]], n: int) -> None:
    src = home / f"source-{n}.txt"
    src.write_text(f"Source text number {n}.\n")
    with session_scope(settings) as session:
        source = KnowledgeSourceManager(session, settings).add(src, slug=f"src-{n}", kind="note", title=f"Source {n}")
        artifacts = ArtifactManager(session, settings)
        artifact = artifacts.create(slug=slug, kind="study_page", title=f"Study {n}", generator="test", sources=[source])
        blocks = ArtifactBlockManager(session, settings)
        for type_, content in page:
            blocks.add(artifact, type=type_, content=validate_block_content(type_, content), is_gate=is_gate_type(type_))
        artifacts.set_status(artifact, "ready")


def views(client: TestClient, slug: str) -> dict[str, Any]:
    blocks = client.get(f"/api/artifacts/{slug}/blocks").json()
    responses = {
        b["key"]: client.get(f"/api/artifacts/{slug}/blocks/{b['key']}/responses").json() for b in blocks["blocks"]
    }
    return {"blocks": blocks, "responses": responses}


def run_steps(client: TestClient, grader: ScriptedJudgeGrader, slug: str, steps: list[dict[str, Any]]) -> dict:
    bundle = client.get(f"/api/offline/artifacts/{slug}").json()
    out = {"slug": slug, "bundle": bundle, "initial": views(client, slug), "steps": []}
    for step in steps:
        if step.get("judge"):
            grader.script.append(step["judge"])
        r = client.post(f"/api/artifacts/{slug}/blocks/{step['key']}/responses", json=step["body"])
        grader.script.clear()  # an error before grading leaves the reply unused
        record = {**step, "status": r.status_code}
        if r.status_code == 201:
            record["reply"] = r.json()
            record.update(views(client, slug))
        else:
            record["detail"] = r.json()["detail"]
        out["steps"].append(record)
    return out


def main() -> None:
    home, out = Path(sys.argv[1]), Path(sys.argv[2])
    if home.exists() and any(home.iterdir()):
        sys.exit(f"{home} is not empty: give a new scratch folder")
    home.mkdir(parents=True, exist_ok=True)
    settings = Settings(root=home / "teacher")
    upgrade_to_head(settings)
    grader = ScriptedJudgeGrader()
    app = create_app(settings, generator=StubGenerator(), lab_generator=StubLabGenerator(), graders={
        "multiple_choice": MultipleChoiceGrader(), "prediction": PredictionGrader(), "blurb": grader,
    })
    with TestClient(app) as client:
        make_page(settings, home, "study", PAGE, 1)
        make_page(settings, home, "old", OLD_PAGE, 2)
        make_page(settings, home, "uploaded", PAGE, 3)

        study = run_steps(client, grader, "study", [
            {"key": "b03", "body": {"response": {"text": LONG}}},                      # locked
            {"key": "b09", "body": {"response": {"choice": 0}}},                       # unknown
            {"key": "b01", "body": {"response": {"choice": 0}}},                       # not a gate
            {"key": "b02", "body": {"response": {"choice": 3}}},                       # out of range
            {"key": "b02", "body": {"response": {"choice": True}}},                    # a bool
            {"key": "b02", "body": {"response": {"pick": 1}}},                         # wrong key
            {"key": "b02", "body": {"response": {"choice": 1, "extra": 2}}},           # unknown key
            {"key": "b02", "body": {"response": {"choice": 1}, "confidence": "sure"}}, # bad confidence
            {"key": "b02", "body": {"response": {"choice": 1}, "other": 1}},           # unknown field
            {"key": "b02", "body": {"response": {"choice": 0}, "confidence": "low"}},
            {"key": "b02", "body": {"response": {"choice": 1}, "confidence": "high"}},
            {"key": "b02", "body": {"response": {"choice": 1}}},                       # already passed
            {"key": "b04", "body": {"response": {"text": "too short"}}},
            {"key": "b04", "body": {"response": {"text": "   "}}},
            {"key": "b04", "body": {"response": {"text": 5}}},
            {"key": "b04", "body": {"response": {"text": LONG}}, "judge": judge(False, False, True, feedback="Look again.")},
            {"key": "b04", "body": {"response": {"text": LONG + " again"}, "confidence": "medium"},
             "judge": judge(True, False, False, feedback="Add an example.")},
            {"key": "b05", "body": {"response": {"choice": 1}, "confidence": "low"}},  # prediction: no confidence
            {"key": "b05", "body": {"response": {"choice": 1}}},
        ])
        # b04 is revealed after two attempts; another run passes it.
        old = run_steps(client, grader, "old", [
            {"key": "b01", "body": {"response": {"choice": 1}}},
            {"key": "b01", "body": {"response": {"choice": 0}}},
            {"key": "b02", "body": {"response": {"text": "one two three four five six"}}},  # under 20 words
            {"key": "b02", "body": {"response": {"text": " ".join(["word"] * 20)}},
             "judge": judge(True, True, feedback="Well done.")},
        ])

        # An upload made the way the phone makes it, then the reading endpoints, times included.
        bundle = client.get("/api/offline/artifacts/uploaded").json()
        start = datetime.now(UTC).replace(microsecond=123456) - timedelta(minutes=30)
        items = [
            {"client_id": str(uuid.uuid4()), "key": "b02", "response": {"choice": 1}, "confidence": "high",
             "verdict": "passed", "feedback": "Beta is right.", "missing": [], "details": None,
             "grader": "device:airistotle-rules", "made_at": start.isoformat()},
            {"client_id": str(uuid.uuid4()), "key": "b04", "response": {"text": LONG}, "confidence": None,
             "verdict": "partial", "feedback": "Add an example.", "missing": [R2],
             "details": {"model": MODEL, "score": 2 / 3, "criteria": []}, "grader": DEVICE,
             "made_at": (start + timedelta(minutes=1)).isoformat()},
        ]
        upload = client.post("/api/offline/artifacts/uploaded/attempts", json={"version": bundle["version"], "attempts": items})
        assert upload.status_code == 200, upload.text
        uploaded = {"slug": "uploaded", "bundle_before": bundle, "items": items, "answer": upload.json(),
                    "after": views(client, "uploaded")}

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"pages": [study, old], "upload": uploaded}, indent=1, sort_keys=True) + "\n")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
