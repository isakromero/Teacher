"""`teacher references extract <slug> | --all`: one source or every one still to do, with the
offline stub or a fake-client extractor; failures are recorded and give status 1."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from teacher import cli
from teacher.config import Settings
from teacher.db.engine import session_scope
from teacher.managers import KnowledgeSourceManager, SourceReferenceManager
from teacher.reference_extraction import ReferenceExtractor

CITING = "# Citing\n\n## References\n\n- Vaswani. \"Attention Is All You Need\". arXiv:1706.03762\n"


@pytest.fixture
def home(settings: Settings, settings_env: Any, tmp_path: Path) -> Settings:
    """$TEACHER_HOME at the migrated `settings`, with sources `citing`, `attention`, `plain`."""
    settings_env(settings)
    for slug, text, kw in (
        ("citing", CITING, {}),
        ("attention", "# Attention\n", {"identifiers": {"arxiv": "1706.03762"},
                                        "title": "Attention Is All You Need"}),
        ("plain", "# Plain\n\nNothing cited.\n", {}),
    ):  # fmt: skip
        path = tmp_path / f"{slug}.md"
        path.write_text(text, encoding="utf-8")
        with session_scope(settings) as session:
            KnowledgeSourceManager(session, settings).add(
                path, slug=slug, kind="paper", title=kw.pop("title", slug.title()), **kw
            )
    return settings


def states(settings: Settings) -> dict[str, tuple[str | None, int | None]]:
    with session_scope(settings) as session:
        manager = SourceReferenceManager(session, settings)
        out = {}
        for source in KnowledgeSourceManager(session, settings).list():
            row = manager.extraction(source)
            out[source.slug] = (row.status, row.reference_count) if row else (None, None)
        return out


def test_extract_one_source_with_the_stub(home: Settings, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["references", "extract", "citing", "--stub"]) == 0
    out = capsys.readouterr().out
    assert "citing: 1 reference(s) from its reference list; 1 in the library, 0 not." in out
    assert states(home)["citing"] == ("succeeded", 1)
    assert states(home)["plain"] == (None, None)


def test_extract_all_skips_done_sources_unless_redo(
    home: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["references", "extract", "citing", "--stub"]) == 0
    capsys.readouterr()
    assert cli.main(["references", "extract", "--all", "--stub"]) == 0
    out = capsys.readouterr().out
    assert "2 source(s) to extract; 1 skipped" in out
    assert "plain: no references found (the source cites no works)." in out
    assert "Extracted 2 of 2 source(s): 0 reference(s), 0 in the library" in out
    assert states(home) == {"citing": ("succeeded", 1), "attention": ("succeeded", 0),
                            "plain": ("succeeded", 0)}  # fmt: skip
    assert cli.main(["references", "extract", "--all", "--redo", "--stub"]) == 0
    assert "3 source(s) to extract; 0 skipped" in capsys.readouterr().out
    assert cli.main(["references", "extract", "--all", "--stub"]) == 0
    assert "There are no sources to extract references from." in capsys.readouterr().out


def test_failures_are_recorded_and_exit_1(
    home: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], fake_anthropic
) -> None:
    good = fake_anthropic.reply(text=json.dumps({"found_in": "none", "references": []}))
    bad = fake_anthropic.reply(text="not json")
    fake = fake_anthropic(bad, good, good)
    monkeypatch.setattr(
        cli, "build_reference_extractor", lambda models, stub=False: ReferenceExtractor(fake)
    )
    assert cli.main(["references", "extract", "--all"]) == 1
    captured = capsys.readouterr()
    assert "error: citing: References could not be extracted:" in captured.err
    assert "Extracted 2 of 3 source(s)" in captured.out and "1 failed (recorded" in captured.out
    assert states(home)["citing"] == ("failed", None)
    assert states(home)["plain"] == ("succeeded", 0)


def test_errors_before_any_call(home: Settings, capsys: pytest.CaptureFixture[str],
                                forbid_real_messages_api: None) -> None:
    assert cli.main(["references", "extract", "nope", "--stub"]) == 1
    assert "no knowledge source with slug 'nope'" in capsys.readouterr().err
    assert cli.main(["references", "extract", "citing", "--redo", "--stub"]) == 1
    assert "--redo is only used with --all" in capsys.readouterr().err
    # The real extractor without credentials fails before anything is claimed.
    assert cli.main(["references", "extract", "citing"]) == 1
    assert "credentials" in capsys.readouterr().err
    assert states(home)["citing"] == (None, None)


def test_slug_or_all_is_required() -> None:
    with pytest.raises(SystemExit):
        cli.main(["references", "extract"])
    with pytest.raises(SystemExit):
        cli.main(["references", "extract", "citing", "--all"])
    with pytest.raises(SystemExit):
        cli.main(["references"])
