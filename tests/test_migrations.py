"""Migration chain: creation from empty, idempotency, backups, cwd independence, model parity,
and each revision's upgrade/downgrade with existing data."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Index, UniqueConstraint
from sqlalchemy.exc import OperationalError

from teacher.config import Settings
from teacher.db.base import Base
from teacher.db.engine import dispose_engine
from teacher.db import fts
from teacher.db.fts import (
    ConceptSearchUnavailableError,
    fts_errors,
    is_concept_search_table,
    require_concept_search,
    require_fts5,
    require_trigram,
)
from teacher.db.migrate import (
    alembic_config,
    current_revision,
    head_revision,
    upgrade_to_head,
)

TABLES_0010 = {
    "knowledge_sources",
    "knowledge_source_identifiers",
    "source_segments",
    "artifacts",
    "artifact_sources",
    "artifact_blocks",
    "block_segment_refs",
    "block_responses",
    "generation_runs",
    "chat_threads",
    "chat_messages",
    "glossary_terms",
    "concepts",
    "concept_aliases",
    "concept_relations",
    "concept_embeddings",
    "source_concepts",
    "concept_pair_judgements",
    "chat_blocks",
    "chat_block_responses",
    "lab_workspaces",
    "lab_runs",
    "study_sessions",
    "study_events",
    "attention_spans",
}
TAG_TABLES = {"block_concepts"}
REVIEW_TABLES = {"review_variants", "review_items", "review_attempts"}  # 0013
CITATION_TABLES = {"source_reference_extractions", "source_references"}  # 0015
LEDGER_TABLES = {"api_calls"}  # 0016
USER_TABLES = {"users", "user_sessions"}  # 0017
OFFLINE_TABLES = {"offline_attempts"}  # 0018
HIGHLIGHT_TABLES = {"highlights", "offline_review_answers"}  # 0019
TABLES = (
    TABLES_0010 | TAG_TABLES | REVIEW_TABLES | CITATION_TABLES | LEDGER_TABLES | USER_TABLES
    | OFFLINE_TABLES | HIGHLIGHT_TABLES
)
# The two FTS5 virtual tables (word and trigram index) and the shadow tables FTS5 keeps for them.
FTS_TABLES = {
    "concept_search",
    "concept_search_data",
    "concept_search_idx",
    "concept_search_content",
    "concept_search_docsize",
    "concept_search_config",
    "concept_trigram",
    "concept_trigram_data",
    "concept_trigram_idx",
    "concept_trigram_content",
    "concept_trigram_docsize",
    "concept_trigram_config",
}
CONCEPT_TABLES = {
    "concepts", "concept_aliases", "concept_relations", "concept_embeddings", "source_concepts",
    "concept_pair_judgements",
}
CHAT_BLOCK_TABLES = {"chat_blocks", "chat_block_responses"}
LAB_TABLES = {"lab_workspaces", "lab_runs"}
HISTORY_TABLES = {"study_sessions", "study_events", "attention_spans"}
ACTIVITY_TABLES = ("block_responses", "chat_block_responses", "chat_messages", "lab_runs")
HEAD = "0019"

# The chain's SQLite side: raw sqlite3, PRAGMAs, the file backup and FTS5. The Postgres side is
# in test_migrations_postgres.py.
pytestmark = pytest.mark.sqlite_only("checks the SQLite schema with raw sqlite3 and PRAGMAs")


@contextmanager
def _sqlite(path: Settings | Path) -> Iterator[sqlite3.Connection]:
    """Raw sqlite3 connection (no FK pragma); commits and closes on exit."""
    conn = sqlite3.connect(path.db_path if isinstance(path, Settings) else path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _tables(settings: Settings) -> set[str]:
    with _sqlite(settings) as conn:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {r[0] for r in rows}


def test_upgrade_from_empty_creates_all_tables_and_dirs(tmp_path: Path) -> None:
    settings = Settings(root=tmp_path / "fresh")
    assert not settings.root.exists()

    backup = upgrade_to_head(settings)

    assert backup is None
    assert _tables(settings) == TABLES | FTS_TABLES | {"alembic_version"}
    assert current_revision(settings) == head_revision(settings) == HEAD
    for d in (
        settings.sources_dir,
        settings.artifacts_dir,
        settings.derived_dir,
        settings.backups_dir,
    ):
        assert d.is_dir()
    assert list(settings.backups_dir.iterdir()) == []


def test_upgrade_is_idempotent(settings: Settings) -> None:
    before = _tables(settings)
    assert upgrade_to_head(settings) is None
    assert upgrade_to_head(settings) is None
    assert _tables(settings) == before
    assert list(settings.backups_dir.iterdir()) == []


def test_backup_made_when_migration_pending(tmp_path: Path) -> None:
    settings = Settings(root=tmp_path / "home")
    upgrade_to_head(settings)
    with _sqlite(settings) as conn:
        conn.execute("CREATE TABLE marker (x INTEGER)")
        conn.execute("INSERT INTO marker VALUES (42)")
    command.downgrade(alembic_config(settings), "base")
    assert current_revision(settings) is None

    backup = upgrade_to_head(settings)

    assert backup is not None and backup.exists()
    assert backup.parent == settings.backups_dir
    assert backup.name.startswith("teacher-") and backup.suffix == ".db"
    with _sqlite(backup) as conn:
        assert conn.execute("SELECT x FROM marker").fetchone() == (42,)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert not (TABLES & tables)  # the backup is the pre-migration state
    assert current_revision(settings) == HEAD
    assert TABLES <= _tables(settings)


def test_upgrade_independent_of_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    settings = Settings(root=tmp_path / "home")
    upgrade_to_head(settings)
    assert TABLES <= _tables(settings)
    assert list(elsewhere.iterdir()) == []


def test_downgrade_removes_everything(settings: Settings) -> None:
    dispose_engine(settings)
    command.downgrade(alembic_config(settings), "base")
    assert _tables(settings) == {"alembic_version"}


def test_autogenerate_finds_no_diff(settings: Settings) -> None:
    """Models and migration agree on tables, columns, types, server defaults, FKs, uniques, indexes."""
    command.check(alembic_config(settings))  # raises AutogenerateDiffsDetected on any diff


def _table_sql(settings: Settings) -> dict[str, str]:
    with _sqlite(settings) as conn:
        rows = conn.execute("SELECT name, sql FROM sqlite_master WHERE type='table'").fetchall()
    return {name: sql for name, sql in rows}


def test_named_constraints_match_models(settings: Settings) -> None:
    """Autogenerate does not compare CHECKs or constraint names on SQLite, so compare the DDL."""
    sql = _table_sql(settings)
    with _sqlite(settings) as conn:
        for table in Base.metadata.sorted_tables:
            ddl = sql[table.name]
            for c in table.constraints:
                if isinstance(c, CheckConstraint | UniqueConstraint | ForeignKeyConstraint):
                    assert f"CONSTRAINT {c.name} " in ddl, (table.name, c.name)
                if isinstance(c, CheckConstraint):
                    assert str(c.sqltext) in ddl, (table.name, c.name)
            assert f"CONSTRAINT pk_{table.name} PRIMARY KEY" in ddl

            fks = {
                (row[3], row[2], row[6])  # from-column, referred table, on_delete
                for row in conn.execute(f"PRAGMA foreign_key_list({table.name})")
            }
            expected_fks = {
                (fk.parent.name, fk.column.table.name, fk.ondelete) for fk in table.foreign_keys
            }
            assert fks == expected_fks, table.name

            db_indexes = {
                row[1]
                for row in conn.execute(f"PRAGMA index_list({table.name})")
                if row[3] == "c"  # created by CREATE INDEX, not autoindexes
            }
            assert db_indexes == {i.name for i in table.indexes if isinstance(i, Index)}

            defaults = {
                row[1]: row[4] for row in conn.execute(f"PRAGMA table_info({table.name})")
            }
            for col in table.columns:
                if col.server_default is None:
                    assert defaults[col.name] is None, (table.name, col.name)
                else:
                    assert defaults[col.name] is not None, (table.name, col.name)


def test_spec_constraints_present(settings: Settings) -> None:
    """Spot-check the exact constraints the spec names."""
    sql = _table_sql(settings)
    assert "CONSTRAINT ck_artifacts_status CHECK (status IN ('generating', 'ready', 'failed'))" in (
        sql["artifacts"]
    )
    assert (
        "CONSTRAINT ck_generation_runs_status CHECK (status IN ('running', 'succeeded', 'failed'))"
        in sql["generation_runs"]
    )
    for table, col in [
        ("knowledge_sources", "creators"),
        ("knowledge_sources", "metadata"),
        ("source_segments", "locator"),
        ("artifacts", "params"),
        ("block_responses", "response"),
        ("block_responses", "details"),
    ]:
        assert f"CONSTRAINT ck_{table}_{col}_json CHECK (json_valid({col}))" in sql[table]
    with _sqlite(settings) as conn:
        idx = {r[1] for r in conn.execute("PRAGMA index_list(knowledge_source_identifiers)")}
        defaults = {
            (t, r[1]): r[4]
            for t in ("knowledge_sources", "artifacts", "artifact_sources", "artifact_blocks",
                      "generation_runs", "block_responses")
            for r in conn.execute(f"PRAGMA table_info({t})")
        }
    assert "ix_knowledge_source_identifiers_source_id" in idx
    assert defaults[("knowledge_sources", "creators")] == "'[]'"
    assert defaults[("knowledge_sources", "metadata")] == "'{}'"
    assert defaults[("artifacts", "params")] == "'{}'"
    assert defaults[("artifacts", "status")] == "'generating'"
    assert defaults[("artifact_sources", "role")] == "'primary'"
    assert defaults[("artifact_blocks", "is_gate")] == "0"
    assert defaults[("generation_runs", "status")] == "'running'"
    for key in [
        ("knowledge_sources", "added_at"),
        ("knowledge_sources", "updated_at"),
        ("artifacts", "created_at"),
        ("block_responses", "created_at"),
        ("generation_runs", "started_at"),
    ]:
        assert "CURRENT_TIMESTAMP" in defaults[key]


def _insert_response_rows(db: Path) -> None:
    """One artifact, one gate block and two attempts, written with raw SQL (schema 0001)."""
    with _sqlite(db) as conn:
        conn.execute(
            "INSERT INTO artifacts (id, slug, kind, title, dir_path, generator) "
            "VALUES (1, 'a', 'study_page', 'A', 'artifacts/a', 'test')"
        )
        conn.execute(
            "INSERT INTO artifact_blocks (id, artifact_id, position, key, type, is_gate, "
            "content_file, content_sha256) VALUES (1, 1, 1, 'b01', 'blurb', 1, 'b01.json', 'x')"
        )
        conn.executemany(
            "INSERT INTO block_responses (block_id, attempt, response, passed, feedback, grader) "
            "VALUES (1, ?, ?, ?, ?, 'word-count-v1')",
            [(1, '{"text": "short"}', 0, "more"), (2, '{"text": "long enough"}', 1, "ok")],
        )


def test_upgrade_0001_to_0002_keeps_responses(tmp_path: Path) -> None:
    settings = Settings(root=tmp_path / "home")
    settings.root.mkdir(parents=True)
    command.upgrade(alembic_config(settings), "0001")
    _insert_response_rows(settings.db_path)

    command.upgrade(alembic_config(settings), "0002")
    assert current_revision(settings) == "0002"
    with _sqlite(settings) as conn:
        rows = conn.execute(
            "SELECT attempt, response, passed, feedback, grader, details, details IS NULL "
            "FROM block_responses ORDER BY attempt"
        ).fetchall()
        assert rows == [
            (1, '{"text": "short"}', 0, "more", "word-count-v1", None, 1),
            (2, '{"text": "long enough"}', 1, "ok", "word-count-v1", None, 1),
        ]
        conn.execute(
            "INSERT INTO block_responses (block_id, attempt, response, passed, grader, details) "
            "VALUES (1, 3, '{}', 1, 'llm-judge-v1', '{\"score\": 1.0}')"
        )
        with pytest.raises(sqlite3.IntegrityError, match="ck_block_responses_details_json"):
            conn.execute(
                "INSERT INTO block_responses (block_id, attempt, response, passed, grader, "
                "details) VALUES (1, 4, '{}', 1, 'j', 'not json')"
            )
        fk_problems = conn.execute("PRAGMA foreign_key_check(block_responses)").fetchall()
    assert fk_problems == []


def test_downgrade_0002_to_0001_keeps_responses(settings: Settings) -> None:
    dispose_engine(settings)
    _insert_response_rows(settings.db_path)
    command.downgrade(alembic_config(settings), "0001")
    with _sqlite(settings) as conn:
        columns = {r[1] for r in conn.execute("PRAGMA table_info(block_responses)")}
        count = conn.execute("SELECT count(*) FROM block_responses").fetchone()[0]
    assert "details" not in columns and count == 2
    assert current_revision(settings) == "0001"


def test_upgrade_0002_to_0003_adds_chat_and_keeps_data(tmp_path: Path) -> None:
    settings = Settings(root=tmp_path / "home")
    settings.root.mkdir(parents=True)
    command.upgrade(alembic_config(settings), "0002")
    _insert_response_rows(settings.db_path)
    with _sqlite(settings) as conn:
        conn.execute(
            "UPDATE block_responses SET details = '{\"score\": 0.5}' WHERE attempt = 2"
        )

    backup = upgrade_to_head(settings)

    assert backup is not None and current_revision(settings) == HEAD
    with _sqlite(backup) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "chat_threads" not in tables  # the backup is the 0002 state
    with _sqlite(settings) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        rows = conn.execute(
            "SELECT attempt, response, passed, details FROM block_responses ORDER BY attempt"
        ).fetchall()
        assert rows == [
            (1, '{"text": "short"}', 0, None),
            (2, '{"text": "long enough"}', 1, '{"score": 0.5}'),
        ]
        conn.execute("INSERT INTO chat_threads (id, artifact_id) VALUES (1, 1)")
        conn.execute(
            "INSERT INTO chat_messages (thread_id, position, role, text, api_content) "
            "VALUES (1, 1, 'user', 'hi', '[{\"type\": \"text\", \"text\": \"hi\"}]')"
        )
        row = conn.execute(
            'SELECT kind, "references", context_blocks, created_at IS NOT NULL '
            "FROM chat_messages"
        ).fetchone()
        assert row == ("message", "[]", "[]", 1)
        with pytest.raises(sqlite3.IntegrityError, match="ck_chat_messages_role"):
            conn.execute(
                "INSERT INTO chat_messages (thread_id, position, role, text, api_content) "
                "VALUES (1, 2, 'robot', 'x', '[]')"
            )
        with pytest.raises(sqlite3.IntegrityError, match="ck_chat_messages_api_content_json"):
            conn.execute(
                "INSERT INTO chat_messages (thread_id, position, role, text, api_content) "
                "VALUES (1, 2, 'user', 'x', 'nope')"
            )
        conn.execute("DELETE FROM artifacts WHERE id = 1")
        assert conn.execute("SELECT count(*) FROM chat_threads").fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM chat_messages").fetchone() == (0,)


def test_downgrade_0003_to_0002_drops_only_chat(settings: Settings) -> None:
    dispose_engine(settings)
    _insert_response_rows(settings.db_path)
    with _sqlite(settings) as conn:
        conn.execute("INSERT INTO chat_threads (artifact_id) VALUES (1)")
    command.downgrade(alembic_config(settings), "0002")
    assert _tables(settings) == (
        TABLES_0010 - HISTORY_TABLES - LAB_TABLES - CHAT_BLOCK_TABLES - {"chat_threads", "chat_messages", "glossary_terms"}
        - CONCEPT_TABLES
    ) | {"alembic_version"}
    with _sqlite(settings) as conn:
        assert conn.execute("SELECT count(*) FROM block_responses").fetchone() == (2,)
    assert current_revision(settings) == "0002"


def test_upgrade_0003_to_0004_adds_glossary_and_keeps_data(tmp_path: Path) -> None:
    settings = Settings(root=tmp_path / "home")
    settings.root.mkdir(parents=True)
    command.upgrade(alembic_config(settings), "0003")
    _insert_response_rows(settings.db_path)
    with _sqlite(settings) as conn:
        conn.execute("INSERT INTO chat_threads (id, artifact_id) VALUES (1, 1)")
        conn.execute(
            "INSERT INTO chat_messages (thread_id, position, role, text, api_content) "
            "VALUES (1, 1, 'user', 'hi', '[]')"
        )

    backup = upgrade_to_head(settings)

    assert backup is not None and current_revision(settings) == HEAD
    with _sqlite(backup) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        columns = {r[1] for r in conn.execute("PRAGMA table_info(artifacts)")}
    assert "glossary_terms" not in tables and "glossary_built_at" not in columns  # 0003 state
    with _sqlite(settings) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        # Existing rows are kept, and no existing artifact has a glossary yet.
        assert conn.execute(
            "SELECT slug, title, status, glossary_built_at FROM artifacts"
        ).fetchall() == [("a", "A", "generating", None)]
        assert conn.execute("SELECT count(*) FROM block_responses").fetchone() == (2,)
        assert conn.execute("SELECT count(*) FROM chat_messages").fetchone() == (1,)
        conn.execute(
            "INSERT INTO glossary_terms (artifact_id, position, term, kind, definition) "
            "VALUES (1, 1, 'GAN', 'acronym', 'A pair of networks.')"
        )
        row = conn.execute("SELECT aliases, expansion FROM glossary_terms").fetchone()
        assert row == ("[]", None)
        with pytest.raises(sqlite3.IntegrityError, match="ck_glossary_terms_kind"):
            conn.execute(
                "INSERT INTO glossary_terms (artifact_id, position, term, kind, definition) "
                "VALUES (1, 2, 'x', 'word', 'd')"
            )
        with pytest.raises(sqlite3.IntegrityError, match="ck_glossary_terms_aliases_json"):
            conn.execute(
                "INSERT INTO glossary_terms (artifact_id, position, term, kind, definition, "
                "aliases) VALUES (1, 2, 'x', 'term', 'd', 'nope')"
            )
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            conn.execute(
                "INSERT INTO glossary_terms (artifact_id, position, term, kind, definition) "
                "VALUES (1, 2, 'GAN', 'acronym', 'again')"
            )
        conn.execute("DELETE FROM artifacts WHERE id = 1")
        assert conn.execute("SELECT count(*) FROM glossary_terms").fetchone() == (0,)


def test_downgrade_0004_to_0003_drops_only_glossary(settings: Settings) -> None:
    dispose_engine(settings)
    _insert_response_rows(settings.db_path)
    with _sqlite(settings) as conn:
        conn.execute("UPDATE artifacts SET glossary_built_at = CURRENT_TIMESTAMP")
        conn.execute(
            "INSERT INTO glossary_terms (artifact_id, position, term, kind, definition) "
            "VALUES (1, 1, 'GAN', 'acronym', 'd')"
        )
    command.downgrade(alembic_config(settings), "0003")
    assert _tables(settings) == (
        TABLES_0010 - HISTORY_TABLES - LAB_TABLES - CHAT_BLOCK_TABLES - {"glossary_terms"} - CONCEPT_TABLES
    ) | {"alembic_version"}
    with _sqlite(settings) as conn:
        columns = {r[1] for r in conn.execute("PRAGMA table_info(artifacts)")}
        assert "glossary_built_at" not in columns
        assert conn.execute("SELECT slug FROM artifacts").fetchall() == [("a",)]
        assert conn.execute("SELECT count(*) FROM block_responses").fetchone() == (2,)
    assert current_revision(settings) == "0003"


def _insert_glossary_rows(db: Path) -> None:
    """Rows of every 0004 table group: a response history, a chat message and a glossary term."""
    _insert_response_rows(db)
    with _sqlite(db) as conn:
        conn.execute("UPDATE artifacts SET glossary_built_at = '2026-11-01 10:00:00'")
        conn.execute("INSERT INTO chat_threads (id, artifact_id) VALUES (1, 1)")
        conn.execute(
            "INSERT INTO chat_messages (thread_id, position, role, text, api_content) "
            "VALUES (1, 1, 'user', 'hi', '[]')"
        )
        conn.execute(
            "INSERT INTO glossary_terms (artifact_id, position, term, kind, definition) "
            "VALUES (1, 1, 'GAN', 'acronym', 'A pair of networks.')"
        )


def test_upgrade_0004_to_0005_adds_the_concept_graph_and_keeps_data(tmp_path: Path) -> None:
    settings = Settings(root=tmp_path / "home")
    settings.root.mkdir(parents=True)
    command.upgrade(alembic_config(settings), "0004")
    _insert_glossary_rows(settings.db_path)

    backup = upgrade_to_head(settings)

    assert backup is not None and current_revision(settings) == HEAD
    with _sqlite(backup) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert not (tables & (CONCEPT_TABLES | FTS_TABLES))  # the backup is the 0004 state
    assert _tables(settings) == TABLES | FTS_TABLES | {"alembic_version"}
    with _sqlite(settings) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        assert conn.execute(
            "SELECT slug, glossary_built_at FROM artifacts"
        ).fetchall() == [("a", "2026-11-01 10:00:00")]
        assert conn.execute("SELECT count(*) FROM block_responses").fetchone() == (2,)
        assert conn.execute("SELECT count(*) FROM chat_messages").fetchone() == (1,)
        assert conn.execute("SELECT term FROM glossary_terms").fetchall() == [("GAN",)]
        assert conn.execute("SELECT count(*) FROM concept_search").fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM concept_trigram").fetchone() == (0,)

        conn.executemany(
            "INSERT INTO concepts (id, slug, name, name_norm, summary, origin) "
            "VALUES (?, ?, ?, ?, 'S.', 'paper')",
            [(1, "a", "A", "a"), (2, "b", "B", "b")],
        )
        row = conn.execute(
            "SELECT reviewed_at, created_at IS NOT NULL, updated_at IS NOT NULL FROM concepts "
            "WHERE id = 1"
        ).fetchone()
        assert row == (None, 1, 1)
        with pytest.raises(sqlite3.IntegrityError, match="ck_concepts_origin"):
            conn.execute(
                "INSERT INTO concepts (slug, name, name_norm, summary, origin) "
                "VALUES ('c', 'C', 'c', 'S.', 'robot')"
            )
        with pytest.raises(sqlite3.IntegrityError, match="concepts.slug is immutable"):
            conn.execute("UPDATE concepts SET slug = 'z' WHERE id = 1")
        conn.execute("UPDATE concepts SET name = 'A2' WHERE id = 1")  # other columns may change

        conn.execute("INSERT INTO concept_aliases (concept_id, alias, alias_norm) VALUES (1, 'X', 'x')")
        conn.execute("INSERT INTO concept_aliases (concept_id, alias, alias_norm) VALUES (2, 'X', 'x')")
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            conn.execute(
                "INSERT INTO concept_aliases (concept_id, alias, alias_norm) VALUES (1, 'x', 'x')"
            )

        insert_relation = (
            "INSERT INTO concept_relations (type, from_id, to_id, low_id, high_id, rationale, "
            "origin) VALUES (?, ?, ?, ?, ?, 'r', 'user')"
        )
        conn.execute(insert_relation, ("builds_on", 2, 1, 1, 2))
        for values, constraint in [
            (("relates_to", 1, 2, 1, 2), "UNIQUE"),
            (("relates_to", 2, 1, 1, 2), "ck_concept_relations_symmetric_order"),
            (("builds_on", 1, 1, 1, 1), "ck_concept_relations_not_self"),
            (("builds_on", 1, 2, 2, 1), "ck_concept_relations_pair"),
            (("parent_of", 1, 2, 1, 2), "ck_concept_relations_type"),
        ]:
            with pytest.raises(sqlite3.IntegrityError, match=constraint):
                conn.execute(insert_relation, values)

        conn.execute(
            "INSERT INTO concept_embeddings (concept_id, model, dimensions, vector, text_hash) "
            "VALUES (1, 'm', 1, x'0000803f', 'h')"
        )
        with pytest.raises(sqlite3.IntegrityError, match="ck_concept_embeddings_vector_size"):
            conn.execute(
                "INSERT INTO concept_embeddings (concept_id, model, dimensions, vector, text_hash) "
                "VALUES (2, 'm', 2, x'0000803f', 'h')"
            )

        conn.execute(
            "INSERT INTO concept_search (rowid, name, aliases, summary) "
            "VALUES (1, 'Café attention', 'X', 'S.')"
        )
        assert conn.execute(
            "SELECT rowid FROM concept_search WHERE concept_search MATCH '\"cafe\"'"
        ).fetchall() == [(1,)]
        assert conn.execute(  # served by the prefix index
            "SELECT rowid FROM concept_search WHERE concept_search MATCH '\"att\"*'"
        ).fetchall() == [(1,)]
        conn.execute(
            "INSERT INTO concept_trigram (rowid, text, compact, label) "
            "VALUES (65536, 'cafe attention', 'cafeattention', 'Café attention')"
        )
        for match in ('"ttent"', '"feat"', '"CAFE"'):  # substrings; no case folding of its own
            found = conn.execute(
                "SELECT rowid, label FROM concept_trigram WHERE concept_trigram MATCH ?", (match,)
            ).fetchall()
            assert found == ([] if match == '"CAFE"' else [(65536, "Café attention")]), match

        conn.execute("DELETE FROM concepts WHERE id = 1")  # cascades to everything of concept 1
        assert conn.execute("SELECT concept_id FROM concept_aliases").fetchall() == [(2,)]
        assert conn.execute("SELECT count(*) FROM concept_relations").fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM concept_embeddings").fetchone() == (0,)


def test_downgrade_0005_to_0004_drops_only_the_concept_graph(settings: Settings) -> None:
    dispose_engine(settings)
    _insert_glossary_rows(settings.db_path)
    with _sqlite(settings) as conn:
        conn.execute(
            "INSERT INTO concepts (id, slug, name, name_norm, summary, origin) "
            "VALUES (1, 'a', 'A', 'a', 'S.', 'user')"
        )
        conn.execute("INSERT INTO concept_search (rowid, name, aliases, summary) VALUES (1, 'A', '', 'S.')")
        conn.execute("INSERT INTO concept_trigram (rowid, text, compact, label) VALUES (65536, 'a', 'a', 'A')")

    command.downgrade(alembic_config(settings), "0004")  # through 0005, so links go too

    assert current_revision(settings) == "0004"
    assert _tables(settings) == (TABLES_0010 - HISTORY_TABLES - LAB_TABLES - CHAT_BLOCK_TABLES - CONCEPT_TABLES) | {
        "alembic_version"
    }
    with _sqlite(settings) as conn:
        triggers = conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'").fetchall()
        assert triggers == []
        assert conn.execute("SELECT term FROM glossary_terms").fetchall() == [("GAN",)]
        assert conn.execute("SELECT count(*) FROM chat_messages").fetchone() == (1,)

    upgrade_to_head(settings)  # and back up again
    assert current_revision(settings) == HEAD
    assert _tables(settings) == TABLES | FTS_TABLES | {"alembic_version"}


def test_concept_search_ddl_and_slug_trigger(settings: Settings) -> None:
    sql = _table_sql(settings)
    assert sql["concept_search"] == (
        "CREATE VIRTUAL TABLE concept_search USING fts5("
        "name, aliases, summary, tokenize = 'unicode61 remove_diacritics 2', prefix = '2 3 4')"
    )
    assert sql["concept_trigram"] == (
        "CREATE VIRTUAL TABLE concept_trigram USING fts5("
        "text, compact, label UNINDEXED, tokenize = 'trigram case_sensitive 1')"
    )
    with _sqlite(settings) as conn:
        triggers = conn.execute(
            "SELECT name, tbl_name FROM sqlite_master WHERE type = 'trigger'"
        ).fetchall()
    assert triggers == [("trg_concepts_slug_immutable", "concepts")]
    assert "CONSTRAINT uq_concept_relations_low_id_high_id UNIQUE (low_id, high_id)" in (
        sql["concept_relations"]
    )


def test_only_the_fts_tables_are_hidden_from_autogenerate() -> None:
    assert {name for name in FTS_TABLES | TABLES if is_concept_search_table(name)} == FTS_TABLES
    assert not is_concept_search_table("concept_search_extra")
    assert not is_concept_search_table("concept_trigram_extra")
    assert not is_concept_search_table(None)


class _NoFts5Connection:
    def exec_driver_sql(self, sql: str) -> None:
        raise OperationalError(sql, {}, sqlite3.OperationalError("no such module: fts5"))


class _OtherErrorConnection:
    def exec_driver_sql(self, sql: str) -> None:
        raise OperationalError(sql, {}, sqlite3.OperationalError("disk I/O error"))


def test_require_fts5_fails_clearly_without_the_module() -> None:
    with pytest.raises(ConceptSearchUnavailableError, match=r"has no FTS5 module"):
        require_fts5(_NoFts5Connection())
    with pytest.raises(OperationalError, match="disk I/O error"):
        require_fts5(_OtherErrorConnection())


class _NoTrigramConnection:
    """A SQLite with FTS5 but older than 3.34: no trigram tokenizer."""

    def __init__(self) -> None:
        self.statements: list[str] = []

    def exec_driver_sql(self, sql: str) -> None:
        self.statements.append(sql)
        if "trigram" in sql:
            raise OperationalError(sql, {}, sqlite3.OperationalError("no such tokenizer: trigram"))


def test_require_trigram_fails_clearly_without_the_tokenizer() -> None:
    with pytest.raises(ConceptSearchUnavailableError, match=r"no FTS5 trigram tokenizer.*3\.34"):
        require_trigram(_NoTrigramConnection())
    with pytest.raises(OperationalError, match="disk I/O error"):
        require_trigram(_OtherErrorConnection())
    connection = _NoTrigramConnection()
    with pytest.raises(ConceptSearchUnavailableError, match="trigram"):
        require_concept_search(connection)
    assert len(connection.statements) == 3  # the FTS5 probe (create, drop), then the trigram one
    with pytest.raises(ConceptSearchUnavailableError, match="has no FTS5 module"):
        require_concept_search(_NoFts5Connection())


def test_fts_errors_translates_a_missing_trigram_tokenizer_at_use() -> None:
    with pytest.raises(ConceptSearchUnavailableError, match="no FTS5 trigram tokenizer"):
        with fts_errors():
            raise OperationalError("SELECT", {}, sqlite3.OperationalError("no such tokenizer: trigram"))
    with pytest.raises(ConceptSearchUnavailableError, match="concept_trigram trigram table is missing"):
        with fts_errors():
            raise OperationalError("SELECT", {}, sqlite3.OperationalError("no such table: concept_trigram"))


def test_upgrade_without_the_trigram_tokenizer_fails_before_changing_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(root=tmp_path / "home")
    settings.root.mkdir(parents=True)
    command.upgrade(alembic_config(settings), "0004")
    real = fts.require_trigram
    monkeypatch.setattr(fts, "require_trigram", lambda _connection: real(_NoTrigramConnection()))

    with pytest.raises(ConceptSearchUnavailableError, match="no FTS5 trigram tokenizer"):
        upgrade_to_head(settings)

    assert current_revision(settings) == "0004"
    assert not (_tables(settings) & (CONCEPT_TABLES | FTS_TABLES))


def test_upgrade_0005_to_0006_adds_source_concepts_and_keeps_data(tmp_path: Path) -> None:
    settings = Settings(root=tmp_path / "home")
    settings.root.mkdir(parents=True)
    command.upgrade(alembic_config(settings), "0005")
    _insert_glossary_rows(settings.db_path)
    with _sqlite(settings) as conn:
        conn.execute(
            "INSERT INTO knowledge_sources (id, slug, kind, title, dir_path, primary_file, "
            "media_type, content_sha256) VALUES (1, 'paper', 'paper', 'P', 'knowledge_sources/"
            "paper', 'paper.txt', 'text/plain', 'h')"
        )
        conn.execute(
            "INSERT INTO concepts (id, slug, name, name_norm, summary, origin) "
            "VALUES (1, 'a', 'A', 'a', 'S.', 'paper')"
        )

    backup = upgrade_to_head(settings)

    assert backup is not None and current_revision(settings) == HEAD
    with _sqlite(backup) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "source_concepts" not in tables and "concepts" in tables  # the 0005 state
    with _sqlite(settings) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        assert conn.execute("SELECT slug FROM concepts").fetchall() == [("a",)]
        assert conn.execute("SELECT term FROM glossary_terms").fetchall() == [("GAN",)]
        assert conn.execute("SELECT count(*) FROM source_concepts").fetchone() == (0,)
        insert = (
            "INSERT INTO source_concepts (source_id, concept_id, role, resolution, "
            "candidate_name, judge_reason) VALUES (?, ?, ?, ?, 'A', ?)"
        )
        conn.execute(insert, (1, 1, "uses", "judged", "Same idea."))
        row = conn.execute(
            "SELECT evidence, created_at IS NOT NULL, updated_at IS NOT NULL FROM source_concepts"
        ).fetchone()
        assert row == (None, 1, 1)
        for values, constraint in [
            ((1, 1, "uses", "exact", None), "UNIQUE"),
            ((1, 1, "mentions", "exact", None), "ck_source_concepts_role"),
            ((1, 1, "uses", "guessed", None), "ck_source_concepts_resolution"),
            ((1, 1, "uses", "judged", None), "ck_source_concepts_judge_reason"),
            ((1, 1, "uses", "created", "why"), "ck_source_concepts_judge_reason"),
            ((1, 9, "uses", "exact", None), "FOREIGN KEY"),
        ]:
            with pytest.raises(sqlite3.IntegrityError, match=constraint):
                conn.execute(insert, values)
        conn.execute("DELETE FROM concepts WHERE id = 1")  # cascades to the link
        assert conn.execute("SELECT count(*) FROM source_concepts").fetchone() == (0,)
        conn.execute(
            "INSERT INTO concepts (id, slug, name, name_norm, summary, origin) "
            "VALUES (2, 'b', 'B', 'b', 'S.', 'paper')"
        )
        conn.execute(insert, (1, 2, "assumes", "exact", None))
        conn.execute("DELETE FROM knowledge_sources WHERE id = 1")  # so does the source
        assert conn.execute("SELECT count(*) FROM source_concepts").fetchone() == (0,)
        assert conn.execute("SELECT slug FROM concepts").fetchall() == [("b",)]


def test_downgrade_0006_to_0005_drops_only_source_concepts(settings: Settings) -> None:
    dispose_engine(settings)
    _insert_glossary_rows(settings.db_path)
    with _sqlite(settings) as conn:
        conn.execute(
            "INSERT INTO knowledge_sources (id, slug, kind, title, dir_path, primary_file, "
            "media_type, content_sha256) VALUES (1, 'paper', 'paper', 'P', 'knowledge_sources/"
            "paper', 'paper.txt', 'text/plain', 'h')"
        )
        conn.execute(
            "INSERT INTO concepts (id, slug, name, name_norm, summary, origin) "
            "VALUES (1, 'a', 'A', 'a', 'S.', 'paper')"
        )
        conn.execute(
            "INSERT INTO source_concepts (source_id, concept_id, role, resolution, "
            "candidate_name) VALUES (1, 1, 'uses', 'created', 'A')"
        )

    command.downgrade(alembic_config(settings), "0005")

    assert current_revision(settings) == "0005"
    assert _tables(settings) == (
        (TABLES_0010 - HISTORY_TABLES - LAB_TABLES - CHAT_BLOCK_TABLES - {"source_concepts", "concept_pair_judgements"})
        | FTS_TABLES
        | {"alembic_version"}
    )
    with _sqlite(settings) as conn:
        assert conn.execute("SELECT slug FROM concepts").fetchall() == [("a",)]
        assert conn.execute("SELECT slug FROM knowledge_sources").fetchall() == [("paper",)]
        indexes = conn.execute(
            "SELECT name FROM sqlite_master WHERE name = 'ix_source_concepts_concept_id'"
        ).fetchall()
        assert indexes == []

    upgrade_to_head(settings)  # and back up again
    assert current_revision(settings) == HEAD
    assert "source_concepts" in _tables(settings)


def _concepts_and_a_relation(conn: sqlite3.Connection) -> None:
    conn.executemany(
        "INSERT INTO concepts (id, slug, name, name_norm, summary, origin) "
        "VALUES (?, ?, ?, ?, 'S.', 'paper')",
        [(1, "a", "A", "a"), (2, "b", "B", "b"), (3, "c", "C", "c")],
    )
    conn.execute(
        "INSERT INTO concept_relations (id, type, from_id, to_id, low_id, high_id, rationale, "
        "origin) VALUES (1, 'relates_to', 1, 2, 1, 2, 'Why.', 'model')"
    )


def test_upgrade_0006_to_0007_adds_the_judgement_memo_and_keeps_data(tmp_path: Path) -> None:
    settings = Settings(root=tmp_path / "home")
    settings.root.mkdir(parents=True)
    command.upgrade(alembic_config(settings), "0006")
    with _sqlite(settings) as conn:
        _concepts_and_a_relation(conn)

    backup = upgrade_to_head(settings)

    assert backup is not None and current_revision(settings) == HEAD
    with _sqlite(backup) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "concept_pair_judgements" not in tables and "source_concepts" in tables
    with _sqlite(settings) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        assert conn.execute("SELECT count(*) FROM concept_relations").fetchone() == (1,)
        insert = (
            "INSERT INTO concept_pair_judgements (low_id, high_id, focus_id, low_text_sha256, "
            "high_text_sha256, found_by, similarity, verdict, outcome, rationale, refusal, "
            "relation_id, model) VALUES (?, ?, ?, 'h1', 'h2', ?, 0.5, ?, ?, 'Why.', ?, ?, 'm')"
        )
        conn.execute(insert, (1, 2, 1, '["embedding"]', "relates_to", "added", None, 1))
        conn.execute(insert, (1, 3, 3, '["proposal:builds_on"]', "none", "none", None, None))
        row = conn.execute(
            "SELECT judged_at IS NOT NULL FROM concept_pair_judgements WHERE high_id = 2"
        ).fetchone()
        assert row == (1,)
        for values, constraint in [
            ((1, 2, 1, "[]", "none", "none", None, None), "UNIQUE"),
            ((2, 1, 2, "[]", "none", "none", None, None), "ck_concept_pair_judgements_pair_order"),
            ((2, 3, 1, "[]", "none", "none", None, None), "ck_concept_pair_judgements_focus"),
            ((2, 3, 2, "[]", "same_as", "none", None, None), "ck_concept_pair_judgements_verdict"),
            ((2, 3, 2, "[]", "relates_to", "kept", None, None),
             "ck_concept_pair_judgements_outcome"),
            ((2, 3, 2, "[]", "relates_to", "none", None, None),
             "ck_concept_pair_judgements_none_outcome"),
            ((2, 3, 2, "[]", "relates_to", "refused", None, None),
             "ck_concept_pair_judgements_refusal"),
            ((2, 3, 2, "[]", "relates_to", "added", "why", None),
             "ck_concept_pair_judgements_refusal"),
            ((2, 3, 2, "[]", "none", "none", None, 1),
             "ck_concept_pair_judgements_relation_outcome"),
            ((2, 3, 2, "not json", "none", "none", None, None),
             "ck_concept_pair_judgements_found_by_json"),
            ((2, 9, 2, "[]", "none", "none", None, None), "FOREIGN KEY"),
        ]:
            with pytest.raises(sqlite3.IntegrityError, match=constraint):
                conn.execute(insert, values)
        conn.execute("DELETE FROM concept_relations WHERE id = 1")  # sets relation_id NULL
        assert conn.execute(
            "SELECT outcome, relation_id FROM concept_pair_judgements WHERE high_id = 2"
        ).fetchone() == ("added", None)
        conn.execute("DELETE FROM concepts WHERE id = 3")  # cascades to its pair
        assert conn.execute(
            "SELECT low_id, high_id FROM concept_pair_judgements"
        ).fetchall() == [(1, 2)]


def test_downgrade_0007_to_0006_drops_only_the_judgement_memo(settings: Settings) -> None:
    dispose_engine(settings)
    with _sqlite(settings) as conn:
        _concepts_and_a_relation(conn)
        conn.execute(
            "INSERT INTO concept_pair_judgements (low_id, high_id, focus_id, low_text_sha256, "
            "high_text_sha256, found_by, verdict, outcome, rationale, relation_id, model) "
            "VALUES (1, 2, 1, 'h1', 'h2', '[]', 'relates_to', 'added', 'Why.', 1, 'm')"
        )

    command.downgrade(alembic_config(settings), "0006")

    assert current_revision(settings) == "0006"
    assert _tables(settings) == (
        (TABLES_0010 - HISTORY_TABLES - LAB_TABLES - CHAT_BLOCK_TABLES - {"concept_pair_judgements"})
        | FTS_TABLES
        | {"alembic_version"}
    )
    with _sqlite(settings) as conn:
        assert conn.execute("SELECT slug FROM concepts ORDER BY id").fetchall() == [
            ("a",), ("b",), ("c",)
        ]
        assert conn.execute("SELECT count(*) FROM concept_relations").fetchone() == (1,)
        indexes = conn.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE 'ix_concept_pair_judgements_%'"
        ).fetchall()
        assert indexes == []

    upgrade_to_head(settings)  # and back up again
    assert current_revision(settings) == HEAD
    assert "concept_pair_judgements" in _tables(settings)


def test_upgrade_0007_to_0008_adds_chat_blocks_and_keeps_data(tmp_path: Path) -> None:
    settings = Settings(root=tmp_path / "home")
    settings.root.mkdir(parents=True)
    command.upgrade(alembic_config(settings), "0007")
    _insert_response_rows(settings.db_path)  # an artifact with id 1, among other rows
    with _sqlite(settings) as conn:
        conn.execute("INSERT INTO chat_threads (id, artifact_id) VALUES (1, 1)")
        for position, role in ((1, "user"), (2, "assistant")):
            conn.execute(
                "INSERT INTO chat_messages (thread_id, position, role, text, api_content) "
                "VALUES (1, ?, ?, 'hi', '[{}]')",
                (position, role),
            )

    backup = upgrade_to_head(settings)

    assert backup is not None and current_revision(settings) == HEAD
    with _sqlite(backup) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert not (tables & CHAT_BLOCK_TABLES) and "chat_messages" in tables
    with _sqlite(settings) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        assert conn.execute("SELECT count(*) FROM chat_messages").fetchone() == (2,)
        block = (
            "INSERT INTO chat_blocks (message_id, position, text_offset, type, is_gate, content) "
            "VALUES (?, ?, 0, 'multiple_choice', 1, ?)"
        )
        conn.execute(block, (2, 1, "{}"))
        response = (
            "INSERT INTO chat_block_responses (block_id, attempt, response, passed, grader, "
            "details, reported_message_id) VALUES (?, ?, ?, 0, 'g', ?, ?)"
        )
        conn.execute(response, (1, 1, '{"choice": 0}', None, 1))
        assert conn.execute(
            "SELECT created_at IS NOT NULL FROM chat_block_responses"
        ).fetchone() == (1,)
        for sql, values, constraint in [
            (block, (2, 1, "{}"), "UNIQUE"),
            (block, (2, 2, "not json"), "ck_chat_blocks_content_json"),
            (block, (99, 1, "{}"), "FOREIGN KEY"),
            (response, (1, 1, "{}", None, None), "UNIQUE"),
            (response, (1, 2, "not json", None, None), "ck_chat_block_responses_response_json"),
            (response, (1, 2, "{}", "not json", None), "ck_chat_block_responses_details_json"),
            (response, (99, 1, "{}", None, None), "FOREIGN KEY"),
        ]:
            with pytest.raises(sqlite3.IntegrityError, match=constraint):
                conn.execute(sql, values)
        # the reporting message goes: the attempt stays, unreported; the message with the
        # block goes: the block and its attempts go with it
        conn.execute("DELETE FROM chat_messages WHERE id = 1")
        assert conn.execute(
            "SELECT reported_message_id FROM chat_block_responses"
        ).fetchone() == (None,)
        conn.execute("DELETE FROM chat_messages WHERE id = 2")
        assert conn.execute("SELECT count(*) FROM chat_blocks").fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM chat_block_responses").fetchone() == (0,)


def test_downgrade_0008_to_0007_drops_only_chat_blocks(settings: Settings) -> None:
    command.downgrade(alembic_config(settings), "0007")
    assert _tables(settings) == (TABLES_0010 - HISTORY_TABLES - LAB_TABLES - CHAT_BLOCK_TABLES) | FTS_TABLES | {"alembic_version"}
    upgrade_to_head(settings)  # and back up again
    assert current_revision(settings) == HEAD
    assert _tables(settings) == TABLES | FTS_TABLES | {"alembic_version"}


def test_upgrade_0008_to_0009_adds_labs_and_keeps_data(tmp_path: Path) -> None:
    settings = Settings(root=tmp_path / "home")
    settings.root.mkdir(parents=True)
    command.upgrade(alembic_config(settings), "0008")
    _insert_glossary_rows(settings.db_path)  # an artifact with id 1, among other rows
    with _sqlite(settings) as conn:
        conn.execute(
            "INSERT INTO artifacts (id, slug, kind, title, dir_path, generator, params) VALUES "
            "(2, 'lab', 'lab', 'L', 'artifacts/lab', 'stub-lab-v1', '{\"study_page\": \"a\"}')"
        )

    backup = upgrade_to_head(settings)

    assert backup is not None and current_revision(settings) == HEAD
    with _sqlite(backup) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert not (tables & LAB_TABLES) and "chat_blocks" in tables  # the 0008 state
    assert _tables(settings) == TABLES | FTS_TABLES | {"alembic_version"}
    with _sqlite(settings) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        assert conn.execute("SELECT slug FROM artifacts ORDER BY id").fetchall() == [
            ("a",), ("lab",)
        ]
        assert conn.execute("SELECT term FROM glossary_terms").fetchall() == [("GAN",)]
        assert conn.execute("SELECT count(*) FROM block_responses").fetchone() == (2,)

        workspace = "INSERT INTO lab_workspaces (artifact_id, files) VALUES (?, ?)"
        conn.execute(workspace, (2, '[{"path": "a.py", "content": "print(1)"}]'))
        assert conn.execute(
            "SELECT solution_revealed_at, updated_at IS NOT NULL FROM lab_workspaces"
        ).fetchone() == (None, 1)
        run = (
            "INSERT INTO lab_runs (artifact_id, kind, step_index, passed, files, output, error) "
            "VALUES (?, ?, ?, ?, ?, '', NULL)"
        )
        conn.execute(run, (2, "run", None, 1, "[]"))
        conn.execute(run, (2, "run", 1, 0, "[]"))  # a run may say which step the reader was on
        conn.execute(run, (2, "check", 0, 0, "[]"))
        assert conn.execute(
            "SELECT count(*), min(created_at IS NOT NULL) FROM lab_runs"
        ).fetchone() == (3, 1)
        for sql, values, constraint in [
            (workspace, (2, "[]"), "UNIQUE"),
            (workspace, (1, "not json"), "ck_lab_workspaces_files_json"),
            (workspace, (99, "[]"), "FOREIGN KEY"),
            (run, (2, "test", None, 1, "[]"), "ck_lab_runs_kind"),
            (run, (2, "check", None, 1, "[]"), "ck_lab_runs_check_step"),
            (run, (2, "check", -1, 1, "[]"), "ck_lab_runs_step_index"),
            (run, (2, "run", None, 1, "nope"), "ck_lab_runs_files_json"),
            (run, (99, "run", None, 1, "[]"), "FOREIGN KEY"),
        ]:
            with pytest.raises(sqlite3.IntegrityError, match=constraint):
                conn.execute(sql, values)
        conn.execute("DELETE FROM artifacts WHERE id = 2")  # cascades to the lab's rows
        assert conn.execute("SELECT count(*) FROM lab_workspaces").fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM lab_runs").fetchone() == (0,)


def test_downgrade_0009_to_0008_drops_only_labs(settings: Settings) -> None:
    dispose_engine(settings)
    _insert_glossary_rows(settings.db_path)
    with _sqlite(settings) as conn:
        conn.execute("INSERT INTO lab_workspaces (artifact_id, files) VALUES (1, '[]')")
        conn.execute(
            "INSERT INTO lab_runs (artifact_id, kind, passed, files, output) "
            "VALUES (1, 'run', 1, '[]', 'hi')"
        )
    command.downgrade(alembic_config(settings), "0008")
    assert current_revision(settings) == "0008"
    assert _tables(settings) == (TABLES_0010 - HISTORY_TABLES - LAB_TABLES) | FTS_TABLES | {"alembic_version"}
    with _sqlite(settings) as conn:
        assert conn.execute("SELECT slug FROM artifacts").fetchall() == [("a",)]
        assert conn.execute("SELECT term FROM glossary_terms").fetchall() == [("GAN",)]
        indexes = conn.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE 'ix_lab_runs_%'"
        ).fetchall()
        assert indexes == []
    upgrade_to_head(settings)  # and back up again
    assert current_revision(settings) == HEAD
    assert LAB_TABLES <= _tables(settings)


# -- 0010: the study history ------------------------------------------------------------------

# Activity at 0009, in time order across the four tables: 10:10 -> 10:40 is exactly the idle gap
# (same session), 10:40 -> 12:00 and 15:00 -> 15:30:00.000001 are more (new sessions). 10:10:00
# has no fraction, as CURRENT_TIMESTAMP writes it.
ACTIVITY_0009 = [
    ("block_responses", 1, "2026-01-01 10:00:00.500000"),
    ("block_responses", 2, "2026-01-01 10:10:00"),
    ("chat_messages", 1, "2026-01-01 10:40:00.000000"),
    ("chat_messages", 2, "2026-01-01 12:00:00.000000"),
    ("chat_block_responses", 1, "2026-01-01 12:05:00.000000"),
    ("lab_runs", 1, "2026-01-01 15:00:00.000000"),
    ("lab_runs", 2, "2026-01-01 15:30:00.000001"),
]
BACKFILLED_0010 = [  # (started_at, last_activity_at, rows)
    ("2026-01-01 10:00:00.500000", "2026-01-01 10:40:00.000000", 3),
    ("2026-01-01 12:00:00.000000", "2026-01-01 12:05:00.000000", 2),
    ("2026-01-01 15:00:00.000000", "2026-01-01 15:00:00.000000", 1),
    ("2026-01-01 15:30:00.000001", "2026-01-01 15:30:00.000001", 1),
]


def _insert_activity_rows(db: Path) -> None:
    """A study page with a gate, a lab of it, and rows in the four activity tables (0009)."""
    with _sqlite(db) as conn:
        conn.execute(
            "INSERT INTO artifacts (id, slug, kind, title, dir_path, generator, params) VALUES "
            "(1, 'a', 'study_page', 'A', 'artifacts/a', 'test', '{}'), "
            "(2, 'lab', 'lab', 'L', 'artifacts/lab', 'stub-lab-v1', '{\"study_page\": \"a\"}')"
        )
        conn.execute(
            "INSERT INTO artifact_blocks (id, artifact_id, position, key, type, is_gate, "
            "content_file, content_sha256) VALUES (1, 1, 1, 'b01', 'multiple_choice', 1, 'x', 'x')"
        )
        conn.execute("INSERT INTO chat_threads (id, artifact_id) VALUES (1, 1)")
        times = {(table, row_id): stamp for table, row_id, stamp in ACTIVITY_0009}
        for attempt in (1, 2):
            conn.execute(
                "INSERT INTO block_responses (id, block_id, attempt, response, passed, grader, "
                "created_at) VALUES (?, 1, ?, '{}', 0, 'g', ?)",
                (attempt, attempt, times[("block_responses", attempt)]),
            )
        for position, role in ((1, "user"), (2, "assistant")):
            conn.execute(
                "INSERT INTO chat_messages (id, thread_id, position, role, text, api_content, "
                "created_at) VALUES (?, 1, ?, ?, 'hi', '[{}]', ?)",
                (position, position, role, times[("chat_messages", position)]),
            )
        conn.execute(
            "INSERT INTO chat_blocks (id, message_id, position, text_offset, type, is_gate, "
            "content) VALUES (1, 2, 1, 0, 'multiple_choice', 1, '{}')"
        )
        conn.execute(
            "INSERT INTO chat_block_responses (id, block_id, attempt, response, passed, grader, "
            "created_at) VALUES (1, 1, 1, '{}', 1, 'g', ?)",
            (times[("chat_block_responses", 1)],),
        )
        for run_id in (1, 2):
            conn.execute(
                "INSERT INTO lab_runs (id, artifact_id, kind, passed, files, output, created_at) "
                "VALUES (?, 2, 'run', 1, '[]', '', ?)",
                (run_id, times[("lab_runs", run_id)]),
            )


def _session_of_each_row(conn: sqlite3.Connection) -> dict[tuple[str, int], int]:
    return {
        (table, row_id): session_id
        for table in ACTIVITY_TABLES
        for row_id, session_id in conn.execute(f"SELECT id, session_id FROM {table}")
    }


def test_upgrade_0009_to_0010_adds_the_history_and_backfills_sessions(tmp_path: Path) -> None:
    settings = Settings(root=tmp_path / "home")
    settings.root.mkdir(parents=True)
    command.upgrade(alembic_config(settings), "0009")
    _insert_activity_rows(settings.db_path)

    backup = upgrade_to_head(settings)

    assert backup is not None and current_revision(settings) == HEAD
    with _sqlite(backup) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert not (tables & HISTORY_TABLES) and LAB_TABLES <= tables  # the 0009 state
    assert _tables(settings) == TABLES | FTS_TABLES | {"alembic_version"}
    with _sqlite(settings) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        sessions = conn.execute(
            "SELECT id, started_at, last_activity_at, backfilled FROM study_sessions ORDER BY id"
        ).fetchall()
        assert [(s, e) for _id, s, e, _b in sessions] == [(s, e) for s, e, _n in BACKFILLED_0010]
        assert {b for *_rest, b in sessions} == {1}
        owner = _session_of_each_row(conn)
        ids = [row[0] for row in sessions]
        expected = {}
        rows = iter(ACTIVITY_0009)
        for session_id, (_s, _e, count) in zip(ids, BACKFILLED_0010, strict=True):
            for _ in range(count):
                table, row_id, _stamp = next(rows)
                expected[(table, row_id)] = session_id
        assert owner == expected
        # the rows themselves are unchanged
        assert conn.execute("SELECT count(*) FROM block_responses").fetchone() == (2,)
        assert conn.execute("SELECT created_at FROM block_responses WHERE id = 2").fetchone() == (
            "2026-01-01 10:10:00",
        )

        event = (
            "INSERT INTO study_events (session_id, kind, artifact_id, lab_id, payload) "
            "VALUES (?, ?, ?, ?, ?)"
        )
        conn.execute(event, (1, "lab_opened", 1, 2, "{}"))
        assert conn.execute(
            "SELECT occurred_at IS NOT NULL, payload FROM study_events"
        ).fetchone() == (1, "{}")
        span = (
            "INSERT INTO attention_spans (session_id, surface, artifact_id, started_at, "
            "last_seen_at, active_seconds) VALUES (?, ?, 1, ?, ?, ?)"
        )
        conn.execute(span, (1, "study_page", "2026-01-01 10:00:00", "2026-01-01 10:00:15", 15.0))
        assert conn.execute("SELECT heartbeats FROM attention_spans").fetchone() == (1,)
        conn.execute("INSERT INTO study_sessions DEFAULT VALUES")
        assert conn.execute(
            "SELECT backfilled FROM study_sessions ORDER BY id DESC LIMIT 1"
        ).fetchone() == (0,)
        later, earlier = "2026-01-01 10:00:01", "2026-01-01 10:00:00"
        for sql, values, constraint in [
            (event, (1, "page_flipped", 1, None, "{}"), "ck_study_events_kind"),
            (event, (1, "lab_opened", 1, 2, "nope"), "ck_study_events_payload_json"),
            (event, (99, "lab_opened", 1, 2, "{}"), "FOREIGN KEY"),
            (event, (1, "lab_opened", 99, 2, "{}"), "FOREIGN KEY"),
            (span, (1, "chat", later, later, 0.0), "ck_attention_spans_surface"),
            (span, (1, "study_page", later, earlier, 0.0), "ck_attention_spans_seen_order"),
            (span, (1, "study_page", earlier, later, -1.0), "ck_attention_spans_active_seconds"),
            ("INSERT INTO study_sessions (started_at, last_activity_at) VALUES (?, ?)",
             (later, earlier), "ck_study_sessions_activity_order"),
            ("UPDATE block_responses SET session_id = ? WHERE id = 1", (99,), "FOREIGN KEY"),
            ("DELETE FROM study_sessions WHERE id = ?", (1,), "FOREIGN KEY"),  # RESTRICT
        ]:
            with pytest.raises(sqlite3.IntegrityError, match=constraint):
                conn.execute(sql, values)
        # a deleted lab: its runs go (as before), its events stay with no lab
        conn.execute("DELETE FROM artifacts WHERE id = 2")
        assert conn.execute("SELECT count(*) FROM lab_runs").fetchone() == (0,)
        assert conn.execute("SELECT artifact_id, lab_id FROM study_events").fetchone() == (1, None)


def test_upgrade_0009_to_0010_with_no_activity_makes_no_sessions(tmp_path: Path) -> None:
    settings = Settings(root=tmp_path / "home")
    settings.root.mkdir(parents=True)
    command.upgrade(alembic_config(settings), "0009")
    upgrade_to_head(settings)
    with _sqlite(settings) as conn:
        assert conn.execute("SELECT count(*) FROM study_sessions").fetchone() == (0,)


def test_downgrade_0010_to_0009_drops_only_the_history(tmp_path: Path) -> None:
    settings = Settings(root=tmp_path / "home")
    settings.root.mkdir(parents=True)
    command.upgrade(alembic_config(settings), "0009")
    _insert_activity_rows(settings.db_path)
    upgrade_to_head(settings)
    with _sqlite(settings) as conn:
        conn.execute("INSERT INTO study_events (session_id, kind) VALUES (1, 'library_opened')")
        conn.execute("INSERT INTO attention_spans (session_id, surface) VALUES (1, 'graph')")

    command.downgrade(alembic_config(settings), "0009")

    assert current_revision(settings) == "0009"
    assert _tables(settings) == (TABLES_0010 - HISTORY_TABLES) | FTS_TABLES | {"alembic_version"}
    with _sqlite(settings) as conn:
        for table in ACTIVITY_TABLES:
            columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            assert "session_id" not in columns and "created_at" in columns, table
            referred = {row[2] for row in conn.execute(f"PRAGMA foreign_key_list({table})")}
            assert "study_sessions" not in referred and referred, table
        indexes = conn.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE '%session_id%'"
        ).fetchall()
        assert indexes == []
        counts = {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in ACTIVITY_TABLES}
        assert counts == {"block_responses": 2, "chat_block_responses": 1, "chat_messages": 2,
                          "lab_runs": 2}
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []

    upgrade_to_head(settings)  # and up again: the same sessions are derived
    with _sqlite(settings) as conn:
        assert conn.execute(
            "SELECT started_at, last_activity_at FROM study_sessions ORDER BY id"
        ).fetchall() == [(s, e) for s, e, _n in BACKFILLED_0010]


def test_0010_froze_the_live_rules() -> None:
    """The migration keeps its own copy of the idle gap and the event kinds (so re-running it
    later gives the same result); while 0010 is the head they must match the code."""
    from teacher.db.models import StudyEvent
    from teacher.managers.study_history import SESSION_IDLE_GAP

    script = ScriptDirectory.from_config(alembic_config(Settings(root=Path("unused"))))
    module = script.get_revision("0010").module
    assert module.IDLE_GAP == SESSION_IDLE_GAP
    (check,) = [
        c for c in StudyEvent.__table__.constraints
        if isinstance(c, CheckConstraint) and c.name == "ck_study_events_kind"
    ]
    assert module.EVENT_KINDS_CHECK == str(check.sqltext)
    assert set(module.ACTIVITY_TABLES) == set(ACTIVITY_TABLES)


# -- 0011: the block concept tags ---------------------------------------------------------------


def _at_0010_with_a_page_and_a_concept(tmp_path: Path) -> Settings:
    settings = Settings(root=tmp_path / "home")
    settings.root.mkdir(parents=True)
    command.upgrade(alembic_config(settings), "0009")
    _insert_activity_rows(settings.db_path)  # page 1 with gate block 1 (and its answers)
    command.upgrade(alembic_config(settings), "0010")
    with _sqlite(settings) as conn:
        conn.execute(
            "INSERT INTO concepts (id, slug, name, name_norm, summary, origin) "
            "VALUES (1, 'attention', 'attention', 'attention', 'Weights.', 'paper')"
        )
    return settings


def test_upgrade_0010_to_0011_adds_the_block_tags_and_keeps_data(tmp_path: Path) -> None:
    settings = _at_0010_with_a_page_and_a_concept(tmp_path)
    backup = upgrade_to_head(settings)
    assert backup is not None and current_revision(settings) == HEAD
    assert _tables(settings) == TABLES | FTS_TABLES | {"alembic_version"}
    with _sqlite(settings) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        assert conn.execute("SELECT slug, block_concepts_tagged_at FROM artifacts ORDER BY id").fetchall() == [
            ("a", None), ("lab", None),
        ]
        assert conn.execute("SELECT count(*) FROM block_responses").fetchone() == (2,)
        assert conn.execute("SELECT count(*) FROM study_sessions").fetchone() == (4,)
        tag = (
            "INSERT INTO block_concepts (block_id, concept_id, role, origin, rationale, model) "
            "VALUES (?, ?, ?, ?, ?, ?)"
        )
        conn.execute(tag, (1, 1, "main", "model", "It asks about attention.", "claude-x"))
        assert conn.execute(
            "SELECT role, origin, created_at IS NOT NULL FROM block_concepts"
        ).fetchone() == ("main", "model", 1)
        for values, constraint in [
            ((1, 1, "incidental", "user", None, None), "UNIQUE"),
            ((1, 99, "main", "user", None, None), "FOREIGN KEY"),
            ((99, 1, "main", "user", None, None), "FOREIGN KEY"),
            ((1, 1, "core", "user", None, None), "ck_block_concepts_role"),
            ((1, 1, "main", "paper", None, None), "ck_block_concepts_origin"),
            ((1, 1, "main", "model", None, "claude-x"), "ck_block_concepts_rationale"),
            ((1, 1, "main", "model", "Why.", None), "ck_block_concepts_model"),
            ((1, 1, "main", "user", None, "claude-x"), "ck_block_concepts_model"),
        ]:
            with pytest.raises(sqlite3.IntegrityError, match=constraint):
                conn.execute(tag, values)
        conn.execute("DELETE FROM concepts WHERE id = 1")  # the tag goes with the concept
        assert conn.execute("SELECT count(*) FROM block_concepts").fetchone() == (0,)


def test_downgrade_0011_to_0010_drops_only_the_block_tags(tmp_path: Path) -> None:
    settings = _at_0010_with_a_page_and_a_concept(tmp_path)
    upgrade_to_head(settings)
    with _sqlite(settings) as conn:
        conn.execute(
            "INSERT INTO block_concepts (block_id, concept_id, role, origin) "
            "VALUES (1, 1, 'main', 'user')"
        )
        conn.execute("UPDATE artifacts SET block_concepts_tagged_at = '2026-01-01 00:00:00'")

    command.downgrade(alembic_config(settings), "0010")

    assert current_revision(settings) == "0010"
    assert _tables(settings) == TABLES_0010 | FTS_TABLES | {"alembic_version"}
    with _sqlite(settings) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(artifacts)")}
        assert "block_concepts_tagged_at" not in columns and "glossary_built_at" in columns
        assert conn.execute("SELECT slug FROM artifacts ORDER BY id").fetchall() == [("a",), ("lab",)]
        assert conn.execute("SELECT count(*) FROM block_responses").fetchone() == (2,)
        assert conn.execute("SELECT count(*) FROM concepts").fetchone() == (1,)
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE '%block_concepts%'"
        ).fetchall() == []
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    upgrade_to_head(settings)  # and up again
    assert TAG_TABLES <= _tables(settings)


# -- 0012: verdicts, missing criteria and confidence on answers ------------------------------


def _at_0011_with_answers(tmp_path: Path) -> Settings:
    settings = _at_0010_with_a_page_and_a_concept(tmp_path)  # two failed page answers, one
    command.upgrade(alembic_config(settings), "0011")  # passed chat answer
    return settings


def test_upgrade_0011_to_0012_fills_in_verdicts_and_keeps_answers(tmp_path: Path) -> None:
    settings = _at_0011_with_answers(tmp_path)
    backup = upgrade_to_head(settings)
    assert backup is not None and current_revision(settings) == HEAD
    with _sqlite(settings) as conn:
        assert conn.execute(
            "SELECT id, passed, verdict, missing, confidence FROM block_responses ORDER BY id"
        ).fetchall() == [(1, 0, "failed", "[]", None), (2, 0, "failed", "[]", None)]
        assert conn.execute(
            "SELECT passed, verdict, missing FROM chat_block_responses"
        ).fetchall() == [(1, "passed", "[]")]
        answer = (
            "INSERT INTO block_responses (block_id, attempt, response, passed, grader, verdict, "
            "missing, confidence) VALUES (1, ?, '{}', ?, 'g', ?, ?, ?)"
        )
        conn.execute(answer, (3, 0, "partial", '["names the cost"]', "high"))
        conn.execute(answer, (4, 1, "passed", "[]", None))
        conn.execute(  # a raw row without the new columns: NULL verdict, no missing criteria
            "INSERT INTO block_responses (block_id, attempt, response, passed, grader) "
            "VALUES (1, 5, '{}', 0, 'g')"
        )
        assert conn.execute(
            "SELECT verdict, missing FROM block_responses WHERE attempt = 5"
        ).fetchone() == (None, "[]")
        for values, constraint in [
            ((6, 0, "wrong", "[]", None), "ck_block_responses_verdict"),
            ((6, 1, "failed", "[]", None), "ck_block_responses_verdict_passed"),
            ((6, 0, "passed", "[]", None), "ck_block_responses_verdict_passed"),
            ((6, 0, "partial", "[]", None), "ck_block_responses_missing_partial"),
            ((6, 0, "failed", '["x"]', None), "ck_block_responses_missing_partial"),
            ((6, 0, "partial", '{"a": 1}', None), "ck_block_responses_missing_array"),
            ((6, 0, "failed", "[]", "sure"), "ck_block_responses_confidence"),
        ]:
            with pytest.raises(sqlite3.IntegrityError, match=constraint):
                conn.execute(answer, values)
        chat = (
            "INSERT INTO chat_block_responses (block_id, attempt, response, passed, grader, "
            "verdict, missing) VALUES (1, ?, '{}', ?, 'g', ?, ?)"
        )
        conn.execute(chat, (2, 0, "partial", '["x"]'))
        with pytest.raises(sqlite3.IntegrityError, match="ck_chat_block_responses_verdict_passed"):
            conn.execute(chat, (3, 1, "partial", '["x"]'))
        columns = {row[1] for row in conn.execute("PRAGMA table_info(chat_block_responses)")}
        assert "confidence" not in columns  # the tutor's exercises do not ask
        indexes = {row[1] for row in conn.execute("PRAGMA index_list(block_responses)")}
        assert "ix_block_responses_session_id" in indexes  # kept by the rebuild
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_downgrade_0012_to_0011_drops_only_the_new_columns(tmp_path: Path) -> None:
    settings = _at_0011_with_answers(tmp_path)
    upgrade_to_head(settings)
    with _sqlite(settings) as conn:
        conn.execute(
            "INSERT INTO block_responses (block_id, attempt, response, passed, grader, verdict, "
            "missing, confidence) VALUES (1, 3, '{}', 0, 'g', 'partial', '[\"x\"]', 'low')"
        )

    command.downgrade(alembic_config(settings), "0011")

    assert current_revision(settings) == "0011"
    with _sqlite(settings) as conn:
        for table in ("block_responses", "chat_block_responses"):
            columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            assert not columns & {"verdict", "missing", "confidence"}, table
            assert "session_id" in columns and "details" in columns, table
        assert conn.execute("SELECT count(*) FROM block_responses").fetchone() == (3,)
        assert conn.execute("SELECT count(*) FROM chat_block_responses").fetchone() == (1,)
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    upgrade_to_head(settings)  # and up again: the partial answer is now a plain failed one
    with _sqlite(settings) as conn:
        assert conn.execute(
            "SELECT verdict FROM block_responses WHERE attempt = 3"
        ).fetchone() == ("failed",)


def test_0012_froze_the_live_checks() -> None:
    """While 0012 is the head its frozen check texts must match the models."""
    from teacher.db.models import BlockResponse, ChatBlockResponse

    script = ScriptDirectory.from_config(alembic_config(Settings(root=Path("unused"))))
    module = script.get_revision("0012").module
    for model in (BlockResponse, ChatBlockResponse):
        table = model.__table__
        live = {
            c.name: str(c.sqltext) for c in table.constraints if isinstance(c, CheckConstraint)
        }
        for check, sqltext in module.CHECKS:
            assert live[f"ck_{table.name}_{check}"] == sqltext
    live = {c.name: str(c.sqltext) for c in BlockResponse.__table__.constraints
            if isinstance(c, CheckConstraint)}
    assert live["ck_block_responses_confidence"] == module.CONFIDENCE_CHECK


def _at_0013_with_a_run(tmp_path: Path) -> Settings:
    settings = _at_0011_with_answers(tmp_path)
    command.upgrade(alembic_config(settings), "0013")
    with _sqlite(settings) as conn:
        (artifact,) = conn.execute("SELECT id FROM artifacts ORDER BY id LIMIT 1").fetchone()
        conn.execute(
            "INSERT INTO generation_runs (artifact_id, stage, model, status, error) "
            "VALUES (?, 'write', 'm', 'failed', 'boom')",
            (artifact,),
        )
    return settings


def test_upgrade_0013_to_0014_adds_run_notes_and_keeps_runs(tmp_path: Path) -> None:
    settings = _at_0013_with_a_run(tmp_path)
    backup = upgrade_to_head(settings)
    assert backup is not None and current_revision(settings) == HEAD
    with _sqlite(settings) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(generation_runs)")}
        assert "note" in columns and "error" in columns
        assert conn.execute(
            "SELECT stage, status, error, note FROM generation_runs"
        ).fetchall() == [("write", "failed", "boom", None)]
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_downgrade_0014_to_0013_drops_only_the_note(tmp_path: Path) -> None:
    settings = _at_0013_with_a_run(tmp_path)
    upgrade_to_head(settings)
    with _sqlite(settings) as conn:
        conn.execute("UPDATE generation_runs SET note = 'two questions answered without the page'")

    command.downgrade(alembic_config(settings), "0013")

    assert current_revision(settings) == "0013"
    with _sqlite(settings) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(generation_runs)")}
        assert "note" not in columns and "output_file" in columns
        assert conn.execute("SELECT stage, error FROM generation_runs").fetchall() == [
            ("write", "boom")
        ]
    upgrade_to_head(settings)  # and up again: the note is gone
    with _sqlite(settings) as conn:
        assert conn.execute("SELECT note FROM generation_runs").fetchall() == [(None,)]


# -- 0013: the review queue ----------------------------------------------------------------------


def test_upgrade_0012_to_0013_adds_the_review_queue_and_keeps_data(tmp_path: Path) -> None:
    settings = _at_0011_with_answers(tmp_path)
    command.upgrade(alembic_config(settings), "0012")
    command.upgrade(alembic_config(settings), "0013")
    assert _tables(settings) == (TABLES - CITATION_TABLES - LEDGER_TABLES - USER_TABLES - OFFLINE_TABLES - HIGHLIGHT_TABLES) | FTS_TABLES | {"alembic_version"}
    with _sqlite(settings) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        assert conn.execute("SELECT count(*) FROM block_responses").fetchone() == (2,)
        for table in REVIEW_TABLES:  # nothing is backfilled: items are derived on first read
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)
        conn.execute(
            "INSERT INTO review_variants (id, block_id, position, type, content, model) "
            "VALUES (1, 1, 1, 'blurb', '{}', 'm')"
        )
        item = (
            "INSERT INTO review_items (id, block_id, cleared_at, first_clean, interval_days, due_at, "
            "last_verdict, last_confidence, last_answered_at, presentation, presented_form, "
            "presented_variant_id, presented_at) VALUES (?, 1, '2026-01-01', 1, ?, '2026-01-04', ?, ?, "
            "'2026-01-01', ?, ?, ?, ?)"
        )
        conn.execute(item, (1, 3.0, "passed", None, "tok", "variant", 1, "2026-01-02"))
        assert conn.execute("SELECT reviews, created_at IS NOT NULL FROM review_items").fetchone() == (0, 1)
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):  # one item per question
            conn.execute(item, (2, 3.0, "passed", None, None, None, None, None))
        conn.execute("DELETE FROM review_items")
        for values, constraint in [
            ((2, 0.0, "passed", None, None, None, None, None), "ck_review_items_interval_days"),
            ((2, 1.0, "right", None, None, None, None, None), "ck_review_items_last_verdict"),
            ((2, 1.0, "passed", "sure", None, None, None, None), "ck_review_items_last_confidence"),
            ((2, 1.0, "passed", None, "tok", None, None, "2026-01-02"), "ck_review_items_presentation"),
            ((2, 1.0, "passed", None, "tok", "copy", None, "2026-01-02"), "ck_review_items_presented_form"),
        ]:
            with pytest.raises(sqlite3.IntegrityError, match=constraint):
                conn.execute(item, values)
        conn.execute(item, (1, 3.0, "passed", None, "tok", "variant", 1, "2026-01-02"))
        attempt = (
            "INSERT INTO review_attempts (item_id, form, variant_id, variant_position, asked, "
            "response, verdict, missing, confidence, grader, interval_before, interval_after, "
            "due_before, due_after) VALUES (1, ?, ?, ?, '{}', '{}', ?, ?, ?, 'g', 3.0, ?, "
            "'2026-01-04', '2026-01-05')"
        )
        conn.execute(attempt, ("variant", 1, 1, "failed", "[]", "high", 1.0))
        for values, constraint in [
            (("original", None, 1, "failed", "[]", "high", 1.0), "ck_review_attempts_variant"),
            (("variant", 1, None, "failed", "[]", "high", 1.0), "ck_review_attempts_variant"),
            (("copy", None, None, "failed", "[]", "high", 1.0), "ck_review_attempts_form"),
            (("original", None, None, "partial", "[]", "high", 1.0), "ck_review_attempts_missing_partial"),
            (("original", None, None, "failed", "[]", None, 1.0), "NOT NULL"),
            (("original", None, None, "failed", "[]", "high", 0.0), "ck_review_attempts_intervals"),
        ]:
            with pytest.raises(sqlite3.IntegrityError, match=constraint):
                conn.execute(attempt, values)
        conn.execute("DELETE FROM review_variants")  # a rebuild: the references go NULL
        assert conn.execute("SELECT presented_variant_id FROM review_items").fetchone() == (None,)
        assert conn.execute("SELECT variant_id, variant_position FROM review_attempts").fetchone() == (None, 1)
        conn.execute("DELETE FROM artifact_blocks WHERE id = 1")  # the page goes: so do they
        for table in REVIEW_TABLES:
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_downgrade_0013_to_0012_drops_only_the_review_queue(tmp_path: Path) -> None:
    settings = _at_0011_with_answers(tmp_path)
    upgrade_to_head(settings)
    command.downgrade(alembic_config(settings), "0012")
    assert current_revision(settings) == "0012"
    assert _tables(settings) == (
        (TABLES - REVIEW_TABLES - CITATION_TABLES - LEDGER_TABLES - USER_TABLES - OFFLINE_TABLES - HIGHLIGHT_TABLES) | FTS_TABLES | {"alembic_version"}
    )
    with _sqlite(settings) as conn:
        assert conn.execute("SELECT count(*) FROM block_responses").fetchone() == (2,)
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE '%review%'"
        ).fetchall() == []
    upgrade_to_head(settings)  # and up again
    assert REVIEW_TABLES <= _tables(settings)


def test_0013_froze_the_live_checks() -> None:
    """The check texts 0013 froze must match the models' (until a later revision changes them)."""
    from teacher.db.models import ReviewAttempt, ReviewItem, ReviewVariant

    script = ScriptDirectory.from_config(alembic_config(Settings(root=Path("unused"))))
    module = script.get_revision("0013").module
    for model in (ReviewVariant, ReviewItem, ReviewAttempt):
        table = model.__table__
        live = {
            c.name: str(c.sqltext) for c in table.constraints if isinstance(c, CheckConstraint)
        }
        frozen = {f"ck_{table.name}_{name}": sqltext for name, sqltext in module.CHECKS[table.name]}
        assert live == frozen, table.name


# -- 0015: citations -----------------------------------------------------------------------------


def _at_0014_with_sources(tmp_path: Path) -> Settings:
    settings = _at_0013_with_a_run(tmp_path)
    command.upgrade(alembic_config(settings), "0014")
    with _sqlite(settings) as conn:
        for n in (1, 2):
            conn.execute(
                "INSERT INTO knowledge_sources (id, slug, kind, title, dir_path, primary_file, "
                "media_type, content_sha256) VALUES (?, ?, 'paper', 'T', ?, 'source.pdf', "
                "'application/pdf', ?)",
                (100 + n, f"cite-{n}", f"knowledge_sources/cite-{n}", str(n) * 64),
            )
    return settings


def test_upgrade_0014_to_0015_adds_citations_and_keeps_data(tmp_path: Path) -> None:
    settings = _at_0014_with_sources(tmp_path)
    backup = upgrade_to_head(settings)
    assert backup is not None and current_revision(settings) == HEAD
    assert CITATION_TABLES <= _tables(settings)
    with _sqlite(settings) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        assert conn.execute("SELECT count(*) FROM knowledge_sources").fetchone()[0] >= 2
        for table in CITATION_TABLES:  # nothing is backfilled: every source is "never run"
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)
        conn.execute(
            "INSERT INTO source_references (source_id, position, raw_text, matched_source_id, "
            "match_method) VALUES (101, 1, 'Ref', 102, 'arxiv')"
        )
        assert conn.execute(
            "SELECT authors, created_at IS NOT NULL FROM source_references"
        ).fetchone() == ("[]", 1)
        extraction = (
            "INSERT INTO source_reference_extractions (source_id, status, run_number, trigger, "
            "extractor, model, finished_at, error, succeeded_at, found_in, reference_count) "
            "VALUES (101, ?, 1, 'cli', 'x', 'm', ?, ?, ?, ?, ?)"
        )
        for values, constraint in [
            (("done", "t", None, "t", "none", 0), "ck_source_reference_extractions_status"),
            (("failed", "t", None, None, None, None), "ck_source_reference_extractions_error"),
            (("running", "t", None, None, None, None), "ck_source_reference_extractions_finished"),
            (("succeeded", "t", None, None, None, None), "ck_source_reference_extractions_succeeded"),
            (("succeeded", "t", None, "t", None, 1), "ck_source_reference_extractions_stored"),
            (("succeeded", "t", None, "t", "nowhere", 1), "ck_source_reference_extractions_found_in"),
            (("succeeded", "t", None, "t", "none", 2), "ck_source_reference_extractions_none_found"),
            (("succeeded", "t", None, "t", "reference_list", -1), "ck_source_reference_extractions_reference_count"),
        ]:
            with pytest.raises(sqlite3.IntegrityError, match=constraint):
                conn.execute(extraction, values)
        conn.execute(extraction, ("succeeded", "t", None, "t", "reference_list", 1))
        assert conn.execute(
            "SELECT started_at IS NOT NULL FROM source_reference_extractions"
        ).fetchone() == (1,)
        for values, constraint in [
            ((101, 0, "x", None), "ck_source_references_position"),
            ((101, 2, "x", "guess"), "ck_source_references_match_method"),
            ((101, 1, "x", None), "UNIQUE"),
        ]:
            with pytest.raises(sqlite3.IntegrityError, match=constraint):
                conn.execute(
                    "INSERT INTO source_references (source_id, position, raw_text, match_method) "
                    "VALUES (?, ?, ?, ?)", values,
                )
        conn.execute("DELETE FROM knowledge_sources WHERE id = 102")  # the cited source: SET NULL
        assert conn.execute("SELECT matched_source_id FROM source_references").fetchone() == (None,)
        conn.execute("DELETE FROM knowledge_sources WHERE id = 101")  # the citing one: CASCADE
        for table in CITATION_TABLES:
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_downgrade_0015_to_0014_drops_only_the_citations(tmp_path: Path) -> None:
    settings = _at_0014_with_sources(tmp_path)
    upgrade_to_head(settings)
    with _sqlite(settings) as conn:
        conn.execute(
            "INSERT INTO source_references (source_id, position, raw_text) VALUES (101, 1, 'x')"
        )
    command.downgrade(alembic_config(settings), "0014")
    assert current_revision(settings) == "0014"
    assert _tables(settings) == (TABLES - CITATION_TABLES - LEDGER_TABLES - USER_TABLES - OFFLINE_TABLES - HIGHLIGHT_TABLES) | FTS_TABLES | {"alembic_version"}
    with _sqlite(settings) as conn:
        assert conn.execute(
            "SELECT count(*) FROM knowledge_sources WHERE id IN (101, 102)"
        ).fetchone() == (2,)
        assert conn.execute("SELECT note FROM generation_runs").fetchall() == [(None,)]
    upgrade_to_head(settings)  # and up again
    assert CITATION_TABLES <= _tables(settings)


def test_0015_froze_the_live_checks() -> None:
    """The check texts 0015 froze must match the models' (until a later revision changes them)."""
    from teacher.db.models import SourceReference, SourceReferenceExtraction

    script = ScriptDirectory.from_config(alembic_config(Settings(root=Path("unused"))))
    module = script.get_revision("0015").module
    for model in (SourceReferenceExtraction, SourceReference):
        table = model.__table__
        live = {
            c.name: str(c.sqltext) for c in table.constraints if isinstance(c, CheckConstraint)
        }
        frozen = {f"ck_{table.name}_{name}": sqltext for name, sqltext in module.CHECKS[table.name]}
        assert live == frozen, table.name


def _at_0015_with_data(tmp_path: Path) -> Settings:
    """At 0015 with an artifact, a failed run, two sources and a chat thread with a reply."""
    settings = _at_0014_with_sources(tmp_path)
    command.upgrade(alembic_config(settings), "0015")
    with _sqlite(settings) as conn:
        (artifact,) = conn.execute("SELECT id FROM artifacts ORDER BY id LIMIT 1").fetchone()
        conn.execute("INSERT INTO chat_threads (id, artifact_id) VALUES (301, ?)", (artifact,))
        conn.execute(
            "INSERT INTO chat_messages (id, thread_id, position, role, text, api_content, model, "
            "input_tokens, output_tokens) VALUES (401, 301, 1, 'assistant', 'hi', '[]', "
            "'claude-opus-5-5', 1000, 10)"
        )
    return settings


def test_upgrade_0015_to_0016_adds_the_cost_ledger_and_keeps_data(tmp_path: Path) -> None:
    settings = _at_0015_with_data(tmp_path)
    with _sqlite(settings) as conn:
        before = {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                  for t in ("artifacts", "generation_runs", "chat_messages", "knowledge_sources")}
    backup = upgrade_to_head(settings)
    assert backup is not None and current_revision(settings) == HEAD
    assert LEDGER_TABLES <= _tables(settings)
    with _sqlite(settings) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        for table, count in before.items():
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == count
        # Nothing is backfilled by the migration (that is `teacher costs backfill`).
        assert conn.execute("SELECT count(*) FROM api_calls").fetchone() == (0,)
        (artifact,) = conn.execute("SELECT id FROM artifacts ORDER BY id LIMIT 1").fetchone()
        (run,) = conn.execute("SELECT id FROM generation_runs LIMIT 1").fetchone()
        insert = (
            "INSERT INTO api_calls (purpose, step, provider, model, input_tokens, "
            "cache_write_tokens, cache_write_1h_tokens, cache_read_tokens, output_tokens, "
            "cost_usd, priced_on, artifact_id, generation_run_id, chat_thread_id, "
            "chat_message_id, knowledge_source_id, backfill_key) "
            "VALUES (?, 's', ?, 'm', ?, ?, ?, 0, 0, ?, ?, ?, ?, ?, ?, ?, ?)"
        )
        ok = ["tutor", "anthropic", 1, 2, 1, 0.5, "2026-11-24", artifact, run, 301, 401, 101, "k"]
        conn.execute(insert, ok)
        assert conn.execute(
            "SELECT created_at IS NOT NULL, is_estimate FROM api_calls"
        ).fetchone() == (1, 0)
        for change, constraint in [
            ({0: "fun"}, "ck_api_calls_purpose"),
            ({1: "google"}, "ck_api_calls_provider"),
            ({2: -1}, "ck_api_calls_tokens"),
            ({4: 3}, "ck_api_calls_cache_write_1h"),
            ({5: -0.1}, "ck_api_calls_cost"),
            ({5: None}, "ck_api_calls_priced"),
            ({}, "UNIQUE"),
        ]:
            values = [change.get(i, v) for i, v in enumerate(ok)]
            with pytest.raises(sqlite3.IntegrityError, match=constraint):
                conn.execute(insert, values)
        conn.execute(insert, [*ok[:5], None, None, *ok[7:12], None])  # no price: NULL cost
        # Deleting what a row points at keeps the row (the money was spent) and clears the link.
        conn.execute("DELETE FROM chat_threads WHERE id = 301")
        conn.execute("DELETE FROM artifacts WHERE id = ?", (artifact,))
        conn.execute("DELETE FROM knowledge_sources WHERE id = 101")
        assert conn.execute(
            "SELECT artifact_id, generation_run_id, chat_thread_id, chat_message_id, "
            "knowledge_source_id FROM api_calls"
        ).fetchall() == [(None,) * 5] * 2
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_downgrade_0016_to_0015_drops_only_the_ledger(tmp_path: Path) -> None:
    settings = _at_0015_with_data(tmp_path)
    upgrade_to_head(settings)
    command.downgrade(alembic_config(settings), "0015")
    assert current_revision(settings) == "0015"
    assert _tables(settings) == (
        (TABLES - LEDGER_TABLES - USER_TABLES - OFFLINE_TABLES - HIGHLIGHT_TABLES) | FTS_TABLES | {"alembic_version"}
    )
    with _sqlite(settings) as conn:
        assert conn.execute("SELECT input_tokens FROM chat_messages WHERE id = 401").fetchall() == [
            (1000,)
        ]
    upgrade_to_head(settings)  # and up again
    assert LEDGER_TABLES <= _tables(settings)


def test_0016_froze_the_live_checks() -> None:
    """The check texts 0016 froze must match the model's (until a later revision changes them)."""
    from teacher.db.models import ApiCall

    script = ScriptDirectory.from_config(alembic_config(Settings(root=Path("unused"))))
    module = script.get_revision("0016").module
    table = ApiCall.__table__
    live = {c.name: str(c.sqltext) for c in table.constraints if isinstance(c, CheckConstraint)}
    frozen = {f"ck_{table.name}_{name}": sqltext for name, sqltext in module.CHECKS[table.name]}
    assert live == frozen


# -- 0017: user accounts ---------------------------------------------------------------------


def _at_0016_with_data(tmp_path: Path) -> Settings:
    """At 0016 with an artifact, two sources, a chat thread and a ledger row."""
    settings = _at_0015_with_data(tmp_path)
    command.upgrade(alembic_config(settings), "0016")
    with _sqlite(settings) as conn:
        conn.execute(
            "INSERT INTO api_calls (purpose, step, provider, model, input_tokens, "
            "cache_write_tokens, cache_read_tokens, output_tokens) "
            "VALUES ('tutor', 's', 'anthropic', 'm', 1, 0, 0, 1)"
        )
    return settings


def test_upgrade_0016_to_0017_adds_accounts_and_keeps_data(tmp_path: Path) -> None:
    settings = _at_0016_with_data(tmp_path)
    counted = ("artifacts", "chat_messages", "knowledge_sources", "api_calls")
    with _sqlite(settings) as conn:
        before = {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in counted}
    backup = upgrade_to_head(settings)
    assert backup is not None and current_revision(settings) == HEAD
    assert USER_TABLES <= _tables(settings)
    with _sqlite(settings) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        for table, count in before.items():
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == count
        # No account is made by the migration: the owner registers one.
        assert conn.execute("SELECT count(*) FROM users").fetchone() == (0,)
        conn.execute("INSERT INTO users (id, email, password_hash) VALUES (1, 'a@b.io', 'h')")
        assert conn.execute(
            "SELECT created_at IS NOT NULL, last_login_at, disabled_at FROM users"
        ).fetchone() == (1, None, None)
        for email, constraint in [("a@b.io", "UNIQUE"), ("ab", "ck_users_email"),
                                  ("x" * 255, "ck_users_email")]:
            with pytest.raises(sqlite3.IntegrityError, match=constraint):
                conn.execute("INSERT INTO users (email, password_hash) VALUES (?, 'h')", (email,))
        insert = (
            "INSERT INTO user_sessions (user_id, token_sha256, created_at, expires_at, label) "
            "VALUES (?, ?, '2026-11-25 12:00:00', ?, ?)"
        )
        ok = [1, "a" * 64, "2026-12-25 12:00:00", "web"]
        conn.execute(insert, ok)
        assert conn.execute("SELECT last_seen_at IS NOT NULL FROM user_sessions").fetchone() == (1,)
        for change, constraint in [
            ({}, "UNIQUE"),
            ({1: "short"}, "ck_user_sessions_token_sha256"),
            ({1: "b" * 64, 2: "2026-11-25 12:00:00"}, "ck_user_sessions_expiry"),
            ({1: "b" * 64, 3: ""}, "ck_user_sessions_label"),
            ({0: 99, 1: "b" * 64}, "FOREIGN KEY"),
        ]:
            values = [change.get(i, v) for i, v in enumerate(ok)]
            with pytest.raises(sqlite3.IntegrityError, match=constraint):
                conn.execute(insert, values)
        conn.execute("DELETE FROM users WHERE id = 1")  # the sessions go with the user
        assert conn.execute("SELECT count(*) FROM user_sessions").fetchone() == (0,)
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_downgrade_0017_to_0016_drops_only_the_accounts(tmp_path: Path) -> None:
    settings = _at_0016_with_data(tmp_path)
    upgrade_to_head(settings)
    with _sqlite(settings) as conn:
        conn.execute("INSERT INTO users (id, email, password_hash) VALUES (1, 'a@b.io', 'h')")
    command.downgrade(alembic_config(settings), "0016")
    assert current_revision(settings) == "0016"
    assert _tables(settings) == (TABLES - USER_TABLES - OFFLINE_TABLES - HIGHLIGHT_TABLES) | FTS_TABLES | {"alembic_version"}
    with _sqlite(settings) as conn:
        assert conn.execute("SELECT count(*) FROM api_calls").fetchone() == (1,)
        assert conn.execute("SELECT input_tokens FROM chat_messages WHERE id = 401").fetchall() == [
            (1000,)
        ]
    upgrade_to_head(settings)  # and up again
    assert USER_TABLES <= _tables(settings)


def test_0017_froze_the_live_checks() -> None:
    """The check texts 0017 froze must match the models' (until a later revision changes them)."""
    from teacher.db.models import User, UserSession

    script = ScriptDirectory.from_config(alembic_config(Settings(root=Path("unused"))))
    module = script.get_revision("0017").module
    for model in (User, UserSession):
        table = model.__table__
        live = {
            c.name: str(c.sqltext) for c in table.constraints if isinstance(c, CheckConstraint)
        }
        frozen = {f"ck_{table.name}_{name}": sqltext for name, sqltext in module.CHECKS[table.name]}
        assert live == frozen, table.name


# -- 0018: attempts uploaded from a phone ----------------------------------------------------


def _at_0017_with_data(tmp_path: Path) -> Settings:
    """At 0017 with an artifact, its answers, two sources, a chat thread, a ledger row and a
    user."""
    settings = _at_0016_with_data(tmp_path)
    command.upgrade(alembic_config(settings), "0017")
    with _sqlite(settings) as conn:
        conn.execute("INSERT INTO users (id, email, password_hash) VALUES (1, 'a@b.io', 'h')")
    return settings


def test_upgrade_0017_to_0018_adds_offline_attempts_and_keeps_data(tmp_path: Path) -> None:
    settings = _at_0017_with_data(tmp_path)
    counted = ("artifacts", "block_responses", "chat_messages", "api_calls", "users")
    with _sqlite(settings) as conn:
        before = {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in counted}
    backup = upgrade_to_head(settings)
    assert backup is not None and current_revision(settings) == HEAD
    assert OFFLINE_TABLES <= _tables(settings)
    with _sqlite(settings) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        for table, count in before.items():
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == count
        assert conn.execute("SELECT count(*) FROM offline_attempts").fetchone() == (0,)
        responses = [r for (r,) in conn.execute("SELECT id FROM block_responses ORDER BY id")]
        assert len(responses) >= 2
        insert = (
            "INSERT INTO offline_attempts (client_id, response_id, made_at, device_grader, "
            "device_verdict) VALUES (?, ?, '2026-11-25 12:00:00', ?, ?)"
        )
        ok = ["0" * 8 + "-0000-4000-8000-" + "0" * 12, responses[0], "device:phone", "failed"]
        conn.execute(insert, ok)
        assert conn.execute("SELECT received_at IS NOT NULL FROM offline_attempts").fetchone() == (1,)
        other = "1" * 8 + "-0000-4000-8000-" + "0" * 12
        for change, constraint in [
            ({1: responses[1]}, "UNIQUE"),  # the same client_id
            ({0: other}, "UNIQUE"),  # the same response
            ({0: "short", 1: responses[1]}, "ck_offline_attempts_client_id"),
            ({0: other, 1: responses[1], 2: "judge"}, "ck_offline_attempts_device_grader"),
            ({0: other, 1: responses[1], 2: "device:"}, "ck_offline_attempts_device_grader"),
            ({0: other, 1: responses[1], 3: "maybe"}, "ck_offline_attempts_device_verdict"),
            ({0: other, 1: 999999}, "FOREIGN KEY"),
        ]:
            values = [change.get(i, v) for i, v in enumerate(ok)]
            with pytest.raises(sqlite3.IntegrityError, match=constraint):
                conn.execute(insert, values)
        conn.execute("DELETE FROM block_responses WHERE id = ?", (responses[0],))
        assert conn.execute("SELECT count(*) FROM offline_attempts").fetchone() == (0,)
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_downgrade_0018_to_0017_drops_only_offline_attempts(tmp_path: Path) -> None:
    settings = _at_0017_with_data(tmp_path)
    upgrade_to_head(settings)
    with _sqlite(settings) as conn:
        (response,) = conn.execute("SELECT id FROM block_responses ORDER BY id LIMIT 1").fetchone()
        conn.execute(
            "INSERT INTO offline_attempts (client_id, response_id, made_at, device_grader, "
            "device_verdict) VALUES (?, ?, '2026-11-25 12:00:00', 'device:phone', 'failed')",
            ("0" * 8 + "-0000-4000-8000-" + "0" * 12, response),
        )
        responses = conn.execute("SELECT count(*) FROM block_responses").fetchone()
    command.downgrade(alembic_config(settings), "0017")
    assert current_revision(settings) == "0017"
    assert _tables(settings) == (TABLES - OFFLINE_TABLES - HIGHLIGHT_TABLES) | FTS_TABLES | {"alembic_version"}
    with _sqlite(settings) as conn:
        assert conn.execute("SELECT count(*) FROM block_responses").fetchone() == responses
        assert conn.execute("SELECT count(*) FROM users").fetchone() == (1,)
    upgrade_to_head(settings)  # and up again
    assert OFFLINE_TABLES <= _tables(settings)


def test_0018_froze_the_live_checks() -> None:
    """The check texts 0018 froze must match the models' (until a later revision changes them)."""
    from teacher.db.models import OfflineAttempt

    script = ScriptDirectory.from_config(alembic_config(Settings(root=Path("unused"))))
    module = script.get_revision("0018").module
    table = OfflineAttempt.__table__
    live = {c.name: str(c.sqltext) for c in table.constraints if isinstance(c, CheckConstraint)}
    frozen = {f"ck_{table.name}_{name}": sqltext for name, sqltext in module.CHECKS[table.name]}
    assert live == frozen


# -- 0019: highlights and notes ---------------------------------------------------------------


def _at_0018_with_data(tmp_path: Path) -> Settings:
    """At 0018 with an artifact, its answers, two sources, a chat thread, a ledger row and a
    user."""
    settings = _at_0017_with_data(tmp_path)
    command.upgrade(alembic_config(settings), "0018")
    return settings


def test_upgrade_0018_to_0019_adds_highlights_and_keeps_data(tmp_path: Path) -> None:
    settings = _at_0018_with_data(tmp_path)
    counted = ("artifacts", "block_responses", "chat_messages", "api_calls", "users")
    with _sqlite(settings) as conn:
        before = {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in counted}
    backup = upgrade_to_head(settings)
    assert backup is not None and current_revision(settings) == HEAD
    assert HIGHLIGHT_TABLES <= _tables(settings)
    with _sqlite(settings) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        for table, count in before.items():
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == count
        assert conn.execute("SELECT count(*) FROM highlights").fetchone() == (0,)
        (artifact,) = conn.execute("SELECT id FROM artifacts ORDER BY id LIMIT 1").fetchone()
        insert = (
            "INSERT INTO highlights (uid, user_id, artifact_id, block_key, quote, prefix, suffix, "
            "start_offset, end_offset, note, deleted_at) VALUES (?, ?, ?, ?, ?, '', '', ?, ?, ?, ?)"
        )
        uid = "0" * 8 + "-0000-4000-8000-" + "0" * 12
        ok = [uid, 1, artifact, "b01", "a passage", 3, 12, None, None]
        conn.execute(insert, ok)
        row = conn.execute(
            "SELECT created_at IS NOT NULL, updated_at IS NOT NULL FROM highlights"
        ).fetchone()
        assert row == (1, 1)
        other = "1" * 8 + "-0000-4000-8000-" + "0" * 12
        for change, constraint in [
            ({}, "UNIQUE"),  # the same uid
            ({0: "short"}, "ck_highlights_uid"),
            ({0: other, 3: ""}, "ck_highlights_block_key"),
            ({0: other, 4: ""}, "ck_highlights_quote"),
            ({0: other, 5: -1}, "ck_highlights_offsets"),
            ({0: other, 5: 12}, "ck_highlights_offsets"),
            ({0: other, 7: ""}, "ck_highlights_note"),
            ({0: other, 7: "a note", 8: "2026-11-26 12:00:00"}, "ck_highlights_deleted_note"),
            ({0: other, 1: 999}, "FOREIGN KEY"),
            ({0: other, 2: 999999}, "FOREIGN KEY"),
        ]:
            values = [change.get(i, v) for i, v in enumerate(ok)]
            with pytest.raises(sqlite3.IntegrityError, match=constraint):
                conn.execute(insert, values)
        conn.execute(insert, [other, None, artifact, "b01", "no account", 0, 10, "n", None])
        conn.execute("DELETE FROM users WHERE id = 1")
        assert conn.execute("SELECT uid FROM highlights").fetchall() == [(other,)]
        conn.execute("DELETE FROM artifacts WHERE id = ?", (artifact,))
        assert conn.execute("SELECT count(*) FROM highlights").fetchone() == (0,)
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("SELECT count(*) FROM offline_review_answers").fetchone() == (0,)
        review_insert = (
            "INSERT INTO offline_review_answers (client_id, attempt_id, made_at, device_grader, "
            "device_verdict) VALUES (?, ?, '2026-11-25 12:00:00', ?, ?)"
        )
        for values, constraint in [
            ([uid, 999999, "device:phone", "failed"], "FOREIGN KEY"),
            (["short", 1, "device:phone", "failed"], "ck_offline_review_answers_client_id"),
            ([uid, 1, "judge", "failed"], "ck_offline_review_answers_device_grader"),
            ([uid, 1, "device:phone", "maybe"], "ck_offline_review_answers_device_verdict"),
        ]:
            with pytest.raises(sqlite3.IntegrityError, match=constraint):
                conn.execute(review_insert, values)


def test_downgrade_0019_to_0018_drops_only_the_new_tables(tmp_path: Path) -> None:
    settings = _at_0018_with_data(tmp_path)
    upgrade_to_head(settings)
    with _sqlite(settings) as conn:
        (artifact,) = conn.execute("SELECT id FROM artifacts ORDER BY id LIMIT 1").fetchone()
        conn.execute(
            "INSERT INTO highlights (uid, artifact_id, block_key, quote, prefix, suffix, "
            "start_offset, end_offset) VALUES (?, ?, 'b01', 'q', '', '', 0, 1)",
            ("0" * 8 + "-0000-4000-8000-" + "0" * 12, artifact),
        )
        responses = conn.execute("SELECT count(*) FROM block_responses").fetchone()
    command.downgrade(alembic_config(settings), "0018")
    assert current_revision(settings) == "0018"
    assert _tables(settings) == (TABLES - HIGHLIGHT_TABLES) | FTS_TABLES | {"alembic_version"}
    with _sqlite(settings) as conn:
        assert conn.execute("SELECT count(*) FROM block_responses").fetchone() == responses
        assert conn.execute("SELECT count(*) FROM users").fetchone() == (1,)
    upgrade_to_head(settings)  # and up again
    assert HIGHLIGHT_TABLES <= _tables(settings)


def test_0019_froze_the_live_checks() -> None:
    """The check texts 0019 froze must match the models' (until a later revision changes them)."""
    from teacher.db.models import Highlight, OfflineReviewAnswer

    script = ScriptDirectory.from_config(alembic_config(Settings(root=Path("unused"))))
    module = script.get_revision("0019").module
    for model in (Highlight, OfflineReviewAnswer):
        table = model.__table__
        live = {
            c.name: str(c.sqltext) for c in table.constraints if isinstance(c, CheckConstraint)
        }
        frozen = {f"ck_{table.name}_{name}": sqltext for name, sqltext in module.CHECKS[table.name]}
        assert live == frozen, table.name
