"""Record what the real server answers, for the iOS app's review and highlight conformance tests.

    uv run python ios/tools/make_review_highlight_fixtures.py <scratch home> ios/AiristotleTests/Fixtures/review_highlight_conformance.json

Builds a throwaway Teacher (SQLite in <scratch home>, which must not exist yet or be empty;
offline stubs only, no model call), with an injected clock, and records:

- `highlights`: a sequence of requests to the highlight endpoints of one study page (valid ones
  and every kind of error the phone reproduces offline), each with its status and body. Ids the
  server made are named `{h1}`, `{h2}`, ... in later paths, so the phone's own ids can be put in.
- `highlight_upload`: highlight changes made "on a phone" (fixed ids and times), the upload's
  answer, and the list the server has after it.
- `review`: the offline review bundle once the questions are due, then answers to the online
  endpoint (the server's grade and schedule for each), with the judge reply used for blurbs.
- `review_upload`: answers made "on a phone" from a second bundle, uploaded twice (accepted,
  then already_accepted), plus one answered online first (already_answered).
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
from teacher.db.models import utcnow  # noqa: E402
from teacher.generation import StubGenerator  # noqa: E402
from teacher.grading import GradeResult, MultipleChoiceGrader, PredictionGrader  # noqa: E402
from teacher.judge import _answer_text, blurb_verdict  # noqa: E402
from teacher.lab_generator import StubLabGenerator  # noqa: E402
from teacher.managers import ArtifactBlockManager, ArtifactManager, KnowledgeSourceManager  # noqa: E402
from teacher.web.app import create_app  # noqa: E402

DEVICE = "device:apple-foundation-models"
MODEL = "apple-on-device-system-language-model"
TZ = "Europe/Stockholm"
R1, R2, R3 = "States that $E = mc^2$ relates mass and energy.", "Gives an example.", "Mentions a limit."
LONG = "mass and energy are the same thing seen two ways, as the formula says plainly here"


class Clock:
    def __init__(self) -> None:
        self.now = utcnow().replace(microsecond=0)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


def mc(question: str, options: list[str], correct: int, explained: bool) -> tuple[str, dict[str, Any]]:
    private: dict[str, Any] = {"correct_index": correct, "explanation": f"Option {correct + 1} is right: $x^2$."}
    if explained:
        private["option_explanations"] = [f"Why option {i + 1} ($o_{i}$) is {'right' if i == correct else 'wrong'}."
                                          for i in range(len(options))]
    return ("multiple_choice", {"question": question, "options": options, "private": private})


def blurb(prompt: str) -> tuple[str, dict[str, Any]]:
    return ("blurb", {
        "prompt": prompt, "min_words": 8,
        "private": {"model_answer": "Mass is a form of energy: $E = mc^2$.",
                    "rubric": [{"criterion": R1}, {"criterion": R2}, {"criterion": R3, "required": False}]},
    })


PAGE: list[tuple[str, dict[str, Any]]] = [
    ("prose", {"markdown": "Energy and mass: $E = mc^2$. Some text to highlight here."}),
    mc(r"Which is $\frac{a}{b}$ when $a = 1$, $b = 2$?", ["$0.5$", "$2$", "$1$"], 0, True),
    ("prose", {"markdown": "More prose after the first question."}),
    mc("Pick the **prime**.", ["4", "6", "7", "9"], 2, False),
    blurb(r"Explain what $E = mc^2$ says, in your own words."),
    blurb("Explain it again, differently."),
    mc("Which colour is the sky?", ["Blue", "Green"], 0, True),
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
    """Blurbs graded from the next scripted judge reply with `blurb_verdict`, in the on-device
    grader's shape (as ios/tools/make_offline_fixtures.py)."""

    def __init__(self) -> None:
        self.script: list[dict[str, Any]] = []

    def grade(self, content: dict[str, Any], response: dict[str, Any]) -> GradeResult:
        _answer_text(content, response)
        reply = self.script.pop(0)
        rubric = content["private"]["rubric"]
        criteria = [
            {"criterion": item["criterion"], "required": item.get("required", True), "met": j["met"],
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


def clear(client: TestClient, grader: ScriptedJudgeGrader, slug: str) -> None:
    """Pass every question of the page on the first try (first review in three days)."""
    blocks = client.get(f"/api/offline/artifacts/{slug}").json()["blocks"]
    for block in blocks:
        if not block["is_gate"]:
            continue
        if block["type"] == "blurb":
            grader.script.append(judge(True, True, True))
            body = {"response": {"text": LONG}, "confidence": "medium"}
        else:
            body = {"response": {"choice": block["private"]["correct_index"]}, "confidence": "medium"}
        r = client.post(f"/api/artifacts/{slug}/blocks/{block['key']}/responses", json=body)
        assert r.status_code == 201 and r.json()["verdict"] == "passed", r.text


def record(client: TestClient, method: str, path: str, body: Any = None, names: dict[str, str] | None = None) -> dict:
    """One request; `path` may name earlier ids as {h1}, ... (`names`)."""
    real = path
    for name, value in (names or {}).items():
        real = real.replace("{" + name + "}", value)
    kwargs: dict[str, Any] = {}
    if body is not None:
        kwargs["content"] = json.dumps(body)
        kwargs["headers"] = {"Content-Type": "application/json"}
    r = client.request(method, real, **kwargs)
    return {"method": method, "path": path, "body": body, "status": r.status_code,
            "json": r.json() if r.content else None}


def highlight_steps(client: TestClient, clock: Clock) -> tuple[list[dict], dict[str, str]]:
    base = "/api/artifacts/hl/highlights"
    names: dict[str, str] = {}
    fixed = "7f0e3a52-1c4b-4d9a-9a51-0d3b8a8b6c01"
    steps: list[dict] = []

    def go(method: str, path: str, body: Any = None, name: str | None = None) -> None:
        clock.advance(seconds=7)
        step = record(client, method, path, body, names)
        if name and step["status"] == 201:
            names[name] = step["json"]["id"]
        steps.append(step)

    anchor = {"block_key": "b01", "quote": "Some text", "prefix": "mc^2$. ", "suffix": " to highlight", "start": 20, "end": 29}
    go("GET", base)
    go("POST", base, anchor, name="h1")
    go("POST", base, {**anchor, "id": fixed.upper(), "quote": "highlight", "start": 33, "end": 42, "note": "  A note  "}, name="h2")
    go("POST", base, {**anchor, "id": fixed})                                  # id taken
    go("POST", base, {**anchor, "quote": "   "})                               # blank quote
    go("POST", base, {**anchor, "block_key": " "})                             # blank key
    go("POST", base, {**anchor, "start": 5, "end": 5})                         # empty range
    go("POST", base, {**anchor, "start": -1})                                  # negative start
    go("POST", base, {**anchor, "block_key": "b09"})                           # unknown block
    go("POST", base, {**anchor, "prefix": "p" * 65})                           # prefix too long
    go("POST", base, {**anchor, "suffix": "é" * 65})                           # suffix too long (code points)
    go("POST", base, {**anchor, "quote": "q" * 4001})                          # quote too long
    go("POST", base, {**anchor, "quote": "a\u0000b"})                          # NUL
    go("POST", base, {**anchor, "note": "n" * 10001})                          # note too long
    go("POST", base, {**anchor, "note": " " + "n" * 10000 + " "})              # long only before stripping: fine
    go("POST", base, {k: v for k, v in anchor.items() if k != "block_key"})     # missing field
    go("POST", base, {**anchor, "colour": "yellow"})                           # unknown field
    go("POST", base, {**anchor, "start": True})                                # a bool
    go("POST", base, {**anchor, "start": 1.5})                                 # a float
    go("POST", base, {**anchor, "quote": 5})                                   # not a string
    go("POST", base, {**anchor, "note": 5})                                    # note not a string
    go("POST", base, {"quote": 5})                                             # several errors
    go("POST", base, [1, 2])                                                   # not an object
    go("POST", base, {**anchor, "block_key": "b04", "quote": "Pick", "prefix": "", "suffix": " the", "start": 0, "end": 4,
                      "note": "   "}, name="h3")                              # blank note: none
    go("POST", base, {**anchor, "block_key": "b02", "quote": "Which", "prefix": "", "suffix": " is", "start": 0, "end": 5},
       name="h4")
    go("GET", base)                                                            # page order
    go("PATCH", base + "/{h1}", {"note": "  New note "})
    go("PATCH", base + "/{h2}", {"note": "   "})
    go("PATCH", base + "/{h2}", {"note": None})
    go("PATCH", base + "/{h1}", {})                                            # missing note
    go("PATCH", base + "/{h1}", {"note": "x", "other": 1})                     # unknown field
    go("PATCH", base + "/{h1}", {"note": "n" * 10001})                         # too long
    go("PATCH", base + "/{h1}", {"note": 5})                                   # not a string
    go("PATCH", base + "/" + str(uuid.UUID(int=12345)), {"note": "x"})         # unknown id
    go("PATCH", base + "/" + fixed.upper(), {"note": "upper case id"})          # any UUID form
    go("DELETE", base + "/{h4}")
    go("DELETE", base + "/{h4}")                                               # removed already
    go("PATCH", base + "/{h4}", {"note": "x"})                                 # removed
    go("POST", base, {**anchor, "id": "{h4}"})                                 # a removed id stays taken
    go("GET", base)
    # the removed id, sent as a body id, needs the real value
    steps[-2] = record(client, "POST", base, {**anchor, "id": names["h4"]}, names)
    steps[-2]["body"] = {**anchor, "id": "{h4}"}
    return steps, names


def highlight_upload(client: TestClient, clock: Clock) -> dict:
    base = "/api/artifacts/up/highlights"
    online = client.post(base, json={"block_key": "b01", "quote": "Energy", "prefix": "", "suffix": " and", "start": 0,
                                     "end": 6, "note": "made online"}).json()
    before = client.get(base).json()
    t0 = clock.now - timedelta(minutes=20)

    def at(minutes: int) -> str:
        return (t0 + timedelta(minutes=minutes)).replace(tzinfo=UTC).isoformat()

    a, b, c = (str(uuid.UUID(int=i)) for i in (101, 102, 103))
    changes = [
        {"op": "create", "id": a, "block_key": "b01", "quote": "mass", "prefix": "Energy and ", "suffix": ": $E",
         "start": 11, "end": 15, "note": None, "made_at": at(1)},
        {"op": "update_note", "id": a, "note": "Mass note", "made_at": at(2)},
        {"op": "create", "id": b, "block_key": "b03", "quote": "More prose", "prefix": "", "suffix": " after",
         "start": 0, "end": 10, "note": "on b03", "made_at": at(3)},
        {"op": "update_note", "id": online["id"], "note": "edited offline", "made_at": at(4)},
        {"op": "create", "id": c, "block_key": "b99", "quote": "nowhere", "prefix": "", "suffix": "", "start": 0,
         "end": 7, "note": None, "made_at": at(5)},
        {"op": "delete", "id": b, "made_at": at(6)},
    ]
    answer = client.post("/api/offline/artifacts/up/highlights", json={"changes": changes})
    assert answer.status_code == 200, answer.text
    return {"before": before, "changes": changes, "answer": answer.json(), "after": client.get(base).json()}


def review(client: TestClient, grader: ScriptedJudgeGrader, clock: Clock) -> tuple[dict, dict]:
    bundle = client.get("/api/offline/review", params={"tz": TZ, "days": 7, "cap": 20})
    assert bundle.status_code == 200, bundle.text
    out = bundle.json()
    assert out["due"] >= 6, out["due"]
    items = {(i["artifact"]["slug"], i["question_key"]): i for i in out["items"]}
    answers = []

    def answer(slug: str, key: str, response: dict, confidence: str, script: dict | None = None) -> None:
        item = items[(slug, key)]
        if script:
            grader.script.append(script)
        r = client.post(f"/api/review/items/{item['id']}/answers",
                        json={"presentation": item["presentation"], "response": response, "confidence": confidence})
        grader.script.clear()
        assert r.status_code == 201, r.text
        answers.append({"item_id": item["id"], "response": response, "confidence": confidence, "judge": script,
                        "answered_at": clock.now.replace(tzinfo=UTC).isoformat(), "reply": r.json()})

    def shown(slug: str, key: str, option_index_text: str) -> int:
        return items[(slug, key)]["block"]["content"]["options"].index(option_index_text)

    answer("rev", "b02", {"choice": shown("rev", "b02", "$0.5$")}, "high")            # right
    answer("rev", "b04", {"choice": shown("rev", "b04", "4")}, "low")                 # wrong, no explanations
    answer("rev", "b07", {"choice": shown("rev", "b07", "Green")}, "medium")          # wrong, explained
    answer("rev", "b05", {"text": LONG}, "medium", judge(True, False, False, feedback="Add an example."))  # partial
    answer("rev", "b06", {"text": LONG}, "high", judge(False, False, True, feedback="Look again."))       # failed
    errors = []
    item = items[("rev", "b02")]
    for body in (
        {"presentation": item["presentation"], "response": {"choice": 0}, "confidence": "medium"},  # answered: 409
    ):
        r = client.post(f"/api/review/items/{item['id']}/answers", json=body)
        errors.append({"item_id": item["id"], "body": body, "status": r.status_code, "json": r.json()})
    return out, {"answers": answers, "errors": errors}


def review_upload(client: TestClient, grader: ScriptedJudgeGrader, clock: Clock) -> dict:
    bundle = client.get("/api/offline/review", params={"tz": TZ, "days": 7, "cap": 20}).json()
    items = {i["question_key"]: i for i in bundle["items"] if i["artifact"]["slug"] == "up2"}
    made = clock.now - timedelta(minutes=3)
    mcq = items["b02"]
    wrong = next(i for i in range(len(mcq["block"]["content"]["options"])) if i != mcq["private"]["correct_index"])
    answers = [
        {"client_id": str(uuid.UUID(int=201)), "item_id": mcq["id"], "presentation": mcq["presentation"],
         "response": {"choice": wrong}, "confidence": "high", "verdict": "failed",
         "feedback": "Not quite. " + mcq["private"]["option_explanations"][wrong], "missing": [], "details": None,
         "grader": "device:airistotle-rules", "made_at": made.replace(tzinfo=UTC).isoformat()},
        {"client_id": str(uuid.UUID(int=202)), "item_id": items["b05"]["id"], "presentation": items["b05"]["presentation"],
         "response": {"text": LONG}, "confidence": "medium", "verdict": "partial", "feedback": "Add an example.",
         "missing": [R2], "details": {"model": MODEL, "score": 0.33, "criteria": []}, "grader": DEVICE,
         "made_at": (made + timedelta(minutes=1)).replace(tzinfo=UTC).isoformat()},
    ]
    # b06 is answered online first, then "on the phone" from the same bundle.
    other = items["b06"]
    grader.script.append(judge(True, True, True))
    online = client.post(f"/api/review/items/{other['id']}/answers",
                         json={"presentation": other["presentation"], "response": {"text": LONG}, "confidence": "low"})
    assert online.status_code == 201, online.text
    answers.append({"client_id": str(uuid.UUID(int=203)), "item_id": other["id"], "presentation": other["presentation"],
                    "response": {"text": LONG}, "confidence": "medium", "verdict": "passed", "feedback": "Good.",
                    "missing": [], "details": None, "grader": DEVICE,
                    "made_at": (made + timedelta(minutes=2)).replace(tzinfo=UTC).isoformat()})
    first = client.post("/api/offline/review/answers", json={"answers": answers})
    assert first.status_code == 200, first.text
    again = client.post("/api/offline/review/answers", json={"answers": answers[:1]})
    assert again.status_code == 200, again.text
    return {"bundle": bundle, "answers": answers, "first": first.json(), "again": again.json()}


def main() -> None:
    home, out = Path(sys.argv[1]), Path(sys.argv[2])
    if home.exists() and any(home.iterdir()):
        sys.exit(f"{home} is not empty: give a new scratch folder")
    home.mkdir(parents=True, exist_ok=True)
    settings = Settings(root=home / "teacher")
    upgrade_to_head(settings)
    grader = ScriptedJudgeGrader()
    clock = Clock()
    app = create_app(settings, generator=StubGenerator(), lab_generator=StubLabGenerator(), clock=clock, graders={
        "multiple_choice": MultipleChoiceGrader(), "prediction": PredictionGrader(), "blurb": grader,
    })
    with TestClient(app) as client:
        make_page(settings, home, "hl", PAGE, 1)
        make_page(settings, home, "up", PAGE, 2)
        make_page(settings, home, "rev", PAGE, 3)
        make_page(settings, home, "up2", PAGE, 4)
        highlights, names = highlight_steps(client, clock)
        uploaded = highlight_upload(client, clock)
        hl_bundle = client.get("/api/offline/artifacts/hl").json()
        up_bundle_blocks = client.get("/api/offline/artifacts/up").json()["blocks"]
        clear(client, grader, "rev")
        clear(client, grader, "up2")
        clock.advance(days=4)
        bundle, answered = review(client, grader, clock)
        uploaded_reviews = review_upload(client, grader, clock)

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "highlights": {"bundle": hl_bundle, "steps": highlights, "names": names},
        "highlight_upload": {**uploaded, "blocks": up_bundle_blocks},
        "review": {"bundle": bundle, **answered},
        "review_upload": uploaded_reviews,
    }, indent=1, sort_keys=True) + "\n")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
