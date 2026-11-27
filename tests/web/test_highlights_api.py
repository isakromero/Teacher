"""Highlights and notes on a study page (`teacher.web.routers.highlights`): the CRUD routes, the
per-account scope, the offline bundle's `highlights` and the upload of changes made offline."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from teacher.blocks import is_gate_type, validate_block_content
from teacher.config import Settings
from teacher.db.engine import session_scope
from teacher.db.models import Highlight, utcnow
from teacher.generation import StubGenerator
from teacher.lab_generator import StubLabGenerator
from teacher.managers import (
    ArtifactBlockManager,
    ArtifactManager,
    KnowledgeSourceManager,
)
from teacher.web.app import create_app
from teacher.web.routers import highlights as highlights_router
from teacher.web.routers import offline as offline_router

CODE = "the-access-code"
PASSWORD = "correct horse battery"

PAGE: list[tuple[str, dict[str, Any]]] = [
    ("prose", {"markdown": "# Intro\n\nOpening text about light."}),
    (
        "multiple_choice",
        {
            "question": "Which letter?",
            "options": ["alpha", "beta"],
            "private": {"correct_index": 1, "explanation": "Beta is right."},
        },
    ),
    ("prose", {"markdown": "Locked prose three."}),
]


class Clock:
    def __init__(self) -> None:
        self.now = utcnow().replace(microsecond=0)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def app(settings: Settings, clock: Clock) -> FastAPI:
    return create_app(
        settings, generator=StubGenerator(), lab_generator=StubLabGenerator(), clock=clock
    )


MakeArtifact = Callable[..., str]


@pytest.fixture
def make_artifact(settings: Settings, tmp_path: Path) -> MakeArtifact:
    counter = iter(range(1, 1000))

    def make(slug: str = "study", *, kind: str = "study_page", page=PAGE) -> str:
        n = next(counter)
        src = tmp_path / f"source-{n}.txt"
        src.write_text(f"Source text number {n}.\n")
        with session_scope(settings) as session:
            source = KnowledgeSourceManager(session, settings).add(
                src, slug=f"src-{n}", kind="note", title=f"Source {n}"
            )
            artifacts = ArtifactManager(session, settings)
            artifact = artifacts.create(
                slug=slug, kind=kind, title="Study", generator="test", sources=[source]
            )
            blocks = ArtifactBlockManager(session, settings)
            for type_, content in page:
                blocks.add(
                    artifact,
                    type=type_,
                    content=validate_block_content(type_, content),
                    is_gate=is_gate_type(type_),
                )
            artifacts.set_status(artifact, "ready")
        return slug

    return make


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def study(make_artifact: MakeArtifact, client: TestClient) -> str:
    return make_artifact()


def url(slug: str, hid: str | None = None) -> str:
    base = f"/api/artifacts/{slug}/highlights"
    return base if hid is None else f"{base}/{hid}"


def upload_url(slug: str) -> str:
    return f"/api/offline/artifacts/{slug}/highlights"


def anchor(**overrides: Any) -> dict[str, Any]:
    out = {
        "block_key": "b01",
        "quote": "Opening text",
        "prefix": "Intro ",
        "suffix": " about light.",
        "start": 6,
        "end": 18,
    }
    out.update(overrides)
    return out


def create(client: TestClient, slug: str, **overrides: Any) -> dict[str, Any]:
    r = client.post(url(slug), json=anchor(**overrides))
    assert r.status_code == 201, r.text
    return r.json()


def listed(client: TestClient, slug: str, **kw: Any) -> list[dict[str, Any]]:
    r = client.get(url(slug), **kw)
    assert r.status_code == 200, r.text
    assert r.json()["artifact"] == slug
    return r.json()["highlights"]


def iso(value: datetime) -> str:
    return value.replace(tzinfo=UTC).isoformat()


# -- the routes -----------------------------------------------------------------------------


def test_create_list_edit_and_remove(client: TestClient, study: str, clock: Clock) -> None:
    assert listed(client, study) == []
    made = create(client, study, note="  Remember this.  ")
    assert set(made) == {
        "id", "artifact", "block_key", "block_position", "quote", "prefix", "suffix", "start",
        "end", "note", "created_at", "updated_at",
    }
    assert str(uuid.UUID(made["id"])) == made["id"]
    assert made["artifact"] == study and made["block_key"] == "b01" and made["block_position"] == 1
    assert (made["quote"], made["prefix"], made["suffix"]) == (
        "Opening text", "Intro ", " about light."
    )
    assert (made["start"], made["end"]) == (6, 18)
    assert made["note"] == "Remember this."  # stripped
    assert made["created_at"] == made["updated_at"] == iso(clock.now)
    assert listed(client, study) == [made]

    clock.advance(minutes=3)
    r = client.patch(url(study, made["id"]), json={"note": "Changed."})
    assert r.status_code == 200, r.text
    edited = r.json()
    assert edited["note"] == "Changed." and edited["updated_at"] == iso(clock.now)
    assert edited["created_at"] == made["created_at"]
    r = client.patch(url(study, made["id"]), json={"note": "   "})
    assert r.status_code == 200 and r.json()["note"] is None  # a blank note is no note
    r = client.patch(url(study, made["id"]), json={"note": None})
    assert r.status_code == 200 and r.json()["note"] is None
    assert listed(client, study)[0]["note"] is None

    r = client.delete(url(study, made["id"]))
    assert (r.status_code, r.content) == (204, b"")
    assert listed(client, study) == []
    assert client.delete(url(study, made["id"])).status_code == 404
    assert client.patch(url(study, made["id"]), json={"note": "x"}).status_code == 404


def test_listed_in_page_order(client: TestClient, study: str, clock: Clock) -> None:
    later = create(client, study, block_key="b03", quote="Locked", prefix="", suffix=" prose",
                   start=0, end=6)
    clock.advance(seconds=1)
    second = create(client, study, quote="text", start=14, end=18)
    clock.advance(seconds=1)
    first = create(client, study)
    clock.advance(seconds=1)
    same_start = create(client, study, quote="Opening", end=13)
    ids = [h["id"] for h in listed(client, study)]
    assert ids == [first["id"], same_start["id"], second["id"], later["id"]]
    assert listed(client, study)[-1]["block_position"] == 3


def test_a_client_id_is_kept_and_cannot_be_reused(client: TestClient, study: str) -> None:
    hid = str(uuid.uuid4()).upper()
    made = create(client, study, id=hid)
    assert made["id"] == hid.lower()
    r = client.post(url(study), json=anchor(id=hid))
    assert r.status_code == 409 and "exists already" in r.json()["detail"]
    client.delete(url(study, made["id"]))
    assert client.post(url(study), json=anchor(id=hid)).status_code == 409  # never reused


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (anchor(block_key="b99"), "has no block 'b99'"),
        (anchor(quote="   "), "not blank"),
        (anchor(quote="x" * 4001), "at most 4000"),
        (anchor(prefix="p" * 65), "prefix is 65 characters"),
        (anchor(suffix="s" * 65), "suffix is 65 characters"),
        (anchor(start=5, end=5), "0 <= start < end"),
        (anchor(start=-1), "0 <= start < end"),
        (anchor(start=True), "start"),
        (anchor(end="9"), "end"),
        (anchor(note="n" * 10001), "the limit is 10000"),
        (anchor(quote="half \ud835"), "lone surrogate"),
        (anchor(color="yellow"), "color"),
        (anchor(id="not-a-uuid"), "id"),
        ({k: v for k, v in anchor().items() if k != "quote"}, "quote"),
    ],
)
def test_create_refuses_a_bad_body(
    client: TestClient, study: str, body: dict[str, Any], message: str
) -> None:
    # sent as ASCII JSON, so that a lone surrogate travels as its \\u escape
    r = client.post(
        url(study), content=json.dumps(body), headers={"Content-Type": "application/json"}
    )
    assert r.status_code == 422, r.text
    assert message in r.json()["detail"]
    assert listed(client, study) == []


def test_unknown_pages_ids_and_labs(
    client: TestClient, study: str, make_artifact: MakeArtifact
) -> None:
    assert client.get(url("nope")).status_code == 404
    assert client.post(url("nope"), json=anchor()).status_code == 404
    lab = make_artifact("a-lab", kind="lab", page=[])
    r = client.get(url(lab))
    assert r.status_code == 422 and "lab" in r.json()["detail"]
    assert client.post(url(lab), json=anchor()).status_code == 422
    missing = str(uuid.uuid4())
    assert client.patch(url(study, missing), json={"note": "x"}).status_code == 404
    assert client.delete(url(study, missing)).status_code == 404
    assert client.delete(url(study, "not-a-uuid")).status_code == 422
    assert client.patch(url(study, missing), json={"note": "x", "extra": 1}).status_code == 422
    assert client.patch(url(study, missing), json={}).status_code == 422
    # a highlight is reached only through its own page
    other = make_artifact("other")
    made = create(client, study)
    assert client.patch(url(other, made["id"]), json={"note": "x"}).status_code == 404
    assert client.delete(url(other, made["id"])).status_code == 404
    assert listed(client, other) == []


def test_highlights_go_with_their_page(
    client: TestClient, study: str, settings: Settings
) -> None:
    create(client, study)
    with session_scope(settings) as session:
        artifacts = ArtifactManager(session, settings)
        artifacts.delete(artifacts.get_by_slug(study))
    with session_scope(settings) as session:
        assert list(session.scalars(select(Highlight))) == []


# -- accounts -------------------------------------------------------------------------------


@pytest.fixture
def gated(settings: Settings, clock: Clock) -> Iterator[TestClient]:
    app = create_app(
        settings,
        generator=StubGenerator(),
        lab_generator=StubLabGenerator(),
        clock=clock,
        access_code=CODE,
    )
    with TestClient(app) as test_client:
        yield test_client


def register(client: TestClient, email: str) -> dict[str, str]:
    r = client.post(
        "/api/auth/register", json={"email": email, "password": PASSWORD, "access_code": CODE}
    )
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def test_each_account_has_its_own_highlights(
    gated: TestClient, make_artifact: MakeArtifact
) -> None:
    slug = make_artifact()
    ann = register(gated, "ann@example.com")
    bob = register(gated, "bob@example.com")
    assert gated.get(url(slug)).status_code == 401  # no session
    r = gated.post(url(slug), json=anchor(note="Ann's"), headers=ann)
    assert r.status_code == 201, r.text
    mine = r.json()
    assert [h["id"] for h in listed(gated, slug, headers=ann)] == [mine["id"]]
    assert listed(gated, slug, headers=bob) == []
    assert gated.patch(url(slug, mine["id"]), json={"note": "x"}, headers=bob).status_code == 404
    assert gated.delete(url(slug, mine["id"]), headers=bob).status_code == 404
    assert listed(gated, slug, headers=ann)[0]["note"] == "Ann's"
    # the offline bundle carries the account's own
    bundle = gated.get(f"/api/offline/artifacts/{slug}", headers=ann).json()
    assert [h["id"] for h in bundle["highlights"]] == [mine["id"]]
    assert gated.get(f"/api/offline/artifacts/{slug}", headers=bob).json()["highlights"] == []
    # and Bob cannot take Ann's id offline either
    change = {"op": "update_note", "id": mine["id"], "note": "Bob's",
              "made_at": datetime.now(UTC).isoformat()}
    r = gated.post(upload_url(slug), json={"changes": [change]}, headers=bob)
    assert r.status_code == 200, r.text
    assert r.json()["results"][0]["reason"] == "id_conflict"
    assert listed(gated, slug, headers=ann)[0]["note"] == "Ann's"


# -- the offline bundle ---------------------------------------------------------------------


def test_the_offline_bundle_carries_the_highlights(client: TestClient, study: str) -> None:
    made = create(client, study, note="A note.")
    bundle = client.get(f"/api/offline/artifacts/{study}").json()
    assert bundle["highlights"] == [made]
    attempt = {
        "client_id": str(uuid.uuid4()), "key": "b02", "response": {"choice": 1},
        "verdict": "passed", "grader": "device:test", "made_at": datetime.now(UTC).isoformat(),
    }
    r = client.post(
        f"/api/offline/artifacts/{study}/attempts",
        json={"version": bundle["version"], "attempts": [attempt]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["bundle"]["highlights"] == [made]


# -- uploading changes made offline ---------------------------------------------------------


def at(minutes_ago: float) -> str:
    return (datetime.now(UTC) - timedelta(minutes=minutes_ago)).isoformat()


def create_change(hid: str | None = None, *, made_at: str | None = None, **overrides: Any) -> dict:
    return {
        "op": "create",
        "id": hid or str(uuid.uuid4()),
        **anchor(),
        "note": None,
        "made_at": made_at or at(30),
        **overrides,
    }


def upload(client: TestClient, slug: str, changes: list[dict[str, Any]]) -> dict[str, Any]:
    r = client.post(upload_url(slug), json={"changes": changes})
    assert r.status_code == 200, r.text
    return r.json()


def test_limits_agree_with_the_attempts_upload() -> None:
    assert highlights_router.MAX_CLOCK_SKEW == offline_router.MAX_CLOCK_SKEW
    assert highlights_router.MAX_CHANGES == offline_router.MAX_UPLOAD_ITEMS


def test_upload_creates_edits_and_removes(client: TestClient, study: str) -> None:
    hid = str(uuid.uuid4())
    made_at = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=30)
    out = upload(
        client,
        study,
        [
            create_change(hid, made_at=made_at.isoformat(), note="First."),
            {"op": "update_note", "id": hid, "note": "Second.", "made_at": at(20)},
        ],
    )
    assert set(out) == {
        "artifact", "applied", "unchanged", "stale", "deleted", "rejected", "results", "highlights",
    }
    assert (out["applied"], out["unchanged"], out["stale"], out["deleted"], out["rejected"]) == (
        2, 0, 0, 0, 0
    )
    first, second = out["results"]
    assert set(first) == {"id", "op", "status", "reason", "detail", "highlight"}
    assert (first["id"], first["op"], first["status"], first["reason"], first["detail"]) == (
        hid, "create", "applied", None, None
    )
    assert first["highlight"]["created_at"] == made_at.isoformat()  # when it was made
    assert second["highlight"]["note"] == "Second."
    assert out["highlights"] == listed(client, study) == [second["highlight"]]

    # sent again (the reply was lost): nothing changes
    again = upload(
        client,
        study,
        [
            create_change(hid, made_at=made_at.isoformat(), note="First."),
            {"op": "update_note", "id": hid, "note": "Second.", "made_at": at(20)},
        ],
    )
    assert [r["status"] for r in again["results"]] == ["stale", "unchanged"]
    assert listed(client, study)[0]["note"] == "Second."

    # removed offline: gone, and a late create or edit does not bring it back
    gone = upload(client, study, [{"op": "delete", "id": hid, "made_at": at(10)}])
    assert gone["results"][0]["status"] == "applied" and gone["results"][0]["highlight"] is None
    assert gone["highlights"] == []
    late = upload(
        client,
        study,
        [
            create_change(hid, made_at=made_at.isoformat(), note="First."),
            {"op": "update_note", "id": hid, "note": "Third.", "made_at": at(1)},
            {"op": "delete", "id": hid, "made_at": at(1)},
        ],
    )
    assert [r["status"] for r in late["results"]] == ["deleted", "deleted", "unchanged"]
    assert (late["deleted"], late["unchanged"]) == (2, 1)
    assert listed(client, study) == []


def test_last_writer_wins_for_notes(client: TestClient, study: str, clock: Clock) -> None:
    made = create(client, study, note="Web.")  # at the server's clock
    old = (clock.now - timedelta(minutes=10)).replace(tzinfo=UTC).isoformat()
    new = (clock.now + timedelta(minutes=1)).replace(tzinfo=UTC).isoformat()
    out = upload(
        client,
        study,
        [
            {"op": "update_note", "id": made["id"], "note": "Older phone edit.", "made_at": old},
            {"op": "update_note", "id": made["id"], "note": "Newer phone edit.", "made_at": new},
        ],
    )
    assert [r["status"] for r in out["results"]] == ["stale", "applied"]
    assert out["results"][0]["highlight"]["note"] == "Web."
    assert listed(client, study)[0]["note"] == "Newer phone edit."
    assert listed(client, study)[0]["updated_at"] == new
    # a create sent with a newer note than the server's applies it too
    later = (clock.now + timedelta(minutes=2)).replace(tzinfo=UTC).isoformat()
    again = upload(
        client,
        study,
        [{**create_change(made["id"], made_at=later), "note": "From the create."}],
    )
    assert again["results"][0]["status"] == "applied"
    assert listed(client, study)[0]["note"] == "From the create."
    # a delete wins whatever the times
    gone = upload(client, study, [{"op": "delete", "id": made["id"], "made_at": old}])
    assert gone["results"][0]["status"] == "applied" and listed(client, study) == []


def test_upload_rejects_what_cannot_apply(
    client: TestClient, study: str, make_artifact: MakeArtifact, clock: Clock
) -> None:
    other = make_artifact("other")
    elsewhere = create(client, other)
    here = create(client, study)
    future = (clock.now + timedelta(minutes=6)).replace(tzinfo=UTC).isoformat()
    changes = [
        create_change(made_at=future),
        create_change(elsewhere["id"]),
        create_change(here["id"], quote="Opening"),
        create_change(block_key="b99"),
        create_change(quote=" "),
        create_change(start=4, end=2),
        {"op": "update_note", "id": str(uuid.uuid4()), "note": "x", "made_at": at(1)},
        {"op": "delete", "id": str(uuid.uuid4()), "made_at": at(1)},
        {"op": "update_note", "id": here["id"], "note": "n" * 10001, "made_at": at(1)},
        {"op": "delete", "id": elsewhere["id"], "made_at": at(1)},
    ]
    out = upload(client, study, changes)
    assert [r["reason"] for r in out["results"]] == [
        "made_at_in_future",
        "id_conflict",
        "id_conflict",
        "unknown_block",
        "invalid_anchor",
        "invalid_anchor",
        "unknown_highlight",
        "unknown_highlight",
        "invalid_note",
        "id_conflict",
    ]
    for result in out["results"]:
        assert result["status"] == "rejected" and result["highlight"] is None
        assert result["detail"]
    assert out["rejected"] == len(changes) and out["applied"] == 0
    assert listed(client, study) == [here] and listed(client, other) == [elsewhere]


@pytest.mark.parametrize(
    "body",
    [
        {"changes": []},
        {"changes": [create_change()] * 501},
        {"changes": [{**create_change(), "made_at": "2026-11-25T10:00:00"}]},  # no offset
        {"changes": [{**create_change(), "op": "move"}]},
        {"changes": [{**create_change(), "colour": "red"}]},
        {"changes": [{"op": "delete", "id": "nope", "made_at": at(1)}]},
        {"changes": [{"op": "update_note", "id": str(uuid.uuid4()), "made_at": at(1)}]},
        {"changes": [create_change()], "version": "x"},
    ],
)
def test_upload_refuses_a_malformed_request(
    client: TestClient, study: str, body: dict[str, Any]
) -> None:
    good = create_change()
    r = client.post(upload_url(study), json={"changes": [good, *body["changes"]], **{
        k: v for k, v in body.items() if k != "changes"
    }} if body["changes"] else body)
    assert r.status_code == 422, r.text
    assert listed(client, study) == []  # nothing applied, not even the good change


def test_upload_unknown_page_and_lab(client: TestClient, make_artifact: MakeArtifact) -> None:
    assert client.post(upload_url("nope"), json={"changes": [create_change()]}).status_code == 404
    lab = make_artifact("a-lab", kind="lab", page=[])
    assert client.post(upload_url(lab), json={"changes": [create_change()]}).status_code == 422
