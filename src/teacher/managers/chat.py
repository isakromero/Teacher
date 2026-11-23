"""ChatManager: the tutor side chat's threads and messages.

A thread belongs to one artifact; its messages are numbered 1, 2, ... in `position` order. The
manager stores what it is given and checks its shape; what goes into a message (the tutor's
request content, which blocks it showed) is decided by `teacher.tutor`.

An assistant message may carry chat blocks (exercises, charts and interactables the tutor
showed, `add_blocks`), and the reader's attempts at a chat exercise are recorded like attempts at
a page gate (`record_response`). An attempt stays "unreported" until a user message tells the
tutor about it (`mark_reported`).
"""

from __future__ import annotations

import builtins
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import and_, case, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, undefer

from teacher.config import Settings
from teacher.db.models import (
    Artifact,
    ChatBlock,
    ChatBlockResponse,
    ChatMessage,
    ChatThread,
)
from teacher.managers.artifacts import (
    _is_unique_violation,
    _require_persistent,
    _validate_text,
)
from teacher.managers.block_responses import check_verdict
from teacher.managers.errors import DuplicateError, NotFoundError
from teacher.managers.study_history import StudyHistoryManager

ROLES: tuple[str, ...] = ("user", "assistant")
KINDS: tuple[str, ...] = ("message", "check_understanding")
PREVIEW_CHARS = 80


@dataclass(frozen=True, slots=True)
class ThreadSummary:
    """A thread with its message count and preview (the first non-empty user text)."""

    thread: ChatThread
    message_count: int
    preview: str | None


@dataclass(frozen=True, slots=True)
class ChatBlockDraft:
    """A block to store with an assistant message: its type, whether it is an exercise, its
    full validated content and how many characters of the message's text come before it."""

    type: str
    is_gate: bool
    content: dict[str, Any]
    text_offset: int


@dataclass(frozen=True, slots=True)
class ChatBlockProgress:
    """How far the reader is with one chat block: attempts made and whether one passed."""

    attempts: int = 0
    passed: bool = False


def _json_dict(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"{name} must be a dict, got {value.__class__.__name__}")
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as err:
        raise ValueError(f"{name} is not JSON-serialisable: {err}") from err
    return value


def _json_list(value: Any, name: str) -> builtins.list[Any]:
    if not isinstance(value, list):
        raise TypeError(f"{name} must be a list, got {value.__class__.__name__}")
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as err:
        raise ValueError(f"{name} is not JSON-serialisable: {err}") from err
    return value


def _references(value: Iterable[Mapping[str, Any]]) -> builtins.list[dict[str, str]]:
    result = []
    for number, item in enumerate(value, start=1):
        if not isinstance(item, Mapping) or set(item) != {"block_key", "text"}:
            raise ValueError(f"reference {number} must have exactly block_key and text")
        key, text = item["block_key"], item["text"]
        if not isinstance(key, str) or not key or not isinstance(text, str) or not text:
            raise ValueError(f"reference {number}: block_key and text must be non-empty strings")
        result.append({"block_key": key, "text": text})
    return result


def _context_blocks(value: Iterable[Mapping[str, Any]]) -> builtins.list[dict[str, Any]]:
    result = []
    for number, item in enumerate(value, start=1):
        if not isinstance(item, Mapping) or set(item) != {"key", "revealed"}:
            raise ValueError(f"context block {number} must have exactly key and revealed")
        if not isinstance(item["key"], str) or not item["key"]:
            raise ValueError(f"context block {number}: key must be a non-empty string")
        if not isinstance(item["revealed"], bool):
            raise ValueError(f"context block {number}: revealed must be a bool")
        result.append({"key": item["key"], "revealed": item["revealed"]})
    return result


def _optional_count(value: Any, name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer or None")
    return value


def make_preview(text: str) -> str:
    """`text` on one line, cut to at most PREVIEW_CHARS characters (with an ellipsis)."""
    flat = " ".join(text.split())
    if len(flat) <= PREVIEW_CHARS:
        return flat
    return flat[: PREVIEW_CHARS - 1].rstrip() + "…"


class ChatManager:
    """Create and read chat threads, and append messages to them."""

    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings

    # -- threads ----------------------------------------------------------------------------

    def create_thread(self, artifact: Artifact) -> ChatThread:
        """A new, empty thread for `artifact`."""
        _require_persistent(artifact, "artifact")
        thread = ChatThread(artifact_id=artifact.id)
        self.session.add(thread)
        self.session.flush()
        self.session.expire(artifact, ["chat_threads"])
        return thread

    def get_thread(self, artifact: Artifact, thread_id: int) -> ChatThread:
        """The thread with `thread_id` of `artifact`; NotFoundError if there is none (also when
        the thread belongs to another artifact)."""
        thread = self.session.get(ChatThread, thread_id)
        if thread is None or thread.artifact_id != artifact.id:
            raise NotFoundError(f"artifact {artifact.slug!r} has no chat thread {thread_id}")
        return thread

    def list_threads(self, artifact: Artifact) -> builtins.list[ChatThread]:
        """The artifact's threads, newest first."""
        return list(
            self.session.scalars(
                select(ChatThread)
                .where(ChatThread.artifact_id == artifact.id)
                .order_by(ChatThread.created_at.desc(), ChatThread.id.desc())
            )
        )

    def summaries(self, threads: Sequence[ChatThread]) -> builtins.list[ThreadSummary]:
        """Message count and preview for each thread, in the given order (two queries; message
        content sent to the API is not loaded)."""
        ids = [thread.id for thread in threads]
        if not ids:
            return []
        counts = dict(
            self.session.execute(
                select(ChatMessage.thread_id, func.count(ChatMessage.id))
                .where(ChatMessage.thread_id.in_(ids))
                .group_by(ChatMessage.thread_id)
            ).all()
        )
        has_text = and_(ChatMessage.role == "user", ChatMessage.text != "")
        first = (
            select(ChatMessage.thread_id, func.min(ChatMessage.position).label("position"))
            .where(ChatMessage.thread_id.in_(ids), has_text)
            .group_by(ChatMessage.thread_id)
            .subquery()
        )
        previews = dict(
            self.session.execute(
                select(ChatMessage.thread_id, ChatMessage.text).join(
                    first,
                    and_(
                        ChatMessage.thread_id == first.c.thread_id,
                        ChatMessage.position == first.c.position,
                    ),
                )
            ).all()
        )
        return [
            ThreadSummary(
                thread,
                counts.get(thread.id, 0),
                make_preview(previews[thread.id]) if thread.id in previews else None,
            )
            for thread in threads
        ]

    def summary(self, thread: ChatThread) -> ThreadSummary:
        return self.summaries([thread])[0]

    # -- messages ---------------------------------------------------------------------------

    def messages(
        self, thread: ChatThread, *, with_api_content: bool = False
    ) -> builtins.list[ChatMessage]:
        """The thread's messages, oldest first. `api_content` is deferred (loaded on access)
        unless `with_api_content` loads it in the same query."""
        stmt = (
            select(ChatMessage)
            .where(ChatMessage.thread_id == thread.id)
            .order_by(ChatMessage.position)
        )
        if with_api_content:
            stmt = stmt.options(undefer(ChatMessage.api_content))
        return list(self.session.scalars(stmt))

    def add_message(
        self,
        thread: ChatThread,
        *,
        role: str,
        text: str,
        api_content: builtins.list[dict[str, Any]],
        kind: str = "message",
        references: Iterable[Mapping[str, Any]] = (),
        context_blocks: Iterable[Mapping[str, Any]] = (),
        model: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
    ) -> ChatMessage:
        """Append a message at the next position (previous max + 1, starting at 1), in the
        open study session (`StudyHistoryManager.activity_session`).

        `api_content` is a non-empty JSON-serialisable list of content blocks. `references`
        (`{block_key, text}`) and `context_blocks` (`{key, revealed}`) are for user messages;
        `model` and the token counts are for assistant messages. ValueError/TypeError otherwise.
        """
        _require_persistent(thread, "thread")
        if role not in ROLES:
            raise ValueError(f"invalid role {role!r}: expected one of {ROLES}")
        if kind not in KINDS:
            raise ValueError(f"invalid kind {kind!r}: expected one of {KINDS}")
        if not isinstance(text, str):
            raise TypeError("text must be a string")
        _json_list(api_content, "api_content")
        if not api_content:
            raise ValueError("api_content must not be empty")
        refs = _references(references)
        context = _context_blocks(context_blocks)
        input_tokens = _optional_count(input_tokens, "input_tokens")
        output_tokens = _optional_count(output_tokens, "output_tokens")
        if role == "assistant":
            if refs or context:
                raise ValueError("an assistant message has no references or context blocks")
            if kind != "message":
                raise ValueError("an assistant message has kind 'message'")
        elif model is not None or input_tokens is not None or output_tokens is not None:
            raise ValueError("a user message has no model or token counts")

        last = self.session.scalar(
            select(func.max(ChatMessage.position)).where(ChatMessage.thread_id == thread.id)
        )
        position = (last or 0) + 1
        row = ChatMessage(
            thread_id=thread.id,
            position=position,
            role=role,
            kind=kind,
            text=text,
            references=refs,
            api_content=api_content,
            context_blocks=context,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            session_id=StudyHistoryManager(self.session, self.settings).activity_session().id,
        )
        try:
            with self.session.begin_nested():
                self.session.add(row)
                self.session.flush()
        except IntegrityError as err:
            if _is_unique_violation(err):
                raise DuplicateError(
                    f"chat thread {thread.id}: message {position} already exists"
                ) from err
            raise
        self.session.expire(thread, ["messages"])
        return row

    # -- blocks the tutor showed ------------------------------------------------------------

    def add_blocks(
        self, message: ChatMessage, drafts: Sequence[ChatBlockDraft]
    ) -> builtins.list[ChatBlock]:
        """Store `drafts` as the blocks of the assistant `message`, in order (positions 1..).

        ValueError for a user message, a message that already has blocks, an offset outside the
        message's text or offsets that go backwards; TypeError/ValueError for content that is
        not a JSON-serialisable dict.
        """
        _require_persistent(message, "message")
        if not drafts:
            return []
        if message.role != "assistant":
            raise ValueError("only an assistant message has blocks")
        if self.blocks([message]).get(message.id):
            raise ValueError(f"chat message {message.id} already has blocks")
        rows = []
        previous = 0
        for position, draft in enumerate(drafts, start=1):
            _validate_text(draft.type, "type")
            if not isinstance(draft.is_gate, bool):
                raise TypeError("is_gate must be a bool")
            offset = draft.text_offset
            if isinstance(offset, bool) or not isinstance(offset, int):
                raise TypeError("text_offset must be an integer")
            if not previous <= offset <= len(message.text):
                raise ValueError(
                    f"block {position}: text_offset {offset} must be between {previous} and "
                    f"the length of the message's text ({len(message.text)})"
                )
            previous = offset
            rows.append(
                ChatBlock(
                    message_id=message.id,
                    position=position,
                    text_offset=offset,
                    type=draft.type,
                    is_gate=draft.is_gate,
                    content=_json_dict(draft.content, f"block {position} content"),
                )
            )
        self.session.add_all(rows)
        self.session.flush()
        self.session.expire(message, ["blocks"])
        return rows

    def blocks(
        self, messages: Sequence[ChatMessage]
    ) -> dict[int, builtins.list[ChatBlock]]:
        """The blocks of each of `messages`, by message id, in position order (one query)."""
        ids = [message.id for message in messages]
        result: dict[int, builtins.list[ChatBlock]] = {}
        if not ids:
            return result
        rows = self.session.scalars(
            select(ChatBlock)
            .where(ChatBlock.message_id.in_(ids))
            .order_by(ChatBlock.message_id, ChatBlock.position)
        )
        for row in rows:
            result.setdefault(row.message_id, []).append(row)
        return result

    def get_block(self, artifact: Artifact, block_id: int) -> ChatBlock:
        """The chat block `block_id` in one of `artifact`'s threads; NotFoundError otherwise."""
        block = self.session.scalar(
            select(ChatBlock)
            .join(ChatMessage, ChatMessage.id == ChatBlock.message_id)
            .join(ChatThread, ChatThread.id == ChatMessage.thread_id)
            .where(ChatBlock.id == block_id, ChatThread.artifact_id == artifact.id)
        )
        if block is None:
            raise NotFoundError(f"artifact {artifact.slug!r} has no chat block {block_id}")
        return block

    def progress(self, blocks: Sequence[ChatBlock]) -> dict[int, ChatBlockProgress]:
        """Attempts and pass state of each of `blocks`, by block id (one query)."""
        result = {block.id: ChatBlockProgress() for block in blocks}
        if not result:
            return result
        rows = self.session.execute(
            select(
                ChatBlockResponse.block_id,
                func.count(ChatBlockResponse.id),
                func.max(case((ChatBlockResponse.passed, 1), else_=0)),
            )
            .where(ChatBlockResponse.block_id.in_(list(result)))
            .group_by(ChatBlockResponse.block_id)
        ).all()
        for block_id, attempts, passed in rows:
            result[block_id] = ChatBlockProgress(attempts, bool(passed))
        return result

    # -- attempts at chat exercises ---------------------------------------------------------

    def record_response(
        self,
        block: ChatBlock,
        *,
        response: dict[str, Any],
        passed: bool,
        grader: str,
        feedback: str | None = None,
        details: dict[str, Any] | None = None,
        verdict: str | None = None,
        missing: tuple[str, ...] = (),
    ) -> ChatBlockResponse:
        """Store the next attempt (previous max + 1, starting at 1) at the exercise `block`, in
        the open study session. `verdict` and `missing` as in `BlockResponseManager.record`
        (the chat asks no confidence)."""
        _require_persistent(block, "block")
        if not block.is_gate:
            raise ValueError(f"chat block {block.id} is not an exercise and takes no responses")
        _json_dict(response, "response")
        if not isinstance(passed, bool):
            raise TypeError("passed must be a bool")
        verdict = check_verdict(passed, verdict, missing)
        _validate_text(grader, "grader")
        if details is not None:
            _json_dict(details, "details")
        last = self.session.scalar(
            select(func.max(ChatBlockResponse.attempt)).where(
                ChatBlockResponse.block_id == block.id
            )
        )
        attempt = (last or 0) + 1
        row = ChatBlockResponse(
            block_id=block.id,
            attempt=attempt,
            response=response,
            passed=passed,
            feedback=feedback,
            grader=grader,
            details=details,
            verdict=verdict,
            missing=list(missing),
            session_id=StudyHistoryManager(self.session, self.settings).activity_session().id,
        )
        try:
            with self.session.begin_nested():
                self.session.add(row)
                self.session.flush()
        except IntegrityError as err:
            if _is_unique_violation(err):
                raise DuplicateError(
                    f"chat block {block.id}: attempt {attempt} already exists"
                ) from err
            raise
        self.session.expire(block, ["responses"])
        return row

    def responses(self, block: ChatBlock) -> builtins.list[ChatBlockResponse]:
        """The attempts at `block`, by attempt number."""
        return list(
            self.session.scalars(
                select(ChatBlockResponse)
                .where(ChatBlockResponse.block_id == block.id)
                .order_by(ChatBlockResponse.attempt)
            )
        )

    def unreported_responses(self, thread: ChatThread) -> builtins.list[ChatBlockResponse]:
        """The attempts at exercises of `thread` the tutor has not been told about, oldest
        first."""
        return list(
            self.session.scalars(
                select(ChatBlockResponse)
                .join(ChatBlock, ChatBlock.id == ChatBlockResponse.block_id)
                .join(ChatMessage, ChatMessage.id == ChatBlock.message_id)
                .where(
                    ChatMessage.thread_id == thread.id,
                    ChatBlockResponse.reported_message_id.is_(None),
                )
                .order_by(ChatBlockResponse.id)
            )
        )

    def mark_reported(
        self, responses: Sequence[ChatBlockResponse], message: ChatMessage
    ) -> None:
        """Record that the user `message` told the tutor about `responses`."""
        _require_persistent(message, "message")
        if message.role != "user":
            raise ValueError("attempts are reported by a user message")
        for response in responses:
            if response.reported_message_id is not None:
                raise ValueError(f"chat block response {response.id} was already reported")
            response.reported_message_id = message.id
        self.session.flush()

    def reported(
        self, messages: Sequence[ChatMessage]
    ) -> dict[int, builtins.list[ChatBlockResponse]]:
        """The attempts each of `messages` reported to the tutor, by message id, oldest first."""
        ids = [message.id for message in messages if message.role == "user"]
        result: dict[int, builtins.list[ChatBlockResponse]] = {}
        if not ids:
            return result
        rows = self.session.scalars(
            select(ChatBlockResponse)
            .where(ChatBlockResponse.reported_message_id.in_(ids))
            .order_by(ChatBlockResponse.id)
        )
        for row in rows:
            assert row.reported_message_id is not None
            result.setdefault(row.reported_message_id, []).append(row)
        return result


__all__ = [
    "KINDS",
    "PREVIEW_CHARS",
    "ROLES",
    "ChatBlockDraft",
    "ChatBlockProgress",
    "ChatManager",
    "ThreadSummary",
    "make_preview",
]
