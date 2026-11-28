"""The database providers (`teacher.db.providers`) and the database settings: `$TEACHER_DB` and
`$SUPABASE_DB_URL` parsing, the supabase default of `Settings.from_env()`, the data root per
mode, the connection string never shown, the transaction-pooler port, the factory's lazy import,
`teacher --db` and `teacher db info`; and, on Postgres runs, the UTC session time zone, floats
read exactly, and SQLite's transaction behaviour (snapshot reads, then one writer at a time)."""

from __future__ import annotations

import logging
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.pool import NullPool

from teacher import cli
from teacher.config import (
    DB_KINDS,
    DEFAULT_DB_KIND,
    ConfigError,
    Settings,
    default_db_kind,
)
from teacher.db.engine import get_engine, session_scope
from teacher.db.models import utcnow
from teacher.db.providers import SqliteDbProvider, create_db_provider

PASSWORD = "s3cr3t-Pa55"
URL = f"postgresql://postgres.abcd:{PASSWORD}@db.example.supabase.co:5432/postgres"
POOLER_URL = f"postgres://postgres.abcd:{PASSWORD}@pooler.example.supabase.com:6543/postgres"


def supabase(tmp_path: Path, url: str = URL) -> Settings:
    return Settings(root=tmp_path, db="supabase", supabase_db_url=url)


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> pytest.MonkeyPatch:
    """A clean database environment rooted at `tmp_path` (restored after the test)."""
    monkeypatch.delenv("TEACHER_DB", raising=False)
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.setenv("TEACHER_HOME", str(tmp_path))
    return monkeypatch


# -- $TEACHER_DB and the default ------------------------------------------------------------


def test_the_kinds_and_the_default() -> None:
    assert DB_KINDS == ("supabase", "sqlite")
    assert DEFAULT_DB_KIND == "supabase"


def test_db_kind_defaults_to_supabase_when_unset(env: pytest.MonkeyPatch) -> None:
    assert default_db_kind() == "supabase"


@pytest.mark.parametrize("value", ["sqlite", " SQLite ", "SQLITE"])
def test_db_kind_reads_the_variable(env: pytest.MonkeyPatch, value: str) -> None:
    env.setenv("TEACHER_DB", value)
    assert default_db_kind() == "sqlite"


@pytest.mark.parametrize("value", ["", "   "])
def test_a_blank_db_kind_is_an_error(env: pytest.MonkeyPatch, value: str) -> None:
    env.setenv("TEACHER_DB", value)
    with pytest.raises(ConfigError, match=r"TEACHER_DB is set but empty.*supabase, sqlite"):
        default_db_kind()
    with pytest.raises(ConfigError, match="TEACHER_DB"):
        Settings.from_env()


def test_an_unknown_db_kind_is_an_error(env: pytest.MonkeyPatch) -> None:
    env.setenv("TEACHER_DB", "mysql")
    with pytest.raises(ConfigError, match=r"'mysql'; choose one of: supabase, sqlite"):
        Settings.from_env()
    with pytest.raises(ConfigError, match="choose one of"):
        Settings(root=Path("unused"), db="postgres")


# -- Settings.from_env ------------------------------------------------------------------------


def test_from_env_defaults_to_supabase_and_needs_the_url(env: pytest.MonkeyPatch) -> None:
    with pytest.raises(ConfigError) as caught:
        Settings.from_env()
    message = str(caught.value)
    assert "SUPABASE_DB_URL" in message
    assert ".env" in message
    assert "TEACHER_DB=sqlite" in message
    env.setenv("SUPABASE_DB_URL", "   ")  # blank is missing, not a fallback
    with pytest.raises(ConfigError, match="SUPABASE_DB_URL"):
        Settings.from_env()


def test_from_env_in_supabase_mode(env: pytest.MonkeyPatch, tmp_path: Path) -> None:
    env.setenv("SUPABASE_DB_URL", URL)
    settings = Settings.from_env()
    assert settings.db == "supabase"
    assert settings.root == tmp_path.resolve()
    assert settings.supabase_db_url == URL.replace("postgresql://", "postgresql+psycopg://", 1)


def test_from_env_in_sqlite_mode_ignores_the_url(env: pytest.MonkeyPatch, tmp_path: Path) -> None:
    env.setenv("TEACHER_DB", "sqlite")
    env.setenv("SUPABASE_DB_URL", "not even a url")
    settings = Settings.from_env()
    assert settings == Settings(root=tmp_path)
    assert settings.supabase_db_url is None


def test_a_constructed_settings_is_sqlite(tmp_path: Path) -> None:
    settings = Settings(root=tmp_path)
    assert settings.db == "sqlite"
    assert isinstance(create_db_provider(settings), SqliteDbProvider)


@pytest.mark.parametrize(
    "given",
    [
        URL,
        URL.replace("postgresql://", "postgres://", 1),
        URL.replace("postgresql://", "postgresql+psycopg://", 1),
        URL.replace("postgresql://", "POSTGRESQL://", 1),
    ],
)
def test_the_url_is_normalised_to_psycopg(tmp_path: Path, given: str) -> None:
    assert supabase(tmp_path, given).supabase_db_url == (
        f"postgresql+psycopg://postgres.abcd:{PASSWORD}@db.example.supabase.co:5432/postgres"
    )


@pytest.mark.parametrize(
    ("given", "error"),
    [
        (f"mysql://u:{PASSWORD}@h/db", "starting with postgresql:// or postgres://"),
        (f"u:{PASSWORD}@h/db", "no scheme"),
        (f"postgresql://u:{PASSWORD}@/db", "names no host"),
        (f"postgresql://u:{PASSWORD}@h:port/db", "cannot be read"),
    ],
)
def test_a_bad_url_is_an_error_that_does_not_echo_it(
    tmp_path: Path, given: str, error: str
) -> None:
    with pytest.raises(ConfigError, match=error) as caught:
        supabase(tmp_path, given)
    assert PASSWORD not in str(caught.value)
    # nothing chained that could print the string
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None or caught.value.__suppress_context__


def test_settings_kind_and_url_must_agree(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="needs supabase_db_url"):
        Settings(root=tmp_path, db="supabase")
    with pytest.raises(ConfigError, match="only used with db='supabase'"):
        Settings(root=tmp_path, supabase_db_url=URL)


# -- the data root ----------------------------------------------------------------------------


def test_sqlite_keeps_the_layout_under_root(tmp_path: Path) -> None:
    settings = Settings(root=tmp_path)
    root = tmp_path.resolve()
    assert settings.data_root == root
    assert settings.db_path == root / "teacher.db"
    assert settings.sources_dir == root / "knowledge_sources"
    assert settings.backups_dir == root / "backups"


def test_supabase_files_live_under_root_supabase(tmp_path: Path) -> None:
    settings = supabase(tmp_path)
    data = tmp_path.resolve() / "supabase"
    assert settings.data_root == data
    assert settings.sources_dir == data / "knowledge_sources"
    assert settings.artifacts_dir == data / "artifacts"
    assert settings.derived_dir == data / "derived"
    assert settings.backups_dir == data / "backups"
    with pytest.raises(ConfigError, match="SQLite file"):
        settings.db_path  # noqa: B018
    with pytest.raises(ConfigError, match="SQLite file"):
        settings.db_url  # noqa: B018


# -- the connection string is never shown -----------------------------------------------------


def test_the_url_is_never_shown(tmp_path: Path) -> None:
    settings = supabase(tmp_path)
    provider = create_db_provider(settings)
    ours = [repr(settings), str(settings), provider.describe(), provider.cache_key, repr(provider)]
    for text_ in ours:
        assert PASSWORD not in text_, text_
        assert "postgres.abcd" not in text_, text_  # not even the user
    engine = provider.create_engine()
    try:
        for text_ in (repr(engine), str(engine.url)):  # SQLAlchemy masks the password
            assert PASSWORD not in text_, text_
    finally:
        engine.dispose()
    assert provider.describe() == "supabase:db.example.supabase.co/postgres"
    assert provider.cache_key.startswith("supabase:db.example.supabase.co/postgres#")


def test_the_cache_key_tells_databases_apart(tmp_path: Path) -> None:
    one = create_db_provider(supabase(tmp_path))
    other = create_db_provider(supabase(tmp_path, URL.replace(PASSWORD, "other")))
    assert one.describe() == other.describe()
    assert one.cache_key != other.cache_key
    assert create_db_provider(Settings(root=tmp_path)).cache_key == Settings(root=tmp_path).db_url


# -- the Postgres engine ----------------------------------------------------------------------


def test_a_direct_connection_sets_utc_and_exact_floats_at_start(tmp_path: Path) -> None:
    provider = create_db_provider(supabase(tmp_path))
    assert not provider.uses_transaction_pooler
    assert provider.connect_args() == {"options": "-c timezone=UTC -c extra_float_digits=3"}


def test_the_transaction_pooler_turns_prepared_statements_off(tmp_path: Path) -> None:
    provider = create_db_provider(supabase(tmp_path, POOLER_URL))
    assert provider.uses_transaction_pooler
    assert provider.connect_args() == {"prepare_threshold": None}


def test_the_engine_pings_and_pools_modestly(tmp_path: Path) -> None:
    from teacher.db.providers.supabase import MAX_OVERFLOW, POOL_SIZE

    provider = create_db_provider(supabase(tmp_path))
    engine = provider.create_engine()
    try:
        assert engine.dialect.name == "postgresql" and engine.dialect.driver == "psycopg"
        assert engine.pool._pre_ping  # type: ignore[attr-defined]
        assert engine.pool.size() == POOL_SIZE  # type: ignore[attr-defined]
        assert engine.pool._max_overflow == MAX_OVERFLOW  # type: ignore[attr-defined]
    finally:
        engine.dispose()
    migration = provider.migration_engine()
    try:
        assert isinstance(migration.pool, NullPool)
    finally:
        migration.dispose()


class _FakeCursor:
    def __init__(self, zone: str, float_digits: str) -> None:
        self.row = (zone, float_digits)

    def execute(self, _sql: str) -> None:
        pass

    def fetchone(self) -> tuple[str, str]:
        return self.row

    def close(self) -> None:
        pass


class _FakeConnection:
    def __init__(self, zone: str, float_digits: str = "1") -> None:
        self.zone = zone
        self.float_digits = float_digits
        self.rolled_back = False

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self.zone, self.float_digits)

    def rollback(self) -> None:
        self.rolled_back = True


def test_a_connection_not_in_utc_is_refused() -> None:
    from teacher.db.providers.supabase import _check_session

    connection = _FakeConnection("UTC")
    _check_session(connection, None)
    assert connection.rolled_back
    _check_session(_FakeConnection("Etc/UTC"), None)
    with pytest.raises(ConfigError, match=r"'Europe/Stockholm', not UTC.*ALTER DATABASE"):
        _check_session(_FakeConnection("Europe/Stockholm"), None)


def test_a_connection_that_rounds_floats_is_refused() -> None:
    from teacher.db.providers.supabase import _check_session

    _check_session(_FakeConnection("UTC", "1"), None)
    _check_session(_FakeConnection("UTC", "3"), None)
    for rounding in ("0", "-15"):
        with pytest.raises(
            ConfigError, match=rf"extra_float_digits is {rounding}.*SET extra_float_digits TO 1"
        ):
            _check_session(_FakeConnection("UTC", rounding), None)


def test_supabase_takes_no_backup_and_says_so(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    provider = create_db_provider(supabase(tmp_path))
    with caplog.at_level(logging.WARNING, logger="teacher.db.providers.supabase"):
        assert provider.backup() is None
    (record,) = caplog.records
    assert "No backup taken" in record.getMessage()
    assert "Supabase's own backups" in record.getMessage()
    assert PASSWORD not in record.getMessage()
    assert not (tmp_path / "supabase" / "backups").exists()


# -- the factory ------------------------------------------------------------------------------


def test_the_factory_caches_per_settings(tmp_path: Path) -> None:
    settings = Settings(root=tmp_path)
    assert create_db_provider(settings) is create_db_provider(Settings(root=tmp_path))
    assert create_db_provider(settings) is not create_db_provider(supabase(tmp_path))
    assert create_db_provider(supabase(tmp_path)).name == "supabase"
    assert create_db_provider(settings).name == "sqlite"


def test_sqlite_mode_never_imports_psycopg_or_the_supabase_provider(tmp_path: Path) -> None:
    code = (
        "import sys\n"
        "from pathlib import Path\n"
        "from teacher.config import Settings\n"
        "from teacher.db.engine import session_scope\n"
        "from teacher.db.migrate import upgrade_to_head\n"
        "from teacher.db.providers import create_db_provider\n"
        f"s = Settings(root=Path({str(tmp_path)!r}))\n"
        "upgrade_to_head(s)\n"
        "with session_scope(s) as session: session.connection()\n"
        "assert create_db_provider(s).name == 'sqlite'\n"
        "loaded = sorted(m for m in sys.modules if m.startswith('psycopg') or m.endswith('.supabase'))\n"
        "print(loaded)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, timeout=120
    )
    assert result.stdout.strip() == "[]"


# -- the command line -------------------------------------------------------------------------


def test_cli_db_flag_overrides_the_variable(
    env: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env.setenv("TEACHER_DB", "supabase")  # restored after the test (main sets it)
    assert cli.main(["db", "info"]) == 1
    assert "SUPABASE_DB_URL" in capsys.readouterr().err

    assert cli.main(["--db", "sqlite", "db", "info"]) == 0
    out = capsys.readouterr().out
    assert f"Database: sqlite:{tmp_path.resolve() / 'teacher.db'}" in out
    assert "Revision: none (not migrated yet)" in out
    assert "Head revision: 0022" in out
    assert f"Data root: {tmp_path.resolve()}" in out


def test_cli_rejects_an_unknown_db(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        cli.main(["--db", "mysql", "db", "info"])
    assert "invalid choice" in capsys.readouterr().err


def test_cli_db_info_after_migrating(
    settings: Settings, test_db: str, env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    env.setenv("TEACHER_HOME", str(settings.root))
    if test_db == "postgres":
        env.setenv("SUPABASE_DB_URL", settings.supabase_db_url or "")
        argv = ["--db", "supabase", "db", "info"]
    else:
        argv = ["--db", "sqlite", "db", "info"]
    assert cli.main(argv) == 0
    out = capsys.readouterr().out
    assert f"Database: {create_db_provider(settings).describe()}" in out
    assert "Revision: 0022" in out
    assert f"Data root: {settings.data_root}" in out
    assert PASSWORD not in out and "teacher_test" not in out


# -- Postgres: the session time zone and naive UTC timestamps --------------------------------


@pytest.mark.postgres_only("the Postgres session time zone")
def test_postgres_sessions_run_in_utc_and_store_naive_utc(
    settings: Settings, postgres_admin: Any
) -> None:
    from teacher.db.engine import dispose_engine

    provider = create_db_provider(settings)
    postgres_admin(
        f'ALTER DATABASE "{provider.database}" SET timezone TO \'America/New_York\''
    )
    dispose_engine(settings)
    with get_engine(settings).connect() as conn:
        assert conn.execute(text("SELECT current_setting('TimeZone')")).scalar() == "UTC"
    before = utcnow()
    with session_scope(settings) as session:
        session.execute(
            text(
                "INSERT INTO artifacts (slug, kind, title, dir_path, generator) "
                "VALUES ('a', 'study_page', 'A', 'artifacts/a', 'test')"
            )
        )
        created = session.execute(text("SELECT created_at FROM artifacts")).scalar_one()
    assert created.tzinfo is None
    assert before - timedelta(seconds=5) <= created <= utcnow() + timedelta(seconds=5)


# -- Postgres: exact floats and SQLite's transaction behaviour --------------------------------


@pytest.mark.postgres_only("the Postgres float output setting")
def test_postgres_floats_are_read_exactly_whatever_the_server_setting(
    settings: Settings, postgres_admin: Any
) -> None:
    from teacher.db.engine import dispose_engine

    provider = create_db_provider(settings)
    postgres_admin(f'ALTER DATABASE "{provider.database}" SET extra_float_digits TO 0')
    dispose_engine(settings)
    value = 0.1 + 0.2  # 0.30000000000000004: rounded to 0.3 at extra_float_digits 0
    for engine in (get_engine(settings), provider.migration_engine()):
        with engine.connect() as conn:
            assert conn.execute(text("SELECT CAST(:v AS float8)"), {"v": value}).scalar() == value
            assert conn.execute(text("SELECT 0.1::float8 + 0.2::float8")).scalar() == value
    from teacher.db.models import API_CALL_PROVIDERS, API_CALL_PURPOSES, ApiCall

    with session_scope(settings) as session:
        session.add(
            ApiCall(
                purpose=API_CALL_PURPOSES[0], step="s", provider=API_CALL_PROVIDERS[0], model="m",
                input_tokens=1, cache_write_tokens=0, cache_read_tokens=0, output_tokens=1,
                cost_usd=value, priced_on="2026-11-25",
            )
        )  # fmt: skip
    with session_scope(settings) as session:
        assert session.scalar(select(ApiCall.cost_usd)) == value


def _add_artifact(session: Any, slug: str) -> None:
    session.execute(
        text(
            "INSERT INTO artifacts (slug, kind, title, dir_path, generator) "
            "VALUES (:s, 'study_page', :s, 'artifacts/' || :s, 'test')"
        ),
        {"s": slug},
    )


def _count_artifacts(session: Any) -> int:
    return session.execute(text("SELECT count(*) FROM artifacts")).scalar_one()


@pytest.mark.postgres_only("the Postgres transaction behaviour")
def test_postgres_reads_see_one_snapshot_until_the_first_write(settings: Settings) -> None:
    from teacher.db.engine import session_factory

    factory = session_factory(settings)
    with factory() as reader:
        assert _count_artifacts(reader) == 0
        assert reader.execute(text("SELECT current_setting('transaction_isolation')")).scalar() == (
            "repeatable read"
        )
        with session_scope(settings) as other:
            _add_artifact(other, "a")
        assert _count_artifacts(reader) == 0  # the same snapshot
        _add_artifact(reader, "b")  # the first write: a new transaction that sees "a"
        assert reader.execute(text("SELECT current_setting('transaction_isolation')")).scalar() == (
            "read committed"
        )
        assert _count_artifacts(reader) == 2
        reader.commit()
        assert _count_artifacts(reader) == 2


@pytest.mark.postgres_only("the Postgres transaction behaviour")
def test_postgres_a_read_then_a_write_of_a_row_changed_meanwhile_does_not_fail(
    settings: Settings,
) -> None:
    """REPEATABLE READ throughout would fail the update ("could not serialize access")."""
    from teacher.db.engine import session_factory

    with session_scope(settings) as session:
        _add_artifact(session, "a")
    with session_factory(settings)() as first:
        assert first.execute(text("SELECT title FROM artifacts")).scalar() == "a"
        with session_scope(settings) as other:
            other.execute(text("UPDATE artifacts SET title = 'other'"))
        first.execute(text("UPDATE artifacts SET title = title || ' and first'"))
        first.commit()
    with session_scope(settings) as session:
        assert session.execute(text("SELECT title FROM artifacts")).scalar() == "other and first"


@pytest.mark.postgres_only("the Postgres transaction behaviour")
def test_postgres_writers_take_turns(settings: Settings) -> None:
    import threading

    from teacher.db.engine import session_factory

    factory = session_factory(settings)
    second_done = threading.Event()
    errors: list[BaseException] = []

    def second() -> None:
        try:
            with session_scope(settings) as session:
                _add_artifact(session, "second")  # waits for the first writer
                seen.append(_count_artifacts(session))
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)
        finally:
            second_done.set()

    seen: list[int] = []
    with factory() as first:
        _add_artifact(first, "first")
        thread = threading.Thread(target=second)
        thread.start()
        assert not second_done.wait(1.0)  # blocked on the write lock
        first.commit()
    thread.join(10)
    assert errors == [] and seen == [2]


@pytest.mark.postgres_only("the Postgres transaction behaviour")
def test_postgres_a_writer_waits_for_the_lock_at_most_the_timeout(settings: Settings) -> None:
    from sqlalchemy.exc import OperationalError

    from teacher.db.engine import session_factory
    from teacher.db.providers.supabase import WRITE_LOCK_TIMEOUT_MS

    factory = session_factory(settings)
    with factory() as first, factory() as second:
        _add_artifact(first, "first")
        started = utcnow()
        with pytest.raises(OperationalError, match="lock timeout"):
            _add_artifact(second, "second")
        waited = (utcnow() - started).total_seconds()
        assert WRITE_LOCK_TIMEOUT_MS / 1000 - 0.5 <= waited < WRITE_LOCK_TIMEOUT_MS / 1000 + 5
        first.commit()


@pytest.mark.postgres_only("the Postgres transaction behaviour")
def test_postgres_an_explicit_isolation_level_is_left_alone(settings: Settings) -> None:
    with get_engine(settings).connect() as conn:
        conn = conn.execution_options(isolation_level="SERIALIZABLE")
        with conn.begin():
            conn.execute(
                text(
                    "INSERT INTO artifacts (slug, kind, title, dir_path, generator) "
                    "VALUES ('a', 'study_page', 'A', 'artifacts/a', 'test')"
                )
            )
            level = conn.execute(text("SELECT current_setting('transaction_isolation')")).scalar()
    assert level == "serializable"
