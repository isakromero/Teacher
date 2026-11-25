"""Tutor side chat endpoints: threads, messages, and the streamed tutor reply.

`POST .../messages` does everything that can fail before the stream starts (artifact, thread,
readiness, body and references, credentials, the one-reply-per-thread guard, building the
request) and stores and commits the reader's message; only then does it return a
`text/event-stream` response. Its events (`event: <name>\\ndata: <json>\\n\\n`):

- `user_message`: the stored user Message (first);
- `delta`: `{"text": <chunk>}` for each piece of the reply's text;
- `block`: `{"type": <block type>}` when the tutor shows a block at this point of the text
  (the block itself arrives with `done`, once it is stored and can take answers);
- `done`: `{"message": <stored assistant Message>, "usage": {"input_tokens", "output_tokens"}}`;
- `error`: `{"detail": <reason>}` when the reply fails; nothing is stored for the assistant.

The stream is a sync generator (Starlette iterates it in a threadpool) that opens its own short
database session only to store the reply, so no session is held during the model call. The
thread's reply slot is released when the stream ends, fails, is closed, or is dropped by a
disconnected client.

Costs (`teacher.costs`, fields in `teacher.web.common.CostOut`): a thread's `cost` is every call
the ledger links to it (its replies, also the ones that failed, and the grading of its
exercises); an assistant Message's `cost` is that reply's model calls (null when the ledger has
none for it, e.g. a reply stored before the ledger and not backfilled).

A Message's `parts` are an assistant reply in display order: `{"type": "text", "text"}` and
`{"type": "block", "block": <ChatBlock>}`. A ChatBlock has the shape of a study-page block
(`content` is the public part; `revealed` the private part once the exercise is cleared), and
exercises take answers at `.../chat/blocks/{id}/responses`, graded and recorded like page gates
but unlocking nothing. A user Message's `answers` are the attempts it reported to the tutor. A
message of kind `exercise_answer` has no text: it only reports the reader's new attempts (422
if there are none) and is stored with kind `message`.

The same endpoints serve a lab (`kind = "lab"`): the tutor then sees the lab, the reader's saved
files and their latest run and check instead of the study page (see `teacher.tutor`). A message
with `references` on a lab is a 422 (passages can only be attached from a study page).
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable, Iterator, Sequence
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from teacher.blocks import private_content, public_content
from teacher.config import Settings
from teacher.costs import Meter, cost_scope, link_message, message_costs, thread_cost
from teacher.db.engine import session_scope
from teacher.db.models import ChatBlock, ChatBlockResponse, ChatMessage
from teacher.grading import Verdict
from teacher.managers import (
    ArtifactManager,
    ChatBlockDraft,
    ChatBlockProgress,
    ChatManager,
    StudyHistoryManager,
    ThreadSummary,
)
from teacher.managers.block_responses import response_verdict
from teacher.reading import OPEN, ReadingConflict, gate_state, require_ready
from teacher.tutor import (
    KIND_ANSWER,
    KIND_MESSAGE,
    ExerciseAnswer,
    PresentedBlock,
    Reference,
    Tutor,
    TutorError,
    TutorReply,
    exercise_answer,
    is_lab,
)
from teacher.web.common import CostOut, to_iso
from teacher.web.deps import SessionDep, SettingsDep
from teacher.web.routers.reading import GradersDep, MaxAttemptsDep, ResponseIn, ResponseOut

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["chat"])

MAX_TEXT_CHARS = 8000
MAX_REFERENCES = 5
MAX_REFERENCE_CHARS = 2000


# -- models ---------------------------------------------------------------------------------


class ReferenceModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    block_key: str = Field(min_length=1)
    text: str

    @field_validator("text")
    @classmethod
    def _text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        if len(value) > MAX_REFERENCE_CHARS:
            raise ValueError(f"must be at most {MAX_REFERENCE_CHARS} characters")
        return value


class MessageIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["message", "check_understanding", "exercise_answer"] = "message"
    text: str = ""
    references: list[ReferenceModel] = Field(default_factory=list, max_length=MAX_REFERENCES)

    @field_validator("text")
    @classmethod
    def _text(cls, value: str) -> str:
        value = value.strip()
        if len(value) > MAX_TEXT_CHARS:
            raise ValueError(f"must be at most {MAX_TEXT_CHARS} characters")
        return value

    @model_validator(mode="after")
    def _text_required(self) -> MessageIn:
        if self.kind == KIND_MESSAGE and not self.text:
            raise ValueError("text must not be empty for a message")
        if self.kind == KIND_ANSWER and (self.text or self.references):
            raise ValueError("an exercise_answer message has no text and no references")
        return self


class ChatBlockOut(BaseModel):
    """A block the tutor showed, in the shape of a study-page block (see the reading API)."""

    id: int
    key: str  # unique on the page: never the key of a study-page block
    type: str
    is_gate: bool
    content: dict[str, Any]  # the public part
    state: str | None
    attempts: int
    revealed: dict[str, Any] | None  # the private part, once the exercise is cleared


class PartOut(BaseModel):
    type: Literal["text", "block"]
    text: str | None = None
    block: ChatBlockOut | None = None


class AnswerOut(BaseModel):
    block: str  # the ChatBlock's key
    type: str
    attempt: int
    passed: bool


class MessageOut(BaseModel):
    id: int
    role: str
    kind: str
    text: str
    references: list[ReferenceModel]
    parts: list[PartOut]  # an assistant message in display order; [] for a user message
    answers: list[AnswerOut]  # the attempts a user message reported to the tutor
    created_at: str
    cost: CostOut | None  # an assistant reply's model calls; null when the ledger has none


class ChatSubmitOut(BaseModel):
    attempt: int
    passed: bool
    verdict: Verdict  # as on a study-page gate (see the reading API)
    feedback: str | None
    missing: list[str]  # a partial answer's unmet required criteria; [] otherwise
    details: dict[str, Any] | None  # null until the exercise is cleared
    state: str | None
    revealed: dict[str, Any] | None


class ThreadOut(BaseModel):
    id: int
    artifact: str
    created_at: str
    message_count: int
    preview: str | None
    cost: CostOut  # every call of the thread: its replies (failed ones too) and its grading


class ThreadDetailOut(ThreadOut):
    messages: list[MessageOut]


def block_key(block_id: int) -> str:
    return f"chat-{block_id}"


def _state(block: ChatBlock, progress: ChatBlockProgress, max_attempts: int) -> str | None:
    return gate_state(
        is_gate=block.is_gate,
        attempts=progress.attempts,
        passed=progress.passed,
        max_attempts=max_attempts,
    )


def chat_block_out(
    block: ChatBlock, progress: ChatBlockProgress, max_attempts: int
) -> ChatBlockOut:
    """The reader's view of a chat block: the private part only once the exercise is cleared."""
    state = _state(block, progress, max_attempts)
    cleared = state is not None and state != OPEN
    return ChatBlockOut(
        id=block.id,
        key=block_key(block.id),
        type=block.type,
        is_gate=block.is_gate,
        content=public_content(dict(block.content)),
        state=state,
        attempts=progress.attempts,
        revealed=private_content(dict(block.content)) if cleared else None,
    )


def message_parts(text: str, blocks: Sequence[ChatBlockOut], offsets: Sequence[int]) -> list[PartOut]:
    """The text cut at each block's offset, with the blocks in between (blank text dropped)."""
    parts: list[PartOut] = []
    cursor = 0
    for block, offset in zip(blocks, offsets, strict=True):
        piece = text[cursor:offset].strip()
        if piece:
            parts.append(PartOut(type="text", text=piece))
        parts.append(PartOut(type="block", block=block))
        cursor = offset
    tail = text[cursor:].strip()
    if tail:
        parts.append(PartOut(type="text", text=tail))
    return parts


def messages_out(
    chat: ChatManager, messages: Sequence[ChatMessage], max_attempts: int
) -> list[MessageOut]:
    """`messages` for the API, with their blocks (and the reader's standing on each) and the
    attempts they reported."""
    blocks = chat.blocks(messages)
    progress = chat.progress([block for rows in blocks.values() for block in rows])
    reported = chat.reported(messages)
    types = {
        block.id: block.type
        for block in chat.session.scalars(
            select(ChatBlock).where(
                ChatBlock.id.in_({r.block_id for rows in reported.values() for r in rows})
            )
        )
    }
    costs = message_costs(chat.session, [m.id for m in messages if m.role == "assistant"])
    result = []
    for message in messages:
        rows = blocks.get(message.id, [])
        outs = [chat_block_out(block, progress[block.id], max_attempts) for block in rows]
        result.append(
            MessageOut(
                id=message.id,
                role=message.role,
                kind=message.kind,
                text=message.text,
                references=[ReferenceModel.model_validate(r) for r in message.references],
                parts=(
                    message_parts(message.text, outs, [block.text_offset for block in rows])
                    if message.role == "assistant"
                    else []
                ),
                answers=[
                    AnswerOut(
                        block=block_key(r.block_id),
                        type=types[r.block_id],
                        attempt=r.attempt,
                        passed=r.passed,
                    )
                    for r in reported.get(message.id, [])
                ],
                created_at=to_iso(message.created_at),
                cost=CostOut.of(costs[message.id]) if message.id in costs else None,
            )
        )
    return result


def message_out(chat: ChatManager, message: ChatMessage, max_attempts: int) -> MessageOut:
    return messages_out(chat, [message], max_attempts)[0]


def pending_answers(
    chat: ChatManager, responses: Sequence[ChatBlockResponse], max_attempts: int
) -> list[ExerciseAnswer]:
    """What the tutor is told about `responses` (attempts it has not heard about yet)."""
    blocks = {r.block_id: r.block for r in responses}
    progress = chat.progress(list(blocks.values()))
    answers = []
    for r in responses:
        block = blocks[r.block_id]
        state = _state(block, progress[block.id], max_attempts)
        assert state is not None  # only exercises take responses
        answers.append(
            exercise_answer(
                block.type,
                block.content,
                r.response,
                attempt=r.attempt,
                passed=r.passed,
                state=state,
                feedback=r.feedback,
            )
        )
    return answers


def thread_out(session: Session, slug: str, summary: ThreadSummary) -> ThreadOut:
    return ThreadOut(
        id=summary.thread.id,
        artifact=slug,
        created_at=to_iso(summary.thread.created_at),
        message_count=summary.message_count,
        preview=summary.preview,
        cost=CostOut.of(thread_cost(session, summary.thread.id)),
    )


# -- one reply per thread -------------------------------------------------------------------


class ReplySlot:
    """A claimed thread; `release()` is idempotent and thread-safe."""

    def __init__(self, registry: ReplyRegistry, thread_id: int) -> None:
        self._registry = registry
        self.thread_id = thread_id
        self._released = False
        self._lock = threading.Lock()

    def release(self) -> None:
        with self._lock:
            if self._released:
                return
            self._released = True
        self._registry._discard(self.thread_id)


class ReplyRegistry:
    """In-process registry of chat threads with a tutor reply in progress."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: set[int] = set()

    def claim(self, thread_id: int) -> ReplySlot:
        """A slot for `thread_id`; HTTP 409 if a reply is already being generated for it."""
        with self._lock:
            if thread_id in self._active:
                raise HTTPException(
                    status_code=409,
                    detail=f"a reply is already being generated in chat thread {thread_id}; "
                    "wait for it to finish",
                )
            self._active.add(thread_id)
        return ReplySlot(self, thread_id)

    def is_active(self, thread_id: int) -> bool:
        with self._lock:
            return thread_id in self._active

    def _discard(self, thread_id: int) -> None:
        with self._lock:
            self._active.discard(thread_id)


class ReplyStream:
    """The SSE body of one reply: iterates `events` and releases `slot` exactly once, when the
    events end or fail, on `close()`, or when the stream is dropped without having finished
    (for example never started because the client went away)."""

    def __init__(self, events: Iterator[str], slot: ReplySlot) -> None:
        self._events = events
        self._slot = slot

    def __iter__(self) -> ReplyStream:
        return self

    def __next__(self) -> str:
        try:
            return next(self._events)
        except BaseException:
            self._slot.release()
            raise

    def close(self) -> None:
        try:
            close: Callable[[], None] | None = getattr(self._events, "close", None)
            if close is not None:
                close()
        finally:
            self._slot.release()

    def __del__(self) -> None:
        self._slot.release()


def sse(event: str, data: Any) -> str:
    """One server-sent event (the JSON is on one line)."""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def reply_events(
    settings: Settings,
    tutor: Tutor,
    request: dict[str, Any],
    *,
    slug: str,
    thread_id: int,
    user_message: dict[str, Any],
    slot: ReplySlot,
    max_attempts: int,
    meter: Meter,
) -> Iterator[str]:
    """`user_message`, the deltas and block markers, then `done` (after storing the reply and
    its blocks) or `error`. The tutor's calls are recorded through `meter` (with the thread)
    and linked to the stored reply in the transaction that stores it."""
    try:
        yield sse("user_message", user_message)
        reply: TutorReply | None = None
        try:
            for item in tutor.stream(request, meter=meter):
                if isinstance(item, TutorReply):
                    reply = item
                elif isinstance(item, PresentedBlock):
                    yield sse("block", {"type": item.type})
                else:
                    yield sse("delta", {"text": item})
        except TutorError as exc:
            yield sse("error", {"detail": str(exc)})
            return
        except Exception as exc:  # anything unexpected still ends the stream visibly
            logger.exception("tutor reply in thread %s failed", thread_id)
            yield sse(
                "error",
                {"detail": f"The tutor's reply failed unexpectedly ({exc.__class__.__name__})."},
            )
            return
        if reply is None:
            yield sse("error", {"detail": "The tutor's reply ended without a message."})
            return
        try:
            with session_scope(settings) as session:
                artifact = ArtifactManager(session, settings).get_by_slug(slug)
                chat = ChatManager(session, settings)
                row = chat.add_message(
                    chat.get_thread(artifact, thread_id),
                    role="assistant",
                    text=reply.text,
                    api_content=reply.api_content,
                    model=reply.model,
                    input_tokens=reply.input_tokens,
                    output_tokens=reply.output_tokens,
                )
                chat.add_blocks(
                    row,
                    [
                        ChatBlockDraft(b.type, b.is_gate, b.content, b.text_offset)
                        for b in reply.blocks
                    ],
                )
                link_message(session, meter.recorded_ids, row.id)
                stored = message_out(chat, row, max_attempts).model_dump()
        except Exception as exc:
            logger.exception("storing the tutor reply in thread %s failed", thread_id)
            yield sse(
                "error",
                {"detail": f"The tutor's reply could not be saved ({exc.__class__.__name__})."},
            )
            return
        yield sse(
            "done",
            {
                "message": stored,
                "usage": {
                    "input_tokens": reply.input_tokens,
                    "output_tokens": reply.output_tokens,
                },
            },
        )
    finally:
        slot.release()


# -- dependencies ---------------------------------------------------------------------------


def get_tutor(request: Request) -> Tutor:
    return request.app.state.tutor


def get_replies(request: Request) -> ReplyRegistry:
    return request.app.state.chat_replies


TutorDep = Annotated[Tutor, Depends(get_tutor)]
RepliesDep = Annotated[ReplyRegistry, Depends(get_replies)]


def _require_ready(artifact: Any) -> None:
    try:
        require_ready(artifact)
    except ReadingConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


# -- endpoints ------------------------------------------------------------------------------


@router.get("/artifacts/{slug}/chat/threads")
def list_threads(slug: str, session: SessionDep, settings: SettingsDep) -> list[ThreadOut]:
    """The artifact's chat threads, newest first."""
    artifact = ArtifactManager(session, settings).get_by_slug(slug)
    chat = ChatManager(session, settings)
    return [
        thread_out(session, artifact.slug, s)
        for s in chat.summaries(chat.list_threads(artifact))
    ]


@router.post("/artifacts/{slug}/chat/threads", status_code=201)
def create_thread(slug: str, session: SessionDep, settings: SettingsDep) -> ThreadOut:
    """A new, empty thread (409 unless the artifact is ready); logs `chat_thread_started`."""
    artifact = ArtifactManager(session, settings).get_by_slug(slug)
    _require_ready(artifact)
    chat = ChatManager(session, settings)
    thread = chat.create_thread(artifact)
    StudyHistoryManager(session, settings).log(
        "chat_thread_started",
        **({"lab": artifact} if is_lab(artifact) else {"artifact": artifact}),
        payload={"thread_id": thread.id},
    )
    return thread_out(session, artifact.slug, chat.summary(thread))


@router.get("/artifacts/{slug}/chat/threads/{thread_id}")
def get_thread(
    slug: str,
    thread_id: int,
    session: SessionDep,
    settings: SettingsDep,
    max_attempts: MaxAttemptsDep,
) -> ThreadDetailOut:
    """The thread with its messages, oldest first (404 if it is not this artifact's)."""
    artifact = ArtifactManager(session, settings).get_by_slug(slug)
    chat = ChatManager(session, settings)
    thread = chat.get_thread(artifact, thread_id)
    summary = thread_out(session, artifact.slug, chat.summary(thread))
    return ThreadDetailOut(
        **summary.model_dump(),
        messages=messages_out(chat, chat.messages(thread), max_attempts),
    )


@router.post("/artifacts/{slug}/chat/threads/{thread_id}/messages")
def send_message(
    slug: str,
    thread_id: int,
    body: MessageIn,
    settings: SettingsDep,
    max_attempts: MaxAttemptsDep,
    tutor: TutorDep,
    replies: RepliesDep,
) -> StreamingResponse:
    """Store the reader's message and stream the tutor's reply (see the module doc)."""
    with session_scope(settings) as session:
        artifact = ArtifactManager(session, settings).get_by_slug(slug)
        _require_ready(artifact)
        ChatManager(session, settings).get_thread(artifact, thread_id)
    tutor.check_available()  # 503 before anything is stored or streamed

    slot = replies.claim(thread_id)
    try:
        with session_scope(settings) as session:
            artifact = ArtifactManager(session, settings).get_by_slug(slug)
            _require_ready(artifact)
            chat = ChatManager(session, settings)
            thread = chat.get_thread(artifact, thread_id)
            history = chat.messages(thread, with_api_content=True)
            references = [Reference(r.block_key, r.text) for r in body.references]
            unreported = chat.unreported_responses(thread)
            if body.kind == KIND_ANSWER and not unreported:
                raise HTTPException(
                    status_code=422,
                    detail="there is no new answer to a tutor exercise to tell the tutor about",
                )
            turn = tutor.build_turn(
                session,
                settings,
                artifact,
                history,
                kind=body.kind,
                text=body.text,
                references=references,
                max_attempts=max_attempts,
                answers=pending_answers(chat, unreported, max_attempts),
            )
            request = tutor.request(tutor.messages(history, turn), lab=is_lab(artifact))
            row = chat.add_message(
                thread,
                role="user",
                # stored as a plain message: what it reported is in `answers`
                kind=KIND_MESSAGE if body.kind == KIND_ANSWER else body.kind,
                text=body.text,
                references=[r.model_dump() for r in body.references],
                api_content=turn.content,
                context_blocks=turn.context_blocks,
            )
            chat.mark_reported(unreported, row)
            user_message = message_out(chat, row, max_attempts).model_dump()
            meter = Meter.for_settings(
                settings, purpose="tutor", artifact_id=artifact.id, chat_thread_id=thread.id
            )
    except BaseException:
        slot.release()
        raise

    events = reply_events(
        settings,
        tutor,
        request,
        slug=slug,
        thread_id=thread_id,
        user_message=user_message,
        slot=slot,
        max_attempts=max_attempts,
        meter=meter,
    )
    return StreamingResponse(
        ReplyStream(events, slot),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# -- answering the tutor's exercises --------------------------------------------------------


def _exercise(
    chat: ChatManager, artifact: Any, block_id: int, max_attempts: int
) -> tuple[ChatBlock, ChatBlockProgress]:
    """The chat exercise `block_id` of a ready `artifact` and the reader's standing on it (404
    for an unknown block, 409 for a block that is not an exercise)."""
    _require_ready(artifact)
    block = chat.get_block(artifact, block_id)
    if not block.is_gate:
        raise HTTPException(
            status_code=409,
            detail=f"chat block {block_id} is a {block.type}, not an exercise, and takes no "
            "responses",
        )
    return block, chat.progress([block])[block.id]


@router.post("/artifacts/{slug}/chat/blocks/{block_id}/responses", status_code=201)
def create_block_response(
    slug: str,
    block_id: int,
    body: ResponseIn,
    session: SessionDep,
    settings: SettingsDep,
    graders: GradersDep,
    max_attempts: MaxAttemptsDep,
) -> ChatSubmitOut:
    """Grade and record an attempt at an exercise the tutor set (409 once it is passed; a
    malformed response is 422 and an unavailable grader 503, with nothing recorded)."""
    artifact = ArtifactManager(session, settings).get_by_slug(slug)
    chat = ChatManager(session, settings)
    block, before = _exercise(chat, artifact, block_id, max_attempts)
    if before.passed:
        raise HTTPException(status_code=409, detail=f"chat block {block_id} is already passed")
    grader = graders.get(block.type)
    if grader is None:
        raise ValueError(f"no grader for block type {block.type!r}")
    with cost_scope(
        settings,
        purpose="grading",
        artifact_id=artifact.id,
        chat_thread_id=block.message.thread_id,
    ):
        result = grader.grade(dict(block.content), body.response)
    row = chat.record_response(
        block,
        response=body.response,
        passed=result.passed,
        grader=result.grader,
        feedback=result.feedback,
        details=result.details,
        verdict=result.verdict,
        missing=result.missing,
    )
    out = chat_block_out(block, chat.progress([block])[block.id], max_attempts)
    return ChatSubmitOut(
        attempt=row.attempt,
        passed=row.passed,
        verdict=response_verdict(row),
        feedback=row.feedback,
        missing=row.missing,
        details=row.details if out.revealed is not None else None,
        state=out.state,
        revealed=out.revealed,
    )


@router.get("/artifacts/{slug}/chat/blocks/{block_id}/responses")
def list_block_responses(
    slug: str,
    block_id: int,
    session: SessionDep,
    settings: SettingsDep,
    max_attempts: MaxAttemptsDep,
) -> list[ResponseOut]:
    """Recorded attempts at a chat exercise, by attempt number (`details` once cleared)."""
    artifact = ArtifactManager(session, settings).get_by_slug(slug)
    chat = ChatManager(session, settings)
    block, progress = _exercise(chat, artifact, block_id, max_attempts)
    cleared = _state(block, progress, max_attempts) != OPEN
    return [
        ResponseOut(
            attempt=r.attempt,
            response=r.response,
            passed=r.passed,
            verdict=response_verdict(r),
            feedback=r.feedback,
            missing=r.missing,
            confidence=None,  # the tutor's exercises do not ask
            grader=r.grader,
            details=r.details if cleared else None,
            created_at=to_iso(r.created_at),
        )
        for r in chat.responses(block)
    ]


__all__ = [
    "AnswerOut",
    "ChatBlockOut",
    "ChatSubmitOut",
    "MessageIn",
    "MessageOut",
    "PartOut",
    "ReferenceModel",
    "ReplyRegistry",
    "ReplySlot",
    "ReplyStream",
    "ThreadDetailOut",
    "ThreadOut",
    "reply_events",
    "router",
    "sse",
]
