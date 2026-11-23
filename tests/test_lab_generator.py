"""The lab generators through `run_generation`: `ClaudeLabGenerator` with a fake streaming client
and the local runner (happy path, request shape, an infeasible source, a failed verification
repaired once, a repair that fails too, preflight failures, run rows and saved records), the
`StubLabGenerator`, and the store step `run_generation` hands to a generator that brings one."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from teacher.claude_generator import EFFORT, GenerationError
from teacher.config import Settings
from teacher.db.engine import session_scope
from teacher.generation import GenerationContext, StubGenerator, run_generation
from teacher.glossary import GlossaryEntry
from teacher.lab_generator import (
    LAB_FORMAT,
    LAB_MAX_TOKENS,
    LAB_SYSTEM_PROMPT,
    ClaudeLabGenerator,
    StubLabGenerator,
    brief_summary,
    stub_lab,
)
from teacher.labs import LAB_FILE, LabSpec, read_lab
from teacher.managers import ArtifactManager, GenerationRunManager, KnowledgeSourceManager

TEXT = "# Attention\n\nAttention weighs values by how well their keys match a query.\n"
OUTLINE = {
    "summary": "Attention as a weighted average.",
    "main_claims": ["Scaled dot products give the weights."],
    "background": ["linear algebra"],
    "sections": [{"label": "attention", "title": "Attention", "start_page": None,
                  "end_page": None, "key_points": ["Softmax over scores."]}],
}
STUB = stub_lab("study-paper")


def wire(spec: LabSpec = STUB, **overrides: Any) -> dict[str, Any]:
    """A WireLab reply body for `spec`."""
    body = {
        "feasible": True,
        "reason": "Exercises scaled dot-product attention.",
        **{key: value for key, value in spec.model_dump().items() if key != "study_page"},
    }
    body.update(overrides)
    return body


INFEASIBLE = {"feasible": False, "reason": "It is a history of the printing press.",
              "title": "", "brief": "", "packages": [], "entry": "", "files": [], "steps": [],
              "solution": []}


def vacuous() -> dict[str, Any]:
    """The stub lab with a check its starter already passes."""
    body = wire()
    body["steps"][2]["check"] = "from attention import softmax\nassert softmax is not None\n"
    return body


def without_check() -> dict[str, Any]:
    """The stub lab with an implement step that has no check (a spec rule)."""
    body = wire()
    body["steps"][2]["check"] = None
    return body


# -- fixtures -------------------------------------------------------------------------------


@pytest.fixture
def study_page(settings: Settings, tmp_path: Path) -> str:
    """A ready study page `study-paper` (stub blocks) on the text source `paper`, with the
    outline file its Claude generator would have saved."""
    path = tmp_path / "paper.md"
    path.write_text(TEXT, encoding="utf-8")
    with session_scope(settings) as session:
        source = KnowledgeSourceManager(session, settings).add(
            path, slug="paper", kind="note", title="Attention notes", media_type="text/markdown"
        )
        ArtifactManager(session, settings).create(
            slug="study-paper", kind="study_page", title="Study: Attention notes",
            generator="stub-v1", sources=[source],
        )
    assert run_generation(settings, "study-paper", StubGenerator()) == "ready"
    outline_file = settings.artifacts_dir / "study-paper" / "generation" / "analyse.json"
    outline_file.parent.mkdir(parents=True)
    outline_file.write_text(json.dumps({"output": OUTLINE}), encoding="utf-8")
    return "study-paper"


@pytest.fixture
def make_lab(settings: Settings, study_page: str) -> Callable[..., str]:
    """`make_lab(slug, params=...)`: a `generating` lab of the study page."""

    def make(slug: str = "lab-paper", params: dict[str, Any] | None = None) -> str:
        with session_scope(settings) as session:
            artifacts = ArtifactManager(session, settings)
            page = artifacts.get_by_slug(study_page)
            artifacts.create(
                slug=slug, kind="lab", title="Lab: Attention notes", generator="claude-lab-v1",
                params=params if params is not None else {"study_page": study_page},
                sources=artifacts.sources(page),
            )
        return slug

    return make


@pytest.fixture
def client_for(fake_anthropic) -> Callable[..., Any]:
    """`client_for(*bodies)`: a fake client answering with each body (a dict becomes a JSON
    reply; anything else is queued as it is)."""

    def make(*bodies: Any) -> Any:
        outcomes = [
            fake_anthropic.reply(text=json.dumps(body), model="claude-sonnet-5-5",
                                 input_tokens=100 + n, output_tokens=900 + n,
                                 cache_creation_input_tokens=0 if n else 2000,
                                 cache_read_input_tokens=2000 if n else 0)
            if isinstance(body, dict) else body
            for n, body in enumerate(bodies)
        ]
        return fake_anthropic(*outcomes)

    return make


def runs(settings: Settings, slug: str) -> list[dict[str, Any]]:
    with session_scope(settings) as session:
        artifact = ArtifactManager(session, settings).get_by_slug(slug)
        return [
            {"stage": r.stage, "status": r.status, "model": r.model, "error": r.error,
             "input": r.input_tokens, "output": r.output_tokens, "file": r.output_file}
            for r in GenerationRunManager(session, settings).list(artifact)
        ]


def stages(settings: Settings, slug: str) -> list[tuple[str, str]]:
    return [(r["stage"], r["status"]) for r in runs(settings, slug)]


def artifact_row(settings: Settings, slug: str) -> Any:
    with session_scope(settings) as session:
        return ArtifactManager(session, settings).get_by_slug(slug)


def lab_dir(settings: Settings, slug: str) -> Path:
    return settings.artifacts_dir / slug


# -- the happy path -------------------------------------------------------------------------


def test_claude_lab_generation_stores_a_verified_lab(
    settings: Settings, make_lab, client_for, local_runner
) -> None:
    slug = make_lab()
    fake = client_for(wire())
    generator = ClaudeLabGenerator("claude-sonnet-5-5", client=fake, runner=local_runner)
    assert run_generation(settings, slug, generator) == "ready"

    assert stages(settings, slug) == [
        ("preflight", "succeeded"), ("write", "succeeded"), ("verify", "succeeded")
    ]
    recorded = runs(settings, slug)
    assert recorded[1]["input"] == 100 + 2000 and recorded[1]["output"] == 900
    assert recorded[1]["file"] == f"artifacts/{slug}/generation/write.json"
    assert recorded[2]["file"] == f"artifacts/{slug}/generation/verify.json"
    assert {r["model"] for r in recorded} == {"claude-sonnet-5-5"}

    stored = read_lab(lab_dir(settings, slug), slug=slug)
    assert stored == STUB  # the reply, with the study page from the params
    row = artifact_row(settings, slug)
    assert row.status == "ready"
    assert row.title == STUB.title
    assert row.summary == brief_summary(STUB.brief)
    assert row.summary.startswith("This placeholder lab") and "\n" not in row.summary

    write = json.loads((lab_dir(settings, slug) / "generation/write.json").read_text())
    assert write["generator"] == "claude-lab-v1" and write["study_page"] == "study-paper"
    assert write["output"]["feasible"] is True and write["source"]["slug"] == "paper"
    verify = json.loads((lab_dir(settings, slug) / "generation/verify.json").read_text())
    assert verify["starter"]["ok"] and verify["solution"]["checks"][0]["passed"]
    assert verify["starter"]["checks"][0]["passed"] is False
    assert len(local_runner.calls) == 2  # the starter and the solution, once each


def test_the_write_request(settings: Settings, make_lab, client_for, local_runner) -> None:
    slug = make_lab(params={"study_page": "study-paper", "model": "claude-opus-5-5"})
    fake = client_for(wire())
    generator = ClaudeLabGenerator("claude-sonnet-5-5", client=fake, runner=local_runner)
    assert run_generation(settings, slug, generator) == "ready"
    (call,) = fake.calls
    assert call["model"] == "claude-opus-5-5"  # the lab's chosen model
    assert call["system"] == LAB_SYSTEM_PROMPT
    assert call["max_tokens"] == LAB_MAX_TOKENS
    assert call["output_config"] == {"effort": EFFORT, "format": LAB_FORMAT}
    (turn,) = call["messages"]
    document, instructions = turn["content"]
    assert document["type"] == "document" and document["cache_control"] == {"type": "ephemeral"}
    assert document["source"]["data"] == TEXT and document["title"] == "Attention notes"
    text = instructions["text"]
    assert '"summary": "Attention as a weighted average."' in text  # the outline
    assert "<study_page>" in text and '<block number="1" type="prose">' in text
    assert 'hidden="true"' in text  # the page's private parts, marked
    assert "numpy, matplotlib" in text
    assert {r["model"] for r in runs(settings, slug)} == {"claude-opus-5-5"}


def test_the_wire_schema_is_api_compatible() -> None:
    schema = LAB_FORMAT["schema"]
    assert LAB_FORMAT["type"] == "json_schema"
    assert schema["additionalProperties"] is False
    assert schema["required"][:2] == ["feasible", "reason"]
    assert set(schema["required"]) == set(schema["properties"])


# -- infeasible -----------------------------------------------------------------------------


def test_an_infeasible_source_fails_with_the_reason(
    settings: Settings, make_lab, client_for, local_runner
) -> None:
    slug = make_lab()
    generator = ClaudeLabGenerator("claude-sonnet-5-5", client=client_for(INFEASIBLE),
                                   runner=local_runner)
    assert run_generation(settings, slug, generator) == "failed"
    assert stages(settings, slug) == [("preflight", "succeeded"), ("write", "failed")]
    error = runs(settings, slug)[1]["error"]
    assert error.startswith("GenerationError: no lab was made: in the write step Claude judged")
    assert "It is a history of the printing press." in error
    assert not (lab_dir(settings, slug) / LAB_FILE).exists()
    assert artifact_row(settings, slug).status == "failed"
    # the reply is kept for inspection, though the run records no output file
    assert (lab_dir(settings, slug) / "generation/write.json").is_file()
    assert runs(settings, slug)[1]["file"] is None
    assert local_runner.calls == []


# -- verify and repair ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("first", "problem"),
    [
        (vacuous(), "already passes on the starter files, so it tests nothing"),
        (without_check(), "the lab breaks a rule: steps.2: implement step 'Implement attention' "
                          "must have a check"),
    ],
)
def test_a_failed_verification_is_repaired_once(
    settings: Settings, make_lab, client_for, local_runner, first: dict, problem: str
) -> None:
    slug = make_lab()
    fake = client_for(first, wire())
    generator = ClaudeLabGenerator("claude-sonnet-5-5", client=fake, runner=local_runner)
    assert run_generation(settings, slug, generator) == "ready"
    assert stages(settings, slug) == [
        ("preflight", "succeeded"), ("write", "succeeded"), ("verify", "failed"),
        ("repair", "succeeded"), ("verify", "succeeded"),
    ]
    recorded = runs(settings, slug)
    assert recorded[2]["error"].startswith("LabVerificationError: the lab failed verification")
    assert problem in recorded[2]["error"]
    assert recorded[3]["file"] == f"artifacts/{slug}/generation/repair.json"
    assert recorded[4]["file"] == f"artifacts/{slug}/generation/verify-2.json"
    assert recorded[3]["input"] == 101 + 2000  # the repair read the cached document

    write_call, repair_call = fake.calls
    assert repair_call["system"] == write_call["system"]
    assert repair_call["output_config"] == write_call["output_config"]
    assert repair_call["messages"][0] == write_call["messages"][0]  # the same cached prefix
    assistant, user = repair_call["messages"][1:]
    assert assistant == {"role": "assistant",
                         "content": [{"type": "text", "text": json.dumps(first)}]}
    assert user["role"] == "user"
    assert "<verification_problems>\n- " in user["content"][0]["text"]
    assert problem in user["content"][0]["text"]
    assert read_lab(lab_dir(settings, slug), slug=slug) == STUB


def test_a_repair_that_still_fails_fails_the_generation(
    settings: Settings, make_lab, client_for, local_runner
) -> None:
    slug = make_lab()
    broken = wire(files=[{"path": "attention.py", "content": "raise RuntimeError('oops')\n"}])
    fake = client_for(vacuous(), broken)
    generator = ClaudeLabGenerator("claude-sonnet-5-5", client=fake, runner=local_runner)
    assert run_generation(settings, slug, generator) == "failed"
    assert stages(settings, slug) == [
        ("preflight", "succeeded"), ("write", "succeeded"), ("verify", "failed"),
        ("repair", "succeeded"), ("verify", "failed"),
    ]
    error = runs(settings, slug)[-1]["error"]
    assert error.startswith("LabVerificationError: the lab failed verification")
    assert "the starter program (attention.py) failed with an error" in error
    assert "RuntimeError: oops" in error
    assert len(fake.calls) == 2  # exactly one repair
    assert not (lab_dir(settings, slug) / LAB_FILE).exists()
    assert artifact_row(settings, slug).status == "failed"


def test_a_repair_that_gives_up_fails_with_its_reason(
    settings: Settings, make_lab, client_for, local_runner
) -> None:
    slug = make_lab()
    fake = client_for(vacuous(), {**INFEASIBLE, "reason": "No check can test this."})
    generator = ClaudeLabGenerator("claude-sonnet-5-5", client=fake, runner=local_runner)
    assert run_generation(settings, slug, generator) == "failed"
    assert stages(settings, slug)[-1] == ("repair", "failed")
    assert "in the repair step Claude judged" in runs(settings, slug)[-1]["error"]
    assert "No check can test this." in runs(settings, slug)[-1]["error"]


def test_an_api_error_in_the_write_call_fails_loudly(
    settings: Settings, make_lab, fake_anthropic, local_runner
) -> None:
    import anthropic

    slug = make_lab()
    fake = fake_anthropic(fake_anthropic.status_error(anthropic.RateLimitError, 429))
    generator = ClaudeLabGenerator("claude-sonnet-5-5", client=fake, runner=local_runner)
    assert run_generation(settings, slug, generator) == "failed"
    assert stages(settings, slug) == [("preflight", "succeeded"), ("write", "failed")]
    assert "the write call to Claude failed: the Anthropic API rate limit was reached" in (
        runs(settings, slug)[1]["error"]
    )


def test_a_truncated_reply_fails_loudly(
    settings: Settings, make_lab, fake_anthropic, local_runner
) -> None:
    slug = make_lab()
    fake = fake_anthropic(fake_anthropic.reply(text="{", stop_reason="max_tokens"))
    generator = ClaudeLabGenerator("claude-sonnet-5-5", client=fake, runner=local_runner)
    assert run_generation(settings, slug, generator) == "failed"
    assert f"cut off at max_tokens={LAB_MAX_TOKENS}" in runs(settings, slug)[1]["error"]


# -- preflight ------------------------------------------------------------------------------


def test_preflight_names_every_problem_before_any_call(
    settings: Settings, make_lab, local_runner, forbid_real_messages_api
) -> None:
    slug = make_lab(params={"study_page": "gone"})
    local_runner.available = False
    generator = ClaudeLabGenerator("claude-sonnet-5-5", runner=local_runner)  # no credentials
    assert run_generation(settings, slug, generator) == "failed"
    assert stages(settings, slug) == [("preflight", "failed")]
    error = runs(settings, slug)[0]["error"]
    assert error.startswith("GenerationError: ")
    assert "ANTHROPIC_API_KEY" in error  # no credentials
    assert "the Python runtime cannot run the lab's checks: the Python runtime is not " in error
    assert "`teacher labs setup`" in error
    assert "the study page 'gone' no longer exists" in error
    assert local_runner.calls == []


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ("not_ready", "the study page 'study-paper' is failed, not ready"),
        ("no_outline", "the study page 'study-paper' has no outline (generation/analyse.json)"),
        ("bad_outline", "has no outline in its 'output' field"),
        ("no_param", "a lab needs params['study_page'], the slug of its study page; got None"),
        ("a_lab", "artifact 'other-lab' is a lab, not a study page"),
    ],
)
def test_preflight_checks_the_study_page(
    settings: Settings, make_lab, client_for, local_runner, change: str, message: str
) -> None:
    outline_file = settings.artifacts_dir / "study-paper" / "generation" / "analyse.json"
    params: dict[str, Any] = {"study_page": "study-paper"}
    if change == "not_ready":
        with session_scope(settings) as session:
            manager = ArtifactManager(session, settings)
            manager.mark_failed(manager.get_by_slug("study-paper"))
    elif change == "no_outline":
        outline_file.unlink()
    elif change == "bad_outline":
        outline_file.write_text("{}", encoding="utf-8")
    elif change == "no_param":
        params = {}
    elif change == "a_lab":
        make_lab("other-lab")
        params = {"study_page": "other-lab"}
    slug = make_lab(params=params)
    fake = client_for()
    generator = ClaudeLabGenerator("claude-sonnet-5-5", client=fake, runner=local_runner)
    assert run_generation(settings, slug, generator) == "failed"
    assert stages(settings, slug) == [("preflight", "failed")]
    assert message in runs(settings, slug)[0]["error"]
    assert fake.calls == []


def test_constructor_rules(local_runner) -> None:
    with pytest.raises(ValueError, match="model must be a model name"):
        ClaudeLabGenerator(" ", runner=local_runner)
    with pytest.raises(TypeError, match="runner must have check_available"):
        ClaudeLabGenerator("m", runner=object())  # type: ignore[arg-type]
    generator = ClaudeLabGenerator("m", runner=local_runner)
    assert generator.name == "claude-lab-v1" and generator._client is None
    assert generator.model_for({}) == "m"
    assert generator.model_for({"model": " other "}) == "other"
    with pytest.raises(GenerationError, match="the model param must be a model name"):
        generator.model_for({"model": 3})


# -- the stub -------------------------------------------------------------------------------


def test_stub_with_a_runner_verifies(settings: Settings, make_lab, local_runner) -> None:
    slug = make_lab()
    assert run_generation(settings, slug, StubLabGenerator(runner=local_runner)) == "ready"
    assert stages(settings, slug) == [
        ("preflight", "succeeded"), ("write", "succeeded"), ("verify", "succeeded")
    ]
    assert {r["model"] for r in runs(settings, slug)} == {"none"}
    assert read_lab(lab_dir(settings, slug), slug=slug) == STUB
    assert len(local_runner.calls) == 2


def test_stub_without_a_runner_skips_verification(settings: Settings, make_lab) -> None:
    slug = make_lab()
    generator = StubLabGenerator()
    assert generator.name == "stub-lab-v1" and generator.runner is None
    assert run_generation(settings, slug, generator) == "ready"
    assert stages(settings, slug) == [("preflight", "succeeded"), ("write", "succeeded")]
    assert artifact_row(settings, slug).title == STUB.title


def test_stub_needs_the_study_page_param(settings: Settings, make_lab) -> None:
    slug = make_lab(params={})
    assert run_generation(settings, slug, StubLabGenerator()) == "failed"
    assert stages(settings, slug) == [("preflight", "failed")]
    assert "params['study_page']" in runs(settings, slug)[0]["error"]


def test_stub_with_an_unavailable_runner_fails_in_preflight(
    settings: Settings, make_lab, local_runner
) -> None:
    slug = make_lab()
    local_runner.available = False
    assert run_generation(settings, slug, StubLabGenerator(runner=local_runner)) == "failed"
    assert stages(settings, slug) == [("preflight", "failed")]


# -- run_generation with a generator's own store --------------------------------------------


class _StoreGenerator:
    name = "own-store"
    model = "none"

    def __init__(self, result: Any, *, glossary: bool = False, fail: bool = False) -> None:
        self.result, self.glossary, self.fail, self.stored = result, glossary, fail, []

    def generate(self, ctx: GenerationContext) -> Any:
        with ctx.stage("write"):
            if self.glossary:
                ctx.set_glossary([GlossaryEntry(term="x", kind="term", definition="d")])
        return self.result

    def store(self, ctx: GenerationContext, result: Any) -> None:
        if self.fail:
            raise RuntimeError("the store broke")
        self.stored.append(result)
        with ctx.session() as session:
            manager = ArtifactManager(session, ctx.settings)
            manager.mark_ready(manager.get_by_slug(ctx.artifact_slug))


def test_run_generation_calls_a_generators_own_store(settings: Settings, make_lab) -> None:
    slug = make_lab()
    generator = _StoreGenerator({"anything": 1})
    assert run_generation(settings, slug, generator) == "ready"
    assert generator.stored == [{"anything": 1}]  # no block validation, no blocks
    assert stages(settings, slug) == [("write", "succeeded")]


@pytest.mark.parametrize(
    ("generator", "error"),
    [
        (_StoreGenerator(1, fail=True), "RuntimeError: the store broke"),
        (_StoreGenerator(1, glossary=True), "stores its own result but set a glossary"),
    ],
)
def test_a_failing_own_store_is_recorded_as_a_store_run(
    settings: Settings, make_lab, generator: _StoreGenerator, error: str
) -> None:
    slug = make_lab()
    assert run_generation(settings, slug, generator) == "failed"
    assert stages(settings, slug) == [("write", "succeeded"), ("store", "failed")]
    assert error in runs(settings, slug)[-1]["error"]
    assert artifact_row(settings, slug).status == "failed"


def test_store_lab_rejects_a_foreign_result(settings: Settings, make_lab) -> None:
    class Wrong(StubLabGenerator):
        def generate(self, ctx: GenerationContext) -> Any:
            return stub_lab("another-page")

    slug = make_lab()
    assert run_generation(settings, slug, Wrong()) == "failed"
    assert "the lab names study page 'another-page'" in runs(settings, slug)[-1]["error"]
    assert not (lab_dir(settings, slug) / LAB_FILE).exists()

    class NotASpec(StubLabGenerator):
        def generate(self, ctx: GenerationContext) -> Any:
            return []

    other = make_lab("lab-2")
    assert run_generation(settings, other, NotASpec()) == "failed"
    assert "must return a LabSpec, not a list" in runs(settings, other)[-1]["error"]


# -- the app --------------------------------------------------------------------------------


def test_default_app_uses_the_claude_lab_generator(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, local_runner
) -> None:
    from teacher.pyodide_runtime import PythonRunner
    from teacher.web.app import create_app

    monkeypatch.setenv("TEACHER_GENERATION_MODEL", "claude-test-writer")
    app = create_app(settings, generator=StubGenerator())
    generator = app.state.lab_generator
    assert isinstance(generator, ClaudeLabGenerator)
    assert generator.name == "claude-lab-v1" and generator.model == "claude-test-writer"
    assert isinstance(generator.runner, PythonRunner) and generator.runner.settings == settings
    assert app.state.lab_runner is generator.runner
    assert generator._client is None  # nothing is created until a lab is generated

    app = create_app(settings, generator=StubGenerator(), lab_runner=local_runner)
    assert app.state.lab_generator.runner is local_runner


def test_a_claude_lab_through_the_api(
    settings: Settings, study_page: str, client_for, local_runner
) -> None:
    from fastapi.testclient import TestClient

    from teacher.web.app import create_app

    app = create_app(settings, generator=StubGenerator(), lab_runner=local_runner)
    fake = client_for(vacuous(), wire())
    app.state.lab_generator._client = fake
    with TestClient(app) as client:
        created = client.post(f"/api/artifacts/{study_page}/labs", json={"model": "claude-opus-5-5"})
        assert created.status_code == 202, created.text
        assert created.json()["generator"] == "claude-lab-v1"
        assert created.json()["params"] == {"study_page": study_page, "model": "claude-opus-5-5"}
        slug = created.json()["slug"]
        assert client.get(f"/api/artifacts/{slug}").json()["status"] == "ready"
        api_runs = client.get(f"/api/artifacts/{slug}/runs").json()
        lab = client.get(f"/api/labs/{slug}").json()
    assert [(r["stage"], r["status"]) for r in api_runs] == [
        ("preflight", "succeeded"), ("write", "succeeded"), ("verify", "failed"),
        ("repair", "succeeded"), ("verify", "succeeded"),
    ]
    assert {r["model"] for r in api_runs} == {"claude-opus-5-5"}
    assert [call["model"] for call in fake.calls] == ["claude-opus-5-5"] * 2
    assert lab["title"] == STUB.title and lab["study_page"]["slug"] == study_page
