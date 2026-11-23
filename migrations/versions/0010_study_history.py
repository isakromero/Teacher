"""Add the study history: study_sessions, study_events, attention_spans, and a session_id on
the activity tables; existing activity is grouped into backfilled sessions.

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-22 12:30:00.000000

- study_sessions: one sitting. CHECK last_activity_at >= started_at; `backfilled` (default 0)
  marks the sessions this migration derives; CURRENT_TIMESTAMP server defaults on started_at
  and last_activity_at.
- study_events: an append-only log of the reader's actions that leave no row elsewhere. FK
  session_id -> study_sessions.id ON DELETE RESTRICT; FKs artifact_id and lab_id ->
  artifacts.id, block_id -> artifact_blocks.id, concept_id -> concepts.id, source_id ->
  knowledge_sources.id, all nullable and ON DELETE SET NULL; every FK indexed; CHECK kind IN
  (the event kinds below); a json_valid() CHECK on payload (default '{}'); CURRENT_TIMESTAMP
  server default on occurred_at.
- attention_spans: time on a page from the page's heartbeats. FK session_id ON DELETE RESTRICT;
  artifact_id, lab_id, block_id, source_id as in study_events; CHECK surface IN ('study_page',
  'lab', 'source', 'graph'); CHECKs last_seen_at >= started_at, active_seconds >= 0 (default
  0), heartbeats >= 1 (default 1); CURRENT_TIMESTAMP server defaults on started_at and
  last_seen_at.
- block_responses, chat_block_responses, chat_messages, lab_runs: a nullable session_id, FK ->
  study_sessions.id ON DELETE RESTRICT, indexed (a table rebuild in batch mode).

Backfill: every existing row of the four activity tables, taken together in `created_at` order
(ties by table and id), joins the session of the row before it unless more than IDLE_GAP passed
since that row, in which case it starts a new session (the same rule the server applies to
live activity, `teacher.managers.study_history.SESSION_IDLE_GAP`). Each such session is stored
with backfilled = 1, started_at = its first row's time and last_activity_at = its last row's.
IDLE_GAP and the event kinds are frozen here as they were when this revision was written, so
re-running it gives the same result whatever the code says later.

Downgrade drops the session_id columns (with their FKs and indexes) and the three tables; the
activity rows themselves are kept.
"""
from datetime import datetime, timedelta
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0010'
down_revision: Union[str, Sequence[str], None] = '0009'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

IDLE_GAP = timedelta(minutes=30)
ACTIVITY_TABLES = ('block_responses', 'chat_block_responses', 'chat_messages', 'lab_runs')
EVENT_KINDS_CHECK = (
    "kind IN ('library_opened', 'study_page_opened', 'lab_opened', 'source_opened', "
    "'source_file_opened', 'graph_opened', 'gate_cleared', 'glossary_opened', 'glossary_term_viewed', "
    "'glossary_build_requested', 'chat_thread_started', 'lab_workspace_saved', "
    "'lab_hint_viewed', 'lab_solution_viewed', 'lab_exported', 'concept_inspected', "
    "'graph_searched', 'relations_requested', 'source_added', 'source_updated', "
    "'source_deleted', 'study_page_requested', 'lab_requested', 'regeneration_requested', "
    "'artifact_deleted')"
)


def _backfill() -> tuple[int, int]:
    """Group the existing activity rows into backfilled sessions; (sessions, rows) assigned."""
    conn = op.get_bind()
    rows: list[tuple[datetime, str, int]] = []
    for name in ACTIVITY_TABLES:
        table = sa.table(name, sa.column('id', sa.Integer), sa.column('created_at', sa.DateTime))
        for row_id, created_at in conn.execute(sa.select(table.c.id, table.c.created_at)):
            if created_at is None:
                raise RuntimeError(f"{name} row {row_id} has no created_at; cannot backfill")
            rows.append((created_at, name, row_id))
    rows.sort()

    groups: list[list[tuple[datetime, str, int]]] = []
    for row in rows:
        if groups and row[0] - groups[-1][-1][0] <= IDLE_GAP:
            groups[-1].append(row)
        else:
            groups.append([row])

    sessions = sa.table(
        'study_sessions',
        sa.column('id', sa.Integer),
        sa.column('started_at', sa.DateTime),
        sa.column('last_activity_at', sa.DateTime),
        sa.column('backfilled', sa.Boolean),
    )
    for group in groups:
        session_id = conn.execute(
            sessions.insert()
            .values(started_at=group[0][0], last_activity_at=group[-1][0], backfilled=True)
            .returning(sessions.c.id)
        ).scalar_one()
        by_table: dict[str, list[int]] = {}
        for _at, name, row_id in group:
            by_table.setdefault(name, []).append(row_id)
        for name, ids in by_table.items():
            table = sa.table(name, sa.column('id', sa.Integer), sa.column('session_id', sa.Integer))
            conn.execute(
                table.update().where(table.c.id.in_(ids)).values(session_id=session_id)
            )
    return len(groups), len(rows)


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('study_sessions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('started_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('last_activity_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('backfilled', sa.Boolean(), server_default=sa.text('0'), nullable=False),
    sa.CheckConstraint('last_activity_at >= started_at', name=op.f('ck_study_sessions_activity_order')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_study_sessions'))
    )
    op.create_table('attention_spans',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('session_id', sa.Integer(), nullable=False),
    sa.Column('surface', sa.Text(), nullable=False),
    sa.Column('artifact_id', sa.Integer(), nullable=True),
    sa.Column('lab_id', sa.Integer(), nullable=True),
    sa.Column('block_id', sa.Integer(), nullable=True),
    sa.Column('source_id', sa.Integer(), nullable=True),
    sa.Column('started_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('last_seen_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('active_seconds', sa.Float(), server_default=sa.text('0'), nullable=False),
    sa.Column('heartbeats', sa.Integer(), server_default=sa.text('1'), nullable=False),
    sa.CheckConstraint("surface IN ('study_page', 'lab', 'source', 'graph')", name=op.f('ck_attention_spans_surface')),
    sa.CheckConstraint('active_seconds >= 0', name=op.f('ck_attention_spans_active_seconds')),
    sa.CheckConstraint('heartbeats >= 1', name=op.f('ck_attention_spans_heartbeats')),
    sa.CheckConstraint('last_seen_at >= started_at', name=op.f('ck_attention_spans_seen_order')),
    sa.ForeignKeyConstraint(['artifact_id'], ['artifacts.id'], name=op.f('fk_attention_spans_artifact_id_artifacts'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['block_id'], ['artifact_blocks.id'], name=op.f('fk_attention_spans_block_id_artifact_blocks'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['lab_id'], ['artifacts.id'], name=op.f('fk_attention_spans_lab_id_artifacts'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['session_id'], ['study_sessions.id'], name=op.f('fk_attention_spans_session_id_study_sessions'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['source_id'], ['knowledge_sources.id'], name=op.f('fk_attention_spans_source_id_knowledge_sources'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_attention_spans'))
    )
    with op.batch_alter_table('attention_spans', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_attention_spans_artifact_id'), ['artifact_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_attention_spans_block_id'), ['block_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_attention_spans_lab_id'), ['lab_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_attention_spans_session_id'), ['session_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_attention_spans_source_id'), ['source_id'], unique=False)

    op.create_table('study_events',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('session_id', sa.Integer(), nullable=False),
    sa.Column('occurred_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('kind', sa.Text(), nullable=False),
    sa.Column('artifact_id', sa.Integer(), nullable=True),
    sa.Column('lab_id', sa.Integer(), nullable=True),
    sa.Column('block_id', sa.Integer(), nullable=True),
    sa.Column('concept_id', sa.Integer(), nullable=True),
    sa.Column('source_id', sa.Integer(), nullable=True),
    sa.Column('payload', sa.JSON(), server_default='{}', nullable=False),
    sa.CheckConstraint(EVENT_KINDS_CHECK, name=op.f('ck_study_events_kind')),
    sa.CheckConstraint('json_valid(payload)', name=op.f('ck_study_events_payload_json')),
    sa.ForeignKeyConstraint(['artifact_id'], ['artifacts.id'], name=op.f('fk_study_events_artifact_id_artifacts'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['block_id'], ['artifact_blocks.id'], name=op.f('fk_study_events_block_id_artifact_blocks'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['concept_id'], ['concepts.id'], name=op.f('fk_study_events_concept_id_concepts'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['lab_id'], ['artifacts.id'], name=op.f('fk_study_events_lab_id_artifacts'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['session_id'], ['study_sessions.id'], name=op.f('fk_study_events_session_id_study_sessions'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['source_id'], ['knowledge_sources.id'], name=op.f('fk_study_events_source_id_knowledge_sources'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_study_events'))
    )
    with op.batch_alter_table('study_events', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_study_events_artifact_id'), ['artifact_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_study_events_block_id'), ['block_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_study_events_concept_id'), ['concept_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_study_events_lab_id'), ['lab_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_study_events_session_id'), ['session_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_study_events_source_id'), ['source_id'], unique=False)

    for name in ACTIVITY_TABLES:
        with op.batch_alter_table(name, schema=None) as batch_op:
            batch_op.add_column(sa.Column('session_id', sa.Integer(), nullable=True))
            batch_op.create_index(batch_op.f(f'ix_{name}_session_id'), ['session_id'], unique=False)
            batch_op.create_foreign_key(batch_op.f(f'fk_{name}_session_id_study_sessions'), 'study_sessions', ['session_id'], ['id'], ondelete='RESTRICT')

    _backfill()


def downgrade() -> None:
    """Downgrade schema."""
    for name in reversed(ACTIVITY_TABLES):
        with op.batch_alter_table(name, schema=None) as batch_op:
            batch_op.drop_constraint(batch_op.f(f'fk_{name}_session_id_study_sessions'), type_='foreignkey')
            batch_op.drop_index(batch_op.f(f'ix_{name}_session_id'))
            batch_op.drop_column('session_id')

    with op.batch_alter_table('study_events', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_study_events_source_id'))
        batch_op.drop_index(batch_op.f('ix_study_events_session_id'))
        batch_op.drop_index(batch_op.f('ix_study_events_lab_id'))
        batch_op.drop_index(batch_op.f('ix_study_events_concept_id'))
        batch_op.drop_index(batch_op.f('ix_study_events_block_id'))
        batch_op.drop_index(batch_op.f('ix_study_events_artifact_id'))

    op.drop_table('study_events')
    with op.batch_alter_table('attention_spans', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_attention_spans_source_id'))
        batch_op.drop_index(batch_op.f('ix_attention_spans_session_id'))
        batch_op.drop_index(batch_op.f('ix_attention_spans_lab_id'))
        batch_op.drop_index(batch_op.f('ix_attention_spans_block_id'))
        batch_op.drop_index(batch_op.f('ix_attention_spans_artifact_id'))

    op.drop_table('attention_spans')
    op.drop_table('study_sessions')
