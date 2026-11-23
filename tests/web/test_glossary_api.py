"""Glossary API: visibility filtering with sentinel terms, the `generated` flag, building and
rebuilding on demand, 409/503/502/422, the recorded run, and regenerate/delete."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import anthropic
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from teacher.blocks import is_gate_type, validate_block_content
from teacher.claude_generator import GlossaryBuilder
from teacher.config import Settings
from teacher.db.engine import session_scope
from teacher.db.models import GlossaryTerm
from teacher.generation import StubGenerator
from teacher.glossary import GlossaryEntry
from teacher.grading import MultipleChoiceGrader, WordCountBlurbGrader
from teacher.managers import (
    ArtifactBlockManager,
    ArtifactManager,
    GenerationRunManager,
    GlossaryManager,
    KnowledgeSourceManager,
)
from teacher.web.app import create_app

# Each sentinel occurs in exactly one place of the page, so its visibility is unambiguous.
PAGE: list[tuple[str, dict[str, Any]]] = [
    ("prose", {"markdown": "# Intro\n\nWe train a GAN (several GANs) here."}),
    (
        "multiple_choice",
        {
            "question": "Which optimiser does the VISIBLEQ step use?",
            "options": ["SGD", "none"],
            "private": {"correct_index": 0, "explanation": "HIDDENEXPL is the reason."},
        },
    ),
    ("prose", {"markdown": "LOCKEDPROSE appears only after the first gate."}),
    (
        "blurb",
        {
            "prompt": "Summarise.",
            "min_words": 3,
            "private": {"model_answer": "HIDDENANSWER here.", "rubric": [{"criterion": "x"}]},
        },
    ),
]


def wire(term: str, kind: str = "acronym", aliases: list[str] | None = None) -> dict[str, Any]:
    return {"term": term, "kind": kind, "expansion": None, "definition": f"About {term}.",
            "aliases": aliases or []}


TERMS = [
    wire("GAN", aliases=["GANs"]),
    wire("VISIBLEQ"),
    wire("SGD"),
    wire("HIDDENEXPL"),
    wire("LOCKEDPROSE"),
    wire("HIDDENANSWER"),
]


@pytest.fixture
def builder_fake(fake_anthropic) -> Any:
    return fake_anthropic()


@pytest.fixture
def app(settings: Settings, builder_fake: Any) -> FastAPI:
    return create_app(
        settings,
        generator=StubGenerator(),
        graders={"multiple_choice": MultipleChoiceGrader(), "blurb": WordCountBlurbGrader()},
        glossary_builder=GlossaryBuilder(builder_fake, model="claude-test-writer"),
    )


MakeArtifact = Callable[..., str]


@pytest.fixture
def make_artifact(settings: Settings, tmp_path: Path, client: TestClient) -> MakeArtifact:
    counter = iter(range(1, 1000))

    def make(slug: str = "study", *, status: str = "ready") -> str:
        n = next(counter)
        src = tmp_path / f"source-{n}.txt"
        src.write_text(f"Source text number {n}: GANs and SGD.\n")
        with session_scope(settings) as session:
            source = KnowledgeSourceManager(session, settings).add(
                src, slug=f"src-{n}", kind="note", title=f"Source {n}"
            )
            artifacts = ArtifactManager(session, settings)
            artifact = artifacts.create(
                slug=slug, kind="study_page", title="Study", generator="test", sources=[source]
            )
            blocks = ArtifactBlockManager(session, settings)
            for type_, content in PAGE:
                blocks.add(
                    artifact,
                    type=type_,
                    content=validate_block_content(type_, content),
                    is_gate=is_gate_type(type_),
                )
            artifacts.set_status(artifact, status)
        return slug

    return make


def url(slug: str) -> str:
    return f"/api/artifacts/{slug}/glossary"


def store(settings: Settings, slug: str, terms: list[dict[str, Any]]) -> None:
    with session_scope(settings) as session:
        artifact = ArtifactManager(session, settings).get_by_slug(slug)
        GlossaryManager(session, settings).replace(
            artifact,
            [GlossaryEntry(t["term"], t["kind"], t["definition"], t["expansion"],
                           tuple(t["aliases"])) for t in terms],
        )


def names(body: dict[str, Any]) -> list[str]:
    return [t["term"] for t in body["terms"]]


def glossary_runs(settings: Settings, slug: str) -> list[tuple]:
    with session_scope(settings) as session:
        artifact = ArtifactManager(session, settings).get_by_slug(slug)
        return [
            (r.stage, r.status, r.model, r.input_tokens, r.output_tokens, r.error, r.output_file)
            for r in GenerationRunManager(session, settings).list(artifact)
        ]


# -- GET ------------------------------------------------------------------------------------


def test_never_built_is_not_generated(client: TestClient, make_artifact: MakeArtifact) -> None:
    slug = make_artifact()
    assert client.get(url(slug)).json() == {"terms": [], "generated": False}


def test_built_with_no_terms_is_generated(
    client: TestClient, make_artifact: MakeArtifact, settings: Settings
) -> None:
    slug = make_artifact()
    store(settings, slug, [])
    assert client.get(url(slug)).json() == {"terms": [], "generated": True}


def test_only_terms_in_visible_text_are_listed(
    client: TestClient, make_artifact: MakeArtifact, settings: Settings
) -> None:
    slug = make_artifact()
    store(settings, slug, TERMS)
    response = client.get(url(slug))
    assert response.status_code == 200
    body = response.json()
    assert body["generated"] is True
    # Block 2 is an open gate: its question and options are visible, its explanation is not;
    # blocks 3 and 4 are locked.
    assert names(body) == ["GAN", "VISIBLEQ", "SGD"]
    for hidden in ("HIDDENEXPL", "LOCKEDPROSE", "HIDDENANSWER"):
        assert hidden not in response.text
    assert body["terms"][0] == {
        "term": "GAN", "kind": "acronym", "expansion": None, "definition": "About GAN.",
        "aliases": ["GANs"],
    }

    # Clearing the gate reveals its explanation and unlocks block 3 (and the blurb's prompt).
    answer = client.post(f"/api/artifacts/{slug}/blocks/b02/responses", json={"response": {"choice": 0}})
    assert answer.status_code == 201 and answer.json()["passed"] is True
    assert names(client.get(url(slug)).json()) == ["GAN", "VISIBLEQ", "SGD", "HIDDENEXPL", "LOCKEDPROSE"]
    blurb = client.post(
        f"/api/artifacts/{slug}/blocks/b04/responses", json={"response": {"text": "one two three"}}
    )
    assert blurb.status_code == 201 and blurb.json()["state"] == "passed"
    assert names(client.get(url(slug)).json()) == [t["term"] for t in TERMS]


def test_aliases_used_only_in_locked_blocks_are_not_listed(
    client: TestClient, make_artifact: MakeArtifact, settings: Settings
) -> None:
    slug = make_artifact()
    store(settings, slug, [wire("GAN", aliases=["GANs", "LOCKEDPROSE"])])
    assert client.get(url(slug)).json()["terms"][0]["aliases"] == ["GANs"]


@pytest.mark.parametrize("status", ["generating", "failed"])
def test_get_409_unless_ready(
    client: TestClient, make_artifact: MakeArtifact, status: str
) -> None:
    slug = make_artifact(status=status)
    for response in (client.get(url(slug)), client.post(url(slug))):
        assert response.status_code == 409
        assert response.json() == {"detail": f"artifact {slug!r} is {status}, not ready"}


def test_unknown_artifact_404(client: TestClient) -> None:
    assert client.get(url("nope")).status_code == 404
    assert client.post(url("nope")).status_code == 404


# -- POST -----------------------------------------------------------------------------------


def queue(fake: Any, fake_anthropic: Any, terms: list[dict[str, Any]], **kw: Any) -> None:
    fake.outcomes.append(
        fake_anthropic.reply(text=json.dumps({"terms": terms}), model="claude-test-writer",
                             input_tokens=40, output_tokens=60, cache_read_input_tokens=1000, **kw)
    )


def test_build_returns_the_visible_glossary_and_records_a_run(
    client: TestClient, make_artifact: MakeArtifact, settings: Settings, builder_fake: Any,
    fake_anthropic: Any,
) -> None:
    slug = make_artifact()
    queue(builder_fake, fake_anthropic, [*TERMS, wire("ResNet")])
    response = client.post(url(slug))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["generated"] is True and body["dropped_count"] == 1
    assert names(body) == ["GAN", "VISIBLEQ", "SGD"]
    assert client.get(url(slug)).json() == {k: v for k, v in body.items() if k != "dropped_count"}

    # The whole page (locked blocks and private parts too) was sent to the model.
    (call,) = builder_fake.calls
    text = call["messages"][0]["content"][1]["text"]
    for sentinel in ("HIDDENEXPL", "LOCKEDPROSE", "HIDDENANSWER"):
        assert sentinel in text
    assert call["messages"][0]["content"][0]["source"]["data"].startswith("Source text number")
    assert call["model"] == "claude-test-writer"

    with session_scope(settings) as session:
        artifact = ArtifactManager(session, settings).get_by_slug(slug)
        assert [t.term for t in GlossaryManager(session, settings).list(artifact)] == [
            t["term"] for t in TERMS
        ]
    (run,) = glossary_runs(settings, slug)
    assert run == ("glossary", "succeeded", "claude-test-writer", 1040, 60, None,
                   f"artifacts/{slug}/generation/glossary.json")
    record = json.loads((settings.root / run[6]).read_text(encoding="utf-8"))
    assert record["trigger"] == "on_demand"
    assert record["dropped"] == [{"term": "ResNet", "reason": "does not occur in the study page text"}]
    assert client.get(f"/api/artifacts/{slug}").json()["tokens"] == {"input": 1040, "output": 60}


def test_rebuild_replaces(
    client: TestClient, make_artifact: MakeArtifact, settings: Settings, builder_fake: Any,
    fake_anthropic: Any,
) -> None:
    slug = make_artifact()
    store(settings, slug, TERMS)
    queue(builder_fake, fake_anthropic, [wire("SGD")])
    body = client.post(url(slug)).json()
    assert names(body) == ["SGD"] and body["dropped_count"] == 0
    with session_scope(settings) as session:
        assert session.scalar(select(func.count()).select_from(GlossaryTerm)) == 1


def test_no_session_is_held_during_the_model_call(
    client: TestClient, make_artifact: MakeArtifact, settings: Settings, builder_fake: Any,
    fake_anthropic: Any,
) -> None:
    slug = make_artifact()
    queue(builder_fake, fake_anthropic, [wire("SGD")])
    seen: list[Any] = []
    stream = builder_fake.stream

    def checking_stream(**kwargs: Any) -> Any:
        # Another connection can write while the model is being called, and sees the run.
        with session_scope(settings) as session:
            artifact = ArtifactManager(session, settings).get_by_slug(slug)
            artifact.summary = "written during the call"
            seen.extend((r.stage, r.status) for r in GenerationRunManager(session, settings).list(artifact))
        return stream(**kwargs)

    builder_fake.stream = checking_stream
    assert client.post(url(slug)).status_code == 200
    assert seen == [("glossary", "running")]
    assert client.get(f"/api/artifacts/{slug}").json()["summary"] == "written during the call"


def test_missing_credentials_503_without_a_run(
    settings: Settings, make_artifact: MakeArtifact, forbid_real_messages_api: None,
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.delenv("ANTHROPIC_CONFIG_DIR")
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    slug = make_artifact()
    with TestClient(create_app(settings, generator=StubGenerator())) as default_client:
        response = default_client.post(url(slug))
    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail.startswith("The glossary could not be built: no Anthropic API credentials")
    assert glossary_runs(settings, slug) == []


@pytest.mark.parametrize(
    ("outcome", "status", "fragment"),
    [
        (lambda f: f.status_error(anthropic.InternalServerError, 500, "api_error"), 503,
         "the glossary call to Claude failed: the Anthropic API had a server error"),
        (lambda f: f.timeout(), 503, "did not answer within"),
        (lambda f: f.reply(text="", stop_reason="refusal", input_tokens=5, output_tokens=1,
                           stop_details={"type": "refusal", "category": "cyber",
                                         "explanation": None}),
         503, "Claude declined the glossary step (stop_reason 'refusal', category cyber)"),
        (lambda f: f.reply(text='{"terms": [', stop_reason="max_tokens", input_tokens=5,
                           output_tokens=1),
         503, "cut off at max_tokens=32000"),
        (lambda f: f.reply(text="nope", input_tokens=5, output_tokens=1), 502,
         "not a valid WireGlossary"),
        (lambda f: f.reply(text=json.dumps({"terms": [wire("SGD"), wire("ResNet"), wire("VAE")]}),
                           input_tokens=5, output_tokens=1),
         502, "2 of the 3 terms Claude proposed had to be dropped"),
    ],
)
def test_build_failures_are_loud_and_change_nothing(
    client: TestClient, make_artifact: MakeArtifact, settings: Settings, builder_fake: Any,
    fake_anthropic: Any, outcome: Any, status: int, fragment: str,
) -> None:
    slug = make_artifact()
    store(settings, slug, TERMS)
    queued = outcome(fake_anthropic)
    builder_fake.outcomes.append(queued)
    response = client.post(url(slug))
    assert response.status_code == status
    detail = response.json()["detail"]
    assert detail.startswith("The glossary could not be built: ") and fragment in detail, detail
    assert names(client.get(url(slug)).json()) == ["GAN", "VISIBLEQ", "SGD"]  # unchanged
    (run,) = glossary_runs(settings, slug)
    assert run[:2] == ("glossary", "failed") and fragment in run[5]
    if not isinstance(queued, BaseException):
        assert run[3:5] == (5, 1)  # the reply's tokens are recorded even though it failed


def test_unusable_source_file_422(
    client: TestClient, make_artifact: MakeArtifact, settings: Settings, builder_fake: Any
) -> None:
    slug = make_artifact()
    with session_scope(settings) as session:
        artifact = ArtifactManager(session, settings).get_by_slug(slug)
        source, _role = ArtifactManager(session, settings).sources(artifact)[0]
        KnowledgeSourceManager(session, settings).primary_path(source).write_text("  \n")
    response = client.post(url(slug))
    assert response.status_code == 422
    assert "contains only whitespace" in response.json()["detail"]
    assert builder_fake.calls == [] and glossary_runs(settings, slug) == []


# -- regenerate and delete ------------------------------------------------------------------


def test_regenerated_artifact_has_its_own_glossary_and_delete_cascades(
    client: TestClient, make_artifact: MakeArtifact, settings: Settings
) -> None:
    slug = make_artifact()
    store(settings, slug, TERMS)
    created = client.post(f"/api/artifacts/{slug}/regenerate", json={})
    assert created.status_code == 202
    new = created.json()["slug"]
    assert client.get(url(new)).json() == {"terms": [], "generated": False}  # stub: none built
    assert names(client.get(url(slug)).json()) == ["GAN", "VISIBLEQ", "SGD"]

    assert client.delete(f"/api/artifacts/{slug}").status_code == 204
    with session_scope(settings) as session:
        assert session.scalar(select(func.count()).select_from(GlossaryTerm)) == 0
    assert client.get(url(new)).json()["generated"] is False
