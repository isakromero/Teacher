"""ChatManager: threads per artifact, message numbering and validation, summaries, cascades."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DataError, IntegrityError
from sqlalchemy.orm import Session

from teacher.config import Settings
from teacher.db.models import Artifact, ChatMessage, ChatThread
from teacher.managers import ArtifactManager, ChatManager, NotFoundError
from teacher.managers.chat import PREVIEW_CHARS, make_preview
from tests.conftest import (
    CHECK_VIOLATION,
    POSTGRES_BAD_JSON,
    UNIQUE_VIOLATION,
    raw_json,
)

CONTENT = [{"type": "text", "text": "hello"}]


@pytest.fixture
def artifacts(session: Session, settings: Settings) -> ArtifactManager:
    return ArtifactManager(session, settings)


@pytest.fixture
def artifact(artifacts: ArtifactManager) -> Artifact:
    return artifacts.create(slug="guide-1", kind="study_page", title="Guide", generator="g/1")


@pytest.fixture
def chat(session: Session, settings: Settings) -> ChatManager:
    return ChatManager(session, settings)


def user(chat: ChatManager, thread: ChatThread, text: str = "hi", **kw) -> ChatMessage:
    return chat.add_message(thread, role="user", text=text, api_content=CONTENT, **kw)


def assistant(chat: ChatManager, thread: ChatThread, text: str = "hello", **kw) -> ChatMessage:
    return chat.add_message(thread, role="assistant", text=text, api_content=CONTENT, **kw)


# -- threads --------------------------------------------------------------------------------


def test_create_get_and_list_threads(
    chat: ChatManager, artifact: Artifact, artifacts: ArtifactManager, session: Session
) -> None:
    other = artifacts.create(slug="other", kind="study_page", title="Other", generator="g/1")
    first = chat.create_thread(artifact)
    second = chat.create_thread(artifact)
    elsewhere = chat.create_thread(other)
    first.created_at -= timedelta(minutes=5)  # make the order by time unambiguous
    session.flush()

    assert first.id and first.artifact_id == artifact.id and first.created_at is not None
    assert chat.get_thread(artifact, first.id) is first
    assert chat.list_threads(artifact) == [second, first]  # newest first
    assert chat.list_threads(other) == [elsewhere]
    assert artifact.chat_threads == [first, second]


def test_get_thread_of_another_artifact_is_not_found(
    chat: ChatManager, artifact: Artifact, artifacts: ArtifactManager
) -> None:
    other = artifacts.create(slug="other", kind="study_page", title="Other", generator="g/1")
    thread = chat.create_thread(other)
    with pytest.raises(NotFoundError, match="has no chat thread"):
        chat.get_thread(artifact, thread.id)
    with pytest.raises(NotFoundError):
        chat.get_thread(artifact, 999)


def test_create_thread_requires_a_saved_artifact(chat: ChatManager) -> None:
    with pytest.raises(ValueError, match="saved"):
        chat.create_thread(Artifact(slug="x", kind="k", title="t", dir_path="d", generator="g"))


# -- messages -------------------------------------------------------------------------------


def test_add_message_numbers_positions_per_thread(chat: ChatManager, artifact: Artifact) -> None:
    a, b = chat.create_thread(artifact), chat.create_thread(artifact)
    m1 = user(chat, a, "first", references=[{"block_key": "b01", "text": "quote"}],
              context_blocks=[{"key": "b01", "revealed": False}])
    m2 = assistant(chat, a, "answer", model="claude-test", input_tokens=10, output_tokens=3)
    m3 = user(chat, b, "other thread", kind="check_understanding")
    m4 = user(chat, a, "", kind="check_understanding")

    assert (m1.position, m2.position, m3.position, m4.position) == (1, 2, 1, 3)
    assert chat.messages(a) == [m1, m2, m4]
    assert a.messages == [m1, m2, m4]
    assert m1.references == [{"block_key": "b01", "text": "quote"}]
    assert m1.context_blocks == [{"key": "b01", "revealed": False}]
    assert m1.model is None and m1.input_tokens is None
    assert m2.references == [] and m2.context_blocks == []
    assert (m2.model, m2.input_tokens, m2.output_tokens) == ("claude-test", 10, 3)
    assert m4.text == "" and m4.kind == "check_understanding"


def test_messages_round_trip_after_reload(
    chat: ChatManager, artifact: Artifact, session: Session
) -> None:
    thread = chat.create_thread(artifact)
    content = [{"type": "thinking", "thinking": "", "signature": "sig"},
               {"type": "text", "text": "ü $x^2$", "citations": None}]
    chat.add_message(thread, role="assistant", text="ü $x^2$", api_content=content)
    session.commit()
    session.expire_all()
    (loaded,) = chat.messages(thread, with_api_content=True)
    assert loaded.api_content == content
    assert loaded.created_at is not None


def test_api_content_is_deferred(chat: ChatManager, artifact: Artifact, session: Session) -> None:
    thread = chat.create_thread(artifact)
    user(chat, thread)
    session.commit()
    session.expire_all()
    (lazy,) = chat.messages(thread)
    assert "api_content" not in lazy.__dict__
    session.expire_all()
    (eager,) = chat.messages(thread, with_api_content=True)
    assert eager.__dict__["api_content"] == CONTENT


@pytest.mark.parametrize(
    ("kwargs", "error", "match"),
    [
        ({"role": "system"}, ValueError, "invalid role"),
        ({"kind": "quiz"}, ValueError, "invalid kind"),
        ({"text": None}, TypeError, "text must be a string"),
        ({"api_content": []}, ValueError, "must not be empty"),
        ({"api_content": "text"}, TypeError, "api_content must be a list"),
        ({"api_content": [{"x": float("nan")}]}, ValueError, "not JSON-serialisable"),
        ({"references": [{"block_key": "b01"}]}, ValueError, "exactly block_key and text"),
        ({"references": [{"block_key": "", "text": "q"}]}, ValueError, "non-empty strings"),
        ({"context_blocks": [{"key": "b01"}]}, ValueError, "exactly key and revealed"),
        ({"context_blocks": [{"key": "b01", "revealed": 1}]}, ValueError, "must be a bool"),
        ({"model": "claude-test"}, ValueError, "user message has no model"),
        ({"input_tokens": 5}, ValueError, "user message has no model"),
        ({"role": "assistant", "references": [{"block_key": "b", "text": "t"}]}, ValueError,
         "assistant message has no references"),
        ({"role": "assistant", "kind": "check_understanding"}, ValueError, "kind 'message'"),
        ({"role": "assistant", "output_tokens": -1}, ValueError, "non-negative"),
        ({"role": "assistant", "input_tokens": True}, ValueError, "non-negative"),
    ],
)
def test_add_message_validation(
    chat: ChatManager, artifact: Artifact, session: Session, kwargs, error, match
) -> None:
    thread = chat.create_thread(artifact)
    args = {"role": "user", "text": "hi", "api_content": CONTENT, **kwargs}
    with pytest.raises(error, match=match):
        chat.add_message(thread, **args)
    assert session.scalar(select(func.count()).select_from(ChatMessage)) == 0


def test_database_checks(
    chat: ChatManager, artifact: Artifact, session: Session, test_db: str
) -> None:
    thread = chat.create_thread(artifact)
    session.flush()
    # Text that is not JSON: SQLite's json_valid CHECKs reject it, Postgres' json type does.
    bad_json = (
        (DataError, POSTGRES_BAD_JSON)
        if test_db == "postgres"
        else (IntegrityError, CHECK_VIOLATION)
    )
    for sql, (error, message) in (
        ("INSERT INTO chat_messages (thread_id, position, role, text, api_content) "
         "VALUES (:t, 1, 'tool', 'x', '[]')", (IntegrityError, CHECK_VIOLATION)),
        ("INSERT INTO chat_messages (thread_id, position, role, kind, text, api_content) "
         "VALUES (:t, 1, 'user', 'quiz', 'x', '[]')", (IntegrityError, CHECK_VIOLATION)),
        ("INSERT INTO chat_messages (thread_id, position, role, text, api_content) "
         "VALUES (:t, 1, 'user', 'x', 'not json')", bad_json),
        ('INSERT INTO chat_messages (thread_id, position, role, text, api_content, "references") '
         "VALUES (:t, 1, 'user', 'x', '[]', 'nope')", bad_json),
    ):  # fmt: skip
        with pytest.raises(error, match=message):
            with session.begin_nested():
                session.execute(text(sql), {"t": thread.id})
    session.execute(
        text("INSERT INTO chat_messages (thread_id, position, role, text, api_content) "
             "VALUES (:t, 1, 'user', 'x', '[]')"),
        {"t": thread.id},
    )
    row = session.execute(
        text('SELECT kind, "references", context_blocks, created_at FROM chat_messages')
    ).one()
    assert (row[0], raw_json(row[1]), raw_json(row[2])) == ("message", [], [])
    assert row[3] is not None
    with pytest.raises(IntegrityError, match=UNIQUE_VIOLATION):
        with session.begin_nested():
            session.execute(
                text("INSERT INTO chat_messages (thread_id, position, role, text, api_content) "
                     "VALUES (:t, 1, 'user', 'x', '[]')"),
                {"t": thread.id},
            )


# -- summaries ------------------------------------------------------------------------------


def test_summaries(chat: ChatManager, artifact: Artifact) -> None:
    empty, quiz, chatty = (chat.create_thread(artifact) for _ in range(3))
    user(chat, quiz, "", kind="check_understanding")
    assistant(chat, quiz, "What does attention compute?")
    user(chat, chatty, "", kind="check_understanding")
    assistant(chat, chatty, "A question")
    user(chat, chatty, "  Why   does\nit scale?  ")
    user(chat, chatty, "later text")

    summaries = chat.summaries([chatty, empty, quiz])
    assert [s.thread for s in summaries] == [chatty, empty, quiz]
    assert [(s.message_count, s.preview) for s in summaries] == [
        (4, "Why does it scale?"),
        (0, None),
        (2, None),  # no user text yet: an empty check-understanding turn has no preview
    ]
    assert chat.summary(empty).message_count == 0
    assert chat.summaries([]) == []


def test_make_preview() -> None:
    assert make_preview("short") == "short"
    exact = "x" * PREVIEW_CHARS
    assert make_preview(exact) == exact
    long = make_preview("word " * 40)
    assert len(long) <= PREVIEW_CHARS and long.endswith("…")


# -- cascades -------------------------------------------------------------------------------


def test_deleting_the_artifact_deletes_threads_and_messages(
    chat: ChatManager, artifact: Artifact, artifacts: ArtifactManager, session: Session
) -> None:
    keep = artifacts.create(slug="keep", kind="study_page", title="Keep", generator="g/1")
    thread = chat.create_thread(artifact)
    user(chat, thread)
    assistant(chat, thread)
    kept = chat.create_thread(keep)
    user(chat, kept)
    session.commit()

    artifacts.delete(artifact)
    session.commit()
    assert session.scalars(select(ChatThread)).all() == [kept]
    assert session.scalar(select(func.count()).select_from(ChatMessage)) == 1


def test_database_cascade_without_the_orm(
    chat: ChatManager, artifact: Artifact, session: Session
) -> None:
    thread = chat.create_thread(artifact)
    user(chat, thread)
    session.commit()
    session.execute(text("DELETE FROM artifacts WHERE id = :a"), {"a": artifact.id})
    assert session.execute(text("SELECT count(*) FROM chat_threads")).scalar() == 0
    assert session.execute(text("SELECT count(*) FROM chat_messages")).scalar() == 0


# -- blocks the tutor showed and attempts at its exercises ------------------------------------

MC = {"question": "Q?", "options": ["a", "b"], "private": {"correct_index": 0, "explanation": "e"}}
CHART = {"kind": "bar", "title": "t"}


def test_blocks_attempts_and_reporting(
    chat: ChatManager, artifact: Artifact, artifacts: ArtifactManager, session: Session
) -> None:
    from teacher.managers import ChatBlockDraft, ChatBlockProgress

    thread = chat.create_thread(artifact)
    asked = user(chat, thread)
    reply = assistant(chat, thread, text="Look here. And answer this.")
    assert chat.add_blocks(reply, []) == [] and chat.blocks([asked, reply]) == {}

    chart, question = chat.add_blocks(
        reply,
        [ChatBlockDraft("chart", False, CHART, 10), ChatBlockDraft("multiple_choice", True, MC, 27)],
    )
    assert [(b.position, b.text_offset, b.type) for b in chat.blocks([asked, reply])[reply.id]] == [
        (1, 10, "chart"), (2, 27, "multiple_choice")
    ]
    assert question.content == MC and reply.blocks == [chart, question]
    assert chat.get_block(artifact, question.id) is question
    other = artifacts.create(slug="other", kind="study_page", title="Other", generator="g/1")
    for owner, block_id in ((other, question.id), (artifact, 999)):
        with pytest.raises(NotFoundError):
            chat.get_block(owner, block_id)

    with pytest.raises(ValueError, match="already has blocks"):
        chat.add_blocks(reply, [ChatBlockDraft("chart", False, CHART, 0)])
    with pytest.raises(ValueError, match="only an assistant message"):
        chat.add_blocks(asked, [ChatBlockDraft("chart", False, CHART, 0)])
    second = assistant(chat, thread, text="abc")
    for drafts, match in [
        ([ChatBlockDraft("chart", False, CHART, 4)], "text_offset 4 must be between 0 and"),
        ([ChatBlockDraft("chart", False, CHART, 2), ChatBlockDraft("chart", False, CHART, 1)],
         "text_offset 1 must be between 2 and"),
        ([ChatBlockDraft(" ", False, CHART, 0)], "type must be a non-empty string"),
    ]:
        with pytest.raises(ValueError, match=match):
            chat.add_blocks(second, drafts)
    with pytest.raises(TypeError, match="content must be a dict"):
        chat.add_blocks(second, [ChatBlockDraft("chart", False, [], 0)])  # type: ignore[arg-type]

    assert chat.progress([chart, question]) == {
        chart.id: ChatBlockProgress(), question.id: ChatBlockProgress()
    }
    with pytest.raises(ValueError, match="not an exercise"):
        chat.record_response(chart, response={"choice": 0}, passed=True, grader="g")
    wrong = chat.record_response(
        question, response={"choice": 1}, passed=False, grader="g", feedback="No."
    )
    right = chat.record_response(
        question, response={"choice": 0}, passed=True, grader="g", details={"score": 1}
    )
    assert (wrong.attempt, right.attempt) == (1, 2)
    assert (wrong.verdict, wrong.missing, right.verdict) == ("failed", [], "passed")
    assert chat.responses(question) == [wrong, right]
    assert chat.progress([question])[question.id] == ChatBlockProgress(2, True)
    with pytest.raises(ValueError, match="only for one"):
        chat.record_response(question, response={}, passed=False, grader="g", verdict="partial")

    assert chat.unreported_responses(thread) == [wrong, right]
    assert chat.unreported_responses(chat.create_thread(artifact)) == []
    with pytest.raises(ValueError, match="reported by a user message"):
        chat.mark_reported([wrong], reply)
    told = user(chat, thread, text="")
    chat.mark_reported([wrong], told)
    assert chat.unreported_responses(thread) == [right]
    assert chat.reported([asked, reply, told]) == {told.id: [wrong]}
    with pytest.raises(ValueError, match="already reported"):
        chat.mark_reported([wrong], told)

    # Deleting the reply takes its blocks and their attempts with it.
    session.delete(reply)
    session.flush()
    assert chat.blocks([reply]) == {} and chat.unreported_responses(thread) == []
