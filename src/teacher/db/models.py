"""ORM models for all thirty-two Teacher tables.

Conventions: naive-UTC timestamps with both a Python default and a CURRENT_TIMESTAMP server default;
JSON columns carry a named `CHECK (json_valid(col))`; all constraints are named via `Base.metadata`.

The models serve SQLite and PostgreSQL. Check texts are SQLite's (the migrations froze them that
way); the ones that do not run on PostgreSQL are marked `ddl_if(dialect="sqlite")`: the
json_valid() checks (there the json type refuses invalid JSON), json_type() and the two-argument
min()/max(). On PostgreSQL the migrations create json_typeof()/LEAST/GREATEST forms of the last
two instead (0005, 0012, 0013); Alembic does not compare CHECKs, so these are not mirrored here.
Boolean server defaults are `false()` (0 on SQLite, false on PostgreSQL).

Text is ordered by code point on both databases: SQLite's BINARY collation; on PostgreSQL the
database's collation would decide instead (Supabase's is ICU en-US: 'Zebra' after 'apple'), so
an ORDER BY on free text (titles, names, aliases, identifiers) wraps the column in
`by_code_point(...)`, which adds `COLLATE "C"` there. Slugs (`[a-z0-9-]`) sort the same either
way.

The concept graph also has its search tables, which are not ORM models (see `teacher.db.fts`):
the FTS5 virtual tables `concept_search` and `concept_trigram` on SQLite, plain tables
`concept_search`, `concept_search_tokens` and `concept_trigram` on PostgreSQL.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    PrimaryKeyConstraint,
    Text,
    UniqueConstraint,
    false,
    text,
)
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql.compiler import SQLCompiler
from sqlalchemy.sql.functions import FunctionElement

from teacher.db.base import Base

CURRENT_TIMESTAMP = text("CURRENT_TIMESTAMP")


class by_code_point(FunctionElement[str]):
    """A text column to ORDER BY in code point order, as SQLite's BINARY collation orders it:
    the column itself on SQLite, `<column> COLLATE "C"` on PostgreSQL (see the module
    docstring)."""

    type = Text()
    inherit_cache = True
    name = "by_code_point"


@compiles(by_code_point)
def _by_code_point(element: by_code_point, compiler: SQLCompiler, **kw: Any) -> str:
    return compiler.process(element.clauses, **kw)


@compiles(by_code_point, "postgresql")
def _by_code_point_postgres(element: by_code_point, compiler: SQLCompiler, **kw: Any) -> str:
    return f'{compiler.process(element.clauses, **kw)} COLLATE "C"'


def utcnow() -> datetime:
    """Current time as a naive UTC datetime (what every timestamp column stores)."""
    return datetime.now(UTC).replace(tzinfo=None)


class KnowledgeSource(Base):
    __tablename__ = "knowledge_sources"
    __table_args__ = (
        CheckConstraint("json_valid(creators)", name="creators_json").ddl_if(dialect="sqlite"),
        CheckConstraint("json_valid(metadata)", name="metadata_json").ddl_if(dialect="sqlite"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(Text, unique=True)
    kind: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    creators: Mapped[list[str]] = mapped_column(JSON, default=list, server_default="[]")
    originated_on: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    origin_url: Mapped[str | None] = mapped_column(Text)
    dir_path: Mapped[str] = mapped_column(Text, unique=True)
    primary_file: Mapped[str] = mapped_column(Text)
    media_type: Mapped[str] = mapped_column(Text)
    content_sha256: Mapped[str] = mapped_column(Text, unique=True)
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSON, default=dict, server_default="{}"
    )
    notes: Mapped[str | None] = mapped_column(Text)
    added_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, server_default=CURRENT_TIMESTAMP
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, server_default=CURRENT_TIMESTAMP
    )

    identifiers: Mapped[list[KnowledgeSourceIdentifier]] = relationship(
        back_populates="source",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by=lambda: [
            by_code_point(KnowledgeSourceIdentifier.scheme),
            by_code_point(KnowledgeSourceIdentifier.value),
        ],
    )
    segments: Mapped[list[SourceSegment]] = relationship(
        back_populates="source",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="SourceSegment.position",
    )
    # ON DELETE RESTRICT: the ORM must not touch these rows; the database refuses the delete.
    artifact_links: Mapped[list[ArtifactSource]] = relationship(
        back_populates="source", passive_deletes="all"
    )
    artifacts: Mapped[list[Artifact]] = relationship(
        secondary="artifact_sources", viewonly=True, order_by="Artifact.id"
    )

    def __repr__(self) -> str:
        return f"KnowledgeSource(id={self.id!r}, slug={self.slug!r})"


class KnowledgeSourceIdentifier(Base):
    __tablename__ = "knowledge_source_identifiers"
    __table_args__ = (PrimaryKeyConstraint("scheme", "value"),)

    source_id: Mapped[int] = mapped_column(
        ForeignKey("knowledge_sources.id", ondelete="CASCADE"), index=True
    )
    scheme: Mapped[str] = mapped_column(Text)
    value: Mapped[str] = mapped_column(Text)

    source: Mapped[KnowledgeSource] = relationship(back_populates="identifiers")

    def __repr__(self) -> str:
        return f"KnowledgeSourceIdentifier({self.scheme!r}, {self.value!r})"


class SourceSegment(Base):
    __tablename__ = "source_segments"
    __table_args__ = (
        UniqueConstraint("source_id", "kind", "label"),
        CheckConstraint("json_valid(locator)", name="locator_json").ddl_if(dialect="sqlite"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(
        ForeignKey("knowledge_sources.id", ondelete="CASCADE")
    )
    kind: Mapped[str] = mapped_column(Text)
    label: Mapped[str] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("source_segments.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int] = mapped_column(Integer)
    locator_type: Mapped[str] = mapped_column(Text)
    locator: Mapped[dict[str, Any]] = mapped_column(JSON)
    content_file: Mapped[str | None] = mapped_column(Text)

    source: Mapped[KnowledgeSource] = relationship(back_populates="segments")
    parent: Mapped[SourceSegment | None] = relationship(
        back_populates="children", remote_side="SourceSegment.id"
    )
    children: Mapped[list[SourceSegment]] = relationship(
        back_populates="parent",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="SourceSegment.position",
    )
    blocks: Mapped[list[ArtifactBlock]] = relationship(
        secondary="block_segment_refs",
        back_populates="segments",
        passive_deletes=True,
        order_by="ArtifactBlock.id",
    )

    def __repr__(self) -> str:
        return f"SourceSegment(id={self.id!r}, kind={self.kind!r}, label={self.label!r})"


class Artifact(Base):
    __tablename__ = "artifacts"
    __table_args__ = (
        CheckConstraint("status IN ('generating', 'ready', 'failed')", name="status"),
        CheckConstraint("json_valid(params)", name="params_json").ddl_if(dialect="sqlite"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(Text, unique=True)
    kind: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    dir_path: Mapped[str] = mapped_column(Text, unique=True)
    status: Mapped[str] = mapped_column(Text, default="generating", server_default="generating")
    generator: Mapped[str] = mapped_column(Text)
    params: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, server_default="{}")
    supersedes_id: Mapped[int | None] = mapped_column(
        ForeignKey("artifacts.id", ondelete="SET NULL"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, server_default=CURRENT_TIMESTAMP
    )
    # When the glossary was last built (by generation or on demand); NULL = never built, which
    # is different from a glossary built with no terms.
    glossary_built_at: Mapped[datetime | None] = mapped_column(DateTime)
    # When the page's blocks were last tagged with the concepts they cover (`block_concepts`,
    # by generation or `teacher concepts tag-blocks`); NULL = never tagged, which is different
    # from a page tagged with no concepts.
    block_concepts_tagged_at: Mapped[datetime | None] = mapped_column(DateTime)

    supersedes: Mapped[Artifact | None] = relationship(
        back_populates="superseded_by", remote_side="Artifact.id"
    )
    superseded_by: Mapped[list[Artifact]] = relationship(
        back_populates="supersedes", passive_deletes=True, order_by="Artifact.id"
    )
    source_links: Mapped[list[ArtifactSource]] = relationship(
        back_populates="artifact",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="ArtifactSource.source_id",
    )
    sources: Mapped[list[KnowledgeSource]] = relationship(
        secondary="artifact_sources", viewonly=True, order_by="KnowledgeSource.id"
    )
    blocks: Mapped[list[ArtifactBlock]] = relationship(
        back_populates="artifact",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="ArtifactBlock.position",
    )
    runs: Mapped[list[GenerationRun]] = relationship(
        back_populates="artifact",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="GenerationRun.id",
    )
    chat_threads: Mapped[list[ChatThread]] = relationship(
        back_populates="artifact",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="ChatThread.id",
    )
    glossary_terms: Mapped[list[GlossaryTerm]] = relationship(
        back_populates="artifact",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="GlossaryTerm.position",
    )
    # Labs only (kind 'lab'): the reader's files and every run or check they made.
    lab_workspace: Mapped[LabWorkspace | None] = relationship(
        back_populates="artifact", cascade="all, delete-orphan", passive_deletes=True
    )
    lab_runs: Mapped[list[LabRunRecord]] = relationship(
        back_populates="artifact",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="LabRunRecord.id",
    )

    def __repr__(self) -> str:
        return f"Artifact(id={self.id!r}, slug={self.slug!r}, status={self.status!r})"


class ArtifactSource(Base):
    __tablename__ = "artifact_sources"
    __table_args__ = (PrimaryKeyConstraint("artifact_id", "source_id"),)

    artifact_id: Mapped[int] = mapped_column(ForeignKey("artifacts.id", ondelete="CASCADE"))
    source_id: Mapped[int] = mapped_column(
        ForeignKey("knowledge_sources.id", ondelete="RESTRICT"), index=True
    )
    role: Mapped[str] = mapped_column(Text, default="primary", server_default="primary")

    artifact: Mapped[Artifact] = relationship(back_populates="source_links")
    source: Mapped[KnowledgeSource] = relationship(back_populates="artifact_links")

    def __repr__(self) -> str:
        return (
            f"ArtifactSource(artifact_id={self.artifact_id!r}, "
            f"source_id={self.source_id!r}, role={self.role!r})"
        )


class ArtifactBlock(Base):
    __tablename__ = "artifact_blocks"
    __table_args__ = (
        UniqueConstraint("artifact_id", "position"),
        UniqueConstraint("artifact_id", "key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    artifact_id: Mapped[int] = mapped_column(ForeignKey("artifacts.id", ondelete="CASCADE"))
    position: Mapped[int] = mapped_column(Integer)
    key: Mapped[str] = mapped_column(Text)
    type: Mapped[str] = mapped_column(Text)
    is_gate: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    content_file: Mapped[str] = mapped_column(Text)
    content_sha256: Mapped[str] = mapped_column(Text)

    artifact: Mapped[Artifact] = relationship(back_populates="blocks")
    responses: Mapped[list[BlockResponse]] = relationship(
        back_populates="block",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="BlockResponse.attempt",
    )
    segments: Mapped[list[SourceSegment]] = relationship(
        secondary="block_segment_refs",
        back_populates="blocks",
        passive_deletes=True,
        order_by="SourceSegment.position",
    )
    # The concepts the block covers (`BlockConcept`); the database deletes them with the block.

    def __repr__(self) -> str:
        return f"ArtifactBlock(id={self.id!r}, key={self.key!r}, type={self.type!r})"


class BlockSegmentRef(Base):
    """Plain association row; use `ArtifactBlock.segments` / `SourceSegment.blocks` to navigate."""

    __tablename__ = "block_segment_refs"
    __table_args__ = (PrimaryKeyConstraint("block_id", "segment_id"),)

    block_id: Mapped[int] = mapped_column(ForeignKey("artifact_blocks.id", ondelete="CASCADE"))
    segment_id: Mapped[int] = mapped_column(
        ForeignKey("source_segments.id", ondelete="CASCADE"), index=True
    )

    def __repr__(self) -> str:
        return f"BlockSegmentRef(block_id={self.block_id!r}, segment_id={self.segment_id!r})"


# The checks on a graded attempt's `verdict` and `missing` (block_responses and
# chat_block_responses). `verdict` is "passed", "partial" or "failed" and agrees with `passed`;
# `missing` (the unmet required criteria the reader is shown) is a JSON array, non-empty exactly
# for a partial answer. `verdict` is NULL only for rows written outside the managers (migration
# 0012 fills it in for older rows from `passed`), and the checks then hold trivially.
VERDICT_CHECK = "verdict IN ('passed', 'partial', 'failed')"
VERDICT_PASSED_CHECK = "verdict IS NULL OR (verdict = 'passed') = passed"
MISSING_ARRAY_CHECK = "json_type(missing) = 'array'"
MISSING_PARTIAL_CHECK = "(verdict = 'partial') = (json_array_length(missing) > 0)"
CONFIDENCE_CHECK = "confidence IN ('low', 'medium', 'high')"


def _verdict_constraints() -> tuple[CheckConstraint, ...]:
    return (
        CheckConstraint(VERDICT_CHECK, name="verdict"),
        CheckConstraint(VERDICT_PASSED_CHECK, name="verdict_passed"),
        CheckConstraint("json_valid(missing)", name="missing_json").ddl_if(dialect="sqlite"),
        CheckConstraint(MISSING_ARRAY_CHECK, name="missing_array").ddl_if(dialect="sqlite"),
        CheckConstraint(MISSING_PARTIAL_CHECK, name="missing_partial"),
    )


class BlockResponse(Base):
    __tablename__ = "block_responses"
    __table_args__ = (
        UniqueConstraint("block_id", "attempt"),
        CheckConstraint("json_valid(response)", name="response_json").ddl_if(dialect="sqlite"),
        CheckConstraint("json_valid(details)", name="details_json").ddl_if(dialect="sqlite"),
        *_verdict_constraints(),
        CheckConstraint(CONFIDENCE_CHECK, name="confidence"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    block_id: Mapped[int] = mapped_column(ForeignKey("artifact_blocks.id", ondelete="CASCADE"))
    attempt: Mapped[int] = mapped_column(Integer)
    response: Mapped[Any] = mapped_column(JSON)
    passed: Mapped[bool] = mapped_column(Boolean)
    feedback: Mapped[str | None] = mapped_column(Text)
    grader: Mapped[str] = mapped_column(Text)
    # The grader's private verdict (e.g. per-criterion judgements). `none_as_null=True` makes a
    # Python None land as SQL NULL rather than the JSON text 'null'.
    details: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True))
    # `teacher.grading.GradeResult.verdict` and `.missing` (see `_verdict_constraints`).
    verdict: Mapped[str | None] = mapped_column(Text)
    missing: Mapped[list[str]] = mapped_column(JSON, default=list, server_default="[]")
    # How sure the reader said they were before submitting (low / medium / high: Guessing /
    # Fairly sure / Certain); NULL when they were not asked (rows before 0012, predictions).
    confidence: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, server_default=CURRENT_TIMESTAMP
    )
    # The study session the attempt belongs to (`StudyHistoryManager`); NULL only for rows
    # written outside the managers.
    session_id: Mapped[int | None] = mapped_column(
        ForeignKey("study_sessions.id", ondelete="RESTRICT"), index=True
    )

    block: Mapped[ArtifactBlock] = relationship(back_populates="responses")

    def __repr__(self) -> str:
        return (
            f"BlockResponse(id={self.id!r}, block_id={self.block_id!r}, "
            f"attempt={self.attempt!r}, passed={self.passed!r})"
        )


class GenerationRun(Base):
    __tablename__ = "generation_runs"
    __table_args__ = (
        CheckConstraint("status IN ('running', 'succeeded', 'failed')", name="status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    artifact_id: Mapped[int] = mapped_column(
        ForeignKey("artifacts.id", ondelete="CASCADE"), index=True
    )
    stage: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="running", server_default="running")
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    output_file: Mapped[str | None] = mapped_column(Text)
    # Something the owner should see with a stage that succeeded (revision 0014).
    note: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, server_default=CURRENT_TIMESTAMP
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)

    artifact: Mapped[Artifact] = relationship(back_populates="runs")

    def __repr__(self) -> str:
        return (
            f"GenerationRun(id={self.id!r}, stage={self.stage!r}, status={self.status!r})"
        )


class ChatThread(Base):
    """One conversation with the tutor in an artifact's side chat."""

    __tablename__ = "chat_threads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    artifact_id: Mapped[int] = mapped_column(
        ForeignKey("artifacts.id", ondelete="CASCADE"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, server_default=CURRENT_TIMESTAMP
    )

    artifact: Mapped[Artifact] = relationship(back_populates="chat_threads")
    messages: Mapped[list[ChatMessage]] = relationship(
        back_populates="thread",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="ChatMessage.position",
    )

    def __repr__(self) -> str:
        return f"ChatThread(id={self.id!r}, artifact_id={self.artifact_id!r})"


class ChatMessage(Base):
    """One turn of a chat thread.

    `text` is what the reader typed or the assistant's visible text; `api_content` is exactly
    what was sent to (user) or received from (assistant) the Messages API for this turn, replayed
    verbatim on later requests. `context_blocks` records, for a user turn, which study-page
    blocks that turn showed the tutor (`[{"key", "revealed"}]`); it is `[]` for assistant turns.
    `api_content` is deferred: it can hold a whole base64 PDF.
    """

    __tablename__ = "chat_messages"
    __table_args__ = (
        UniqueConstraint("thread_id", "position"),
        CheckConstraint("role IN ('user', 'assistant')", name="role"),
        CheckConstraint("kind IN ('message', 'check_understanding')", name="kind"),
        CheckConstraint('json_valid("references")', name="references_json").ddl_if(dialect="sqlite"),
        CheckConstraint("json_valid(api_content)", name="api_content_json").ddl_if(dialect="sqlite"),
        CheckConstraint("json_valid(context_blocks)", name="context_blocks_json").ddl_if(dialect="sqlite"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    thread_id: Mapped[int] = mapped_column(ForeignKey("chat_threads.id", ondelete="CASCADE"))
    position: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text, default="message", server_default="message")
    text: Mapped[str] = mapped_column(Text)
    references: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, default=list, server_default="[]"
    )
    api_content: Mapped[list[dict[str, Any]]] = mapped_column(JSON, deferred=True)
    context_blocks: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, default=list, server_default="[]"
    )
    model: Mapped[str | None] = mapped_column(Text)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, server_default=CURRENT_TIMESTAMP
    )
    session_id: Mapped[int | None] = mapped_column(
        ForeignKey("study_sessions.id", ondelete="RESTRICT"), index=True
    )

    thread: Mapped[ChatThread] = relationship(back_populates="messages")
    blocks: Mapped[list[ChatBlock]] = relationship(
        back_populates="message",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="ChatBlock.position",
    )

    def __repr__(self) -> str:
        return (
            f"ChatMessage(id={self.id!r}, thread_id={self.thread_id!r}, "
            f"position={self.position!r}, role={self.role!r})"
        )


class ChatBlock(Base):
    """A block the tutor showed in an assistant message of the side chat (an exercise, a chart
    or an interactable), in the same shape as an artifact block's content.

    `content` is the full validated content, including the `private` part of an exercise, so it
    must only leave the server through `teacher.blocks.public_content` / `private_content`.
    `text_offset` is the number of characters of the message's `text` that come before the
    block; `position` orders the blocks of one message (1, 2, ...).
    """

    __tablename__ = "chat_blocks"
    __table_args__ = (
        UniqueConstraint("message_id", "position"),
        CheckConstraint("json_valid(content)", name="content_json").ddl_if(dialect="sqlite"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    message_id: Mapped[int] = mapped_column(ForeignKey("chat_messages.id", ondelete="CASCADE"))
    position: Mapped[int] = mapped_column(Integer)
    text_offset: Mapped[int] = mapped_column(Integer)
    type: Mapped[str] = mapped_column(Text)
    is_gate: Mapped[bool] = mapped_column(Boolean)
    content: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, server_default=CURRENT_TIMESTAMP
    )

    message: Mapped[ChatMessage] = relationship(back_populates="blocks")
    responses: Mapped[list[ChatBlockResponse]] = relationship(
        back_populates="block",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="ChatBlockResponse.attempt",
    )

    def __repr__(self) -> str:
        return f"ChatBlock(id={self.id!r}, message_id={self.message_id!r}, type={self.type!r})"


class ChatBlockResponse(Base):
    """One attempt by the reader at an exercise the tutor set in the chat (like BlockResponse).

    `reported_message_id` is the user message that told the tutor about this attempt; NULL
    until one has.
    """

    __tablename__ = "chat_block_responses"
    __table_args__ = (
        UniqueConstraint("block_id", "attempt"),
        CheckConstraint("json_valid(response)", name="response_json").ddl_if(dialect="sqlite"),
        CheckConstraint("json_valid(details)", name="details_json").ddl_if(dialect="sqlite"),
        *_verdict_constraints(),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    block_id: Mapped[int] = mapped_column(ForeignKey("chat_blocks.id", ondelete="CASCADE"))
    attempt: Mapped[int] = mapped_column(Integer)
    response: Mapped[Any] = mapped_column(JSON)
    passed: Mapped[bool] = mapped_column(Boolean)
    feedback: Mapped[str | None] = mapped_column(Text)
    grader: Mapped[str] = mapped_column(Text)
    details: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True))
    verdict: Mapped[str | None] = mapped_column(Text)  # as in BlockResponse
    missing: Mapped[list[str]] = mapped_column(JSON, default=list, server_default="[]")
    reported_message_id: Mapped[int | None] = mapped_column(
        ForeignKey("chat_messages.id", ondelete="SET NULL"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, server_default=CURRENT_TIMESTAMP
    )
    session_id: Mapped[int | None] = mapped_column(
        ForeignKey("study_sessions.id", ondelete="RESTRICT"), index=True
    )

    block: Mapped[ChatBlock] = relationship(back_populates="responses")

    def __repr__(self) -> str:
        return (
            f"ChatBlockResponse(id={self.id!r}, block_id={self.block_id!r}, "
            f"attempt={self.attempt!r}, passed={self.passed!r})"
        )


class GlossaryTerm(Base):
    """One term of an artifact's glossary (shown on hover in the study page).

    `term` is written as it should be displayed; `aliases` are other surface forms on the page
    that mark the same term; `expansion` is the spelled-out form of an acronym (or NULL);
    `position` keeps the order the glossary was built in (1, 2, ...).
    """

    __tablename__ = "glossary_terms"
    __table_args__ = (
        UniqueConstraint("artifact_id", "term"),
        UniqueConstraint("artifact_id", "position"),
        CheckConstraint("kind IN ('acronym', 'term', 'symbol')", name="kind"),
        CheckConstraint("json_valid(aliases)", name="aliases_json").ddl_if(dialect="sqlite"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    artifact_id: Mapped[int] = mapped_column(ForeignKey("artifacts.id", ondelete="CASCADE"))
    position: Mapped[int] = mapped_column(Integer)
    term: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    expansion: Mapped[str | None] = mapped_column(Text)
    definition: Mapped[str] = mapped_column(Text)
    aliases: Mapped[list[str]] = mapped_column(JSON, default=list, server_default="[]")

    artifact: Mapped[Artifact] = relationship(back_populates="glossary_terms")

    def __repr__(self) -> str:
        return f"GlossaryTerm(id={self.id!r}, term={self.term!r}, kind={self.kind!r})"


# -- labs -------------------------------------------------------------------------------------
# A lab is an artifact with kind 'lab'; its spec is `lab.json` in its folder (`teacher.labs`).
# Writes go through `teacher.managers.LabManager`.


class LabWorkspace(Base):
    """The reader's current files of one lab (`[{"path", "content"}]`), saved on every edit and
    run. `solution_revealed_at` is set the first time the reader asks for the solution."""

    __tablename__ = "lab_workspaces"
    __table_args__ = (CheckConstraint("json_valid(files)", name="files_json").ddl_if(dialect="sqlite"),)

    artifact_id: Mapped[int] = mapped_column(
        ForeignKey("artifacts.id", ondelete="CASCADE"), primary_key=True
    )
    files: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    solution_revealed_at: Mapped[datetime | None] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, server_default=CURRENT_TIMESTAMP
    )

    artifact: Mapped[Artifact] = relationship(back_populates="lab_workspace")

    def __repr__(self) -> str:
        return f"LabWorkspace(artifact_id={self.artifact_id!r})"


class LabRunRecord(Base):
    """One run (`kind = 'run'`) or check (`'check'`, of step `step_index`, counted from 0) the
    reader made in the browser, with the files it ran and what came out. The browser runs the
    code; `passed` is what it reported. (Named `...Record` so it does not clash with
    `teacher.pyodide_runtime.LabRun`, the runtime's result type.)"""

    __tablename__ = "lab_runs"
    __table_args__ = (
        CheckConstraint("kind IN ('run', 'check')", name="kind"),
        CheckConstraint("kind != 'check' OR step_index IS NOT NULL", name="check_step"),
        CheckConstraint("step_index IS NULL OR step_index >= 0", name="step_index"),
        CheckConstraint("json_valid(files)", name="files_json").ddl_if(dialect="sqlite"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    artifact_id: Mapped[int] = mapped_column(
        ForeignKey("artifacts.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(Text)
    step_index: Mapped[int | None] = mapped_column(Integer)
    passed: Mapped[bool] = mapped_column(Boolean)
    files: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    output: Mapped[str] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, server_default=CURRENT_TIMESTAMP
    )
    session_id: Mapped[int | None] = mapped_column(
        ForeignKey("study_sessions.id", ondelete="RESTRICT"), index=True
    )

    artifact: Mapped[Artifact] = relationship(back_populates="lab_runs")

    def __repr__(self) -> str:
        return (
            f"LabRunRecord(id={self.id!r}, kind={self.kind!r}, step_index={self.step_index!r}, "
            f"passed={self.passed!r})"
        )


# -- the concept graph ------------------------------------------------------------------------
# Writes go through `teacher.managers.ConceptManager` / `ConceptRelationManager`, which keep the
# invariants SQLite cannot express (acyclic builds_on, the FTS index) and give clear errors for
# the ones it can.


class Concept(Base):
    """One concept of the study graph.

    `slug` is fixed at creation (a trigger refuses changing it); `name_norm` is
    `teacher.concepts.normalize_name(name)`. `origin` says who proposed it (a paper, a model or
    the user); `reviewed_at` is NULL until a person confirms it, and any content edit clears it.
    """

    __tablename__ = "concepts"
    __table_args__ = (CheckConstraint("origin IN ('paper', 'model', 'user')", name="origin"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(Text, unique=True)
    name: Mapped[str] = mapped_column(Text)
    name_norm: Mapped[str] = mapped_column(Text, index=True)
    summary: Mapped[str] = mapped_column(Text)
    origin: Mapped[str] = mapped_column(Text)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, server_default=CURRENT_TIMESTAMP
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, server_default=CURRENT_TIMESTAMP
    )

    aliases: Mapped[list[ConceptAlias]] = relationship(
        back_populates="concept",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by=lambda: [
            by_code_point(ConceptAlias.alias_norm),
            by_code_point(ConceptAlias.alias),
        ],
    )
    embedding: Mapped[ConceptEmbedding | None] = relationship(
        back_populates="concept", cascade="all, delete-orphan", passive_deletes=True
    )
    # Relations are deliberately not mapped as collections here: the managers query them, and
    # the database cascade removes them with the concept.

    def __repr__(self) -> str:
        return f"Concept(id={self.id!r}, slug={self.slug!r})"


class ConceptAlias(Base):
    """Another name for a concept (an acronym, a spelling). Unique per concept by `alias_norm`;
    the same alias may belong to several concepts. The concept's own name is never an alias."""

    __tablename__ = "concept_aliases"
    __table_args__ = (UniqueConstraint("concept_id", "alias_norm"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    concept_id: Mapped[int] = mapped_column(ForeignKey("concepts.id", ondelete="CASCADE"))
    alias: Mapped[str] = mapped_column(Text)
    alias_norm: Mapped[str] = mapped_column(Text, index=True)

    concept: Mapped[Concept] = relationship(back_populates="aliases")

    def __repr__(self) -> str:
        return f"ConceptAlias(concept_id={self.concept_id!r}, alias={self.alias!r})"


class ConceptRelation(Base):
    """A typed edge between two concepts.

    "A builds_on B" is stored as from=A (the dependent), to=B (the prerequisite).
    `different_version_of` and `relates_to` are symmetric and stored once with from_id < to_id.
    `low_id`/`high_id` are min/max of the two ids, kept by a CHECK, so the UNIQUE over them
    allows at most one relation per unordered pair whatever the type.
    """

    __tablename__ = "concept_relations"
    __table_args__ = (
        UniqueConstraint("low_id", "high_id"),
        CheckConstraint(
            "type IN ('builds_on', 'different_version_of', 'relates_to')", name="type"
        ),
        CheckConstraint("origin IN ('paper', 'model', 'user')", name="origin"),
        CheckConstraint("from_id != to_id", name="not_self"),
        CheckConstraint("type = 'builds_on' OR from_id < to_id", name="symmetric_order"),
        CheckConstraint(
            "low_id = min(from_id, to_id) AND high_id = max(from_id, to_id)", name="pair"
        ).ddl_if(dialect="sqlite"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    type: Mapped[str] = mapped_column(Text)
    from_id: Mapped[int] = mapped_column(
        ForeignKey("concepts.id", ondelete="CASCADE"), index=True
    )
    to_id: Mapped[int] = mapped_column(ForeignKey("concepts.id", ondelete="CASCADE"), index=True)
    low_id: Mapped[int] = mapped_column(Integer)
    high_id: Mapped[int] = mapped_column(Integer)
    rationale: Mapped[str] = mapped_column(Text)
    origin: Mapped[str] = mapped_column(Text)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, server_default=CURRENT_TIMESTAMP
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, server_default=CURRENT_TIMESTAMP
    )

    # View-only: the managers set the id columns (and low/high) themselves.
    from_concept: Mapped[Concept] = relationship(foreign_keys=[from_id], viewonly=True)
    to_concept: Mapped[Concept] = relationship(foreign_keys=[to_id], viewonly=True)

    def __repr__(self) -> str:
        return (
            f"ConceptRelation(id={self.id!r}, type={self.type!r}, "
            f"from_id={self.from_id!r}, to_id={self.to_id!r})"
        )


class ConceptEmbedding(Base):
    """A concept's embedding: `dimensions` float32 little-endian values in `vector`, made by
    `model` from the text whose SHA-256 is `text_hash` (`teacher.embeddings.concept_text`)."""

    __tablename__ = "concept_embeddings"
    __table_args__ = (
        CheckConstraint("dimensions > 0", name="dimensions"),
        CheckConstraint("length(vector) = 4 * dimensions", name="vector_size"),
    )

    concept_id: Mapped[int] = mapped_column(
        ForeignKey("concepts.id", ondelete="CASCADE"), primary_key=True
    )
    model: Mapped[str] = mapped_column(Text)
    dimensions: Mapped[int] = mapped_column(Integer)
    vector: Mapped[bytes] = mapped_column(LargeBinary)
    text_hash: Mapped[str] = mapped_column(Text)
    embedded_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, server_default=CURRENT_TIMESTAMP
    )

    concept: Mapped[Concept] = relationship(back_populates="embedding")

    def __repr__(self) -> str:
        return (
            f"ConceptEmbedding(concept_id={self.concept_id!r}, model={self.model!r}, "
            f"dimensions={self.dimensions!r})"
        )


class SourceConcept(Base):
    """A link from a knowledge source to a concept it introduces, uses or assumes.

    Written by concept extraction (`teacher.concept_extraction`), which replaces all of a
    source's links in one go; at most one link per (source, concept). `role` is how the source
    relates to the concept (`teacher.concepts.SOURCE_ROLES`); `resolution` how the run found it
    (`created` a new concept, an `exact` name match, or `judged` the same as an existing one by
    the model, whose one-sentence `judge_reason` is kept, and only then). `candidate_name` is
    the name the extraction gave; `evidence` a short phrase from the source (NULL for an assumed
    concept the source never names). Deleting the source or the concept deletes the link.
    """

    __tablename__ = "source_concepts"
    __table_args__ = (
        UniqueConstraint("source_id", "concept_id"),
        CheckConstraint("role IN ('introduces', 'uses', 'assumes')", name="role"),
        CheckConstraint("resolution IN ('created', 'exact', 'judged')", name="resolution"),
        CheckConstraint(
            "(resolution = 'judged') = (judge_reason IS NOT NULL)", name="judge_reason"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("knowledge_sources.id", ondelete="CASCADE"))
    concept_id: Mapped[int] = mapped_column(
        ForeignKey("concepts.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(Text)
    resolution: Mapped[str] = mapped_column(Text)
    candidate_name: Mapped[str] = mapped_column(Text)
    judge_reason: Mapped[str | None] = mapped_column(Text)
    evidence: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, server_default=CURRENT_TIMESTAMP
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, server_default=CURRENT_TIMESTAMP
    )

    # View-only: the link manager and the concept merge set the id columns themselves.
    source: Mapped[KnowledgeSource] = relationship(viewonly=True)
    concept: Mapped[Concept] = relationship(viewonly=True)

    def __repr__(self) -> str:
        return (
            f"SourceConcept(source_id={self.source_id!r}, concept_id={self.concept_id!r}, "
            f"role={self.role!r})"
        )


# What the relation judge can answer for a (focus, candidate) pair, seen from the focus concept
# (`teacher.concept_relating`), and what became of it.
JUDGEMENT_VERDICTS: tuple[str, ...] = (
    "focus_builds_on_candidate",
    "candidate_builds_on_focus",
    "different_version_of",
    "relates_to",
    "none",
)
JUDGEMENT_OUTCOMES: tuple[str, ...] = ("added", "none", "refused")


def _sql_list(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


class ConceptPairJudgement(Base):
    """The relation judge's verdict on one unordered pair of concepts, so the pair is not judged
    again while both concepts' texts are unchanged (`teacher.concept_relating`).

    The pair is stored once with low_id < high_id (UNIQUE). `focus_id` (one of the two) is the
    concept the run was for: `verdict` and `found_by` are seen from it. `low_text_sha256` and
    `high_text_sha256` are the SHA-256 of each concept's `teacher.embeddings.concept_text` when
    it was judged; a changed text makes the row stale and the pair is judged again (the row is
    then replaced). `found_by` lists how the candidate was found ("embedding",
    "proposal:<slot>"); `similarity` is the focus-candidate cosine when the embedding search
    gave it. `outcome`: `added` (the relation `relation_id` was written; NULL once that relation
    is deleted), `none` (exactly for verdict none) or `refused` (the write was refused, e.g. a
    builds_on cycle; `refusal` says why, and only then). `rationale` is the judge's sentence.
    Deleting either concept deletes the row; a merge drops the merged-away concept's rows.
    """

    __tablename__ = "concept_pair_judgements"
    __table_args__ = (
        UniqueConstraint("low_id", "high_id"),
        CheckConstraint("low_id < high_id", name="pair_order"),
        CheckConstraint("focus_id IN (low_id, high_id)", name="focus"),
        CheckConstraint(f"verdict IN ({_sql_list(JUDGEMENT_VERDICTS)})", name="verdict"),
        CheckConstraint(f"outcome IN ({_sql_list(JUDGEMENT_OUTCOMES)})", name="outcome"),
        CheckConstraint("(verdict = 'none') = (outcome = 'none')", name="none_outcome"),
        CheckConstraint("(outcome = 'refused') = (refusal IS NOT NULL)", name="refusal"),
        CheckConstraint("relation_id IS NULL OR outcome = 'added'", name="relation_outcome"),
        CheckConstraint("json_valid(found_by)", name="found_by_json").ddl_if(dialect="sqlite"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    low_id: Mapped[int] = mapped_column(ForeignKey("concepts.id", ondelete="CASCADE"))
    high_id: Mapped[int] = mapped_column(
        ForeignKey("concepts.id", ondelete="CASCADE"), index=True
    )
    focus_id: Mapped[int] = mapped_column(Integer)
    low_text_sha256: Mapped[str] = mapped_column(Text)
    high_text_sha256: Mapped[str] = mapped_column(Text)
    found_by: Mapped[list[str]] = mapped_column(JSON)
    similarity: Mapped[float | None] = mapped_column(Float)
    verdict: Mapped[str] = mapped_column(Text)
    outcome: Mapped[str] = mapped_column(Text)
    rationale: Mapped[str] = mapped_column(Text)
    refusal: Mapped[str | None] = mapped_column(Text)
    relation_id: Mapped[int | None] = mapped_column(
        ForeignKey("concept_relations.id", ondelete="SET NULL"), index=True
    )
    model: Mapped[str] = mapped_column(Text)
    judged_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, server_default=CURRENT_TIMESTAMP
    )

    def __repr__(self) -> str:
        return (
            f"ConceptPairJudgement(low_id={self.low_id!r}, high_id={self.high_id!r}, "
            f"verdict={self.verdict!r}, outcome={self.outcome!r})"
        )


# How a study-page block covers a concept (`teacher.block_tagging`): `main`, the block is about
# it (a question tests it); `incidental`, the block mentions or uses it in passing.
BLOCK_CONCEPT_ROLES: tuple[str, ...] = ("main", "incidental")
BLOCK_CONCEPT_ORIGINS: tuple[str, ...] = ("model", "user")


class BlockConcept(Base):
    """A concept one study-page block covers, so answers to the block and time on it can be
    traced to the concept graph (`teacher.managers.concept_standing`).

    Written by `teacher.managers.BlockConceptManager` (the block tagger, `teacher.block_tagging`,
    during generation or `teacher concepts tag-blocks`). At most one row per (block, concept).
    `role` is one of `BLOCK_CONCEPT_ROLES`; `origin` who tagged it, `model` (then `model` names
    the Claude model and `rationale`, the tagger's one sentence, is required) or `user`.
    Deleting the block (its page) or the concept deletes the row; a concept merge moves it.
    """

    __tablename__ = "block_concepts"
    __table_args__ = (
        UniqueConstraint("block_id", "concept_id"),
        CheckConstraint(f"role IN ({_sql_list(BLOCK_CONCEPT_ROLES)})", name="role"),
        CheckConstraint(f"origin IN ({_sql_list(BLOCK_CONCEPT_ORIGINS)})", name="origin"),
        CheckConstraint("origin = 'user' OR rationale IS NOT NULL", name="rationale"),
        CheckConstraint("(origin = 'model') = (model IS NOT NULL)", name="model"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    block_id: Mapped[int] = mapped_column(ForeignKey("artifact_blocks.id", ondelete="CASCADE"))
    concept_id: Mapped[int] = mapped_column(
        ForeignKey("concepts.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(Text)
    origin: Mapped[str] = mapped_column(Text)
    rationale: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, server_default=CURRENT_TIMESTAMP
    )

    # View-only: the manager and the concept merge set the id columns themselves.
    block: Mapped[ArtifactBlock] = relationship(viewonly=True)
    concept: Mapped[Concept] = relationship(viewonly=True)

    def __repr__(self) -> str:
        return (
            f"BlockConcept(block_id={self.block_id!r}, concept_id={self.concept_id!r}, "
            f"role={self.role!r}, origin={self.origin!r})"
        )


# -- study history ----------------------------------------------------------------------------
# Writes go through `teacher.managers.StudyHistoryManager`, which assigns sessions, checks each
# event's references and payload, and keeps attention spans. The activity tables above
# (block_responses, chat_block_responses, chat_messages, lab_runs) carry a `session_id` too.

# What a study event can record (see `teacher.managers.study_history` for what each one means,
# which references it takes and where it is logged).
STUDY_EVENT_KINDS: tuple[str, ...] = (
    "library_opened",
    "study_page_opened",
    "lab_opened",
    "source_opened",
    "source_file_opened",
    "graph_opened",
    "gate_cleared",
    "glossary_opened",
    "glossary_term_viewed",
    "glossary_build_requested",
    "chat_thread_started",
    "lab_workspace_saved",
    "lab_hint_viewed",
    "lab_solution_viewed",
    "lab_exported",
    "concept_inspected",
    "graph_searched",
    "relations_requested",
    "source_added",
    "source_updated",
    "source_deleted",
    "study_page_requested",
    "lab_requested",
    "regeneration_requested",
    "artifact_deleted",
)
# The pages that report the reader's attention (heartbeats).
ATTENTION_SURFACES: tuple[str, ...] = ("study_page", "lab", "source", "graph")


class StudySession(Base):
    """One sitting: every logged activity joins the open session, or starts a new one after an
    idle gap (`teacher.managers.study_history.SESSION_IDLE_GAP`). `last_activity_at` is the
    time of its latest activity. `backfilled` sessions were derived by migration 0010 from the
    timestamps of activity recorded before sessions existed; live activity never joins them.
    Sessions are never deleted (the references to them are ON DELETE RESTRICT).
    """

    __tablename__ = "study_sessions"
    __table_args__ = (
        CheckConstraint("last_activity_at >= started_at", name="activity_order"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, server_default=CURRENT_TIMESTAMP
    )
    last_activity_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, server_default=CURRENT_TIMESTAMP
    )
    backfilled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())

    def __repr__(self) -> str:
        return f"StudySession(id={self.id!r}, backfilled={self.backfilled!r})"


class StudyEvent(Base):
    """An append-only record of one action of the reader that leaves no row elsewhere (a page
    opened, a gate cleared, a glossary term looked up, a lab's solution viewed, ...).

    `kind` is one of `STUDY_EVENT_KINDS`. The references say what the action was about: for a
    lab, `lab_id` is the lab and `artifact_id` the study page it was made from (NULL once that
    page is gone), so a page's events include its labs'; `block_id` is a study-page block.
    Every reference is ON DELETE SET NULL, so the event outlives what it was about; events of
    deletions keep the slug and title in `payload` (a JSON object, kind-specific).
    """

    __tablename__ = "study_events"
    __table_args__ = (
        CheckConstraint(f"kind IN ({_sql_list(STUDY_EVENT_KINDS)})", name="kind"),
        CheckConstraint("json_valid(payload)", name="payload_json").ddl_if(dialect="sqlite"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("study_sessions.id", ondelete="RESTRICT"), index=True
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, server_default=CURRENT_TIMESTAMP
    )
    kind: Mapped[str] = mapped_column(Text)
    artifact_id: Mapped[int | None] = mapped_column(
        ForeignKey("artifacts.id", ondelete="SET NULL"), index=True
    )
    lab_id: Mapped[int | None] = mapped_column(
        ForeignKey("artifacts.id", ondelete="SET NULL"), index=True
    )
    block_id: Mapped[int | None] = mapped_column(
        ForeignKey("artifact_blocks.id", ondelete="SET NULL"), index=True
    )
    concept_id: Mapped[int | None] = mapped_column(
        ForeignKey("concepts.id", ondelete="SET NULL"), index=True
    )
    source_id: Mapped[int | None] = mapped_column(
        ForeignKey("knowledge_sources.id", ondelete="SET NULL"), index=True
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, server_default="{}")

    def __repr__(self) -> str:
        return f"StudyEvent(id={self.id!r}, kind={self.kind!r}, session_id={self.session_id!r})"


class AttentionSpan(Base):
    """A stretch of time the reader spent on one page with the tab visible and active, built
    from the page's heartbeats (`StudyHistoryManager.heartbeat`).

    `surface` is one of `ATTENTION_SURFACES`; the references name what was on screen, as in
    `StudyEvent` (a study page: `artifact_id`, and `block_id` for the block in view; a lab:
    `lab_id` and its study page as `artifact_id`; a source page: `source_id`; the graph: none).
    `active_seconds` is the time credited between consecutive heartbeats (never more than the
    wall-clock time from `started_at` to `last_seen_at`); `heartbeats` counts them.
    """

    __tablename__ = "attention_spans"
    __table_args__ = (
        CheckConstraint(f"surface IN ({_sql_list(ATTENTION_SURFACES)})", name="surface"),
        CheckConstraint("last_seen_at >= started_at", name="seen_order"),
        CheckConstraint("active_seconds >= 0", name="active_seconds"),
        CheckConstraint("heartbeats >= 1", name="heartbeats"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("study_sessions.id", ondelete="RESTRICT"), index=True
    )
    surface: Mapped[str] = mapped_column(Text)
    artifact_id: Mapped[int | None] = mapped_column(
        ForeignKey("artifacts.id", ondelete="SET NULL"), index=True
    )
    lab_id: Mapped[int | None] = mapped_column(
        ForeignKey("artifacts.id", ondelete="SET NULL"), index=True
    )
    block_id: Mapped[int | None] = mapped_column(
        ForeignKey("artifact_blocks.id", ondelete="SET NULL"), index=True
    )
    source_id: Mapped[int | None] = mapped_column(
        ForeignKey("knowledge_sources.id", ondelete="SET NULL"), index=True
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, server_default=CURRENT_TIMESTAMP
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, server_default=CURRENT_TIMESTAMP
    )
    active_seconds: Mapped[float] = mapped_column(Float, default=0.0, server_default=text("0"))
    heartbeats: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))

    def __repr__(self) -> str:
        return (
            f"AttentionSpan(id={self.id!r}, surface={self.surface!r}, "
            f"active_seconds={self.active_seconds!r})"
        )


# -- the review queue ---------------------------------------------------------------------------
# Written by `teacher.managers.review.ReviewManager` and `teacher.review_bank`; see
# `teacher.managers.review` for the rules. Everything cascades with the question's block (so
# with its page); a page that is only superseded keeps its rows.

REVIEW_QUESTION_TYPES: tuple[str, ...] = ("multiple_choice", "blurb")
REVIEW_FORMS: tuple[str, ...] = ("original", "variant")
REVIEW_VERDICT_CHECK = "verdict IN ('passed', 'partial', 'failed')"
REVIEW_LAST_VERDICT_CHECK = "last_verdict IN ('passed', 'partial', 'failed')"
REVIEW_LAST_CONFIDENCE_CHECK = "last_confidence IN ('low', 'medium', 'high')"
REVIEW_PRESENTATION_CHECK = (
    "(presentation IS NULL) = (presented_form IS NULL) "
    "AND (presentation IS NULL) = (presented_at IS NULL)"
)


class ReviewVariant(Base):
    """One fresh form of a study-page question for review (the "review bank"): the same idea
    and concept asked with a different surface, stored as full block content of the question's
    own type (validated by `teacher.blocks.validate_block_content`). `position` numbers a
    question's variants from 1. `run_id` is the `review_bank` generation run that wrote it
    (NULL once that run is gone); a rebuild replaces a page's variants."""

    __tablename__ = "review_variants"
    __table_args__ = (
        UniqueConstraint("block_id", "position"),
        CheckConstraint(f"type IN ({_sql_list(REVIEW_QUESTION_TYPES)})", name="type"),
        CheckConstraint("json_valid(content)", name="content_json").ddl_if(dialect="sqlite"),
        CheckConstraint("position >= 1", name="position"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    block_id: Mapped[int] = mapped_column(ForeignKey("artifact_blocks.id", ondelete="CASCADE"))
    position: Mapped[int] = mapped_column(Integer)
    type: Mapped[str] = mapped_column(Text)
    content: Mapped[dict[str, Any]] = mapped_column(JSON)
    model: Mapped[str] = mapped_column(Text)
    run_id: Mapped[int | None] = mapped_column(
        ForeignKey("generation_runs.id", ondelete="SET NULL"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, server_default=CURRENT_TIMESTAMP
    )

    def __repr__(self) -> str:
        return (
            f"ReviewVariant(id={self.id!r}, block_id={self.block_id!r}, "
            f"position={self.position!r})"
        )


class ReviewItem(Base):
    """A cleared study-page question in the review queue, with its schedule
    (`teacher.review_schedule`): due at `due_at`, after which a successful review multiplies
    `interval_days`. `cleared_at` and `first_clean` come from the page's answers; `last_*` from
    the latest answer (on the page, or a review). The `presented_*` columns hold the form and
    option order last served and not yet answered (`presentation` is its token)."""

    __tablename__ = "review_items"
    __table_args__ = (
        UniqueConstraint("block_id"),
        CheckConstraint("interval_days > 0", name="interval_days"),
        CheckConstraint("reviews >= 0", name="reviews"),
        CheckConstraint(REVIEW_LAST_VERDICT_CHECK, name="last_verdict"),
        CheckConstraint(REVIEW_LAST_CONFIDENCE_CHECK, name="last_confidence"),
        CheckConstraint(f"presented_form IN ({_sql_list(REVIEW_FORMS)})", name="presented_form"),
        CheckConstraint("json_valid(presented_order)", name="presented_order_json").ddl_if(dialect="sqlite"),
        CheckConstraint(REVIEW_PRESENTATION_CHECK, name="presentation"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    block_id: Mapped[int] = mapped_column(ForeignKey("artifact_blocks.id", ondelete="CASCADE"))
    cleared_at: Mapped[datetime] = mapped_column(DateTime)
    first_clean: Mapped[bool] = mapped_column(Boolean)
    interval_days: Mapped[float] = mapped_column(Float)
    due_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    reviews: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    last_verdict: Mapped[str] = mapped_column(Text)
    last_confidence: Mapped[str | None] = mapped_column(Text)
    last_answered_at: Mapped[datetime] = mapped_column(DateTime)
    presentation: Mapped[str | None] = mapped_column(Text)
    presented_form: Mapped[str | None] = mapped_column(Text)
    presented_variant_id: Mapped[int | None] = mapped_column(
        ForeignKey("review_variants.id", ondelete="SET NULL"), index=True
    )
    presented_order: Mapped[list[int] | None] = mapped_column(JSON(none_as_null=True))
    presented_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, server_default=CURRENT_TIMESTAMP
    )

    def __repr__(self) -> str:
        return f"ReviewItem(id={self.id!r}, block_id={self.block_id!r}, due_at={self.due_at!r})"


class ReviewAttempt(Base):
    """One review answer: which form was asked (`form`, the variant and its `variant_position`;
    `variant_id` is NULL for the original or once a rebuild removed the variant), what was shown
    (`asked`: the public content, options in the order shown, and `option_order`: shown position
    -> the form's option index, multiple choice only), the `response` in the form's own option
    order (what the grader graded), the grade (`verdict`, `missing`, `feedback`, `grader`,
    `details`), the reader's `confidence`, and the schedule before and after."""

    __tablename__ = "review_attempts"
    __table_args__ = (
        CheckConstraint(f"form IN ({_sql_list(REVIEW_FORMS)})", name="form"),
        CheckConstraint("(form = 'variant') = (variant_position IS NOT NULL)", name="variant"),
        CheckConstraint("json_valid(asked)", name="asked_json").ddl_if(dialect="sqlite"),
        CheckConstraint("json_valid(option_order)", name="option_order_json").ddl_if(dialect="sqlite"),
        CheckConstraint("json_valid(response)", name="response_json").ddl_if(dialect="sqlite"),
        CheckConstraint("json_valid(details)", name="details_json").ddl_if(dialect="sqlite"),
        CheckConstraint(REVIEW_VERDICT_CHECK, name="verdict"),
        CheckConstraint("json_valid(missing)", name="missing_json").ddl_if(dialect="sqlite"),
        CheckConstraint(MISSING_ARRAY_CHECK, name="missing_array").ddl_if(dialect="sqlite"),
        CheckConstraint(MISSING_PARTIAL_CHECK, name="missing_partial"),
        CheckConstraint(CONFIDENCE_CHECK, name="confidence"),
        CheckConstraint("interval_before > 0 AND interval_after > 0", name="intervals"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    item_id: Mapped[int] = mapped_column(
        ForeignKey("review_items.id", ondelete="CASCADE"), index=True
    )
    form: Mapped[str] = mapped_column(Text)
    variant_id: Mapped[int | None] = mapped_column(
        ForeignKey("review_variants.id", ondelete="SET NULL"), index=True
    )
    variant_position: Mapped[int | None] = mapped_column(Integer)
    asked: Mapped[dict[str, Any]] = mapped_column(JSON)
    option_order: Mapped[list[int] | None] = mapped_column(JSON(none_as_null=True))
    response: Mapped[Any] = mapped_column(JSON)
    verdict: Mapped[str] = mapped_column(Text)
    missing: Mapped[list[str]] = mapped_column(JSON, default=list, server_default="[]")
    confidence: Mapped[str] = mapped_column(Text)
    feedback: Mapped[str | None] = mapped_column(Text)
    grader: Mapped[str] = mapped_column(Text)
    details: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True))
    interval_before: Mapped[float] = mapped_column(Float)
    interval_after: Mapped[float] = mapped_column(Float)
    due_before: Mapped[datetime] = mapped_column(DateTime)
    due_after: Mapped[datetime] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, server_default=CURRENT_TIMESTAMP
    )

    def __repr__(self) -> str:
        return (
            f"ReviewAttempt(id={self.id!r}, item_id={self.item_id!r}, verdict={self.verdict!r})"
        )


# -- citations: what each source cites (revision 0015, `teacher.reference_extraction`) ----------

# Where an extraction found a source's references: a reference list (bibliography), citations
# or addresses in the running text (a web page without a list), or nowhere.
REFERENCE_FOUND_IN: tuple[str, ...] = ("reference_list", "inline_citations", "none")
REFERENCE_MATCH_METHODS: tuple[str, ...] = ("doi", "arxiv", "title")
REFERENCE_RUN_STATUSES: tuple[str, ...] = ("running", "succeeded", "failed")


class SourceReferenceExtraction(Base):
    """The state of reference extraction for one knowledge source; no row: never run.

    `status`, `run_number`, `trigger`, `extractor`, `model`, `started_at`, `finished_at`,
    `error` and the token counts describe the latest run (`running`, `succeeded` or `failed`;
    `error` is set exactly when it failed). `succeeded_at`, `found_in` and `reference_count`
    describe the references stored now, i.e. those of the latest run that succeeded (all NULL
    until one has): a failed run leaves the earlier references as they were. A run counts as
    interrupted (the server stopped during it) once it is `running` for longer than
    `teacher.reference_extraction.STALE_RUN_AFTER`. Deleting the source deletes the row.
    """

    __tablename__ = "source_reference_extractions"
    __table_args__ = (
        CheckConstraint(f"status IN ({_sql_list(REFERENCE_RUN_STATUSES)})", name="status"),
        CheckConstraint("(status = 'failed') = (error IS NOT NULL)", name="error"),
        CheckConstraint("(status = 'running') = (finished_at IS NULL)", name="finished"),
        CheckConstraint("status != 'succeeded' OR succeeded_at IS NOT NULL", name="succeeded"),
        CheckConstraint(
            "(succeeded_at IS NULL) = (found_in IS NULL) "
            "AND (succeeded_at IS NULL) = (reference_count IS NULL)",
            name="stored",
        ),
        CheckConstraint(f"found_in IN ({_sql_list(REFERENCE_FOUND_IN)})", name="found_in"),
        CheckConstraint("reference_count >= 0", name="reference_count"),
        CheckConstraint("found_in != 'none' OR reference_count = 0", name="none_found"),
        CheckConstraint("run_number >= 1", name="run_number"),
    )

    source_id: Mapped[int] = mapped_column(
        ForeignKey("knowledge_sources.id", ondelete="CASCADE"), primary_key=True
    )
    status: Mapped[str] = mapped_column(Text)
    run_number: Mapped[int] = mapped_column(Integer)
    trigger: Mapped[str] = mapped_column(Text)
    extractor: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, server_default=CURRENT_TIMESTAMP
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    error: Mapped[str | None] = mapped_column(Text)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    succeeded_at: Mapped[datetime | None] = mapped_column(DateTime)
    found_in: Mapped[str | None] = mapped_column(Text)
    reference_count: Mapped[int | None] = mapped_column(Integer)

    def __repr__(self) -> str:
        return (
            f"SourceReferenceExtraction(source_id={self.source_id!r}, status={self.status!r}, "
            f"run_number={self.run_number!r})"
        )


class SourceReference(Base):
    """One entry of a knowledge source's reference list, as extracted (`raw_text` as printed;
    the parsed fields NULL, or `authors` empty, where the extraction found nothing).

    `matched_source_id` is the library source the entry was matched to (by `match_method`:
    `doi`, `arxiv` or `title`; see `teacher.managers.source_references`), NULL when it is not
    in the library. `match_method` is meaningful only while `matched_source_id` is set: deleting
    the matched source sets the id NULL (and the app re-runs matching, which clears the
    method). Deleting the citing source deletes its references.
    """

    __tablename__ = "source_references"
    __table_args__ = (
        UniqueConstraint("source_id", "position"),
        CheckConstraint("position >= 1", name="position"),
        CheckConstraint("json_valid(authors)", name="authors_json").ddl_if(dialect="sqlite"),
        CheckConstraint(
            f"match_method IN ({_sql_list(REFERENCE_MATCH_METHODS)})", name="match_method"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("knowledge_sources.id", ondelete="CASCADE"))
    position: Mapped[int] = mapped_column(Integer)
    raw_text: Mapped[str] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    authors: Mapped[list[str]] = mapped_column(JSON, default=list, server_default="[]")
    year: Mapped[int | None] = mapped_column(Integer)
    venue: Mapped[str | None] = mapped_column(Text)
    doi: Mapped[str | None] = mapped_column(Text)
    arxiv_id: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(Text)
    matched_source_id: Mapped[int | None] = mapped_column(
        ForeignKey("knowledge_sources.id", ondelete="SET NULL"), index=True
    )
    match_method: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, server_default=CURRENT_TIMESTAMP
    )

    def __repr__(self) -> str:
        return (
            f"SourceReference(id={self.id!r}, source_id={self.source_id!r}, "
            f"position={self.position!r}, matched_source_id={self.matched_source_id!r})"
        )


# What a recorded API call was for: the job it served (`teacher.costs`).
API_CALL_PURPOSES: tuple[str, ...] = (
    "generation",
    "lab_generation",
    "review_bank",
    "glossary",
    "tutor",
    "grading",
    "block_tagging",
    "concept_extraction",
    "concept_relating",
    "metadata",
    "reference_extraction",
    "embedding",
)
API_CALL_PROVIDERS: tuple[str, ...] = ("anthropic", "openai")


class ApiCall(Base):
    """One model or embedding API call and what it cost: the cost ledger (revision 0016).

    Written by `teacher.costs` right after the API answers, in a transaction of its own, so a
    call whose reply is then rejected, or whose surrounding work rolls back, keeps its row.
    `purpose` is the job the call served (`API_CALL_PURPOSES`), `step` what the call did within
    it (a generation stage such as "analyse", "open-book check", "judge", "embedding", ...).
    Tokens: `input_tokens` is fresh (uncached) input, `cache_write_tokens` all cache writes, of
    which `cache_write_1h_tokens` at the 1-hour TTL (NULL: the split is unknown),
    `cache_read_tokens` input read from the cache. `cost_usd` is the price at the time of the
    call (`teacher.pricing`, checked on `priced_on`), NULL exactly when the model has no known
    price. `is_estimate`: a backfilled call of which only the combined input count was kept
    (then in `input_tokens`, the cache columns 0) or whose cache-write TTL split is unknown;
    `cost_usd` is then an upper bound. The links say what the call was for; each is set NULL
    when what it points at is deleted, and the row stays (the money was spent). `backfill_key`
    names the record a backfilled row was made from (`teacher.cost_backfill`), NULL for a call
    recorded live.
    """

    __tablename__ = "api_calls"
    __table_args__ = (
        CheckConstraint(f"purpose IN ({_sql_list(API_CALL_PURPOSES)})", name="purpose"),
        CheckConstraint(f"provider IN ({_sql_list(API_CALL_PROVIDERS)})", name="provider"),
        CheckConstraint(
            "input_tokens >= 0 AND cache_write_tokens >= 0 AND cache_read_tokens >= 0 "
            "AND output_tokens >= 0",
            name="tokens",
        ),
        CheckConstraint(
            "cache_write_1h_tokens IS NULL "
            "OR (cache_write_1h_tokens >= 0 AND cache_write_1h_tokens <= cache_write_tokens)",
            name="cache_write_1h",
        ),
        CheckConstraint("cost_usd IS NULL OR cost_usd >= 0", name="cost"),
        CheckConstraint("(cost_usd IS NULL) = (priced_on IS NULL)", name="priced"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, server_default=CURRENT_TIMESTAMP
    )
    purpose: Mapped[str] = mapped_column(Text)
    step: Mapped[str] = mapped_column(Text)
    provider: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(Text)
    response_id: Mapped[str | None] = mapped_column(Text)
    input_tokens: Mapped[int] = mapped_column(Integer)
    cache_write_tokens: Mapped[int] = mapped_column(Integer)
    cache_write_1h_tokens: Mapped[int | None] = mapped_column(Integer)
    cache_read_tokens: Mapped[int] = mapped_column(Integer)
    output_tokens: Mapped[int] = mapped_column(Integer)
    cost_usd: Mapped[float | None] = mapped_column(Float)
    priced_on: Mapped[str | None] = mapped_column(Text)
    is_estimate: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    artifact_id: Mapped[int | None] = mapped_column(
        ForeignKey("artifacts.id", ondelete="SET NULL"), index=True
    )
    generation_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("generation_runs.id", ondelete="SET NULL"), index=True
    )
    chat_thread_id: Mapped[int | None] = mapped_column(
        ForeignKey("chat_threads.id", ondelete="SET NULL"), index=True
    )
    chat_message_id: Mapped[int | None] = mapped_column(
        ForeignKey("chat_messages.id", ondelete="SET NULL"), index=True
    )
    knowledge_source_id: Mapped[int | None] = mapped_column(
        ForeignKey("knowledge_sources.id", ondelete="SET NULL"), index=True
    )
    backfill_key: Mapped[str | None] = mapped_column(Text, unique=True)

    def __repr__(self) -> str:
        return (
            f"ApiCall(id={self.id!r}, purpose={self.purpose!r}, step={self.step!r}, "
            f"model={self.model!r}, cost_usd={self.cost_usd!r})"
        )


__all__ = [
    "API_CALL_PROVIDERS",
    "API_CALL_PURPOSES",
    "ApiCall",
    "ATTENTION_SURFACES",
    "BLOCK_CONCEPT_ORIGINS",
    "BLOCK_CONCEPT_ROLES",
    "Artifact",
    "ArtifactBlock",
    "ArtifactSource",
    "AttentionSpan",
    "Base",
    "BlockConcept",
    "BlockResponse",
    "BlockSegmentRef",
    "ChatBlock",
    "ChatBlockResponse",
    "ChatMessage",
    "ChatThread",
    "Concept",
    "ConceptAlias",
    "ConceptEmbedding",
    "ConceptPairJudgement",
    "ConceptRelation",
    "GenerationRun",
    "GlossaryTerm",
    "JUDGEMENT_OUTCOMES",
    "JUDGEMENT_VERDICTS",
    "KnowledgeSource",
    "KnowledgeSourceIdentifier",
    "LabRunRecord",
    "LabWorkspace",
    "REFERENCE_FOUND_IN",
    "REFERENCE_MATCH_METHODS",
    "REFERENCE_RUN_STATUSES",
    "REVIEW_FORMS",
    "REVIEW_QUESTION_TYPES",
    "ReviewAttempt",
    "ReviewItem",
    "ReviewVariant",
    "STUDY_EVENT_KINDS",
    "SourceConcept",
    "SourceReference",
    "SourceReferenceExtraction",
    "SourceSegment",
    "StudyEvent",
    "StudySession",
    "utcnow",
]
