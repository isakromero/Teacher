"""The study history page and the page side of the history: `/history`, the nav link, the
heartbeat module on every page, the study-time line on a study page, the page scripts that
report events, and the heartbeat and history helpers under node."""

from __future__ import annotations

import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from teacher.config import Settings
from teacher.db.engine import session_scope
from teacher.db.models import StudyEvent
from teacher.web.routers.pages import STATIC_DIR, TEMPLATES_DIR

Json = dict[str, Any]
JS_DIR = STATIC_DIR / "js"
NODE_TEST = Path(__file__).parent / "js" / "history_test.mjs"
SINKS = ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write")


def html_of(client: TestClient, path: str) -> str:
    response = client.get(path)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/html")
    return response.text


def test_history_page(client: TestClient, settings: Settings) -> None:
    html = html_of(client, "/history")
    assert "<title>History - Teacher</title>" in html
    assert 'id="history-page" data-page="history"' in html
    assert 'class="page page-history"' in html
    for element in ("history-filters", "history-subject", "history-from", "history-to",
                    "history-apply", "history-clear", "history-error", "history-totals",
                    "history-count", "history-sessions", "history-more",
                    "history-export-json", "history-export-markdown"):
        assert f'id="{element}"' in html, element
    for label in ("history-subject", "history-from", "history-to"):
        assert f'for="{label}"' in html, label
    assert 'href="/api/history/export?format=json"' in html
    assert 'href="/api/history/export?format=markdown"' in html
    assert "ends after 30 minutes without activity" in html  # from SESSION_IDLE_GAP
    assert '<script type="module" src="/static/js/history.js"></script>' in html
    assert '<link rel="stylesheet" href="/static/css/history.css">' in html
    for path in ("/static/js/history.js", "/static/js/history_model.js", "/static/css/history.css"):
        assert client.get(path).status_code == 200, path
    with session_scope(settings) as session:  # looking at the history is not studying
        assert session.scalars(select(StudyEvent)).all() == []


def test_every_page_links_to_the_history_and_loads_the_heartbeat(
    client: TestClient, upload_source: Callable[..., Json]
) -> None:
    upload_source(title="Paper", slug="paper")
    for path in ("/", "/history", "/graph", "/sources/paper"):
        html = html_of(client, path)
        nav = html.split('<nav class="site-nav"', 1)[1].split("</nav>", 1)[0]
        assert re.search(r'<a href="/history"( aria-current="page")?>History</a>', nav), path
        head = html.split("</head>", 1)[0]
        assert '<script type="module" src="/static/js/attention.js"></script>' in head, path


def test_study_page_has_a_study_time_line(client: TestClient, upload_source: Callable[..., Json]) -> None:
    upload_source(title="Paper", slug="paper")
    slug = client.post("/api/artifacts", json={"sources": ["paper"]}).json()["slug"]
    html = html_of(client, f"/artifacts/{slug}")
    assert re.search(r'<p id="artifact-study-time" class="muted small" hidden></p>', html)


def test_page_scripts_report_to_the_history() -> None:
    attention = (JS_DIR / "attention.js").read_text(encoding="utf-8")
    for fragment in ('"/api/history/heartbeat"', "visibilitychange", "HEARTBEAT_MS",
                     '"#blocks > [data-block-key]"', 'surface: "study_page"', 'surface: "lab"',
                     'surface: "source"', 'surface: "graph"', "/api/history/artifacts/",
                     "artifact-study-time", "/history?artifact=", "pagehide", "keepalive: true",
                     "heartbeat.leave()", "heartbeat.settled()", "SETTLE_MS"):
        assert fragment in attention, fragment
    activity = (JS_DIR / "activity.js").read_text(encoding="utf-8")
    assert '"/api/history/events"' in activity and "showPageError" in activity
    for name, fragment in (
        ("graph.js", 'logEvent("concept_inspected"'),
        ("glossary.js", 'logEvent("glossary_term_viewed"'),
        ("glossary.js", 'logEvent("glossary_opened"'),
        ("lab.js", 'logEvent("lab_hint_viewed"'),
        ("source.js", 'logEvent("source_file_opened"'),
    ):
        assert fragment in (JS_DIR / name).read_text(encoding="utf-8"), (name, fragment)
    history = (JS_DIR / "history.js").read_text(encoding="utf-8")
    for fragment in ("/api/history/sessions", "/api/history/artifacts", "renderMarkdown"):
        assert fragment in history, fragment
    for name in ("attention.js", "activity.js", "history.js", "history_model.js"):
        code = (JS_DIR / name).read_text(encoding="utf-8")
        for sink in SINKS:
            assert sink not in code, f"{name}: {sink}"
    template = (TEMPLATES_DIR / "history.html").read_text(encoding="utf-8")
    assert not re.search(r"\son[a-z]+\s*=", template)


def test_history_page_knows_every_event_kind() -> None:
    """history.js describes every kind (and throws for an unknown one rather than guessing)."""
    from teacher.db.models import STUDY_EVENT_KINDS

    code = (JS_DIR / "history.js").read_text(encoding="utf-8")
    described = set(re.findall(r'case "([a-z_]+)":', code))
    assert set(STUDY_EVENT_KINDS) <= described


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_heartbeat_and_history_helpers_under_node() -> None:
    result = subprocess.run(
        ["node", str(NODE_TEST)], capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert "history ok" in result.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_history_scripts_parse() -> None:
    for name in ("activity.js", "attention.js", "history.js", "history_model.js"):
        result = subprocess.run(
            ["node", "--check", str(JS_DIR / name)], capture_output=True, text=True, timeout=60,
            check=False,
        )
        assert result.returncode == 0, f"{name}: {result.stderr}"
