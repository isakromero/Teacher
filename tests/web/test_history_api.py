"""Study history API (`/api/history/...`): heartbeats and page events with their errors, the
events the other endpoints log, the sessions and their timelines, per-artifact totals and the
export (JSON and Markdown, chat text included)."""

from __future__ import annotations

import io
import zipfile
from collections.abc import Callable
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from teacher.concept_relating import stub_relater
from teacher.config import Settings
from teacher.db.engine import session_scope
from teacher.db.models import AttentionSpan, StudyEvent, StudySession
from teacher.generation import StubGenerator
from teacher.glossary import GlossaryEntry
from teacher.grading import MultipleChoiceGrader, WordCountBlurbGrader
from teacher.lab_generator import StubLabGenerator
from teacher.managers import ArtifactManager, ConceptManager, GlossaryManager
from teacher.tutor import Tutor
from teacher.web.app import create_app

Json = dict[str, Any]


@pytest.fixture
def fake(fake_anthropic):
    return fake_anthropic()


@pytest.fixture
def app(settings: Settings, fake) -> FastAPI:
    """Offline: stub generators and relater, offline graders, the tutor on a fake client."""
    return create_app(
        settings,
        generator=StubGenerator(),
        lab_generator=StubLabGenerator(),
        concept_relater=stub_relater(),
        graders={"multiple_choice": MultipleChoiceGrader(), "blurb": WordCountBlurbGrader()},
        tutor=Tutor(fake),
    )


@pytest.fixture
def page(client: TestClient, upload_source: Callable[..., Json]) -> Json:
    """A ready study page, `study-deep-learning` (b01 prose, b02 a multiple choice gate)."""
    upload_source(title="Deep Learning", slug="deep-learning")
    created = client.post("/api/artifacts", json={"sources": ["deep-learning"]})
    assert created.status_code == 202, created.text
    return client.get(f"/api/artifacts/{created.json()['slug']}").json()


@pytest.fixture
def lab(client: TestClient, page: Json) -> Json:
    response = client.post(f"/api/artifacts/{page['slug']}/labs")
    assert response.status_code == 202, response.text
    return client.get(f"/api/artifacts/{response.json()['slug']}").json()


def events(settings: Settings) -> list[tuple[str, dict[str, Any]]]:
    with session_scope(settings) as session:
        return [(e.kind, e.payload) for e in session.scalars(select(StudyEvent).order_by(StudyEvent.id))]


def kinds(settings: Settings) -> list[str]:
    return [kind for kind, _payload in events(settings)]


def heartbeat(client: TestClient, **body: Any):
    return client.post("/api/history/heartbeat", json=body)


def post_event(client: TestClient, **body: Any):
    return client.post("/api/history/events", json=body)


def wrong_answer(client: TestClient, slug: str) -> Json:
    response = client.post(f"/api/artifacts/{slug}/blocks/b02/responses", json={"response": {"choice": 1}})
    assert response.status_code == 201, response.text
    return response.json()


# -- heartbeats -------------------------------------------------------------------------------


def test_heartbeats_extend_a_span(client: TestClient, page: Json, settings: Settings) -> None:
    first = heartbeat(client, surface="study_page", slug=page["slug"], block="b01")
    assert first.status_code == 200, first.text
    body = first.json()
    assert set(body) == {"id", "session_id", "surface", "block", "started_at", "last_seen_at",
                         "active_seconds", "heartbeats"}
    assert (body["surface"], body["block"], body["heartbeats"]) == ("study_page", "b01", 1)
    second = heartbeat(client, surface="study_page", slug=page["slug"], block="b01").json()
    assert second["id"] == body["id"] and second["heartbeats"] == 2
    assert second["active_seconds"] > 0
    moved = heartbeat(client, surface="study_page", slug=page["slug"], block="b02").json()
    assert moved["id"] != body["id"] and moved["block"] == "b02"
    assert heartbeat(client, surface="graph").json()["surface"] == "graph"
    assert heartbeat(client, surface="source", slug="deep-learning").status_code == 200


def test_a_resumed_heartbeat_starts_a_span_with_nothing_credited(
    client: TestClient, page: Json
) -> None:
    first = heartbeat(client, surface="study_page", slug=page["slug"], block="b01").json()
    back = heartbeat(client, surface="study_page", slug=page["slug"], block="b01", resumed=True)
    assert back.status_code == 200, back.text
    assert back.json()["id"] != first["id"] and back.json()["active_seconds"] == 0
    refused = heartbeat(client, surface="graph", resumed="later")
    assert refused.status_code == 422, refused.text


def test_heartbeat_on_a_lab(client: TestClient, lab: Json, page: Json, settings: Settings) -> None:
    assert heartbeat(client, surface="lab", slug=lab["slug"]).status_code == 200
    with session_scope(settings) as session:
        span = session.scalars(select(AttentionSpan)).one()
        lab_row = ArtifactManager(session, settings).get_by_slug(lab["slug"])
        page_row = ArtifactManager(session, settings).get_by_slug(page["slug"])
        assert (span.lab_id, span.artifact_id) == (lab_row.id, page_row.id)


@pytest.mark.parametrize(
    ("body", "status", "detail"),
    [
        ({"surface": "study_page", "slug": "nope"}, 404, "no artifact with slug 'nope'"),
        ({"surface": "study_page", "slug": "study-deep-learning", "block": "b99"}, 404, "b99"),
        ({"surface": "study_page"}, 422, "a study_page heartbeat needs the slug of the page"),
        ({"surface": "graph", "slug": "x"}, 422, "a graph heartbeat takes no slug or block"),
        ({"surface": "source", "slug": "deep-learning", "block": "b01"}, 422, "takes no block"),
        ({"surface": "source", "slug": "nope"}, 404, "nope"),
        ({"surface": "lab", "slug": "study-deep-learning"}, 422, "not a lab"),
        ({"surface": "reading"}, 422, "surface"),
        ({"surface": "graph", "extra": 1}, 422, "extra"),
    ],
)
def test_heartbeat_errors(
    client: TestClient, page: Json, settings: Settings, body: Json, status: int, detail: str
) -> None:
    response = heartbeat(client, **body)
    assert response.status_code == status, response.text
    assert detail in response.json()["detail"]
    with session_scope(settings) as session:
        assert session.scalars(select(AttentionSpan)).all() == []


def test_a_lab_as_a_study_page_heartbeat_is_refused(client: TestClient, lab: Json) -> None:
    response = heartbeat(client, surface="study_page", slug=lab["slug"])
    assert response.status_code == 422 and "send a lab heartbeat" in response.json()["detail"]


# -- events from the page -----------------------------------------------------------------------


def test_page_events(client: TestClient, page: Json, lab: Json, settings: Settings) -> None:
    with session_scope(settings) as session:
        artifact = ArtifactManager(session, settings).get_by_slug(page["slug"])
        GlossaryManager(session, settings).replace(
            artifact, [GlossaryEntry(term="Deep Learning", kind="term", definition="Many layers.")]
        )
        ConceptManager(session, settings).create("Attention", "Weighting.", "paper", slug="attention")
    before = len(kinds(settings))
    for body in (
        {"kind": "glossary_opened", "artifact": page["slug"]},
        {"kind": "glossary_term_viewed", "artifact": page["slug"], "payload": {"term": "Deep Learning"}},
        {"kind": "lab_hint_viewed", "lab": lab["slug"], "payload": {"step": 1}},
        {"kind": "concept_inspected", "concept": "attention"},
        {"kind": "source_file_opened", "source": "deep-learning"},
    ):
        response = post_event(client, **body)
        assert response.status_code == 201, response.text
        assert set(response.json()) == {"id", "session_id", "kind", "occurred_at"}
        assert response.json()["kind"] == body["kind"]
    assert kinds(settings)[before:] == [
        "glossary_opened", "glossary_term_viewed", "lab_hint_viewed", "concept_inspected",
        "source_file_opened",
    ]


@pytest.mark.parametrize(
    ("body", "status", "detail"),
    [
        ({"kind": "bogus"}, 422, "unknown study event kind 'bogus'"),
        ({"kind": "gate_cleared", "artifact": "study-deep-learning"}, 422, "logged by the server"),
        ({"kind": "glossary_opened"}, 422, "glossary_opened: needs artifact"),
        ({"kind": "glossary_opened", "artifact": "nope"}, 404, "nope"),
        ({"kind": "glossary_opened", "artifact": "study-deep-learning", "payload": {"x": 1}}, 422,
         "unexpected x"),
        ({"kind": "glossary_term_viewed", "artifact": "study-deep-learning",
          "payload": {"term": "Unknown"}}, 404, "no glossary term 'Unknown'"),
        ({"kind": "concept_inspected", "concept": "nope"}, 404, "nope"),
        ({"kind": "concept_inspected", "block": "b01"}, 422, "give the artifact too"),
        ({"kind": "glossary_opened", "artifact": "study-deep-learning", "lab": "x"}, 404, "'x'"),
        ({"kind": "glossary_opened", "nonsense": 1}, 422, "nonsense"),
    ],
)
def test_page_event_errors(
    client: TestClient, page: Json, settings: Settings, body: Json, status: int, detail: str
) -> None:
    before = kinds(settings)
    response = post_event(client, **body)
    assert response.status_code == status, response.text
    assert detail in response.json()["detail"]
    assert kinds(settings) == before  # nothing was logged


# -- events the other endpoints log --------------------------------------------------------------


def test_opening_pages_logs_events(client: TestClient, page: Json, lab: Json, settings: Settings) -> None:
    with session_scope(settings) as session:
        ConceptManager(session, settings).create("Attention", "Weighting.", "paper", slug="attention")
    before = len(events(settings))
    # Home, the add page and the history log nothing; the library logs library_opened.
    for path in ("/", "/add", "/library", "/sources/deep-learning", f"/artifacts/{page['slug']}",
                 f"/labs/{lab['slug']}", "/graph", "/graph?concept=attention", "/history"):
        assert client.get(path).status_code == 200, path
    # a redirect and a 404 log nothing
    assert client.get(f"/artifacts/{lab['slug']}", follow_redirects=False).status_code == 307
    assert client.get("/artifacts/nope").status_code == 404
    assert events(settings)[before:] == [
        ("library_opened", {}), ("source_opened", {}), ("study_page_opened", {}),
        ("lab_opened", {}), ("graph_opened", {}), ("graph_opened", {}),
    ]
    with session_scope(settings) as session:
        rows = session.scalars(select(StudyEvent).order_by(StudyEvent.id)).all()[before:]
        assert rows[-1].concept_id is not None and rows[-2].concept_id is None


def test_answers_and_cleared_gates(client: TestClient, page: Json, settings: Settings) -> None:
    before = len(events(settings))
    first = wrong_answer(client, page["slug"])
    assert first["state"] == "open"
    assert kinds(settings)[before:] == []  # a failed attempt that clears nothing
    second = wrong_answer(client, page["slug"])
    assert second["state"] == "revealed"  # the last attempt allowed (max_attempts 2)
    (cleared,) = events(settings)[before:]
    assert cleared[0] == "gate_cleared"
    assert cleared[1]["state"] == "revealed" and cleared[1]["attempt"] == 2
    assert cleared[1]["unlocked"] == [b["key"] for b in second["unlocked"]]
    passed = client.post(
        f"/api/artifacts/{page['slug']}/blocks/b02/responses", json={"response": {"choice": 0}}
    ).json()
    assert passed["passed"] and len(events(settings)) == before + 1  # already cleared
    with session_scope(settings) as session:
        assert len(session.scalars(select(StudySession.id)).all()) == 1  # all in one session


def test_generation_chat_and_library_events(
    client: TestClient, page: Json, settings: Settings, fake
) -> None:
    assert kinds(settings) == ["source_added", "study_page_requested"]
    assert events(settings)[1][1] == {"sources": ["deep-learning"], "model": None}
    thread = client.post(f"/api/artifacts/{page['slug']}/chat/threads").json()
    fake.outcomes.append(fake.reply(text="Attention weighs values."))
    sent = client.post(
        f"/api/artifacts/{page['slug']}/chat/threads/{thread['id']}/messages", json={"text": "What?"}
    )
    assert sent.status_code == 200
    regenerated = client.post(f"/api/artifacts/{page['slug']}/regenerate").json()
    assert client.patch("/api/sources/deep-learning", json={"notes": "good"}).status_code == 200
    assert client.delete(f"/api/artifacts/{regenerated['slug']}").status_code == 204
    assert events(settings)[2:] == [
        ("chat_thread_started", {"thread_id": thread["id"]}),
        ("regeneration_requested", {"supersedes": page["slug"], "model": None}),
        ("source_updated", {"fields": ["notes"]}),
        ("artifact_deleted", {"slug": regenerated["slug"], "title": regenerated["title"],
                              "kind": "study_page"}),
    ]
    with session_scope(settings) as session:
        deleted = session.scalars(select(StudyEvent).where(StudyEvent.kind == "artifact_deleted")).one()
        assert deleted.artifact_id is None  # SET NULL: the event outlives the page


def test_lab_events(client: TestClient, page: Json, lab: Json, settings: Settings) -> None:
    assert kinds(settings)[-1] == "lab_requested"
    slug = lab["slug"]
    files = [{"path": "attention.py", "content": "x = 1\n"}]
    assert client.put(f"/api/labs/{slug}/workspace", json={"files": files}).status_code == 200
    assert client.get(f"/api/labs/{slug}/solution").status_code == 200
    assert client.get(f"/api/labs/{slug}/solution").status_code == 200
    export = client.get(f"/api/labs/{slug}/export")
    assert export.status_code == 200 and zipfile.ZipFile(io.BytesIO(export.content)).namelist()
    run = client.post(f"/api/labs/{slug}/runs", json={
        "kind": "run", "step": None, "files": files, "passed": True, "output": "ok\n", "error": None,
    })
    assert run.status_code == 201
    assert events(settings)[-4:] == [
        ("lab_workspace_saved", {"files": 1, "chars": 6}),
        ("lab_solution_viewed", {"first_time": True}),
        ("lab_solution_viewed", {"first_time": False}),
        ("lab_exported", {}),
    ]


def test_graph_events(client: TestClient, settings: Settings) -> None:
    with session_scope(settings) as session:
        concepts = ConceptManager(session, settings)
        concepts.create("Attention", "Weighting.", "paper", slug="attention")
        concepts.create("Softmax", "Normalised exponentials.", "paper", slug="softmax")
    assert client.get("/api/graph/search?q=atten").status_code == 200
    relate = client.post("/api/graph/concepts/attention/relate", json={})
    assert relate.status_code == 200, relate.text
    (search, related) = events(settings)
    assert search[0] == "graph_searched" and search[1]["query"] == "atten"
    assert search[1]["results"] == 1 and isinstance(search[1]["semantic"], str)
    assert related == ("relations_requested", {"regenerate": False, "model": related[1]["model"]})
    assert client.post("/api/graph/concepts/nope/relate", json={}).status_code == 404
    assert len(events(settings)) == 2


def test_source_events(client: TestClient, upload_source: Callable[..., Json], settings: Settings) -> None:
    upload_source(title="Notes", slug="notes")
    assert client.delete("/api/sources/notes").status_code == 204
    assert events(settings) == [
        ("source_added", {"via": "upload"}),
        ("source_deleted", {"slug": "notes", "title": "Notes"}),
    ]


def test_a_refused_delete_logs_nothing(client: TestClient, page: Json, settings: Settings) -> None:
    before = kinds(settings)
    assert client.delete("/api/sources/deep-learning").status_code == 409  # a page uses it
    assert kinds(settings) == before


# -- reading the history ------------------------------------------------------------------------


def test_sessions_and_timeline(client: TestClient, page: Json, settings: Settings, fake) -> None:
    client.get(f"/artifacts/{page['slug']}")
    heartbeat(client, surface="study_page", slug=page["slug"], block="b01")
    heartbeat(client, surface="study_page", slug=page["slug"], block="b01")
    wrong_answer(client, page["slug"])
    thread = client.post(f"/api/artifacts/{page['slug']}/chat/threads").json()
    fake.outcomes.append(fake.reply(text="TUTOR-SAYS-HELLO"))
    client.post(f"/api/artifacts/{page['slug']}/chat/threads/{thread['id']}/messages",
                json={"text": "READER-ASKS"})

    listed = client.get("/api/history/sessions").json()
    assert (listed["total"], listed["limit"], listed["offset"]) == (1, 20, 0)
    (s,) = listed["sessions"]
    assert (s["answers"], s["answers_passed"], s["user_messages"], s["tutor_messages"]) == (1, 0, 1, 1)
    assert s["backfilled"] is False and s["active_seconds"] > 0
    assert s["artifacts"] == [{"slug": page["slug"], "title": page["title"], "kind": "study_page"}]
    assert s["sources"] == [{"slug": "deep-learning", "title": "Deep Learning"}]

    detail = client.get(f"/api/history/sessions/{s['id']}").json()
    types = [e["type"] for e in detail["timeline"]]
    assert types.count("span") == 1 and types.count("answer") == 1
    assert types.count("chat_message") == 2
    texts = [e["text"] for e in detail["timeline"] if e["type"] == "chat_message"]
    assert texts == ["READER-ASKS", "TUTOR-SAYS-HELLO"]
    answer = next(e for e in detail["timeline"] if e["type"] == "answer")
    assert answer["passed"] is False and answer["feedback"] and answer["question"]
    assert answer["response"] == {"choice": 1}  # raw, as stored
    assert answer["answer"]["form"] == "choice" and answer["answer"]["choice"] == 1
    assert answer["answer"]["text"] and answer["answer"]["note"] is None
    assert answer["block"] == "b02" and answer["block_label"]
    span = next(e for e in detail["timeline"] if e["type"] == "span")
    assert span["block"] == "b01" and span["block_label"]
    assert [e["at"] for e in detail["timeline"]] == sorted(e["at"] for e in detail["timeline"])

    assert client.get("/api/history/sessions/999").status_code == 404
    assert client.get(f"/api/history/sessions?artifact={page['slug']}").json()["total"] == 1
    assert client.get("/api/history/sessions?source=deep-learning").json()["total"] == 1
    assert client.get("/api/history/sessions?since=2999-01-01T00:00:00Z").json()["total"] == 0
    assert client.get("/api/history/sessions?until=2000-01-01T00:00:00%2B02:00").json()["total"] == 0
    for query, status in (("artifact=nope", 404), ("source=nope", 404), ("limit=0", 422),
                          ("limit=201", 422), ("offset=-1", 422), ("since=yesterday", 422),
                          ("since=2026-01-02T00:00:00&until=2026-01-01T00:00:00", 422)):
        assert client.get(f"/api/history/sessions?{query}").status_code == status, query


def test_artifact_totals(client: TestClient, page: Json, lab: Json, settings: Settings) -> None:
    heartbeat(client, surface="study_page", slug=page["slug"], block="b01")
    heartbeat(client, surface="study_page", slug=page["slug"], block="b01")
    wrong_answer(client, page["slug"])
    totals = client.get(f"/api/history/artifacts/{page['slug']}").json()
    assert totals["active_seconds"] > 0 and totals["block_seconds"][0]["block"] == "b01"
    assert totals["block_seconds"][0]["label"]
    assert (totals["answers"], totals["answers_passed"], totals["pass_rate"]) == (1, 0, 0.0)
    assert totals["sessions"] == 1
    every = client.get("/api/history/artifacts").json()
    assert [t["artifact"]["slug"] for t in every] == [page["slug"], lab["slug"]]
    assert client.get("/api/history/artifacts/nope").status_code == 404


def test_export(client: TestClient, page: Json, settings: Settings, fake) -> None:
    thread = client.post(f"/api/artifacts/{page['slug']}/chat/threads").json()
    fake.outcomes.append(fake.reply(text="TUTOR-EXPORTED"))
    client.post(f"/api/artifacts/{page['slug']}/chat/threads/{thread['id']}/messages",
                json={"text": "READER-EXPORTED"})
    as_json = client.get("/api/history/export")
    assert as_json.status_code == 200
    assert as_json.headers["content-type"].startswith("application/json")
    assert 'attachment; filename="study-history-' in as_json.headers["content-disposition"]
    data = as_json.json()
    assert data["format"] == "teacher-study-history" and len(data["sessions"]) == 1
    assert "READER-EXPORTED" in as_json.text and "TUTOR-EXPORTED" in as_json.text
    as_md = client.get("/api/history/export?format=markdown")
    assert as_md.headers["content-type"].startswith("text/markdown")
    assert ".md\"" in as_md.headers["content-disposition"]
    assert as_md.text.startswith("# Study history")
    assert "  > READER-EXPORTED" in as_md.text and "  > TUTOR-EXPORTED" in as_md.text
    assert client.get("/api/history/export?format=pdf").status_code == 422
