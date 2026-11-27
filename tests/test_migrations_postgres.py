"""The migration chain on PostgreSQL (Postgres test runs only; test_migrations.py covers SQLite):
upgrade from an empty database, idempotency, no file backup, model parity, downgrade and up
again, the constraints whose PostgreSQL form differs from SQLite's (the slug trigger,
LEAST/GREATEST, json_typeof, the json type itself, boolean defaults), and the concept search
tables with the pg_trgm extension (in `extensions` when that schema exists, as on Supabase)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from alembic import command
from sqlalchemy import CheckConstraint, Connection, inspect, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from teacher.config import Settings
from teacher.db import models  # noqa: F401  (registers every table)
from teacher.db.base import Base
from teacher.db.engine import dispose_engine, session_scope
from teacher.db.migrate import alembic_config, current_revision, head_revision, upgrade_to_head
from teacher.db.fts import CONCEPT_SEARCH_TABLES
from teacher.db.providers import create_db_provider
from teacher.managers import ConceptManager

pytestmark = pytest.mark.postgres_only("the migration chain on PostgreSQL")

HEAD = "0019"
TABLES = set(Base.metadata.tables)
SEARCH_TABLES = {"concept_search", "concept_search_tokens", "concept_trigram"}
SEARCH_INDEXES = {
    "ix_concept_search_tokens_key",
    "ix_concept_trigram_text",
    "ix_concept_trigram_compact",
}


@contextmanager
def _connect(settings: Settings) -> Iterator[Connection]:
    """A connection in one transaction that is rolled back (statements may fail on purpose)."""
    engine = create_db_provider(settings).migration_engine()
    try:
        with engine.connect() as conn:
            yield conn
    finally:
        engine.dispose()


def _tables(settings: Settings) -> set[str]:
    with _connect(settings) as conn:
        return set(inspect(conn).get_table_names())


def test_upgrade_from_empty_creates_every_table_and_takes_no_backup(
    empty_postgres_settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    settings = empty_postgres_settings
    assert current_revision(settings) is None
    assert _tables(settings) == set()

    assert upgrade_to_head(settings) is None

    # The concept search tables are plain tables here (no FTS5), hidden from autogenerate.
    assert _tables(settings) == TABLES | SEARCH_TABLES | {"alembic_version"}
    assert SEARCH_TABLES <= CONCEPT_SEARCH_TABLES
    # A database without an `extensions` schema gets pg_trgm in the current one.
    assert _search_layout(settings) == ("public", SEARCH_INDEXES, "public.gin_trgm_ops")
    assert current_revision(settings) == head_revision(settings) == HEAD
    for folder in (settings.sources_dir, settings.artifacts_dir, settings.derived_dir):
        assert folder.is_dir() and folder.parent == settings.root / "supabase"
    assert list(settings.backups_dir.iterdir()) == []
    assert "No backup taken" in caplog.text


def test_upgrade_is_idempotent(settings: Settings, caplog: pytest.LogCaptureFixture) -> None:
    assert upgrade_to_head(settings) is None
    assert upgrade_to_head(settings) is None
    assert "No backup taken" not in caplog.text  # nothing was pending
    assert current_revision(settings) == HEAD


def test_autogenerate_finds_no_diff(settings: Settings) -> None:
    """Models and migrations agree on PostgreSQL too (tables, columns, types, server defaults,
    FKs, uniques, indexes)."""
    command.check(alembic_config(settings))


def test_downgrade_to_base_and_up_again(settings: Settings) -> None:
    dispose_engine(settings)
    command.downgrade(alembic_config(settings), "base")
    assert _tables(settings) == {"alembic_version"}
    assert current_revision(settings) is None
    with _connect(settings) as conn:
        functions = conn.execute(
            text("SELECT count(*) FROM pg_proc WHERE proname = 'trg_concepts_slug_immutable'")
        ).scalar()
    assert functions == 0
    with _connect(settings) as conn:  # the downgrade leaves pg_trgm installed
        assert conn.execute(text("SELECT count(*) FROM pg_extension WHERE extname = 'pg_trgm'")).scalar() == 1
    upgrade_to_head(settings)
    assert _tables(settings) == TABLES | SEARCH_TABLES | {"alembic_version"}


def _search_layout(settings: Settings) -> tuple[str, set[str], str]:
    """(the schema pg_trgm is in, the search indexes, the trigram index's operator class)."""
    with _connect(settings) as conn:
        schema = conn.execute(
            text(
                "SELECT n.nspname FROM pg_extension e JOIN pg_namespace n "
                "ON n.oid = e.extnamespace WHERE e.extname = 'pg_trgm'"
            )
        ).scalar()
        indexes = set(
            conn.execute(
                text(
                    "SELECT indexname FROM pg_indexes WHERE indexname LIKE 'ix_concept_search%' "
                    "OR indexname LIKE 'ix_concept_trigram%'"
                )
            ).scalars()
        )
        opclass = conn.execute(
            text(
                "SELECT n.nspname || '.' || o.opcname FROM pg_index i "
                "JOIN pg_opclass o ON o.oid = i.indclass[0] "
                "JOIN pg_namespace n ON n.oid = o.opcnamespace "
                "WHERE i.indexrelid = 'ix_concept_trigram_text'::regclass"
            )
        ).scalar()
    return schema, indexes, opclass


def test_pg_trgm_goes_into_the_extensions_schema_when_there_is_one(
    empty_postgres_settings: Settings,
) -> None:
    """Supabase keeps extensions in `extensions` (on the search path); the migration does too,
    and qualifies the operator class, so the search works whatever the search path."""
    settings = empty_postgres_settings
    engine = create_db_provider(settings).migration_engine()
    try:
        with engine.begin() as conn:
            conn.execute(text("CREATE SCHEMA extensions"))
    finally:
        engine.dispose()
    upgrade_to_head(settings)
    assert _search_layout(settings) == ("extensions", SEARCH_INDEXES, "extensions.gin_trgm_ops")
    with session_scope(settings) as session:
        concepts = ConceptManager(session, settings)
        concepts.create("Policy gradient", "Ascend the return.", "paper", aliases=["PG"])
        assert [h.kind for h in concepts.search_hits("dient")] == ["substring"]
        assert [h.kind for h in concepts.search_hits("polcy")] == ["fuzzy"]
        assert concepts.check() == []


def test_an_installed_pg_trgm_is_used_where_it_is(empty_postgres_settings: Settings) -> None:
    settings = empty_postgres_settings
    engine = create_db_provider(settings).migration_engine()
    try:
        with engine.begin() as conn:
            conn.execute(text("CREATE SCHEMA extensions"))
            conn.execute(text("CREATE SCHEMA trgm"))
            conn.execute(text("CREATE EXTENSION pg_trgm WITH SCHEMA trgm"))
    finally:
        engine.dispose()
    upgrade_to_head(settings)
    assert _search_layout(settings) == ("trgm", SEARCH_INDEXES, "trgm.gin_trgm_ops")


def test_the_checks_are_named_as_in_the_models(settings: Settings) -> None:
    """Every model CHECK exists by name, except the json_valid() ones, which are SQLite-only;
    the two-argument min/max and json_type() checks exist in their PostgreSQL form."""
    with _connect(settings) as conn:
        inspector = inspect(conn)
        for table in Base.metadata.sorted_tables:
            live = {c["name"]: c["sqltext"] for c in inspector.get_check_constraints(table.name)}
            for check in table.constraints:
                if not isinstance(check, CheckConstraint):
                    continue
                if "json_valid(" in str(check.sqltext):
                    assert check.name not in live, (table.name, check.name)
                else:
                    assert check.name in live, (table.name, check.name)
    with _connect(settings) as conn:
        inspector = inspect(conn)
        pair = {c["name"]: c["sqltext"] for c in inspector.get_check_constraints("concept_relations")}
        assert "LEAST" in pair["ck_concept_relations_pair"]
        missing = {c["name"]: c["sqltext"] for c in inspector.get_check_constraints("review_attempts")}
        assert "json_typeof" in missing["ck_review_attempts_missing_array"]


def _concepts(conn: Connection) -> None:
    conn.execute(
        text(
            "INSERT INTO concepts (id, slug, name, name_norm, summary, origin) VALUES "
            "(1, 'a', 'A', 'a', 'S.', 'paper'), (2, 'b', 'B', 'b', 'S.', 'paper')"
        )
    )


def test_the_slug_trigger(settings: Settings) -> None:
    with _connect(settings) as conn:
        _concepts(conn)
        conn.execute(text("UPDATE concepts SET name = 'A2', slug = 'a' WHERE id = 1"))  # same slug
        with pytest.raises(IntegrityError, match="concepts.slug is immutable"):
            conn.execute(text("UPDATE concepts SET slug = 'renamed' WHERE id = 1"))


def test_the_relation_pair_check_uses_least_and_greatest(settings: Settings) -> None:
    insert = (
        "INSERT INTO concept_relations (type, from_id, to_id, low_id, high_id, rationale, origin) "
        "VALUES ('builds_on', 2, 1, {low}, {high}, 'r', 'paper')"
    )
    with _connect(settings) as conn:
        _concepts(conn)
        conn.execute(text(insert.format(low=1, high=2)))
        conn.execute(text("DELETE FROM concept_relations"))
        with pytest.raises(IntegrityError, match="ck_concept_relations_pair"):
            conn.execute(text(insert.format(low=2, high=1)))


def test_json_columns_and_boolean_defaults(settings: Settings) -> None:
    with _connect(settings) as conn:
        conn.execute(
            text(
                "INSERT INTO artifacts (id, slug, kind, title, dir_path, generator) "
                "VALUES (1, 'a', 'study_page', 'A', 'artifacts/a', 'test')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO artifact_blocks (id, artifact_id, position, key, type, content_file, "
                "content_sha256) VALUES (1, 1, 1, 'b01', 'blurb', 'b01.json', 'x')"
            )
        )
        assert conn.execute(text("SELECT is_gate FROM artifact_blocks")).scalar() is False
        assert conn.execute(text("SELECT params::text FROM artifacts")).scalar() == "{}"
        insert = (
            "INSERT INTO block_responses (block_id, attempt, response, passed, grader, verdict, "
            "missing) VALUES (1, :attempt, '{}', false, 'g', 'failed', :missing)"
        )
        conn.execute(text(insert), {"attempt": 1, "missing": "[]"})
        nested = conn.begin_nested()
        with pytest.raises(IntegrityError, match="ck_block_responses_missing_array"):
            conn.execute(text(insert), {"attempt": 2, "missing": "{}"})
        nested.rollback()
        with pytest.raises(DBAPIError, match="invalid input syntax for type json"):
            conn.execute(text(insert), {"attempt": 3, "missing": "not json"})


def test_the_account_tables_have_row_level_security(settings: Settings) -> None:
    """0017 enables RLS (with no policy) on users and user_sessions, 0018 on offline_attempts
    and 0019 on highlights and offline_review_answers, so Supabase's Data API roles see none of
    their rows; the app, connecting as the tables' owner, is not affected."""
    with _connect(settings) as conn:
        rls = dict(
            conn.execute(
                text(
                    "SELECT relname, relrowsecurity FROM pg_class WHERE relname IN "
                    "('users', 'user_sessions', 'offline_attempts', 'highlights', "
                    "'offline_review_answers', 'api_calls')"
                )
            ).all()
        )
        assert rls == {
            "users": True, "user_sessions": True, "offline_attempts": True, "highlights": True,
            "offline_review_answers": True, "api_calls": False,
        }
        # A role like Supabase's `anon`, with SELECT on the table (rolled back with the rest).
        probe = f"rls_probe_{uuid.uuid4().hex[:12]}"
        conn.execute(text(f"CREATE ROLE {probe} NOLOGIN"))
        conn.execute(text(f"GRANT {probe} TO CURRENT_USER"))
        conn.execute(text(f"GRANT SELECT ON users TO {probe}"))
        conn.execute(text("INSERT INTO users (email, password_hash) VALUES ('a@b.io', 'h')"))
        assert conn.execute(text("SELECT count(*) FROM users")).scalar() == 1  # the owner sees it
        conn.execute(text(f"SET LOCAL ROLE {probe}"))
        assert conn.execute(text("SELECT count(*) FROM users")).scalar() == 0  # others do not
