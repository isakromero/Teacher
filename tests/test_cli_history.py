"""`teacher history export`: JSON and Markdown, to stdout or a new file, and its errors."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from teacher import cli
from teacher.config import Settings
from teacher.db.engine import session_scope
from teacher.managers import ArtifactManager, ChatManager, StudyHistoryManager


@pytest.fixture
def home(settings: Settings, settings_env: Any) -> Settings:
    """$TEACHER_HOME at the migrated test `settings`, with a page opened and a chat message."""
    settings_env(settings)
    with session_scope(settings) as session:
        page = ArtifactManager(session, settings).create(
            slug="page", kind="study_page", title="The Page", generator="test"
        )
        ArtifactManager(session, settings).mark_ready(page)
        StudyHistoryManager(session, settings).log("study_page_opened", artifact=page)
        chat = ChatManager(session, settings)
        chat.add_message(
            chat.create_thread(page), role="user", text="CLI-CHAT-TEXT",
            api_content=[{"type": "text", "text": "CLI-CHAT-TEXT"}],
        )
    return settings


def test_export_json_to_stdout(home: Settings, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["history", "export"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["format"] == "teacher-study-history"
    (session,) = data["sessions"]
    assert [e["type"] for e in session["timeline"]] == ["event", "chat_message"]
    assert session["timeline"][1]["text"] == "CLI-CHAT-TEXT"


def test_export_markdown_to_a_file(
    home: Settings, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "history.md"
    assert cli.main(["history", "export", "--format", "markdown", "--output", str(target)]) == 0
    text = target.read_text(encoding="utf-8")
    assert text.startswith("# Study history") and "> CLI-CHAT-TEXT" in text
    assert "Opened the study page *The Page*." in text
    assert f"Wrote 1 session(s) to {target}" in capsys.readouterr().err


def test_export_never_overwrites(home: Settings, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target = tmp_path / "taken.json"
    target.write_text("keep me", encoding="utf-8")
    assert cli.main(["history", "export", "--output", str(target)]) == 1
    assert "already exists" in capsys.readouterr().err
    assert target.read_text(encoding="utf-8") == "keep me"


def test_export_format_is_checked() -> None:
    with pytest.raises(SystemExit):
        cli.main(["history", "export", "--format", "pdf"])
    with pytest.raises(SystemExit):
        cli.main(["history"])
