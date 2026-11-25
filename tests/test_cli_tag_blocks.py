"""`teacher concepts tag-blocks` with a fake Claude client (no network): one page, every page
(stopping at the first error and resuming), --retag, --model, --stub and the errors."""

from __future__ import annotations

import json
from typing import Any

import pytest

from teacher import cli
from teacher.block_tagging import BlockTagger
from teacher.db.engine import session_scope
from teacher.managers import ArtifactBlockManager, ArtifactManager, BlockConceptManager

MODEL = "claude-test-concepts"


def tagging(fake: Any, blocks: dict[int, list[tuple[int, str, str]]], tokens: int = 500) -> Any:
    body = {"blocks": [{"block": n, "concepts": [{"concept": c, "role": r, "rationale": w}
                                                for c, r, w in items]}
                       for n, items in blocks.items()]}
    return fake.reply(text=json.dumps(body), model=MODEL, input_tokens=tokens, output_tokens=50)


@pytest.fixture
def home(tag_library: Any, settings_env: Any) -> Any:
    """$TEACHER_HOME at the tag library, with a second ready page `second` (2 blocks)."""
    lib = tag_library
    settings_env(lib.settings)
    with session_scope(lib.settings) as session:
        artifacts = ArtifactManager(session, lib.settings)
        source = artifacts.sources(artifacts.get_by_slug(lib.page))[0][0]
        second = artifacts.create(slug="second", kind="study_page", title="Second",
                                  generator="test", sources=[source])
        blocks = ArtifactBlockManager(session, lib.settings)
        blocks.add(second, type="prose", content={"markdown": "About attention."})
        blocks.add(second, type="prose", content={"markdown": "The end."})
        artifacts.mark_ready(second)
        artifacts.create(slug="broken", kind="study_page", title="Broken", generator="test",
                         sources=[source])
        ArtifactManager(session, lib.settings).mark_failed(artifacts.get_by_slug("broken"))
    return lib


def use(monkeypatch: pytest.MonkeyPatch, client: Any) -> None:
    monkeypatch.setattr(
        cli, "build_block_tagger",
        lambda models, stub=False: BlockTagger(client, model=MODEL),
    )


def first_page(lib: Any, fake: Any) -> Any:
    a, s = lib.concepts["attention"], lib.concepts["softmax"]
    return tagging(fake, {1: [(a, "main", "Attention.")], 2: [(a, "main", "It asks.")],
                          3: [(s, "main", "Softmax.")], 4: [], 5: [(s, "main", "It asks.")]})


def tags(lib: Any, slug: str) -> list[tuple[str, str, str]]:
    with session_scope(lib.settings) as session:
        page = ArtifactManager(session, lib.settings).get_by_slug(slug)
        return [(b.key, c.slug, t.role)
                for b, t, c in BlockConceptManager(session, lib.settings).for_artifact(page)]


def test_tag_one_page(home: Any, monkeypatch: pytest.MonkeyPatch, capsys: Any, fake_anthropic) -> None:
    lib = home
    fake = fake_anthropic(first_page(lib, fake_anthropic))
    use(monkeypatch, fake)
    assert cli.main(["concepts", "tag-blocks", "study-paper", "-v"]) == 0
    out = capsys.readouterr().out
    assert ("Tagged 'study-paper' (Study: The Paper): 5 blocks (3 questions) against 4 "
            "concept(s) with claude-test-concepts (500 input / 50 output tokens).") in out
    assert "Tags 4: 4 main, 0 incidental, on 2 concept(s); 2 of 3 questions have a main concept." in out
    assert "Blocks with no concept 1: b04." in out and "Questions with no main concept 1: b04." in out
    assert "  b02 multiple_choice: attention [main] It asks." in out
    path = lib.settings.derived_dir / "_block_concepts" / "study-paper.json"
    assert f"Record: {path}" in out
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["trigger"] == "cli" and record["written"]["added"] == 4
    assert tags(lib, "study-paper")[0] == ("b01", "attention", "main")

    use(monkeypatch, fake_anthropic())  # a second run would refuse the call
    assert cli.main(["concepts", "tag-blocks", "study-paper"]) == 1
    assert "was already tagged on" in capsys.readouterr().err

    s = lib.concepts["softmax"]
    use(monkeypatch, fake_anthropic(tagging(fake_anthropic, {1: [], 2: [], 3: [(s, "main", "S.")],
                                                             4: [], 5: []})))
    assert cli.main(["concepts", "tag-blocks", "study-paper", "--retag"]) == 0
    assert "Replaced 4 earlier model tag(s) with 1; kept 0 tag(s) a person made." in capsys.readouterr().out
    assert tags(lib, "study-paper") == [("b03", "softmax", "main")]


def test_tag_all_stops_at_the_first_error_and_resumes(
    home: Any, monkeypatch: pytest.MonkeyPatch, capsys: Any, fake_anthropic
) -> None:
    lib = home
    bad = tagging(fake_anthropic, {1: [(999, "main", "No such concept.")], 2: []}, tokens=70)
    use(monkeypatch, fake_anthropic(first_page(lib, fake_anthropic), bad, bad))
    assert cli.main(["concepts", "tag-blocks", "--all"]) == 1
    captured = capsys.readouterr()
    assert ("2 study page(s) to tag; 0 tagged before skipped; 1 not ready (failed or "
            "generating) skipped.") in captured.out
    assert "Tagged 'study-paper'" in captured.out
    assert ("Stopped at 'second' (2 of 2); the tags of the 1 page(s) before it are kept. Run it "
            "again to continue.") in captured.err
    assert "concept id 999 is not one of the page's concepts" in captured.err
    assert "(the 2 call(s) used 140 input / 100 output tokens)" in captured.err
    assert tags(lib, "study-paper") and tags(lib, "second") == []

    a = lib.concepts["attention"]
    use(monkeypatch, fake_anthropic(tagging(fake_anthropic, {1: [(a, "main", "A.")], 2: []}, 80)))
    assert cli.main(["concepts", "tag-blocks", "--all"]) == 0
    out = capsys.readouterr().out
    assert "1 study page(s) to tag; 1 tagged before skipped (pass --retag to redo them);" in out
    assert "Tagged 1 page(s): 1 tags (1 main); 80 input / 50 output tokens." in out
    assert tags(lib, "second") == [("b01", "attention", "main")]

    use(monkeypatch, fake_anthropic())
    assert cli.main(["concepts", "tag-blocks", "--all"]) == 0
    assert "There are no study pages to tag." in capsys.readouterr().out


def test_errors_before_any_call(home: Any, monkeypatch: pytest.MonkeyPatch, capsys: Any,
                                fake_anthropic) -> None:
    fake = fake_anthropic()
    use(monkeypatch, fake)
    for argv, fragment in [
        (["study-paper", "--model", "claude-nope"], "unknown model 'claude-nope'"),
        (["nope"], "no artifact with slug 'nope'"),
        (["broken"], "'broken' is failed, not ready"),
    ]:
        assert cli.main(["concepts", "tag-blocks", *argv]) == 1
        assert fragment in capsys.readouterr().err, argv
    assert fake.calls == []
    with pytest.raises(SystemExit):  # a slug or --all, not both
        cli.main(["concepts", "tag-blocks", "study-paper", "--all"])


def test_the_stub(home: Any, capsys: Any) -> None:
    assert cli.main(["concepts", "tag-blocks", "study-paper", "--stub"]) == 0
    assert "with stub-block-concepts (0 input / 0 output tokens)" in capsys.readouterr().out
    assert ("b01", "attention", "main") in tags(home, "study-paper")
