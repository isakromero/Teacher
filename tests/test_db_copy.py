"""`teacher db copy` (`teacher.db_copy`): copying every row and file of one database into the
other, empty one, with its refusals, the dry run, the verification and the rollback.

SQLite to SQLite runs everywhere; the Postgres round trips (SQLite -> Postgres -> SQLite), the
sequence reset, the NUL refusal and the command line against Postgres carry `postgres_only`.
The sample library has rows of every column type: JSON null next to SQL NULL, booleans,
datetimes with microseconds, bytes (with zero bytes), unicode and a large text, and
self-referencing foreign keys that point at a row with a higher id.
"""

from __future__ import annotations

import os
import sqlite3
import struct
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from sqlalchemy import JSON, insert, select, text, update
from sqlalchemy.pool import NullPool

from teacher import cli, db_copy
from teacher.config import Settings
from teacher.db.engine import dispose_engine, session_scope
from teacher.db.migrate import alembic_config, current_revision, head_revision, upgrade_to_head
from teacher.db.models import (
    ApiCall,
    Artifact,
    ArtifactBlock,
    BlockResponse,
    ConceptEmbedding,
    KnowledgeSource,
    SourceSegment,
    StudySession,
)
from teacher.db.providers import create_db_provider
from teacher.db_copy import DbCopyError, copied_tables, copy_database, plan_copy
from teacher.managers import ArtifactManager, ConceptManager
from tests.conftest import build_tag_library

postgres_only = pytest.mark.postgres_only("copies to and from the Postgres test database")

WHEN = datetime(2026, 3, 4, 5, 6, 7, 123456)
LARGE = ("Größe ✓ 漢字 🙂 " * 40_000) + "end"  # ~0.9 MB of unicode
VECTOR = struct.pack("<4f", 0.5, 0.0, -1.25, 0.0)  # holds zero bytes


# -- a sample library -----------------------------------------------------------------------


def sqlite_settings(root: Path) -> Settings:
    settings = Settings(root=root)
    upgrade_to_head(settings)
    return settings


def fill(settings: Settings, tmp_path: Path) -> None:
    """The tag library (sources, concepts, relations, a page with blocks) plus rows of every
    column type, and files in derived/ (a run record, a vector index, a lab runtime)."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    library = build_tag_library(settings, tmp_path)
    with session_scope(settings) as session:
        artifacts = ArtifactManager(session, settings)
        newer = artifacts.create(slug="newer", kind="study_page", title="Newer", generator="test")
        session.flush()
        session.execute(
            update(Artifact).where(Artifact.slug == library.page).values(supersedes_id=newer.id)
        )
        source_id = session.scalar(select(KnowledgeSource.id))
        session.execute(
            update(KnowledgeSource).where(KnowledgeSource.id == source_id).values(
                summary=LARGE, creators=["Ada Lovelace", "Émile"], metadata_={"n": None}
            )
        )
        # A parent segment with a higher id than its child.
        session.execute(
            insert(SourceSegment),
            [
                {"id": 20, "source_id": source_id, "kind": "section", "label": "2",
                 "title": "Parent", "parent_id": None, "position": 1, "locator_type": "page",
                 "locator": {"page": 2}},
                {"id": 10, "source_id": source_id, "kind": "section", "label": "2.1",
                 "title": None, "parent_id": 20, "position": 2, "locator_type": "page",
                 "locator": {"page": 3, "nested": [1, None, True]}},
            ],
        )
        study = session.execute(
            insert(StudySession).values(
                started_at=WHEN, last_activity_at=WHEN.replace(microsecond=999999),
                backfilled=True,
            ).returning(StudySession.id)
        ).scalar_one()
        block_id = library.blocks["b02"]
        session.execute(
            insert(BlockResponse),
            [
                # response is the JSON null (a plain JSON column); details SQL NULL.
                {"block_id": block_id, "attempt": 1, "response": JSON.NULL, "passed": False,
                 "grader": "exact", "details": None, "missing": [], "created_at": WHEN,
                 "session_id": study},
                # details is the JSON null (a none_as_null column).
                {"block_id": block_id, "attempt": 2, "response": {"text": "naïve ✓  "},
                 "passed": True, "grader": "llm", "details": JSON.NULL, "missing": [],
                 "feedback": "Ünïcode", "created_at": WHEN, "session_id": None},
                {"block_id": block_id, "attempt": 3, "response": 0, "passed": True,
                 "grader": "llm", "details": {"input_tokens": 5, "score": 0.1},
                 "missing": [], "created_at": WHEN.replace(microsecond=1)},
            ],
        )
        concept_id = library.concepts["attention"]
        session.execute(
            insert(ConceptEmbedding).values(
                concept_id=concept_id, model="fake-embed", dimensions=4, vector=VECTOR,
                text_hash="abc", embedded_at=WHEN,
            )
        )
        session.execute(
            insert(ApiCall).values(
                created_at=WHEN, purpose="tutor", step="reply", provider="anthropic",
                model="claude-test", input_tokens=10, cache_write_tokens=0,
                cache_read_tokens=0, output_tokens=3, cost_usd=0.1 + 0.2,
                priced_on="2026-01-01", is_estimate=True, artifact_id=newer.id,
            )
        )
    derived = settings.derived_dir
    record = derived / "_concept_relations" / "attention.json"
    record.parent.mkdir(parents=True)
    record.write_text('{"calls": []}\n', encoding="utf-8")
    os.utime(record, (1_700_000_000, 1_700_000_000))
    (derived / "paper").mkdir()
    (derived / "paper" / "concepts.json").write_text("{}", encoding="utf-8")
    (derived / "_vector_index" / "x").mkdir(parents=True)
    (derived / "_vector_index" / "x" / "manifest.json").write_text("{}", encoding="utf-8")
    (derived / "pyodide" / "0.1").mkdir(parents=True)
    (derived / "pyodide" / "0.1" / "pyodide.js").write_text("//", encoding="utf-8")
    (settings.artifacts_dir / "newer" / "note.bin").write_bytes(b"\x00\x01binary")


def table_rows(settings: Settings) -> dict[str, list[Any]]:
    """Every row of every copied table as (type name, value) pairs plus the JSON columns'
    IS NULL flags, sorted in Python (collations differ between the databases)."""
    engine = create_db_provider(settings).create_engine(poolclass=NullPool)
    out: dict[str, list[Any]] = {}
    try:
        with engine.connect() as conn:
            if settings.db == "supabase":  # every digit of a float8, whatever the server says
                conn.exec_driver_sql("SET extra_float_digits = 3")
            for table in copied_tables():
                json_columns = [c for c in table.c if isinstance(c.type, JSON)]
                statement = select(*table.c, *[c.is_(None) for c in json_columns])
                rows = []
                for row in conn.execute(statement):
                    values = tuple((type(v).__name__, v) for v in row[: len(table.c)])
                    flags = tuple(bool(flag) for flag in row[len(table.c):])
                    rows.append((values, flags))
                out[table.name] = sorted(rows, key=repr)
    finally:
        engine.dispose()
    return out


def files_under(root: Path) -> dict[str, tuple[bytes, int]]:
    return {
        p.relative_to(root).as_posix(): (p.read_bytes(), int(p.stat().st_mtime))
        for p in root.rglob("*")
        if p.is_file()
    }


def quiet(_line: str) -> None:
    pass


@pytest.fixture
def source(tmp_path: Path) -> Settings:
    settings = sqlite_settings(tmp_path / "src")
    fill(settings, tmp_path / "upload")
    return settings


@pytest.fixture
def skip_rebuild(monkeypatch: pytest.MonkeyPatch) -> list[Settings]:
    """Replace the final rebuild step with a recorder (to see whether it ran)."""
    calls: list[Settings] = []

    def record(settings: Settings, result: Any, out: Any) -> None:
        calls.append(settings)

    monkeypatch.setattr(db_copy, "rebuild_derived", record)
    return calls


def assert_copied_files(source: Settings, target: Settings) -> None:
    for name in ("knowledge_sources", "artifacts"):
        assert files_under(target.data_root / name) == files_under(source.data_root / name)
    derived = files_under(target.derived_dir)
    assert derived["_concept_relations/attention.json"] == (b'{"calls": []}\n', 1_700_000_000)
    assert "paper/concepts.json" in derived
    assert not any(name.startswith(("pyodide/", "_vector_index/x/")) for name in derived)


# -- SQLite to SQLite -----------------------------------------------------------------------


def test_copies_every_row_and_file_and_rebuilds_the_indexes(
    source: Settings, tmp_path: Path
) -> None:
    target = Settings(root=tmp_path / "dst")
    lines: list[str] = []
    result = copy_database(source, target, out=lines.append)

    assert table_rows(target) == table_rows(source)
    assert all(t.ok for t in result.tables)
    assert {t.name for t in result.tables} == {t.name for t in copied_tables()}
    assert sum(t.target_rows for t in result.tables) >= 25
    assert_copied_files(source, target)
    assert current_revision(target) == head_revision(target)
    # The final step: the search index and the vector index of the one embedding model.
    assert result.search_concepts == 4
    assert result.vector_indexes == ["fake-embed: 1 vector(s)"]
    assert list((target.derived_dir / "_vector_index").rglob("manifest.json"))
    with session_scope(target) as session:
        concepts = ConceptManager(session, target)
        assert [c.slug for c in concepts.search("attention")][:1] == ["attention"]
        assert concepts.check() == []
        made = concepts.create("brand new", "Made after the copy.", "paper")  # ids follow
        assert made.id > max(c.id for c in concepts.list() if c.id != made.id)
    text_out = "\n".join(lines)
    assert "Verification" in text_out and "DIFFERS" not in text_out
    assert "AUTOINCREMENT" in text_out  # SQLite: no sqlite_sequence to reset
    dispose_engine(target)


def test_the_dry_run_changes_nothing(source: Settings, tmp_path: Path) -> None:
    target = Settings(root=tmp_path / "dst")
    before = table_rows(source)
    plan = plan_copy(source, target)
    assert not (tmp_path / "dst").exists()
    assert table_rows(source) == before
    lines = "\n".join(plan.lines())
    assert "does not exist: it would be created" in lines
    assert dict(plan.tables)["block_responses"] == 3
    assert dict(plan.tables)["source_segments"] == 2
    actions = {e.path: e.action for e in plan.folders}
    assert actions == {
        "knowledge_sources": "copy",
        "artifacts": "copy",
        "derived/_concept_relations": "copy",
        "derived/_vector_index": "rebuild",
        "derived/paper": "copy",
        "derived/pyodide": "skip",
    }
    assert "artifacts.supersedes_id" in plan.deferred
    assert "source_segments.parent_id" in plan.deferred
    assert "teacher labs setup" in lines


def test_a_difference_rolls_everything_back(
    source: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A type-conversion bug (here: microseconds dropped on the way in) fails the verification,
    and the target is left empty: no row, no copied file."""
    real = db_copy._insert_params

    def lossy(spec: Any, mapping: Any) -> dict[str, Any]:
        params = real(spec, mapping)
        return {
            k: v.replace(microsecond=0) if isinstance(v, datetime) else v
            for k, v in params.items()
        }

    monkeypatch.setattr(db_copy, "_insert_params", lossy)
    target = Settings(root=tmp_path / "dst")
    lines: list[str] = []
    with pytest.raises(DbCopyError, match="differs from the source in: .*block_responses"):
        copy_database(source, target, out=lines.append)
    assert "DIFFERS" in "\n".join(lines)
    assert all(not rows for rows in table_rows(target).values())
    for name in ("knowledge_sources", "artifacts", "derived"):
        assert not any((target.data_root / name).iterdir())
    # The target is still a valid empty database: the copy can simply be run again.
    monkeypatch.setattr(db_copy, "_insert_params", real)
    copy_database(source, target, out=quiet)
    assert table_rows(target) == table_rows(source)
    dispose_engine(target)


def test_a_failure_in_the_rebuild_step_says_the_copy_is_committed(
    source: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(settings: Settings) -> int:
        raise RuntimeError("no search tables")

    monkeypatch.setattr(db_copy, "rebuild_search_index", broken)
    target = Settings(root=tmp_path / "dst")
    with pytest.raises(DbCopyError, match="committed and verified.*db rebuild-search"):
        copy_database(source, target, out=quiet)
    assert table_rows(target) == table_rows(source)
    dispose_engine(target)


# -- refusals -------------------------------------------------------------------------------


def test_refuses_the_same_database(source: Settings) -> None:
    with pytest.raises(DbCopyError, match="the same database"):
        plan_copy(source, Settings(root=source.root))


def test_refuses_a_missing_source(tmp_path: Path) -> None:
    with pytest.raises(DbCopyError, match="does not exist"):
        plan_copy(Settings(root=tmp_path / "none"), Settings(root=tmp_path / "dst"))
    assert not (tmp_path / "none" / "teacher.db").exists()


def test_refuses_a_source_behind_head_and_does_not_migrate_it(tmp_path: Path) -> None:
    old = Settings(root=tmp_path / "old")
    command.upgrade(alembic_config(old), "0015")
    with pytest.raises(DbCopyError, match="at revision 0015.*never migrates the source.*serve"):
        plan_copy(old, Settings(root=tmp_path / "dst"))
    assert current_revision(old) == "0015"


def test_refuses_a_target_with_rows_and_names_the_tables(
    source: Settings, tmp_path: Path
) -> None:
    target = sqlite_settings(tmp_path / "dst")
    with session_scope(target) as session:
        session.add(StudySession(started_at=WHEN, last_activity_at=WHEN))
        ConceptManager(session, target).create("x", "y", "paper")
    for check in (plan_copy, lambda s, t: copy_database(s, t, out=quiet)):
        with pytest.raises(DbCopyError, match=r"not empty: .*concepts \(1\).*study_sessions \(1\)"):
            check(source, target)
    assert not any(target.sources_dir.iterdir())
    dispose_engine(target)


@pytest.mark.parametrize("folder", ["knowledge_sources", "artifacts", "derived"])
def test_refuses_target_folders_with_files(
    source: Settings, tmp_path: Path, folder: str
) -> None:
    target = Settings(root=tmp_path / "dst")
    (target.data_root / folder / "something").mkdir(parents=True)
    with pytest.raises(DbCopyError, match=f"{folder} is not empty"):
        plan_copy(source, target)


def test_a_lab_runtime_may_stay_in_the_target(source: Settings, tmp_path: Path) -> None:
    target = Settings(root=tmp_path / "dst")
    (target.derived_dir / "pyodide" / "0.1").mkdir(parents=True)
    plan_copy(source, target)  # no error


def test_refuses_a_source_with_broken_foreign_keys(source: Settings, tmp_path: Path) -> None:
    dispose_engine(source)
    conn = sqlite3.connect(source.db_path)
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("INSERT INTO artifact_sources (artifact_id, source_id) VALUES (999, 1)")
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(DbCopyError, match="1 row.*artifact_sources rowid .* -> artifacts"):
        plan_copy(source, Settings(root=tmp_path / "dst"))


def test_refuses_a_source_whose_files_are_not_there(source: Settings, tmp_path: Path) -> None:
    """E.g. a Supabase database whose files live on the server, not under this TEACHER_HOME."""
    import shutil

    shutil.rmtree(source.artifacts_dir / "newer")
    with pytest.raises(DbCopyError, match="1 row.*artifacts 'newer': no folder"):
        plan_copy(source, Settings(root=tmp_path / "dst"))


def test_refuses_a_table_it_does_not_know(source: Settings, tmp_path: Path) -> None:
    dispose_engine(source)
    conn = sqlite3.connect(source.db_path)
    try:
        conn.execute("CREATE TABLE notes_extra (id INTEGER PRIMARY KEY)")
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(DbCopyError, match="does not know: notes_extra"):
        plan_copy(source, Settings(root=tmp_path / "dst"))


def test_nul_characters_are_fine_between_sqlite_databases(
    source: Settings, tmp_path: Path
) -> None:
    with session_scope(source) as session:
        session.execute(update(ArtifactBlock).values(content_file="a\x00b"))
    target = Settings(root=tmp_path / "dst")
    copy_database(source, target, out=quiet)
    assert table_rows(target) == table_rows(source)
    dispose_engine(target)


# -- the command line -----------------------------------------------------------------------


def test_cli_refuses_the_same_kind_and_the_db_flag(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main(["db", "copy", "--from", "sqlite", "--to", "sqlite"]) == 1
    assert "both sqlite" in capsys.readouterr().err
    assert cli.main(["--db", "sqlite", "db", "copy", "--from", "sqlite", "--to", "supabase"]) == 1
    assert "--db is not used" in capsys.readouterr().err


def test_cli_needs_the_supabase_url(
    source: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("TEACHER_HOME", str(source.root))
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    assert cli.main(["db", "copy", "--from", "sqlite", "--to", "supabase", "--dry-run"]) == 1
    assert "SUPABASE_DB_URL is not set" in capsys.readouterr().err


def test_cli_rebuild_search(
    source: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("TEACHER_HOME", str(source.root))
    monkeypatch.setenv("TEACHER_DB", "sqlite")
    with session_scope(source) as session:
        session.execute(text("DELETE FROM concept_search"))
    assert cli.main(["db", "rebuild-search"]) == 0
    assert "(4 concept(s))" in capsys.readouterr().out
    with session_scope(source) as session:
        assert ConceptManager(session, source).check() == []


# -- Postgres -------------------------------------------------------------------------------


def raw_json(settings: Settings) -> list[tuple[Any, ...]]:
    engine = create_db_provider(settings).create_engine(poolclass=NullPool)
    try:
        with engine.connect() as conn:
            cast = "::text" if settings.db == "supabase" else ""
            return conn.execute(
                text(
                    f"SELECT attempt, response{cast}, details IS NULL, details{cast} "
                    "FROM block_responses ORDER BY attempt"
                )
            ).all()
    finally:
        engine.dispose()


@postgres_only
def test_round_trip_through_postgres(
    source: Settings,
    empty_postgres_settings: Settings,
    tmp_path: Path,
) -> None:
    pg = empty_postgres_settings
    lines: list[str] = []
    result = copy_database(source, pg, out=lines.append)
    assert all(t.ok for t in result.tables)
    expected = table_rows(source)
    assert table_rows(pg) == expected
    assert_copied_files(source, pg)
    assert pg.data_root == pg.root / "supabase"
    # The final step ran on Postgres: concept search and the vector index work.
    assert result.search_concepts == 4
    assert result.vector_indexes == ["fake-embed: 1 vector(s)"]
    with session_scope(pg) as session:
        concepts = ConceptManager(session, pg)
        assert [c.slug for c in concepts.search("attention")][:1] == ["attention"]
        assert concepts.check() == []
    # JSON null and SQL NULL stay apart in Postgres itself.
    rows = raw_json(pg)
    assert rows[0] == (1, "null", True, None)
    assert rows[1][2:] == (False, "null")
    assert rows[2][1:3] == ("0", False)
    # Every sequence follows the copied ids: inserting works and takes the next id.
    assert any("sequence knowledge_sources_id_seq" in line for line in lines)
    with session_scope(pg) as session:
        top = session.scalar(select(StudySession.id).order_by(StudySession.id.desc()))
        added = StudySession(started_at=WHEN, last_activity_at=WHEN)
        session.add(added)
        session.flush()
        assert added.id == top + 1
        session.rollback()

    back = Settings(root=tmp_path / "back")
    copy_database(pg, back, out=quiet)
    assert table_rows(back) == expected
    assert raw_json(back) == raw_json(source)
    assert_copied_files(pg, back)
    dispose_engine(back)


@postgres_only
def test_dry_run_against_postgres_changes_nothing(
    source: Settings, empty_postgres_settings: Settings
) -> None:
    plan = plan_copy(source, empty_postgres_settings)
    assert "at revision none: it would be migrated" in "\n".join(plan.lines())
    assert current_revision(empty_postgres_settings) is None
    assert not empty_postgres_settings.data_root.exists()


@postgres_only
def test_refuses_nul_characters_for_postgres_and_names_them(
    source: Settings, empty_postgres_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    pg = empty_postgres_settings
    with session_scope(source) as session:
        block = session.scalar(select(ArtifactBlock.id).order_by(ArtifactBlock.id))
        session.execute(
            update(ArtifactBlock).where(ArtifactBlock.id == block).values(content_file="a\x00b")
        )
    pattern = rf"1 text value.*NUL.*artifact_blocks\.content_file at id={block}"
    # Found by the checks, before anything is changed.
    with pytest.raises(DbCopyError, match=pattern):
        plan_copy(source, pg)
    with pytest.raises(DbCopyError, match=pattern):
        copy_database(source, pg, out=quiet)
    assert current_revision(pg) is None
    assert not pg.data_root.exists()
    # And by the copy itself (a NUL written after the checks): rolled back, files removed.
    monkeypatch.setattr(db_copy, "_scan_nul", lambda conn, specs: [])
    with pytest.raises(DbCopyError, match=pattern):
        copy_database(source, pg, out=quiet)
    assert current_revision(pg) == head_revision(pg)
    assert all(not rows for rows in table_rows(pg).values())
    assert not any(pg.sources_dir.iterdir())


@postgres_only
def test_refuses_a_postgres_target_with_rows(
    source: Settings, empty_postgres_settings: Settings
) -> None:
    upgrade_to_head(empty_postgres_settings)
    with session_scope(empty_postgres_settings) as session:
        session.add(StudySession(started_at=WHEN, last_activity_at=WHEN))
    with pytest.raises(DbCopyError, match=r"not empty: .*study_sessions \(1\)"):
        plan_copy(source, empty_postgres_settings)


@postgres_only
def test_a_difference_rolls_back_postgres(
    source: Settings,
    empty_postgres_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    skip_rebuild: list[Settings],
) -> None:
    real = db_copy._insert_params

    def flip(spec: Any, mapping: Any) -> dict[str, Any]:
        params = real(spec, mapping)
        if spec.table.name == "study_sessions":
            params["backfilled"] = not params["backfilled"]
        return params

    monkeypatch.setattr(db_copy, "_insert_params", flip)
    with pytest.raises(DbCopyError, match="differs from the source in: study_sessions"):
        copy_database(source, empty_postgres_settings, out=quiet)
    assert all(not rows for rows in table_rows(empty_postgres_settings).values())
    assert skip_rebuild == []


@postgres_only
def test_cli_copies_sqlite_to_supabase_without_printing_the_url(
    tmp_path: Path,
    empty_postgres_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    pg = empty_postgres_settings
    local = sqlite_settings(pg.root)  # both databases share $TEACHER_HOME
    fill(local, tmp_path / "upload")
    monkeypatch.setenv("TEACHER_HOME", str(pg.root))
    monkeypatch.setenv("SUPABASE_DB_URL", pg.supabase_db_url or "")
    assert cli.main(["db", "copy", "--from", "sqlite", "--to", "supabase", "--dry-run"]) == 0
    assert cli.main(["db", "copy", "--from", "sqlite", "--to", "supabase"]) == 0
    printed = capsys.readouterr()
    password = create_db_provider(pg).url.password
    assert password and password not in printed.out + printed.err
    assert "Dry run: nothing was changed." in printed.out
    assert create_db_provider(pg).describe() in printed.out
    assert table_rows(pg) == table_rows(local)
    # And the other way, into a new local database: refused while the local one has rows.
    assert cli.main(["db", "copy", "--from", "supabase", "--to", "sqlite"]) == 1
    assert "is not empty" in capsys.readouterr().err
    dispose_engine(local)
