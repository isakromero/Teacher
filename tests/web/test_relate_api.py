"""`POST /api/graph/concepts/{slug}/relate` with a ConceptRelater on fake clients (no network):
the result, the record, and every error status."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import anthropic
import pytest
from fastapi.testclient import TestClient

from teacher.concept_relating import SLOTS, ConceptRelater, StubEmbedder
from teacher.config import Settings
from teacher.db.engine import session_scope
from teacher.generation import StubGenerator
from teacher.managers import ConceptManager, ConceptRelationManager
from teacher.web.app import create_app

MODEL = "claude-test-relations"
TABLE = {"attention": [1.0], "softmax": [0.6, 0.8], "dot product": [0.5, 0.0, 0.866]}
URL = "/api/graph/concepts/attention/relate"


def proposals(fake: Any, **slots: list[dict]) -> Any:
    body = {slot: slots.get(slot, []) for slot in SLOTS}
    return fake.reply(text=json.dumps(body), model=MODEL, input_tokens=100, output_tokens=10)


def verdicts(fake: Any, *items: tuple[int, str, str]) -> Any:
    body = {"verdicts": [{"candidate": c, "verdict": v, "rationale": r} for c, v, r in items]}
    return fake.reply(text=json.dumps(body), model=MODEL, input_tokens=200, output_tokens=20)


@pytest.fixture
def ids(settings: Settings) -> dict[str, int]:
    with session_scope(settings) as session:
        c = ConceptManager(session, settings)
        made = {x.slug: x.id for x in (c.create(n, f"About {n}.", "paper")
                                        for n in ("attention", "softmax", "dot product"))}
    return made


@pytest.fixture
def make_client(settings: Settings, fake_embedder: type) -> Iterator[Any]:
    clients: list[TestClient] = []

    def make(fake: Any = None, *, relater: Any = None) -> TestClient:
        if relater is None:
            relater = ConceptRelater(fake, embedder=fake_embedder(TABLE), model=MODEL)
        app = create_app(settings, generator=StubGenerator(), concept_relater=relater)
        client = TestClient(app)
        client.__enter__()
        clients.append(client)
        return client

    yield make
    for client in clients:
        client.__exit__(None, None, None)


def test_relate_returns_what_it_did(
    settings: Settings, ids: dict[str, int], make_client, fake_anthropic
) -> None:
    with session_scope(settings) as session:
        c = ConceptManager(session, settings)
        ConceptRelationManager(session, settings).add(
            "builds_on", c.get_by_slug("softmax"), c.get_by_slug("dot-product"), "Needs.", "user"
        )
    fake = fake_anthropic(
        proposals(fake_anthropic, relates_to=[{"name": "quantum chromodynamics", "aliases": [],
                                               "description": "Strong force."}]),
        verdicts(fake_anthropic,
                 (ids["softmax"], "focus_builds_on_candidate", "Attention uses softmax."),
                 (ids["dot-product"], "candidate_builds_on_focus", "Claimed the wrong way.")),
    )
    response = make_client(fake).post(URL)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["concept"]["slug"] == "attention" and data["model"] == MODEL
    assert data["judge_called"] is True and data["stale_embedded"] == 3
    [added] = data["added"]
    assert added["type"] == "builds_on" and added["direction"] == "out"
    assert added["other"] == {"id": ids["softmax"], "slug": "softmax", "name": "softmax",
                              "origin": "paper", "reviewed": False}
    assert added["edge"]["from_id"] == ids["attention"] and added["edge"]["to_id"] == ids["softmax"]
    assert added["edge"]["origin"] == "model" and added["edge"]["reviewed"] is False
    [refused] = data["refused"]
    assert refused["cycle"] == ["attention", "softmax", "dot-product", "attention"]
    assert refused["direction"] == "in" and "cyclic" in refused["reason"]
    assert data["judged_none"] == []
    assert data["unmatched"] == [{"slot": "relates_to", "name": "quantum chromodynamics",
                                  "aliases": [], "description": "Strong force."}]
    assert data["counts"]["judged"] == 2 and data["counts"]["dropped"]["cap"] == 0
    assert data["tokens"] == {"input": 300, "output": 30}
    assert [n["slug"] for n in data["nodes"]] == ["attention", "softmax"]
    assert data["edges"] == [added["edge"]]
    record = settings.derived_dir / "_concept_relations" / "attention.json"
    assert json.loads(record.read_text(encoding="utf-8"))["trigger"] == "web"
    # The concept's details now show the relation.
    detail = make_client(fake_anthropic()).get("/api/graph/concepts/attention").json()
    assert [(r["type"], r["direction"], r["other"]["slug"]) for r in detail["relations"]] == [
        ("builds_on", "out", "softmax")
    ]


def test_unknown_slug_is_404_before_anything_is_spent(make_client, fake_anthropic) -> None:
    fake = fake_anthropic()
    response = make_client(fake).post("/api/graph/concepts/nope/relate")
    assert response.status_code == 404
    assert response.json() == {"detail": "no concept with slug 'nope'"}
    assert fake.calls == []


def test_missing_credentials_are_503(
    ids, make_client, forbid_real_messages_api: None
) -> None:
    relater = ConceptRelater(embedder=StubEmbedder(), model=MODEL)  # no client, no credentials
    response = make_client(relater=relater).post(URL)
    assert response.status_code == 503
    assert response.json()["detail"].startswith("Relations could not be found: ")


@pytest.mark.parametrize(
    ("outcomes", "status", "fragment"),
    [
        (lambda f, ids: [f.status_error(anthropic.InternalServerError, 529, "overloaded_error")],
         503, "the relation proposals call to Claude failed"),
        (lambda f, ids: [f.reply(text='{"builds_on": []}', model=MODEL)], 502,
         "not a valid WireProposals"),
        (lambda f, ids: [proposals(f), verdicts(f, (999, "none", "No."))], 502,
         "which it was not asked about"),
    ],
)
def test_api_and_output_errors(
    ids, make_client, fake_anthropic, outcomes, status: int, fragment: str
) -> None:
    response = make_client(fake_anthropic(*outcomes(fake_anthropic, ids))).post(URL)
    assert response.status_code == status, response.text
    assert fragment in response.json()["detail"]


def test_a_graph_changed_mid_run_is_409(
    settings: Settings, ids, make_client, fake_anthropic
) -> None:
    fake = fake_anthropic(proposals(fake_anthropic),
                          verdicts(fake_anthropic, (ids["softmax"], "relates_to", "Both."),
                                   (ids["dot-product"], "none", "No.")))
    original = fake.stream

    def stream(**kwargs: Any) -> Any:
        if len(fake.calls) == 1:
            with session_scope(settings) as session:
                c = ConceptManager(session, settings)
                c.update(c.get_by_slug("softmax"), summary="Edited.")
        return original(**kwargs)

    fake.stream = stream
    response = make_client(fake).post(URL)
    assert response.status_code == 409
    assert "the concept graph changed" in response.json()["detail"]


def test_a_second_run_for_the_same_concept_is_409(ids, make_client, fake_anthropic) -> None:
    fake = fake_anthropic()
    client = make_client(fake)
    with client.app.state.relating.claim("attention"):
        response = client.post(URL)
    assert response.status_code == 409
    assert "already being found" in response.json()["detail"]
    assert fake.calls == []


def test_the_default_app_has_a_relater(settings: Settings) -> None:
    app = create_app(settings, generator=StubGenerator())
    assert isinstance(app.state.concept_relater, ConceptRelater)


# -- the body: model and regenerate ------------------------------------------------------------


def link(settings: Settings, type: str, a: str, b: str, origin: str = "model",
         reviewed: bool = False) -> int:
    with session_scope(settings) as session:
        c = ConceptManager(session, settings)
        r = ConceptRelationManager(session, settings)
        relation = r.add(type, c.get_by_slug(a), c.get_by_slug(b), "An old reason.", origin)
        if reviewed:
            r.mark_reviewed(relation)
        return relation.id


def test_regenerate_returns_what_it_replaced_removed_and_kept(
    settings: Settings, ids: dict[str, int], make_client, fake_anthropic
) -> None:
    with session_scope(settings) as session:
        ConceptManager(session, settings).create("gamma", "About gamma.", "user")
    old_softmax = link(settings, "relates_to", "attention", "softmax")  # replaced, other type
    old_dot = link(settings, "builds_on", "attention", "dot-product")  # removed: judged none
    kept = link(settings, "relates_to", "attention", "gamma", origin="paper", reviewed=True)
    fake = fake_anthropic(
        proposals(fake_anthropic),
        verdicts(fake_anthropic,
                 (ids["softmax"], "focus_builds_on_candidate", "Attention uses softmax."),
                 (ids["dot-product"], "none", "Not a direct prerequisite.")),
    )
    response = make_client(fake).post(URL, json={"regenerate": True, "model": "claude-opus-5-5"})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["regenerate"] is True and data["model"] == "claude-opus-5-5"
    assert [call["model"] for call in fake.calls] == ["claude-opus-5-5", "claude-opus-5-5"]
    [added] = data["added"]
    assert added["other"]["slug"] == "softmax" and added["type"] == "builds_on"
    assert added["replaces"] == {"relation_id": old_softmax, "type": "relates_to",
                                 "direction": "symmetric", "rationale": "An old reason.",
                                 "type_changed": True, "direction_changed": True}
    assert data["removed"] == [{
        "relation_id": old_dot, "type": "builds_on", "direction": "out",
        "other": {"id": ids["dot-product"], "slug": "dot-product", "name": "dot product",
                  "origin": "paper", "reviewed": False},
        "rationale": "An old reason.", "reason": "none", "detail": "Not a direct prerequisite.",
    }]
    [k] = data["kept"]
    assert (k["relation_id"], k["other"]["slug"], k["origin"], k["because"]) == (
        kept, "gamma", "paper", ["reviewed", "origin paper"])
    assert k["reviewed_at"].endswith("+00:00")
    assert sorted(data["deleted_edge_ids"]) == sorted([old_softmax, old_dot])
    counts = data["counts"]
    assert (counts["existing"], counts["replaced"], counts["removed"], counts["kept"]) == (
        2, 1, 1, 1)
    assert data["edges"] == [added["edge"]]
    record = json.loads((settings.derived_dir / "_concept_relations" / "attention.json")
                        .read_text(encoding="utf-8"))
    assert record["regenerate"] is True and record["model"] == "claude-opus-5-5"
    assert [r["relation_id"] for r in record["output"]["removed"]] == [old_dot]
    detail = make_client(fake_anthropic()).get("/api/graph/concepts/attention").json()
    assert sorted((r["type"], r["other"]["slug"]) for r in detail["relations"]) == [
        ("builds_on", "softmax"), ("relates_to", "gamma")]


@pytest.mark.parametrize("body", [None, {}, {"regenerate": False}, {"model": None}])
def test_no_body_or_defaults_is_an_incremental_run_with_the_default_model(
    settings: Settings, ids, make_client, fake_anthropic, body: Any
) -> None:
    link(settings, "relates_to", "attention", "softmax")
    fake = fake_anthropic(proposals(fake_anthropic),
                          verdicts(fake_anthropic, (ids["dot-product"], "none", "No.")))
    client = make_client(fake)
    response = client.post(URL) if body is None else client.post(URL, json=body)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["regenerate"] is False and data["model"] == MODEL
    assert data["removed"] == [] and data["kept"] == [] and data["deleted_edge_ids"] == []
    assert [d["slug"] for d in data["dropped"] if d["reason"] == "related"] == ["softmax"]
    assert [call["model"] for call in fake.calls] == [MODEL, MODEL]


@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        ({"model": "gpt-5"}, "unknown model 'gpt-5': expected one of claude-fable-5-2, "
                             "claude-opus-5-5, claude-sonnet-5-5, claude-haiku-5-5, " + MODEL),
        ({"regenerate": "yes"}, "regenerate: Input should be a valid boolean"),
        ({"regenerate": 1}, "regenerate: Input should be a valid boolean"),
        ({"model": 5}, "model: Input should be a valid string"),
        ({"force": True}, "force: Extra inputs are not permitted"),
    ],
)
def test_a_bad_body_is_422_before_anything_is_spent(
    ids, make_client, fake_anthropic, body: dict, fragment: str
) -> None:
    fake = fake_anthropic()
    response = make_client(fake).post(URL, json=body)
    assert response.status_code == 422, response.text
    assert fragment in response.json()["detail"]
    assert fake.calls == []


def test_the_guards_hold_with_a_body(ids, make_client, fake_anthropic,
                                     forbid_real_messages_api: None) -> None:
    fake = fake_anthropic()
    client = make_client(fake)
    body = {"regenerate": True, "model": "claude-haiku-5-5"}
    assert client.post("/api/graph/concepts/nope/relate", json=body).status_code == 404
    with client.app.state.relating.claim("attention"):
        assert client.post(URL, json=body).status_code == 409
    assert fake.calls == []
    no_credentials = ConceptRelater(embedder=StubEmbedder(), model=MODEL)
    response = make_client(relater=no_credentials).post(URL, json=body)
    assert response.status_code == 503


def test_relate_models(make_client, fake_anthropic) -> None:
    data = make_client(fake_anthropic()).get("/api/graph/relate-models").json()
    assert data["selectable"] is True and data["default"] == MODEL
    assert [m["id"] for m in data["models"]] == [
        "claude-fable-5-2", "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", MODEL]
    assert data["models"][-1] == {"id": MODEL, "label": MODEL}


def test_relate_models_of_the_default_app(settings: Settings, monkeypatch) -> None:
    monkeypatch.setenv("TEACHER_CONCEPT_MODEL", "claude-opus-5-5")
    with TestClient(create_app(settings, generator=StubGenerator())) as client:
        data = client.get("/api/graph/relate-models").json()
    assert data["default"] == "claude-opus-5-5"
    assert [m["id"] for m in data["models"]] == [
        "claude-fable-5-2", "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"]
