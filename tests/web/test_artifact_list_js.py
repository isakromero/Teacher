"""The pure helpers of the artifact lists (static/js/artifact_list.js), run under node."""

import shutil
import subprocess
from pathlib import Path

import pytest

NODE_TEST = Path(__file__).parent / "js" / "artifact_list_test.mjs"


def test_artifact_list_helpers() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    result = subprocess.run([node, str(NODE_TEST)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
