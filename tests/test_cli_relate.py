"""`teacher concepts relate` with fake Claude and embedding clients (no network)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from teacher import cli
from teacher.concept_relating import SLOTS, ConceptRelater
from teacher.config import ENV_HOME, Settings
from teacher.db.engine import session_scope
from teacher.managers import ConceptEmbeddingManager, ConceptManager

MODEL = "claude-test-relations"
TABLE = {"attention": [1.0], "softmax": [0.6, 0.8], "dot product": [0.5, 0.0, 0.866]}


def proposals(fake: Any, **slots: list[dict]) -> Any:
    body = {slot: slots.get(slot, []) for slot in SLOTS}
    return fake.reply(text=json.dumps(body), model=MODEL, input_tokens=100, output_tokens=10)


def verdicts(fake: Any, *items: tuple[int, str, str]) -> Any:
    body = {"verdicts": [{"candidate": c, "verdict": v, "rationale": r} for c, v, r in items]}
    return fake.reply(text=json.dumps(body), model=MODEL, input_tokens=200, output_tokens=20)


@pytest.fixture
def home(settings: Settings, monkeypatch: pytest.MonkeyPatch, fake_embedder: type) -> Any:
    """$TEACHER_HOME at the test settings with attention, softmax, dot product (embedded)."""
    monkeypatch.setenv(ENV_HOME, str(settings.root))
    embedder = fake_embedder(TABLE)
    with session_scope(settings) as session:
        concepts = ConceptManager(session, settings)
        ids = {c.slug: c.id for c in (concepts.create(n, f"About {n}.", "user")
                                       for n in ("attention", "softmax", "dot product"))}
        ConceptEmbeddingManager(session, settings).embed_stale(embedder)
    embedder.calls.clear()
    return settings, embedder, ids


def use(monkeypatch: pytest.MonkeyPatch, client: Any, embedder: Any) -> None:
    monkeypatch.setattr(
        cli,
        "build_concept_relater",
        lambda models, stub=False: ConceptRelater(client, embedder=embedder, model=MODEL),
    )


def test_relate_one_concept_prints_the_summary_and_keeps_a_record(
    home, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], fake_anthropic
) -> None:
    settings, embedder, ids = home
    fake = fake_anthropic(
        proposals(fake_anthropic, builds_on=[{"name": "linear algebra", "aliases": [],
                                              "description": "Vectors and matrices."}]),
        verdicts(fake_anthropic, (ids["softmax"], "focus_builds_on_candidate", "Attention uses it."),
                 (ids["dot-product"], "none", "Too low level.")),
    )
    use(monkeypatch, fake, embedder)
    assert cli.main(["concepts", "relate", "attention"]) == 0
    out = capsys.readouterr().out
    assert "Relations for 'attention' (attention) with claude-test-relations " \
           "(300 input / 30 output tokens)." in out
    assert "Proposals: 1 builds_on, 0 built_on_by, 0 different_version_of, 0 relates_to; " \
           "0 found existing concepts, 1 did not." in out
    assert "Candidates 2: 2 by embedding, 0 from proposals, 0 by both." in out
    assert "Added 1:\n  + attention builds on softmax: Attention uses it.\n" in out
    assert "Judged none 1 (list them with -v):\nRefused 0:\n" in out
    assert "Unmatched suggestions 1 (not in the graph):\n" \
           "  ? builds_on: linear algebra - Vectors and matrices.\n" in out
    path = settings.derived_dir / "_concept_relations" / "attention.json"
    assert f"Record: {path}" in out
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["trigger"] == "cli" and len(record["output"]["added"]) == 1

    # Again, verbose: everything is settled, so no judge call (the fake would refuse one).
    use(monkeypatch, fake_anthropic(proposals(fake_anthropic)), embedder)
    assert cli.main(["concepts", "relate", "attention", "-v"]) == 0
    out = capsys.readouterr().out
    assert "  - softmax [related]: relation #1 (attention builds_on softmax) already joins" in out
    assert "  - dot-product [judged]: judged none (none) on " in out
    assert "Judged 0: nothing left to judge, so no judge call was made." in out


def test_relate_all_skips_settled_concepts_and_counts_judge_calls(
    home, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], fake_anthropic
) -> None:
    settings, embedder, ids = home
    fake = fake_anthropic(
        # attention: softmax and dot product are judged
        proposals(fake_anthropic),
        verdicts(fake_anthropic, (ids["softmax"], "relates_to", "They go together."),
                 (ids["dot-product"], "none", "No.")),
        # dot product: attention was judged with it; softmax (cosine 0.30) is new
        proposals(fake_anthropic),
        verdicts(fake_anthropic, (ids["softmax"], "none", "No.")),
        # softmax: attention is related and dot product judged, so no judge call
        proposals(fake_anthropic),
    )
    use(monkeypatch, fake, embedder)
    assert cli.main(["concepts", "relate", "--all"]) == 0
    out = capsys.readouterr().out
    assert [line.split(" (")[0] for line in out.splitlines() if line.startswith("Relations for")] \
        == ["Relations for 'attention'", "Relations for 'dot-product'", "Relations for 'softmax'"]
    assert "Related 3 concept(s): 1 added, 2 judged none, 0 refused; 2 judge call(s); " in out
    assert fake.outcomes == []

    # --limit: only the first concept in slug order.
    use(monkeypatch, fake_anthropic(proposals(fake_anthropic)), embedder)
    assert cli.main(["concepts", "relate", "--all", "--limit", "1"]) == 0
    out = capsys.readouterr().out
    assert "Related 1 concept(s): 0 added, 0 judged none, 0 refused; 0 judge call(s)" in out


def test_relate_all_stops_at_the_first_error_and_keeps_earlier_work(
    home, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], fake_anthropic
) -> None:
    settings, embedder, ids = home
    fake = fake_anthropic(
        proposals(fake_anthropic),
        verdicts(fake_anthropic, (ids["softmax"], "relates_to", "They go together."),
                 (ids["dot-product"], "none", "No.")),
        fake_anthropic.reply(text='{"builds_on": []}', model=MODEL),  # dot product: invalid
    )
    use(monkeypatch, fake, embedder)
    assert cli.main(["concepts", "relate", "--all"]) == 1
    captured = capsys.readouterr()
    assert "Relations for 'attention'" in captured.out and "softmax'" not in captured.out
    assert "Stopped at 'dot-product' (2 of 3); the relations of the 1 concept(s) before it " \
           "are kept." in captured.err
    assert "error: Relations could not be found: Claude's relation proposals reply is not a " \
           "valid WireProposals" in captured.err
    with session_scope(settings) as session:
        from teacher.managers import ConceptRelationManager

        assert len(ConceptRelationManager(session, settings).list()) == 1


def test_relate_errors_exit_1(
    home, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], fake_anthropic
) -> None:
    settings, embedder, _ids = home
    client = fake_anthropic()
    use(monkeypatch, client, embedder)
    assert cli.main(["concepts", "relate", "nope"]) == 1
    assert capsys.readouterr().err == "error: no concept with slug 'nope'\n"
    assert cli.main(["concepts", "relate", "attention", "--limit", "2"]) == 1
    assert "--limit needs --all" in capsys.readouterr().err
    embedder.available = False
    assert cli.main(["concepts", "relate", "attention"]) == 1
    assert "no OpenAI API key (fake)" in capsys.readouterr().err
    assert client.calls == []  # checked before any call
    with pytest.raises(SystemExit):
        cli.main(["concepts", "relate"])  # a slug or --all
    with pytest.raises(SystemExit):
        cli.main(["concepts", "relate", "attention", "--all"])


def test_relate_with_the_real_defaults_needs_keys(
    home, capsys: pytest.CaptureFixture[str], forbid_real_messages_api: None
) -> None:
    assert cli.main(["concepts", "relate", "attention"]) == 1
    err = capsys.readouterr().err
    assert "Anthropic credentials could not be loaded" in err or "no Anthropic API" in err
    assert "no OpenAI API key was found" in err


def test_relate_with_the_offline_stub(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(ENV_HOME, str(settings.root))
    with session_scope(settings) as session:
        c = ConceptManager(session, settings)
        c.create("attention", "Attention weighs values by query key similarity.", "user")
        c.create("self-attention", "Attention where queries and keys share one sequence.", "user")
    assert cli.main(["concepts", "relate", "attention", "--stub"]) == 0
    out = capsys.readouterr().out
    assert "with stub-relations" in out and "  + attention relates to self-attention: " in out


def test_relate_all_on_an_empty_graph(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(ENV_HOME, str(settings.root))
    assert cli.main(["concepts", "relate", "--all", "--stub"]) == 0
    assert capsys.readouterr().out == "There are no concepts to relate.\n"


# -- --regenerate, --model, --yes ------------------------------------------------------------------


def link(settings: Settings, type: str, a: str, b: str, origin: str = "model") -> int:
    from teacher.managers import ConceptRelationManager

    with session_scope(settings) as session:
        c = ConceptManager(session, settings)
        return ConceptRelationManager(session, settings).add(
            type, c.get_by_slug(a), c.get_by_slug(b), "An old reason.", origin
        ).id


def relation_rows(settings: Settings) -> list[tuple]:
    from sqlalchemy import text

    with session_scope(settings) as session:
        return sorted(session.execute(text("SELECT * FROM concept_relations")).all())


def test_relate_regenerate_with_a_model(
    home, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], fake_anthropic
) -> None:
    settings, embedder, ids = home
    link(settings, "relates_to", "attention", "softmax")
    link(settings, "builds_on", "attention", "dot-product", origin="user")  # kept
    fake = fake_anthropic(
        proposals(fake_anthropic),
        verdicts(fake_anthropic, (ids["softmax"], "none", "Not related after all.")),
    )
    use(monkeypatch, fake, embedder)
    assert cli.main(["concepts", "relate", "attention", "--regenerate",
                     "--model", "claude-haiku-5-5"]) == 0
    out = capsys.readouterr().out
    assert [call["model"] for call in fake.calls] == ["claude-haiku-5-5", "claude-haiku-5-5"]
    assert "Relations for 'attention' (attention) with claude-haiku-5-5 " in out
    assert "Regenerated: 1 model-made unreviewed relation(s) re-decided; 1 kept" in out
    assert "Removed 1:\n  - attention relates to softmax [none]: Not related after all.\n" in out
    assert "Kept 1:\n  = attention builds on dot-product (origin user)\n" in out
    record = json.loads((settings.derived_dir / "_concept_relations" / "attention.json")
                        .read_text(encoding="utf-8"))
    assert record["regenerate"] is True and record["model"] == "claude-haiku-5-5"
    assert len(relation_rows(settings)) == 1  # only the kept one


def test_relate_rejects_an_unknown_model_before_any_call(
    home, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], fake_anthropic
) -> None:
    settings, embedder, _ids = home
    fake = fake_anthropic()
    use(monkeypatch, fake, embedder)
    assert cli.main(["concepts", "relate", "attention", "--model", "gpt-5"]) == 1
    assert "error: unknown model 'gpt-5': expected one of claude-fable-5-2" in \
        capsys.readouterr().err
    assert fake.calls == [] and embedder.calls == []


def test_relate_all_regenerate_needs_yes(
    home, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], fake_anthropic
) -> None:
    settings, embedder, ids = home
    link(settings, "relates_to", "attention", "softmax")
    link(settings, "relates_to", "dot-product", "softmax")
    link(settings, "builds_on", "attention", "dot-product", origin="paper")  # not counted
    before = relation_rows(settings)
    fake = fake_anthropic()
    use(monkeypatch, fake, embedder)
    assert cli.main(["concepts", "relate", "--all", "--regenerate"]) == 1
    captured = capsys.readouterr()
    assert "Regenerating the relations of 3 concept(s) replaces 2 unreviewed model-made " \
           "relation(s); reviewed ones and those from a paper or a person are kept." in captured.out
    assert "error: nothing was changed; pass --yes to replace them" in captured.err
    assert fake.calls == [] and embedder.calls == [] and relation_rows(settings) == before

    # With --limit only the first concepts count (attention: 1 replaceable relation).
    assert cli.main(["concepts", "relate", "--all", "--limit", "1", "--regenerate"]) == 1
    assert "of 1 concept(s) replaces 1 unreviewed" in capsys.readouterr().out

    # --yes alone is an error: it only answers --all --regenerate.
    for argv in (["attention", "--yes"], ["attention", "--regenerate", "--yes"],
                 ["--all", "--yes"]):
        assert cli.main(["concepts", "relate", *argv]) == 1
        assert "--yes is only used with --all --regenerate" in capsys.readouterr().err
    assert fake.calls == []


def test_relate_all_regenerate_with_yes_runs(
    home, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], fake_anthropic
) -> None:
    settings, embedder, ids = home
    link(settings, "relates_to", "attention", "softmax")
    fake = fake_anthropic(
        # attention: softmax (old neighbour, also 0.6) and dot product (0.5)
        proposals(fake_anthropic),
        verdicts(fake_anthropic, (ids["softmax"], "relates_to", "They go together."),
                 (ids["dot-product"], "none", "No.")),
        # dot product: attention (0.5) and softmax (0.30) are judged afresh
        proposals(fake_anthropic),
        verdicts(fake_anthropic, (ids["attention"], "none", "No."),
                 (ids["softmax"], "none", "No.")),
        # softmax: the relation attention made a moment ago is re-decided too
        proposals(fake_anthropic),
        verdicts(fake_anthropic, (ids["attention"], "relates_to", "They go together."),
                 (ids["dot-product"], "none", "No.")),
    )
    use(monkeypatch, fake, embedder)
    assert cli.main(["concepts", "relate", "--all", "--regenerate", "--yes"]) == 0
    out = capsys.readouterr().out
    assert "replaces 1 unreviewed model-made relation(s)" in out
    assert "Related 3 concept(s): 2 added, 4 judged none, 0 refused, 0 removed; 3 judge " \
           "call(s);" in out
    assert fake.outcomes == []
    assert len(relation_rows(settings)) == 1
