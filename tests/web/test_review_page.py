"""The review page (/review), its entry point in the header, the app's review settings, and the
page model's node test (tests/web/js/review_test.mjs)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from teacher.config import Settings
from teacher.generation import StubGenerator
from teacher.review_bank import ClaudeReviewBankBuilder, StubReviewBankBuilder
from teacher.web.app import create_app

JS_TESTS = Path(__file__).parent / "js"
STATIC_JS = Path(__file__).parents[2] / "src" / "teacher" / "web" / "static" / "js"


def test_the_review_page_renders(client: TestClient) -> None:
    r = client.get("/review")
    assert r.status_code == 200
    html = r.text
    assert 'id="review-page"' in html and "js/review.js" in html and "css/review.css" in html
    assert 'href="/review" aria-current="page"' in html
    assert "data-concept" not in html


def test_the_review_page_for_one_concept(client: TestClient, settings: Settings) -> None:
    from teacher.db.engine import session_scope
    from teacher.managers import ConceptManager

    with session_scope(settings) as session:
        ConceptManager(session, settings).create("soft max", "Weights.", "user")
    r = client.get("/review", params={"concept": "soft-max"})
    assert r.status_code == 200
    assert 'data-concept="soft-max"' in r.text and "<strong>soft max</strong>" in r.text
    missing = client.get("/review", params={"concept": "nope"})
    assert missing.status_code == 404 and "There is no concept called" in missing.text


def test_every_page_links_to_the_review_with_its_due_count(client: TestClient) -> None:
    html = client.get("/library").text
    assert '<a href="/review">Review<span id="nav-review-due"' in html
    assert "js/review_count.js" in html


def test_the_bank_builder_comes_with_the_default_generator_only(settings: Settings) -> None:
    assert create_app(settings).state.review_bank_builder.__class__ is ClaudeReviewBankBuilder
    offline = create_app(settings, generator=StubGenerator())
    assert offline.state.review_bank_builder is None
    stub = StubReviewBankBuilder()
    assert create_app(settings, generator=StubGenerator(), review_bank_builder=stub).state.review_bank_builder is stub
    assert callable(offline.state.clock)
    with pytest.raises(TypeError, match="clock"):
        create_app(settings, generator=StubGenerator(), clock="now")


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_review_page_model_under_node() -> None:
    result = subprocess.run(
        ["node", str(JS_TESTS / "review_test.mjs")], capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "review ok" in result.stdout
    for name in ("review.js", "review_count.js", "review_model.js"):
        check = subprocess.run(
            ["node", "--check", str(STATIC_JS / name)], capture_output=True, text=True, timeout=60, check=False
        )
        assert check.returncode == 0, check.stderr
