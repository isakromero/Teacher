"""The cached engine is usable from several threads (web threadpool + generation jobs)."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import text

from teacher.config import Settings
from teacher.db.engine import BUSY_TIMEOUT_MS, get_engine, session_scope
from teacher.db.models import KnowledgeSource


def test_connection_pragmas(settings: Settings) -> None:
    with get_engine(settings).connect() as conn:
        assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1
        assert conn.execute(text("PRAGMA busy_timeout")).scalar() == BUSY_TIMEOUT_MS
        assert conn.execute(text("PRAGMA journal_mode")).scalar() == "wal"


def test_connection_reused_across_threads(settings: Settings) -> None:
    engine = get_engine(settings)
    # Open (and pool) a connection in this thread, then use the pool from other threads.
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))

    def count_in_thread(_: int) -> int:
        with session_scope(settings) as session:
            return session.query(KnowledgeSource).count()

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(count_in_thread, range(20))) == [0] * 20


def test_concurrent_writers_wait_instead_of_failing(settings: Settings) -> None:
    errors: list[BaseException] = []
    barrier = threading.Barrier(4)

    def write(n: int) -> None:
        try:
            barrier.wait()
            for i in range(5):
                with session_scope(settings) as session:
                    session.add(
                        KnowledgeSource(
                            slug=f"s-{n}-{i}",
                            kind="note",
                            title="t",
                            dir_path=f"knowledge_sources/s-{n}-{i}",
                            primary_file="source.txt",
                            media_type="text/plain",
                            content_sha256=f"{n}-{i}",
                        )
                    )
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=write, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    with session_scope(settings) as session:
        assert session.query(KnowledgeSource).count() == 20


def _source(slug: str) -> KnowledgeSource:
    return KnowledgeSource(
        slug=slug,
        kind="note",
        title="t",
        dir_path=f"knowledge_sources/{slug}",
        primary_file="source.txt",
        media_type="text/plain",
        content_sha256=slug,
    )


def test_a_write_after_a_read_survives_a_commit_in_between(settings: Settings) -> None:
    """A session that read, then writes after another connection committed, must not fail with
    "database is locked" (a transaction begun before the read would hold a stale snapshot)."""
    with session_scope(settings) as reader:
        assert reader.query(KnowledgeSource).count() == 0
        with session_scope(settings) as other:
            other.add(_source("other"))
        reader.add(_source("mine"))
    with session_scope(settings) as session:
        assert session.query(KnowledgeSource).count() == 2


def test_a_savepoint_stays_inside_the_callers_transaction(settings: Settings) -> None:
    with session_scope(settings) as session:
        with session.begin_nested():
            session.add(_source("kept"))
        session.flush()
        session.rollback()
    with session_scope(settings) as session:
        assert session.query(KnowledgeSource).count() == 0
