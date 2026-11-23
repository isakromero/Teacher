"""The `figure` block on the study page: its content and image come through the API, and the
renderer's content checks (figure_model.js) hold under node."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from teacher.blocks import validate_block_content
from teacher.config import Settings
from teacher.db.engine import session_scope
from teacher.figures import encode_png
from teacher.managers import ArtifactBlockManager, ArtifactManager

NODE_TEST = Path(__file__).parent / "js" / "figure_test.mjs"

FIGURE = {
    "asset": "figures/figure-2-0123456789.png",
    "label": "Figure 2",
    "source_caption": "Results on the benchmark.",
    "page": 5,
    "width": 4,
    "height": 3,
    "caption": "The **left** bar is the baseline.",
}


@pytest.fixture
def figure_page(settings: Settings, client: TestClient) -> str:
    """A ready study page: an explanation, then a figure whose crop is in its assets folder."""
    with session_scope(settings) as session:
        artifacts = ArtifactManager(session, settings)
        artifact = artifacts.create(
            slug="figures", kind="study_page", title="Figures", generator="test"
        )
        blocks = ArtifactBlockManager(session, settings)
        blocks.add(artifact, type="prose", content={"markdown": "The results, below."})
        blocks.add(artifact, type="figure", content=validate_block_content("figure", FIGURE))
        artifacts.mark_ready(artifact)
        folder = artifacts.dir(artifact) / "assets" / "figures"
    folder.mkdir(parents=True)
    pixels = np.full((3, 4, 3), 255, dtype=np.uint8)
    (folder / "figure-2-0123456789.png").write_bytes(encode_png(pixels))
    return "figures"


def test_figure_block_and_its_image_are_served(client: TestClient, figure_page: str) -> None:
    body = client.get(f"/api/artifacts/{figure_page}/blocks").json()
    blocks = body["blocks"] if isinstance(body, dict) else body
    figure = next(b for b in blocks if b["type"] == "figure")
    assert figure["is_gate"] is False and figure["content"] == FIGURE
    image = client.get(f"/api/artifacts/{figure_page}/assets/{figure['content']['asset']}")
    assert image.status_code == 200
    assert image.headers["content-type"] == "image/png"
    assert image.content.startswith(b"\x89PNG\r\n\x1a\n")


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_figure_model_under_node() -> None:
    result = subprocess.run(
        ["node", str(NODE_TEST)], capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert "figure ok" in result.stdout
