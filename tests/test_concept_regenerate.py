"""ConceptRelater regenerate runs and the per-run model, with fake Claude and embedding clients
(no network): which relations are replaced or kept (each origin x reviewed), the memo ignored and
replaced, the old neighbours always judged and never capped, `implied` computed without the
replaceable relations, atomicity (a failed Claude call or write changes nothing), the removed /
kept / replaces reporting (same pair with another type or direction), a cycle refused during a
regenerate, a graph changed mid-run, and the model choice (validated, used in both calls, valid
request shape for every model offered)."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy import text

from teacher import concept_relating as cr
from teacher.claude_generator import EFFORT
from teacher.concept_relating import (
    SLOTS,
    ConceptRelater,
    RelationsGraphChangedError,
    RelationsUnavailableError,
    relate_model_choices,
    replaceable_relations,
    stub_relater,
)
from teacher.config import GENERATION_MODEL_CHOICES, Settings
from teacher.db.engine import session_scope
from teacher.managers import (
    ConceptEmbeddingManager,
    ConceptJudgementManager,
    ConceptManager,
    ConceptRelationManager,
)
from tests.conftest import raw_json

MODEL = "claude-test-relations"
WHY = "They are connected in a direct way."
# Cosines to "attention": softmax 0.6, dot product 0.5; every other name is orthogonal to all.
TABLE = {
    "attention": [1.0],
    "softmax": [0.6, 0.8],
    "dot product": [0.5, 0.0, 0.8660254],
}


# -- helpers ---------------------------------------------------------------------------------------


@pytest.fixture
def embedder(fake_embedder: type) -> Any:
    return fake_embedder(TABLE)


def seed(settings: Settings, embedder: Any, *names: str) -> dict[str, int]:
    with session_scope(settings) as session:
        concepts = ConceptManager(session, settings)
        ids = {c.slug: c.id for c in (concepts.create(n, f"About {n}.", "user") for n in names)}
        ConceptEmbeddingManager(session, settings).embed_stale(embedder)
    embedder.calls.clear()
    return ids


def link(settings: Settings, type: str, a: str, b: str, origin: str = "model",
         reviewed: bool = False, why: str = "An old reason.") -> int:
    """Add relation `a type b` (slugs); returns its id."""
    with session_scope(settings) as session:
        c = ConceptManager(session, settings)
        r = ConceptRelationManager(session, settings)
        relation = r.add(type, c.get_by_slug(a), c.get_by_slug(b), why, origin)
        if reviewed:
            r.mark_reviewed(relation)
        return relation.id


def prop(name: str) -> dict:
    return {"name": name, "aliases": [], "description": f"The idea of {name}."}


def proposals(fake: Any, model: str = MODEL, **slots: list[dict]) -> Any:
    body = {slot: slots.get(slot, []) for slot in SLOTS}
    return fake.reply(text=json.dumps(body), model=model, input_tokens=100, output_tokens=10)


def verdict(candidate: int, value: str, rationale: str = WHY) -> dict:
    return {"candidate": candidate, "verdict": value, "rationale": rationale}


def verdicts(fake: Any, *items: dict, model: str = MODEL) -> Any:
    return fake.reply(text=json.dumps({"verdicts": list(items)}), model=model, input_tokens=200,
                      output_tokens=20)


def relater(fake: Any, embedder: Any) -> ConceptRelater:
    return ConceptRelater(fake, embedder=embedder, model=MODEL, timeout=33.0)


def candidates_of(call: dict[str, Any]) -> list[dict]:
    body = call["messages"][0]["content"][0]["text"]
    return json.loads(body.split("<candidates>\n", 1)[1].split("\n</candidates>", 1)[0])


def relations(settings: Settings) -> list[tuple]:
    """(id, type, from slug, to slug, origin, reviewed, rationale), by id."""
    sql = (
        "SELECT r.id, r.type, f.slug, t.slug, r.origin, r.reviewed_at IS NOT NULL, r.rationale "
        "FROM concept_relations r JOIN concepts f ON f.id = r.from_id "
        "JOIN concepts t ON t.id = r.to_id ORDER BY r.id"
    )
    with session_scope(settings) as session:
        return [tuple(row) for row in session.execute(text(sql))]


def memo(settings: Settings) -> dict[tuple[str, str], dict]:
    with session_scope(settings) as session:
        slugs = dict(session.execute(text("SELECT id, slug FROM concepts")).all())
        rows = session.execute(text("SELECT * FROM concept_pair_judgements")).mappings()
        return {
            tuple(sorted((slugs[r["low_id"]], slugs[r["high_id"]]))): dict(r) for r in rows
        }


def snapshot(settings: Settings) -> dict[str, list]:
    with session_scope(settings) as session:
        return {
            key: sorted(session.execute(text(f"SELECT * FROM {key}")).all())
            for key in ("concept_relations", "concept_pair_judgements")
        }


def hook_before_judge(fake: Any, action: Callable[[], None]) -> None:
    original = fake.stream

    def stream(**kwargs: Any) -> Any:
        if len(fake.calls) == 1:
            action()
        return original(**kwargs)

    fake.stream = stream


# -- what is replaced and what is kept -------------------------------------------------------------


def test_only_unreviewed_model_relations_are_replaced(
    settings: Settings, embedder, fake_anthropic
) -> None:
    ids = seed(settings, embedder, "attention", "alpha", "beta", "gamma", "delta", "epsilon",
               "zeta")
    rel = {
        "alpha": link(settings, "relates_to", "attention", "alpha"),  # replaceable
        "beta": link(settings, "relates_to", "attention", "beta", reviewed=True),
        "gamma": link(settings, "relates_to", "attention", "gamma", origin="paper"),
        "delta": link(settings, "relates_to", "attention", "delta", origin="paper",
                      reviewed=True),
        "epsilon": link(settings, "relates_to", "attention", "epsilon", origin="user"),
        "zeta": link(settings, "relates_to", "attention", "zeta", origin="user", reviewed=True),
    }
    before = {row[0]: row for row in relations(settings)}
    fake = fake_anthropic(
        proposals(fake_anthropic, relates_to=[prop("beta")]),  # a kept pair: stays settled
        verdicts(fake_anthropic, verdict(ids["alpha"], "none", "They are not connected.")),
    )
    run = relater(fake, embedder).relate(settings, "attention", regenerate=True)

    assert [c["id"] for c in candidates_of(fake.calls[1])] == [ids["alpha"]]
    assert [(o.other.slug, o.relation_id) for o in run.replaceable] == [("alpha", rel["alpha"])]
    assert [(k.other.slug, k.kept_because) for k in run.kept] == [
        ("beta", ["reviewed"]),
        ("gamma", ["origin paper"]),
        ("delta", ["reviewed", "origin paper"]),
        ("epsilon", ["origin user"]),
        ("zeta", ["reviewed", "origin user"]),
    ]
    assert [(d.candidate.concept.slug, d.reason) for d in run.dropped] == [("beta", "related")]
    [removed] = run.removed
    assert (removed.old.other.slug, removed.reason, removed.detail) == (
        "alpha", "none", "They are not connected.")
    # The kept relations are untouched, row for row; the replaceable one is gone.
    after = {row[0]: row for row in relations(settings)}
    assert after == {i: row for i, row in before.items() if i != rel["alpha"]}
    assert run.counts()["removed"] == 1 and run.counts()["kept"] == 5
    assert run.to_dict()["deleted_relation_ids"] == [rel["alpha"]]


def test_replaceable_relations_counts_per_concept_or_graph(settings, embedder) -> None:
    ids = seed(settings, embedder, "attention", "alpha", "beta", "gamma")
    a = link(settings, "relates_to", "attention", "alpha")
    link(settings, "relates_to", "attention", "beta", reviewed=True)
    c = link(settings, "builds_on", "gamma", "alpha")
    link(settings, "relates_to", "beta", "gamma", origin="user")
    with session_scope(settings) as session:
        assert [r.id for r in replaceable_relations(session, [ids["attention"]])] == [a]
        assert [r.id for r in replaceable_relations(session, [ids["alpha"]])] == [a, c]
        assert [r.id for r in replaceable_relations(session)] == [a, c]
        assert replaceable_relations(session, []) == []


# -- the memo, the cap, implied --------------------------------------------------------------------


def test_the_memo_is_ignored_and_replaced(settings: Settings, embedder, fake_anthropic) -> None:
    ids = seed(settings, embedder, "attention", "softmax")
    fake = fake_anthropic(proposals(fake_anthropic),
                          verdicts(fake_anthropic, verdict(ids["softmax"], "none", "Not now.")))
    relater(fake, embedder).relate(settings, "attention")
    old = memo(settings)[("attention", "softmax")]
    # An incremental run skips the judged pair ...
    run = relater(fake_anthropic(proposals(fake_anthropic)), embedder).relate(settings, "attention")
    assert [d.reason for d in run.dropped] == ["judged"]
    # ... a regenerate run judges it again, and the new verdict replaces the row.
    fake = fake_anthropic(
        proposals(fake_anthropic, model="claude-haiku-5-5"),
        verdicts(fake_anthropic, verdict(ids["softmax"], "relates_to"), model="claude-haiku-5-5"),
    )
    run = relater(fake, embedder).relate(settings, "attention", regenerate=True,
                                         model="claude-haiku-5-5")
    assert run.dropped == () and [a.other.slug for a in run.added] == ["softmax"]
    assert run.added[0].replaces is None  # the pair had no relation, only a memo row
    new = memo(settings)[("attention", "softmax")]
    assert (old["verdict"], new["verdict"]) == ("none", "relates_to")
    assert (new["outcome"], new["model"], new["relation_id"]) == (
        "added", "claude-haiku-5-5", run.added[0].relation_id)
    assert len(memo(settings)) == 1


def test_old_neighbours_are_always_judged_and_never_capped(
    settings: Settings, embedder, fake_anthropic, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cr, "MAX_CANDIDATES", 1)
    ids = seed(settings, embedder, "attention", "softmax", "dot product", "alpha", "beta")
    link(settings, "relates_to", "attention", "alpha")
    link(settings, "builds_on", "beta", "attention")
    fake = fake_anthropic(
        proposals(fake_anthropic),
        verdicts(fake_anthropic, verdict(ids["alpha"], "relates_to"),
                 verdict(ids["beta"], "candidate_builds_on_focus")),
    )
    run = relater(fake, embedder).relate(settings, "attention", regenerate=True)
    judged = candidates_of(fake.calls[1])
    # Both old neighbours (similarity 0: orthogonal names) are judged, over the cap of 1; the
    # embedding neighbours softmax (0.6) and dot product (0.5) are cut instead.
    assert sorted(c["id"] for c in judged) == sorted([ids["alpha"], ids["beta"]])
    assert sorted((d.candidate.concept.slug, d.reason) for d in run.dropped) == [
        ("dot-product", "cap"), ("softmax", "cap")]
    for c in judged:
        assert c["found_by"][0].startswith("already joined to the focus by an unreviewed "
                                           "relation another model made")
        assert "relates_to" not in c["found_by"][0]  # the old type is not given as a hint
    rows = memo(settings)
    assert raw_json(rows[("alpha", "attention")]["found_by"]) == ["existing"]
    assert run.counts()["existing"] == 2 and run.counts()["replaced"] == 2


def test_implied_is_computed_without_the_replaceable_relations(
    settings: Settings, embedder, fake_anthropic
) -> None:
    ids = seed(settings, embedder, "attention", "softmax", "alpha", "beta", "gamma")
    # attention -> alpha (replaceable) -> softmax (kept): softmax is implied only through a
    # relation that is being re-decided, so a regenerate judges it.
    link(settings, "builds_on", "attention", "alpha")
    link(settings, "builds_on", "alpha", "softmax", origin="user")
    # attention -> beta (kept) -> gamma (kept), and attention -> gamma (replaceable): gamma is
    # still implied by the kept path, so the old direct edge is removed without a judgement.
    link(settings, "builds_on", "attention", "beta", origin="user")
    link(settings, "builds_on", "beta", "gamma", origin="paper")
    old_gamma = link(settings, "builds_on", "attention", "gamma")

    incremental = relater(fake_anthropic(proposals(fake_anthropic)), embedder).relate(
        settings, "attention")
    assert [(d.candidate.concept.slug, d.reason) for d in incremental.dropped] == [
        ("softmax", "implied")]

    fake = fake_anthropic(
        proposals(fake_anthropic),
        verdicts(fake_anthropic, verdict(ids["softmax"], "focus_builds_on_candidate"),
                 verdict(ids["alpha"], "none", "Not needed.")),
    )
    run = relater(fake, embedder).relate(settings, "attention", regenerate=True)
    assert sorted(c["id"] for c in candidates_of(fake.calls[1])) == sorted(
        [ids["softmax"], ids["alpha"]])
    assert [(d.candidate.concept.slug, d.reason) for d in run.dropped] == [("gamma", "implied")]
    assert sorted((r.old.other.slug, r.reason) for r in run.removed) == [
        ("alpha", "none"), ("gamma", "implied")]
    gamma = next(r for r in run.removed if r.old.other.slug == "gamma")
    assert gamma.old.relation_id == old_gamma
    assert gamma.detail == "builds_on path attention -> beta -> gamma"
    assert [(t, f, to) for _i, t, f, to, *_ in relations(settings)] == [
        ("builds_on", "alpha", "softmax"),
        ("builds_on", "attention", "beta"),
        ("builds_on", "beta", "gamma"),
        ("builds_on", "attention", "softmax"),
    ]


# -- reporting -------------------------------------------------------------------------------------


def test_replacements_report_a_changed_type_or_direction(
    settings: Settings, embedder, fake_anthropic
) -> None:
    ids = seed(settings, embedder, "attention", "softmax", "alpha", "beta", "gamma")
    old = {
        "alpha": link(settings, "builds_on", "attention", "alpha"),  # attention builds on alpha
        "beta": link(settings, "relates_to", "attention", "beta"),
        "gamma": link(settings, "relates_to", "attention", "gamma"),
    }
    fake = fake_anthropic(
        proposals(fake_anthropic),
        verdicts(
            fake_anthropic,
            verdict(ids["softmax"], "relates_to", "Softmax and attention go together."),
            verdict(ids["alpha"], "candidate_builds_on_focus", "Alpha uses attention."),
            verdict(ids["beta"], "relates_to", "Beta goes with attention."),
            verdict(ids["gamma"], "focus_builds_on_candidate", "Attention needs gamma."),
        ),
    )
    run = relater(fake, embedder).relate(settings, "attention", regenerate=True)
    by_slug = {a.other.slug: a for a in run.added}
    assert by_slug["softmax"].replaces is None
    assert (by_slug["alpha"].replaces.relation_id, by_slug["alpha"].type_changed,
            by_slug["alpha"].direction_changed) == (old["alpha"], False, True)
    assert (by_slug["beta"].replaces.relation_id, by_slug["beta"].type_changed,
            by_slug["beta"].direction_changed) == (old["beta"], False, False)
    assert (by_slug["gamma"].replaces.type, by_slug["gamma"].type, by_slug["gamma"].type_changed,
            by_slug["gamma"].direction_changed) == ("relates_to", "builds_on", True, True)
    assert run.removed == () and run.kept == ()
    # The old rows were deleted and new ones added (SQLite may give a new row a freed id).
    assert len(relations(settings)) == 4
    current = {(t, f, to): why for _i, t, f, to, _o, _r, why in relations(settings)}
    assert current == {
        ("relates_to", "attention", "softmax"): "Softmax and attention go together.",
        ("builds_on", "alpha", "attention"): "Alpha uses attention.",
        ("relates_to", "attention", "beta"): "Beta goes with attention.",
        ("builds_on", "attention", "gamma"): "Attention needs gamma.",
    }
    lines = run.summary_lines()
    assert "Regenerated: 3 model-made unreviewed relation(s) re-decided; 0 kept (reviewed, or " \
           "from a paper or a person)." in lines
    assert "  + alpha builds on attention (was: attention builds on alpha): Alpha uses " \
           "attention." in lines
    assert "  + attention relates to beta (replaces the same relation): Beta goes with " \
           "attention." in lines
    assert "Removed 0:" in lines and "Kept 0:" in lines
    record = json.loads(json.dumps(run.record("test")))
    assert record["regenerate"] is True
    assert sorted(o["relation_id"] for o in record["replaceable"]) == sorted(old.values())
    added = {a["other"]["slug"]: a for a in record["output"]["added"]}
    assert added["alpha"]["replaces"]["direction_changed"] is True
    assert added["softmax"]["replaces"] is None
    assert record["output"]["counts"]["replaced"] == 3


def test_a_cycle_is_refused_during_a_regenerate(settings, embedder, fake_anthropic) -> None:
    ids = seed(settings, embedder, "attention", "alpha", "beta")
    link(settings, "builds_on", "alpha", "beta", origin="user")  # kept
    old_beta = link(settings, "relates_to", "attention", "beta")  # replaceable
    link(settings, "relates_to", "attention", "alpha")  # replaceable
    fake = fake_anthropic(
        proposals(fake_anthropic),
        verdicts(
            fake_anthropic,
            verdict(ids["alpha"], "focus_builds_on_candidate", "Attention needs alpha."),
            verdict(ids["beta"], "candidate_builds_on_focus", "Beta needs attention."),
        ),
    )
    run = relater(fake, embedder).relate(settings, "attention", regenerate=True)
    assert [a.other.slug for a in run.added] == ["alpha"]
    [refused] = run.refused
    assert refused.cycle == ("attention", "alpha", "beta", "attention")
    [removed] = run.removed
    assert (removed.old.relation_id, removed.reason) == (old_beta, "refused")
    assert "would make builds_on cyclic" in removed.detail
    assert memo(settings)[("attention", "beta")]["outcome"] == "refused"
    assert [(t, f, to, o) for _i, t, f, to, o, _r, _w in relations(settings)] == [
        ("builds_on", "alpha", "beta", "user"),
        ("builds_on", "attention", "alpha", "model"),
    ]
    assert any(line.startswith("  - attention relates to beta [refused]: ")
               for line in run.summary_lines())


# -- atomicity and a changed graph -----------------------------------------------------------------


def prepared(settings: Settings, embedder: Any, fake_anthropic: Any) -> dict[str, int]:
    """attention with two replaceable relations (alpha, beta) and memo rows for both pairs."""
    ids = seed(settings, embedder, "attention", "alpha", "beta")
    link(settings, "relates_to", "attention", "alpha")
    link(settings, "relates_to", "attention", "beta")
    return ids


def test_a_failed_claude_call_changes_nothing(settings, embedder, fake_anthropic) -> None:
    ids = prepared(settings, embedder, fake_anthropic)
    before = snapshot(settings)
    for outcomes in (
        [fake_anthropic.timeout()],
        [proposals(fake_anthropic), fake_anthropic.timeout()],
    ):
        with pytest.raises(RelationsUnavailableError, match="call to Claude failed"):
            relater(fake_anthropic(*outcomes), embedder).relate(settings, "attention",
                                                               regenerate=True)
        assert snapshot(settings) == before
    del ids


def test_a_failed_write_changes_nothing(
    settings, embedder, fake_anthropic, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = prepared(settings, embedder, fake_anthropic)
    with session_scope(settings) as session:  # an earlier memo row, to see it survive
        ConceptJudgementManager(session, settings).record(
            focus_id=ids["attention"], focus_text="old", other_id=ids["alpha"],
            other_text="old", found_by=["embedding"], similarity=None, verdict="none",
            outcome="none", rationale="Earlier.", model="old-model")
    before = snapshot(settings)
    original = ConceptJudgementManager.record
    calls: list[Any] = []

    def record(self: Any, **kwargs: Any) -> Any:
        calls.append(kwargs)
        if len(calls) == 2:
            raise RuntimeError("disk full (fake)")
        return original(self, **kwargs)

    monkeypatch.setattr(ConceptJudgementManager, "record", record)
    fake = fake_anthropic(
        proposals(fake_anthropic),
        verdicts(fake_anthropic, verdict(ids["alpha"], "none", "No."),
                 verdict(ids["beta"], "relates_to")),
    )
    with pytest.raises(RuntimeError, match="disk full"):
        relater(fake, embedder).relate(settings, "attention", regenerate=True)
    assert snapshot(settings) == before  # the old relations and the old memo row are back


@pytest.mark.parametrize("change", ["review", "add", "edit"])
def test_a_graph_changed_mid_run_writes_nothing(
    settings: Settings, embedder, fake_anthropic, change: str
) -> None:
    ids = prepared(settings, embedder, fake_anthropic)
    seed(settings, embedder, "gamma")

    def act() -> None:
        with session_scope(settings) as session:
            c = ConceptManager(session, settings)
            r = ConceptRelationManager(session, settings)
            if change == "review":  # a replaceable relation becomes reviewed
                r.mark_reviewed(r.between(c.get_by_slug("attention"), c.get_by_slug("alpha")))
            elif change == "add":  # another run adds a model relation to attention
                r.add("relates_to", c.get_by_slug("attention"), c.get_by_slug("gamma"),
                      "Added meanwhile.", "model")
            else:
                c.update(c.get_by_slug("beta"), summary="Edited.")

    fake = fake_anthropic(
        proposals(fake_anthropic),
        verdicts(fake_anthropic, verdict(ids["alpha"], "none", "No."),
                 verdict(ids["beta"], "none", "No.")),
    )
    hook_before_judge(fake, act)
    with pytest.raises(RelationsGraphChangedError, match="the concept graph changed") as caught:
        relater(fake, embedder).relate(settings, "attention", regenerate=True)
    if change == "edit":
        assert "concept 'beta' was edited" in str(caught.value)
    else:
        assert "are no longer the ones this run re-decided" in str(caught.value)
    slugs = {(f, to) for _i, _t, f, to, *_ in relations(settings)}
    assert {("attention", "alpha"), ("attention", "beta")} <= slugs  # the old edges remain
    assert memo(settings) == {}


def test_nothing_to_replace_and_nothing_to_judge_writes_nothing(
    settings, embedder, fake_anthropic
) -> None:
    seed(settings, embedder, "attention", "alpha")
    link(settings, "relates_to", "attention", "alpha", origin="user")
    before = snapshot(settings)
    run = relater(fake_anthropic(proposals(fake_anthropic)), embedder).relate(
        settings, "attention", regenerate=True)
    assert not run.judge_called and run.removed == () and len(run.kept) == 1
    assert snapshot(settings) == before


# -- the model -------------------------------------------------------------------------------------


def test_the_chosen_model_is_used_for_both_calls(settings, embedder, fake_anthropic) -> None:
    ids = seed(settings, embedder, "attention", "softmax")
    fake = fake_anthropic(
        proposals(fake_anthropic, model="claude-opus-5-5"),
        verdicts(fake_anthropic, verdict(ids["softmax"], "relates_to"), model="claude-opus-5-5"),
    )
    run = relater(fake, embedder).relate(settings, "attention", model="claude-opus-5-5")
    assert [call["model"] for call in fake.calls] == ["claude-opus-5-5", "claude-opus-5-5"]
    assert run.model == "claude-opus-5-5" and run.regenerate is False
    assert memo(settings)[("attention", "softmax")]["model"] == "claude-opus-5-5"
    assert run.record("test")["model"] == "claude-opus-5-5"
    assert run.summary_lines()[0].startswith("Relations for 'attention' (attention) with "
                                             "claude-opus-5-5")
    # No model: the relater's default.
    fake = fake_anthropic(proposals(fake_anthropic))
    assert relater(fake, embedder).relate(settings, "attention").model == MODEL
    assert fake.calls[0]["model"] == MODEL


def test_an_unknown_model_fails_before_any_call(settings, embedder, fake_anthropic) -> None:
    seed(settings, embedder, "attention")
    fake = fake_anthropic()
    with pytest.raises(ValueError, match="unknown model 'gpt-5': expected one of claude-fable"):
        relater(fake, embedder).relate(settings, "attention", model="gpt-5")
    assert fake.calls == [] and embedder.calls == []


def test_the_model_choices(fake_embedder: type) -> None:
    ids = [model for model, _label in GENERATION_MODEL_CHOICES]
    assert [m for m, _l in relate_model_choices("claude-sonnet-5-5")] == ids
    assert relate_model_choices("claude-custom")[-1] == ("claude-custom", "claude-custom")
    r = ConceptRelater(embedder=fake_embedder(), model="claude-custom")
    assert r.choose_model(None) == "claude-custom" and r.choose_model("claude-custom")
    for model in ids:
        assert r.choose_model(model) == model


@pytest.mark.parametrize("model", [m for m, _label in GENERATION_MODEL_CHOICES])
def test_the_request_shape_is_valid_for_every_model_offered(
    settings: Settings, embedder, fake_anthropic, model: str
) -> None:
    """The same shape the study-page generator sends to every model it offers
    (`claude_generator.stage_request`): no `thinking` (adaptive by default), no sampling
    parameters, `output_config` with `effort` and a JSON-schema `format`, streamed, and
    max_tokens far under the 128K output limit of these models."""
    from teacher.claude_generator import WRITE_MAX_TOKENS, stage_request

    generator_keys = set(stage_request(model=model, timeout=1.0, document={}, instructions="",
                                       format={}, max_tokens=1))
    ids = seed(settings, embedder, "attention", "softmax")
    fake = fake_anthropic(proposals(fake_anthropic, model=model),
                          verdicts(fake_anthropic, verdict(ids["softmax"], "none", "No."),
                                   model=model))
    relater(fake, embedder).relate(settings, "attention", model=model)
    for call in fake.calls:
        assert set(call) == generator_keys == {"model", "max_tokens", "system", "messages",
                                               "output_config", "timeout"}
        assert call["model"] == model
        assert call["max_tokens"] <= WRITE_MAX_TOKENS  # the generator sends this to them all
        assert set(call["output_config"]) == {"effort", "format"}
        assert call["output_config"]["effort"] == EFFORT
        assert call["output_config"]["format"]["type"] == "json_schema"
        assert [b["type"] for b in call["messages"][0]["content"]] == ["text"]


def test_the_offline_stub_regenerates(settings: Settings) -> None:
    with session_scope(settings) as session:
        c = ConceptManager(session, settings)
        attention = c.create("attention", "Attention weighs values by query key similarity.",
                             "user")
        tax = c.create("tax law", "Rules for taxes.", "user")
        c.create("self-attention", "Attention where queries and keys share one sequence.", "user")
        ConceptRelationManager(session, settings).add("relates_to", attention, tax,
                                                      "Claimed once.", "model")
    run = stub_relater().relate(settings, "attention", regenerate=True,
                                model="claude-sonnet-5-5")
    assert [(a.other.slug, a.type) for a in run.added] == [("self-attention", "relates_to")]
    assert [(r.old.other.slug, r.reason) for r in run.removed] == [("tax-law", "none")]
    assert run.model == "claude-sonnet-5-5"

