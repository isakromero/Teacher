"""files.py helpers and Settings."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from teacher.config import Settings
from teacher.files import (
    sha256_bytes,
    sha256_file,
    slugify,
    to_absolute,
    to_relative,
    validate_slug,
)


def test_sha256(tmp_path: Path) -> None:
    data = b"hello world\n" * 100_000
    f = tmp_path / "x.bin"
    f.write_bytes(data)
    expected = hashlib.sha256(data).hexdigest()
    assert sha256_bytes(data) == expected
    assert sha256_file(f) == expected


@pytest.mark.parametrize("slug", ["a", "attention-is-all-you-need", "gpt2", "v1-2-3"])
def test_valid_slugs(slug: str) -> None:
    assert validate_slug(slug) == slug


@pytest.mark.parametrize(
    "slug",
    ["", "A", "-a", "a-", "a--b", "a_b", "a b", "../x", "a/b", "ä", "a\n", 5, None],
)
def test_invalid_slugs(slug: object) -> None:
    with pytest.raises(ValueError):
        validate_slug(slug)  # type: ignore[arg-type]


def test_relative_paths(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    p = root / "knowledge_sources" / "s" / "source.pdf"
    assert to_relative(p, root) == "knowledge_sources/s/source.pdf"
    assert to_relative(Path("derived/s/x.txt"), root) == "derived/s/x.txt"
    assert to_absolute("knowledge_sources/s/source.pdf", root) == p.resolve()
    with pytest.raises(ValueError):
        to_relative(tmp_path / "elsewhere.txt", root)
    with pytest.raises(ValueError):
        to_absolute("../escape", root)
    with pytest.raises(ValueError):
        to_absolute("/etc/passwd", root)


def test_settings_paths(tmp_path: Path) -> None:
    s = Settings(root=tmp_path)
    root = tmp_path.resolve()
    assert s.root == root
    assert s.db_path == root / "teacher.db"
    assert s.sources_dir == root / "knowledge_sources"
    assert s.artifacts_dir == root / "artifacts"
    assert s.derived_dir == root / "derived"
    assert s.backups_dir == root / "backups"
    assert s.db_url == f"sqlite:///{root}/teacher.db"
    with pytest.raises(AttributeError):
        s.root = tmp_path  # type: ignore[misc]


def test_settings_from_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEACHER_HOME", str(tmp_path / "home"))
    assert Settings.from_env().root == (tmp_path / "home").resolve()
    monkeypatch.delenv("TEACHER_HOME")
    monkeypatch.chdir(tmp_path)
    assert Settings.from_env().root == tmp_path.resolve()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Attention Is All You Need", "attention-is-all-you-need"),
        ("  Café -- Notes!  ", "cafe-notes"),
        ("GPT-2: Language Models", "gpt-2-language-models"),
        ("a_b/c", "a-b-c"),
    ],
)
def test_slugify(text: str, expected: str) -> None:
    assert slugify(text) == expected


def test_slugify_truncates_at_a_hyphen() -> None:
    slug = slugify("word " * 40, max_length=12)
    assert slug == "word-word"
    assert validate_slug(slugify("x" * 200)) == "x" * 80


@pytest.mark.parametrize("text", ["", "   ", "!!!", "日本語"])
def test_slugify_rejects_unusable_text(text: str) -> None:
    with pytest.raises(ValueError):
        slugify(text)
