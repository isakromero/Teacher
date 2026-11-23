"""The gate block scripts (multiple choice, blurb, prediction, the shared gate) and prose roles:
they parse, the pure helpers pass under node, they are served, and the contracts the page
relies on are in the source (the caller's context documented in gate.js, the confidence
labels, the critique label, the prediction's disclosure)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from teacher.web.routers.pages import STATIC_DIR

BLOCKS = STATIC_DIR / "js" / "blocks"
NODE_TEST = Path(__file__).parent / "js" / "gate_test.mjs"
SCRIPTS = ("gate.js", "gate_model.js", "multiple_choice.js", "blurb.js", "prediction.js",
           "prose.js", "index.js")
SINKS = ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write")


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_gate_helpers_under_node() -> None:
    result = subprocess.run(
        ["node", str(NODE_TEST)], capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert "gate ok" in result.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_block_scripts_parse() -> None:
    for name in SCRIPTS:
        result = subprocess.run(
            ["node", "--check", str(BLOCKS / name)], capture_output=True, text=True, timeout=60,
            check=False,
        )
        assert result.returncode == 0, (name, result.stderr)


def test_block_scripts_are_served_and_write_no_html(client: TestClient) -> None:
    for name in SCRIPTS:
        assert client.get(f"/static/js/blocks/{name}").status_code == 200, name
        source = (BLOCKS / name).read_text(encoding="utf-8")
        for sink in SINKS:
            assert sink not in source, (name, sink)


def test_every_gate_type_and_prose_role_has_a_renderer() -> None:
    from teacher.blocks import BLOCK_TYPES, PROSE_ROLES

    index = (BLOCKS / "index.js").read_text(encoding="utf-8")
    for name in BLOCK_TYPES:
        assert f'registerBlock("{name}", ' in index, name
    prose = (BLOCKS / "prose.js").read_text(encoding="utf-8")
    for role in PROSE_ROLES:
        assert f'"{role}"' in prose, role
    assert "Beyond the source: critique and open questions" in prose


def test_the_callers_context_is_documented_and_used() -> None:
    gate = (BLOCKS / "gate.js").read_text(encoding="utf-8")
    for hook in ("ctx.askConfidence", "ctx.submit(block, response, meta)",
                 "ctx.loadResponses(block)", "ctx.onSubmitted(block, result)", "ORDER:"):
        assert hook in gate, hook
    reader = (STATIC_DIR / "js" / "reader.js").read_text(encoding="utf-8")
    assert "askConfidence: true" in reader and "{ response, ...meta }" in reader
    chat = (STATIC_DIR / "js" / "chat.js").read_text(encoding="utf-8")
    assert "askConfidence: false" in chat
    model = (BLOCKS / "gate_model.js").read_text(encoding="utf-8")
    for label in ("Guessing", "Fairly sure", "Certain"):
        assert label in model
    prediction = (BLOCKS / "prediction.js").read_text(encoding="utf-8")
    assert "Check your prediction" in prediction and "neutral: true" in prediction
    assert "asksConfidence" not in prediction  # a guess is never rated
