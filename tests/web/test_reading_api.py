"""Reading API: gated blocks, responses, the escape hatch, private-content leaks, assets."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import anthropic
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from teacher.blocks import is_gate_type, validate_block_content
from teacher.config import Settings
from teacher.db.engine import session_scope
from teacher.generation import StubGenerator
from teacher.grading import (
    GradeResult,
    MultipleChoiceGrader,
    PredictionGrader,
    WordCountBlurbGrader,
)
from teacher.grading import offline_graders as offline_grader_set
from teacher.judge import LLMJudgeGrader
from teacher.managers import (
    ArtifactBlockManager,
    ArtifactManager,
    KnowledgeSourceManager,
    SourceSegmentManager,
)
from teacher.web.app import create_app

PRIVATE_FIELDS = (
    "private",
    "correct_index",
    "explanation",
    "model_answer",
    "rubric",
    "criteria",
    "justification",
)
MC_SECRETS = ("SECRET-EXPLANATION",)
BLURB_SECRETS = ("SECRET-MODEL-ANSWER", "SECRET-RUBRIC-ONE", "SECRET-RUBRIC-TWO")
JUDGE_SECRETS = ("SECRET-JUSTIFICATION-ONE", "SECRET-JUSTIFICATION-TWO")
LOCKED_MARKERS = ("LOCKED-PROSE-THREE", "LOCKED-PROSE-FIVE")

PAGE: list[tuple[str, dict[str, Any]]] = [
    ("prose", {"markdown": "# Intro\n\nOpening text."}),
    (
        "multiple_choice",
        {
            "question": "Which letter?",
            "options": ["alpha", "beta", "gamma"],
            "private": {"correct_index": 1, "explanation": "SECRET-EXPLANATION: beta."},
        },
    ),
    ("prose", {"markdown": "LOCKED-PROSE-THREE"}),
    (
        "blurb",
        {
            "prompt": "Summarise the text.",
            "min_words": 5,
            "private": {
                "model_answer": "SECRET-MODEL-ANSWER",
                "rubric": [
                    {"criterion": "SECRET-RUBRIC-ONE"},
                    {"criterion": "SECRET-RUBRIC-TWO", "required": False},
                ],
            },
        },
    ),
    ("prose", {"markdown": "LOCKED-PROSE-FIVE"}),
]
LONG_TEXT = "this summary has well over five words in it"
REVEALED_BLURB = {
    "model_answer": "SECRET-MODEL-ANSWER",
    "rubric": [
        {"criterion": "SECRET-RUBRIC-ONE", "required": True},
        {"criterion": "SECRET-RUBRIC-TWO", "required": False},
    ],
}


def offline_graders() -> dict[str, Any]:
    """Deterministic graders; the default blurb grader (the LLM judge) needs the network."""
    return offline_grader_set()


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    """Overrides tests/web/conftest.py's `app`: blurbs are graded offline, explicitly."""
    return create_app(settings, generator=StubGenerator(), graders=offline_graders())


MakeArtifact = Callable[..., str]


@pytest.fixture
def make_artifact(settings: Settings, tmp_path: Path) -> MakeArtifact:
    """Build and COMMIT an artifact with blocks through the managers; return its slug.

    Block 1 references a text-span segment of the artifact's source.
    """
    counter = iter(range(1, 1000))

    def make(
        slug: str = "study", *, status: str = "ready", page: list[tuple[str, dict]] = PAGE
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
                slug=slug, kind="study_page", title="Study", generator="test", sources=[source]
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


def blocks_url(slug: str) -> str:
    return f"/api/artifacts/{slug}/blocks"


def responses_url(slug: str, key: str) -> str:
    return f"/api/artifacts/{slug}/blocks/{key}/responses"


def post(client: TestClient, slug: str, key: str, response: Any):
    return client.post(responses_url(slug, key), json={"response": response})


def all_keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {k for v in value.values() for k in all_keys(v)}
    if isinstance(value, list):
        return {k for v in value for k in all_keys(v)}
    return set()


def assert_no_leak(body: Any, raw: str, *, secrets: tuple[str, ...]) -> None:
    """No private key anywhere, and no secret text anywhere, in a response."""
    keys = all_keys(body)
    for field in PRIVATE_FIELDS:
        assert field not in keys, f"{field!r} leaked"
    for secret in secrets:
        assert secret not in raw, f"{secret!r} leaked"


def assert_no_secrets_in_content(blocks: list[dict]) -> None:
    for block in blocks:
        assert not set(block["content"]) & set(PRIVATE_FIELDS)


# -- GET blocks -----------------------------------------------------------------------------


def test_blocks_initial_view(client: TestClient, study: str) -> None:
    r = client.get(blocks_url(study))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["artifact"] == study
    assert (body["total_blocks"], body["locked_blocks"], body["complete"]) == (5, 3, False)
    b1, b2 = body["blocks"]
    assert b1 == {
        "key": "b01",
        "position": 1,
        "type": "prose",
        "is_gate": False,
        "content": {"markdown": "# Intro\n\nOpening text."},
        "state": None,
        "attempts": 0,
        "revealed": None,
        "segments": [
            {
                "id": b1["segments"][0]["id"],
                "source": "src-1",
                "kind": "section",
                "label": "intro",
                "title": "Intro",
                "locator": {"type": "text_span", "start_char": 0, "end_char": 6},
            }
        ],
        "concepts": [],
    }
    assert b2 == {
        "key": "b02",
        "position": 2,
        "type": "multiple_choice",
        "is_gate": True,
        "content": {"question": "Which letter?", "options": ["alpha", "beta", "gamma"]},
        "state": "open",
        "attempts": 0,
        "revealed": None,
        "segments": [],
        "concepts": [],
    }


def test_blocks_never_leak_private_or_locked_content(client: TestClient, study: str) -> None:
    r = client.get(blocks_url(study))
    assert_no_leak(r.json(), r.text, secrets=MC_SECRETS + BLURB_SECRETS + LOCKED_MARKERS)
    for key in ("b03", "b04", "b05"):
        assert f'"{key}"' not in r.text


def test_failed_attempt_does_not_leak(client: TestClient, study: str) -> None:
    r = post(client, study, "b02", {"choice": 0})
    assert r.status_code == 201, r.text
    secrets = MC_SECRETS + BLURB_SECRETS + LOCKED_MARKERS
    assert_no_leak(r.json(), r.text, secrets=secrets)
    assert r.json()["revealed"] is None and r.json()["unlocked"] == []

    listing = client.get(responses_url(study, "b02"))
    assert listing.status_code == 200
    assert_no_leak(listing.json(), listing.text, secrets=secrets)

    view = client.get(blocks_url(study))
    assert_no_leak(view.json(), view.text, secrets=secrets)
    assert view.json()["blocks"][1]["attempts"] == 1


def test_blocks_unknown_artifact_404(client: TestClient) -> None:
    r = client.get(blocks_url("nope"))
    assert r.status_code == 404
    assert set(r.json()) == {"detail"}


@pytest.mark.parametrize("status", ["generating", "failed"])
def test_blocks_require_ready(client: TestClient, make_artifact: MakeArtifact, status: str) -> None:
    slug = make_artifact("pending", status=status)
    r = client.get(blocks_url(slug))
    assert r.status_code == 409
    assert status in r.json()["detail"]
    assert post(client, slug, "b02", {"choice": 1}).status_code == 409
    assert client.get(responses_url(slug, "b02")).status_code == 409


def test_artifact_without_gates_is_complete(client: TestClient, make_artifact: MakeArtifact) -> None:
    slug = make_artifact("plain", page=[("prose", {"markdown": "a"}), ("prose", {"markdown": "b"})])
    body = client.get(blocks_url(slug)).json()
    assert (body["total_blocks"], body["locked_blocks"], body["complete"]) == (2, 0, True)
    assert [b["key"] for b in body["blocks"]] == ["b01", "b02"]


# -- POST responses -------------------------------------------------------------------------


def test_correct_answer_reveals_and_unlocks(client: TestClient, study: str) -> None:
    r = post(client, study, "b02", {"choice": 1})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["attempt"] == 1 and body["passed"] is True
    assert body["feedback"] == "SECRET-EXPLANATION: beta."
    assert body["state"] == "passed"
    assert body["revealed"] == {"correct_index": 1, "explanation": "SECRET-EXPLANATION: beta."}
    assert [b["key"] for b in body["unlocked"]] == ["b03", "b04"]
    assert body["locked_blocks"] == 1 and body["complete"] is False
    # the newly visible blurb gate is open: its private part is not sent
    assert_no_leak(body["unlocked"], r.text, secrets=BLURB_SECRETS + ("LOCKED-PROSE-FIVE",))
    blurb = body["unlocked"][1]
    assert blurb["content"] == {"prompt": "Summarise the text.", "min_words": 5}
    assert blurb["state"] == "open" and blurb["revealed"] is None

    view = client.get(blocks_url(study)).json()
    assert [b["key"] for b in view["blocks"]] == ["b01", "b02", "b03", "b04"]
    assert view["blocks"][1]["state"] == "passed"
    assert view["blocks"][1]["revealed"]["correct_index"] == 1
    assert_no_secrets_in_content(view["blocks"])


def test_full_walkthrough_completes(client: TestClient, study: str) -> None:
    assert post(client, study, "b02", {"choice": 1}).status_code == 201
    short = post(client, study, "b04", {"text": "too short"})
    assert short.status_code == 201
    assert short.json()["passed"] is False and short.json()["state"] == "open"
    assert_no_leak(short.json(), short.text, secrets=BLURB_SECRETS + ("LOCKED-PROSE-FIVE",))

    r = post(client, study, "b04", {"text": LONG_TEXT})
    body = r.json()
    assert r.status_code == 201
    assert body["attempt"] == 2 and body["passed"] is True and body["state"] == "passed"
    assert body["revealed"] == REVEALED_BLURB
    assert [b["key"] for b in body["unlocked"]] == ["b05"]
    assert body["locked_blocks"] == 0 and body["complete"] is True

    view = client.get(blocks_url(study)).json()
    assert view["complete"] is True and view["locked_blocks"] == 0
    assert len(view["blocks"]) == 5
    assert_no_secrets_in_content(view["blocks"])


def test_escape_hatch_after_max_attempts(client: TestClient, study: str) -> None:
    first = post(client, study, "b02", {"choice": 0}).json()
    assert first["state"] == "open" and first["revealed"] is None
    second = post(client, study, "b02", {"choice": 2}).json()
    assert second["attempt"] == 2 and second["passed"] is False
    assert second["state"] == "revealed"
    assert second["revealed"] == {"correct_index": 1, "explanation": "SECRET-EXPLANATION: beta."}
    assert [b["key"] for b in second["unlocked"]] == ["b03", "b04"]

    view = client.get(blocks_url(study)).json()
    assert view["blocks"][1]["state"] == "revealed" and view["blocks"][1]["attempts"] == 2
    assert view["blocks"][1]["revealed"]["explanation"].startswith("SECRET-EXPLANATION")

    # a revealed gate still accepts attempts and can still be passed
    third = post(client, study, "b02", {"choice": 1})
    assert third.status_code == 201
    assert third.json()["state"] == "passed" and third.json()["unlocked"] == []
    assert post(client, study, "b02", {"choice": 1}).status_code == 409


def test_max_attempts_from_app(settings: Settings, make_artifact: MakeArtifact) -> None:
    with TestClient(create_app(settings, max_attempts=3)) as client:
        slug = make_artifact()
        for expected in ("open", "open", "revealed"):
            assert post(client, slug, "b02", {"choice": 0}).json()["state"] == expected


@pytest.mark.parametrize(
    ("key", "status"),
    [("b99", 404), ("b01", 409), ("b03", 409), ("b04", 409), ("b05", 409)],
)
def test_post_errors(client: TestClient, study: str, key: str, status: int) -> None:
    r = post(client, study, key, {"choice": 1})
    assert r.status_code == status, r.text
    assert set(r.json()) == {"detail"}


def test_post_unknown_artifact_404(client: TestClient) -> None:
    assert post(client, "nope", "b02", {"choice": 1}).status_code == 404


def test_post_already_passed_409(client: TestClient, study: str) -> None:
    assert post(client, study, "b02", {"choice": 1}).status_code == 201
    r = post(client, study, "b02", {"choice": 1})
    assert r.status_code == 409
    assert "passed" in r.json()["detail"]


@pytest.mark.parametrize(
    "payload",
    [
        {"response": {"choice": "beta"}},
        {"response": {"choice": 7}},
        {"response": {}},
        {"response": {"choice": 1, "extra": True}},
        {"response": [1]},
        {"response": "1"},
        {"choice": 1},
        {"response": {"choice": 1}, "extra": 1},
        {},
    ],
)
def test_post_malformed_422_records_nothing(
    client: TestClient, study: str, payload: Any
) -> None:
    r = client.post(responses_url(study, "b02"), json=payload)
    assert r.status_code == 422, r.text
    assert isinstance(r.json()["detail"], str)
    assert client.get(responses_url(study, "b02")).json() == []


def test_post_without_grader_422(settings: Settings, make_artifact: MakeArtifact) -> None:
    with TestClient(create_app(settings, graders={"blurb": WordCountBlurbGrader()})) as client:
        slug = make_artifact()
        r = post(client, slug, "b02", {"choice": 1})
        assert r.status_code == 422 and "grader" in r.json()["detail"]


def test_custom_graders_from_app(settings: Settings, make_artifact: MakeArtifact) -> None:
    class Lenient:
        def grade(self, content: dict, response: dict) -> GradeResult:
            return GradeResult("passed", feedback="ok", grader="lenient-v1")

    graders = {"multiple_choice": Lenient(), "blurb": Lenient()}
    with TestClient(create_app(settings, graders=graders)) as client:
        slug = make_artifact()
        assert post(client, slug, "b02", {"whatever": 1}).json()["passed"] is True
        listing = client.get(responses_url(slug, "b02")).json()
        assert listing[0]["grader"] == "lenient-v1"


def test_default_graders_installed(settings: Settings) -> None:
    app = create_app(settings)
    assert isinstance(app.state.graders["multiple_choice"], MultipleChoiceGrader)
    assert isinstance(app.state.graders["prediction"], PredictionGrader)
    assert isinstance(app.state.graders["blurb"], LLMJudgeGrader)


# -- GET responses --------------------------------------------------------------------------


def test_list_responses(client: TestClient, study: str) -> None:
    assert client.get(responses_url(study, "b02")).json() == []
    post(client, study, "b02", {"choice": 0})
    post(client, study, "b02", {"choice": 1})
    r = client.get(responses_url(study, "b02"))
    assert r.status_code == 200
    first, second = r.json()
    assert set(first) == {
        "attempt", "response", "passed", "verdict", "feedback", "missing", "confidence",
        "grader", "details", "created_at",
    }
    assert (first["verdict"], first["missing"], first["confidence"]) == ("failed", [], None)
    assert second["verdict"] == "passed"
    assert first["details"] is None and second["details"] is None  # none for multiple choice
    assert (first["attempt"], first["response"], first["passed"]) == (1, {"choice": 0}, False)
    assert (second["attempt"], second["passed"]) == (2, True)
    assert second["grader"] == "multiple-choice-v2"
    assert "T" in first["created_at"] and first["created_at"].endswith("+00:00")
    # non-gate visible blocks simply have no responses
    assert client.get(responses_url(study, "b01")).json() == []


@pytest.mark.parametrize(("key", "status"), [("b03", 409), ("b04", 409), ("b99", 404)])
def test_list_responses_errors(client: TestClient, study: str, key: str, status: int) -> None:
    assert client.get(responses_url(study, key)).status_code == status


def test_list_responses_unknown_artifact(client: TestClient) -> None:
    assert client.get(responses_url("nope", "b01")).status_code == 404


# -- the LLM judge through the API (fake client, no network) --------------------------------


@pytest.fixture
def judge_client(
    settings: Settings, make_artifact: MakeArtifact, fake_anthropic
) -> Iterator[tuple[TestClient, Any, str]]:
    """`(client, fake, slug)`: an app whose blurb grader is the judge on a fake client, with the
    multiple-choice gate b02 already passed so the blurb gate b04 is open."""
    fake = fake_anthropic()
    graders = {"multiple_choice": MultipleChoiceGrader(), "blurb": LLMJudgeGrader(fake)}
    with TestClient(create_app(settings, graders=graders)) as client:
        slug = make_artifact()
        assert post(client, slug, "b02", {"choice": 1}).status_code == 201
        yield client, fake, slug


def judged(fake_anthropic, one: bool, two: bool, feedback: str = "Look again at the cause."):
    return fake_anthropic.reply(
        [(one, "SECRET-JUSTIFICATION-ONE"), (two, "SECRET-JUSTIFICATION-TWO")],
        feedback=feedback,
        input_tokens=500,
        output_tokens=80,
    )


def judge_details(one: bool, two: bool) -> dict[str, Any]:
    return {
        "model": "claude-haiku-5-5",
        "score": (one + two) / 2,
        "criteria": [
            {"criterion": "SECRET-RUBRIC-ONE", "required": True, "met": one,
             "justification": "SECRET-JUSTIFICATION-ONE"},
            {"criterion": "SECRET-RUBRIC-TWO", "required": False, "met": two,
             "justification": "SECRET-JUSTIFICATION-TWO"},
        ],
        "input_tokens": 500,
        "output_tokens": 80,
    }


def blurb_attempts(client: TestClient, slug: str) -> int:
    return client.get(blocks_url(slug)).json()["blocks"][3]["attempts"]


def test_judge_pass_returns_and_persists_details(judge_client, fake_anthropic) -> None:
    client, fake, slug = judge_client
    fake.outcomes.append(judged(fake_anthropic, True, False, feedback="Nicely argued."))
    r = post(client, slug, "b04", {"text": LONG_TEXT})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["passed"] is True and body["state"] == "passed"  # optional item unmet
    assert body["feedback"] == "Nicely argued."
    assert body["details"] == judge_details(True, False)
    assert body["revealed"] == REVEALED_BLURB
    assert f"<learner_answer>\n{LONG_TEXT}\n</learner_answer>" in fake.prompt

    (attempt,) = client.get(responses_url(slug, "b04")).json()
    assert attempt["grader"] == "llm-judge-v2" and attempt["passed"] is True
    assert attempt["verdict"] == "passed" and attempt["missing"] == []
    assert attempt["feedback"] == "Nicely argued."
    assert attempt["details"] == judge_details(True, False)  # round-tripped through the db


def test_judge_details_hidden_until_gate_cleared(judge_client, fake_anthropic) -> None:
    client, fake, slug = judge_client
    secrets = BLURB_SECRETS + JUDGE_SECRETS + ("LOCKED-PROSE-FIVE",)
    fake.outcomes.append(judged(fake_anthropic, False, True))
    first = post(client, slug, "b04", {"text": LONG_TEXT})
    assert first.status_code == 201, first.text
    assert first.json()["passed"] is False and first.json()["state"] == "open"
    assert first.json()["details"] is None and first.json()["revealed"] is None
    assert_no_leak(first.json(), first.text, secrets=secrets)

    listing = client.get(responses_url(slug, "b04"))
    assert [a["details"] for a in listing.json()] == [None]
    assert_no_leak(listing.json(), listing.text, secrets=secrets)
    view = client.get(blocks_url(slug))  # b02 (passed) legitimately shows its revealed part
    assert view.json()["blocks"][3]["revealed"] is None
    assert not [secret for secret in secrets if secret in view.text]

    # the second failed attempt reveals the gate: now the verdicts may be shown
    fake.outcomes.append(judged(fake_anthropic, False, False))
    second = post(client, slug, "b04", {"text": LONG_TEXT + " again"})
    assert second.json()["state"] == "revealed"
    assert second.json()["details"] == judge_details(False, False)
    listing = client.get(responses_url(slug, "b04")).json()
    assert [a["details"] for a in listing] == [
        judge_details(False, True),
        judge_details(False, False),
    ]


def unavailable_outcomes(fake_anthropic) -> dict[str, Any]:
    status = fake_anthropic.status_error
    return {
        "auth": status(anthropic.AuthenticationError, 401, "authentication_error"),
        "permission": status(anthropic.PermissionDeniedError, 403, "permission_error"),
        "rate limit": status(anthropic.RateLimitError, 429, "rate_limit_error"),
        "server": status(anthropic.InternalServerError, 500, "api_error"),
        "overloaded": status(anthropic.OverloadedError, 529, "overloaded_error"),
        "timeout": fake_anthropic.timeout(),
        "connection": fake_anthropic.connection_error(),
        "refusal": fake_anthropic.reply([(True, "a"), (True, "b")], stop_reason="refusal"),
        "truncated": fake_anthropic.reply(text='{"criteria": [', stop_reason="max_tokens"),
        "unparsable": fake_anthropic.reply(text="not json at all"),
        "mismatch": fake_anthropic.reply([(True, "a")]),
    }


@pytest.mark.parametrize(
    "case",
    ["auth", "permission", "rate limit", "server", "overloaded", "timeout", "connection",
     "refusal", "truncated", "unparsable", "mismatch"],
)
def test_judge_unavailable_is_503_and_records_nothing(
    judge_client, fake_anthropic, case: str
) -> None:
    client, fake, slug = judge_client
    fake.outcomes.append(unavailable_outcomes(fake_anthropic)[case])
    r = post(client, slug, "b04", {"text": LONG_TEXT})
    assert r.status_code == 503, r.text
    assert set(r.json()) == {"detail"} and r.json()["detail"].startswith("Grading is unavailable")
    assert_no_leak(r.json(), r.text, secrets=BLURB_SECRETS + JUDGE_SECRETS)
    assert len(fake.calls) == 1
    assert client.get(responses_url(slug, "b04")).json() == []
    assert blurb_attempts(client, slug) == 0
    assert client.get(blocks_url(slug)).json()["blocks"][3]["state"] == "open"


def test_judge_too_short_is_422_and_not_an_attempt(judge_client) -> None:
    client, fake, slug = judge_client
    r = post(client, slug, "b04", {"text": "too short"})
    assert r.status_code == 422
    assert "at least 5 words" in r.json()["detail"]
    assert fake.calls == []
    assert client.get(responses_url(slug, "b04")).json() == []
    assert blurb_attempts(client, slug) == 0


@pytest.mark.parametrize("response", [{"text": "   "}, {"text": LONG_TEXT, "extra": 1}])
def test_judge_malformed_is_422_without_a_call(judge_client, response: Any) -> None:
    client, fake, slug = judge_client
    assert post(client, slug, "b04", response).status_code == 422
    assert fake.calls == [] and blurb_attempts(client, slug) == 0


TWO_REQUIRED_PAGE: list[tuple[str, dict[str, Any]]] = [
    ("prose", {"markdown": "Read this."}),
    (
        "blurb",
        {
            "prompt": "Explain the trade-off.",
            "min_words": 5,
            "private": {
                "model_answer": "SECRET-MODEL-ANSWER",
                "rubric": [
                    {"criterion": "names the benefit"},
                    {"criterion": "names the cost"},
                    {"criterion": "SECRET-OPTIONAL", "required": False},
                ],
            },
        },
    ),
    ("prose", {"markdown": "LOCKED-PROSE-THREE"}),
]


def test_a_partial_answer_lists_what_is_missing_and_keeps_the_gate_open(
    settings: Settings, make_artifact: MakeArtifact, fake_anthropic
) -> None:
    fake = fake_anthropic(
        fake_anthropic.reply(
            [(True, "SECRET-JUSTIFICATION-ONE"), (False, "SECRET-JUSTIFICATION-TWO"), (True, "x")],
            feedback="You have the benefit; now weigh it against something.",
        )
    )
    graders = {**offline_graders(), "blurb": LLMJudgeGrader(fake)}
    with TestClient(create_app(settings, graders=graders)) as client:
        slug = make_artifact(page=TWO_REQUIRED_PAGE)
        r = client.post(
            responses_url(slug, "b02"), json={"response": {"text": LONG_TEXT}, "confidence": "high"}
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert (body["passed"], body["verdict"], body["state"]) == (False, "partial", "open")
        assert body["missing"] == ["names the cost"]  # word for word, straight away
        assert body["confidence"] == "high"
        assert body["details"] is None and body["revealed"] is None and body["unlocked"] == []
        for secret in ("SECRET-MODEL-ANSWER", "SECRET-OPTIONAL", "SECRET-JUSTIFICATION",
                       "LOCKED-PROSE-THREE", "names the benefit"):
            assert secret not in r.text
        (attempt,) = client.get(responses_url(slug, "b02")).json()
        assert (attempt["verdict"], attempt["missing"], attempt["confidence"]) == (
            "partial", ["names the cost"], "high"
        )
        assert attempt["details"] is None


@pytest.mark.parametrize("confidence", ["low", "medium", "high", None])
def test_confidence_is_recorded_with_the_attempt(
    client: TestClient, study: str, confidence: str | None
) -> None:
    r = client.post(
        responses_url(study, "b02"), json={"response": {"choice": 0}, "confidence": confidence}
    )
    assert r.status_code == 201, r.text
    assert r.json()["confidence"] == confidence and r.json()["verdict"] == "failed"
    assert client.get(responses_url(study, "b02")).json()[0]["confidence"] == confidence


@pytest.mark.parametrize("confidence", ["certain", "", 1, "HIGH"])
def test_an_unknown_confidence_is_422_and_records_nothing(
    client: TestClient, study: str, confidence: Any
) -> None:
    r = client.post(
        responses_url(study, "b02"), json={"response": {"choice": 1}, "confidence": confidence}
    )
    assert r.status_code == 422, r.text
    assert client.get(responses_url(study, "b02")).json() == []


PREDICTION_PAGE: list[tuple[str, dict[str, Any]]] = [
    ("prose", {"markdown": "Before the guess."}),
    (
        "prediction",
        {
            "prompt": "Will the loss go up or down?",
            "options": ["up", "down"],
            "private": {"correct_index": 1, "resolution": "SECRET-RESOLUTION: it goes down."},
        },
    ),
    ("prose", {"markdown": "LOCKED-PROSE-THREE"}),
    (
        "multiple_choice",
        {
            "question": "Which?",
            "options": ["a", "b"],
            "private": {
                "correct_index": 0,
                "explanation": "SECRET-EXPLANATION",
                "option_explanations": ["SECRET-RIGHT-WHY", "B misreads the axis."],
            },
        },
    ),
]


def test_a_prediction_is_cleared_by_any_guess_and_then_resolved(
    client: TestClient, make_artifact: MakeArtifact
) -> None:
    slug = make_artifact(page=PREDICTION_PAGE)
    view = client.get(blocks_url(slug)).json()
    prediction = view["blocks"][1]
    assert prediction["type"] == "prediction" and prediction["is_gate"] is True
    assert prediction["content"] == {"prompt": "Will the loss go up or down?", "options": ["up", "down"]}
    assert "SECRET-RESOLUTION" not in str(view) and view["locked_blocks"] == 2

    r = post(client, slug, "b02", {"choice": 0})  # the wrong guess clears it too
    assert r.status_code == 201, r.text
    body = r.json()
    assert (body["passed"], body["verdict"], body["state"]) == (True, "passed", "passed")
    assert body["details"] == {"correct": False} and body["confidence"] is None
    assert body["revealed"] == {"correct_index": 1, "resolution": "SECRET-RESOLUTION: it goes down."}
    assert "SECRET-RESOLUTION" not in body["feedback"]
    assert [b["key"] for b in body["unlocked"]] == ["b03", "b04"]
    assert post(client, slug, "b02", {"choice": 1}).status_code == 409  # one guess


def test_a_prediction_takes_no_confidence(client: TestClient, make_artifact: MakeArtifact) -> None:
    slug = make_artifact(page=PREDICTION_PAGE)
    r = client.post(
        responses_url(slug, "b02"), json={"response": {"choice": 0}, "confidence": "low"}
    )
    assert r.status_code == 422 and "takes no confidence" in r.json()["detail"]
    assert client.get(responses_url(slug, "b02")).json() == []


def test_a_wrong_choice_is_told_why_without_the_answer(
    client: TestClient, make_artifact: MakeArtifact
) -> None:
    slug = make_artifact(page=PREDICTION_PAGE)
    post(client, slug, "b02", {"choice": 1})
    r = post(client, slug, "b04", {"choice": 1})
    body = r.json()
    assert body["verdict"] == "failed" and body["feedback"] == "Not quite. B misreads the axis."
    assert "SECRET" not in r.text
    cleared = post(client, slug, "b04", {"choice": 0}).json()
    assert cleared["revealed"]["option_explanations"] == ["SECRET-RIGHT-WHY", "B misreads the axis."]


def test_default_judge_without_credentials_is_503(
    settings: Settings, make_artifact: MakeArtifact, monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path, forbid_real_messages_api: None,
) -> None:
    monkeypatch.delenv("ANTHROPIC_CONFIG_DIR")
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    with TestClient(create_app(settings)) as client:  # default graders: the real judge
        slug = make_artifact()
        assert post(client, slug, "b02", {"choice": 1}).status_code == 201
        r = post(client, slug, "b04", {"text": LONG_TEXT})
        assert r.status_code == 503
        assert "ANTHROPIC_API_KEY" in r.json()["detail"]
        assert client.get(responses_url(slug, "b04")).json() == []
        assert blurb_attempts(client, slug) == 0


# -- assets ---------------------------------------------------------------------------------


@pytest.fixture
def assets(settings: Settings, study: str) -> Iterator[Path]:
    folder = settings.artifacts_dir / study / "assets"
    (folder / "img").mkdir()
    (folder / "img" / "fig.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>")
    (folder / "notes.txt").write_text("asset text")
    yield folder


def test_asset_served(client: TestClient, study: str, assets: Path) -> None:
    r = client.get(f"/api/artifacts/{study}/assets/notes.txt")
    assert r.status_code == 200 and r.text == "asset text"
    assert r.headers["content-type"].startswith("text/plain")
    svg = client.get(f"/api/artifacts/{study}/assets/img/fig.svg")
    assert svg.status_code == 200 and svg.headers["content-type"].startswith("image/svg+xml")


@pytest.mark.parametrize(
    "path",
    [
        "missing.txt",
        "img",
        "img/",
        "..%2Fblocks%2Fb02.json",
        "%2e%2e/blocks/b02.json",
        "img/%2e%2e/%2e%2e/blocks/b02.json",
        "%2e%2e/%2e%2e/%2e%2e/teacher.db",
        "%2Fetc%2Fpasswd",
        "/etc/passwd",
        "%2e%2e",
    ],
)
def test_asset_not_found_or_outside(
    client: TestClient, study: str, assets: Path, path: str
) -> None:
    r = client.get(f"/api/artifacts/{study}/assets/{path}")
    assert r.status_code == 404, r.text
    assert set(r.json()) == {"detail"}
    assert "SECRET" not in r.text and str(assets) not in r.text


def test_asset_symlink_outside_rejected(
    client: TestClient, study: str, assets: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("outside")
    os.symlink(outside, assets / "link.txt")
    assert client.get(f"/api/artifacts/{study}/assets/link.txt").status_code == 404


def test_asset_unknown_artifact(client: TestClient) -> None:
    assert client.get("/api/artifacts/nope/assets/x.txt").status_code == 404
