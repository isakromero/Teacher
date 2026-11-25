"""Add the cost ledger: api_calls.

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-24 12:00:00.000000

A new table only; no existing row is changed and nothing is backfilled here. Past calls are
added by `teacher costs backfill` (`teacher.cost_backfill`), which reads the run records and
stage files the migration cannot judge.

- api_calls: one row per model or embedding API call (`teacher.costs`): created_at
  (CURRENT_TIMESTAMP server default), purpose, step, provider, model, response_id, the token
  breakdown (input_tokens, cache_write_tokens, cache_write_1h_tokens nullable,
  cache_read_tokens, output_tokens), cost_usd (nullable: no known price) with priced_on,
  is_estimate (false server default: 0 on SQLite), and nullable links, each FK ON DELETE SET NULL and indexed:
  artifact_id -> artifacts, generation_run_id -> generation_runs, chat_thread_id ->
  chat_threads, chat_message_id -> chat_messages, knowledge_source_id -> knowledge_sources.
  backfill_key is UNIQUE (NULL for live rows). CHECKs: _purpose, _provider, _tokens,
  _cache_write_1h, _cost, _priced (cost_usd and priced_on NULL together). There is no check
  that a message link comes with its thread link: deleting a thread clears the two links one
  after the other.

The check texts are frozen here as written; while 0016 is the head they match
`teacher.db.models`. Downgrade drops the table (the ledger is lost).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0016'
down_revision: Union[str, Sequence[str], None] = '0015'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TIMESTAMP = sa.text('(CURRENT_TIMESTAMP)')
PURPOSES = (
    'generation', 'lab_generation', 'review_bank', 'glossary', 'tutor', 'grading',
    'block_tagging', 'concept_extraction', 'concept_relating', 'metadata',
    'reference_extraction', 'embedding',
)
CHECKS = {
    'api_calls': (
        ('purpose', 'purpose IN (' + ', '.join(f"'{p}'" for p in PURPOSES) + ')'),
        ('provider', "provider IN ('anthropic', 'openai')"),
        (
            'tokens',
            'input_tokens >= 0 AND cache_write_tokens >= 0 AND cache_read_tokens >= 0 '
            'AND output_tokens >= 0',
        ),
        (
            'cache_write_1h',
            'cache_write_1h_tokens IS NULL '
            'OR (cache_write_1h_tokens >= 0 AND cache_write_1h_tokens <= cache_write_tokens)',
        ),
        ('cost', 'cost_usd IS NULL OR cost_usd >= 0'),
        ('priced', '(cost_usd IS NULL) = (priced_on IS NULL)'),
    ),
}
LINKS = (
    ('artifact_id', 'artifacts'),
    ('generation_run_id', 'generation_runs'),
    ('chat_thread_id', 'chat_threads'),
    ('chat_message_id', 'chat_messages'),
    ('knowledge_source_id', 'knowledge_sources'),
)


def _checks(table: str) -> list[sa.CheckConstraint]:
    return [
        sa.CheckConstraint(sqltext, name=op.f(f'ck_{table}_{name}'))
        for name, sqltext in CHECKS[table]
    ]


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('api_calls',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), server_default=TIMESTAMP, nullable=False),
    sa.Column('purpose', sa.Text(), nullable=False),
    sa.Column('step', sa.Text(), nullable=False),
    sa.Column('provider', sa.Text(), nullable=False),
    sa.Column('model', sa.Text(), nullable=False),
    sa.Column('response_id', sa.Text(), nullable=True),
    sa.Column('input_tokens', sa.Integer(), nullable=False),
    sa.Column('cache_write_tokens', sa.Integer(), nullable=False),
    sa.Column('cache_write_1h_tokens', sa.Integer(), nullable=True),
    sa.Column('cache_read_tokens', sa.Integer(), nullable=False),
    sa.Column('output_tokens', sa.Integer(), nullable=False),
    sa.Column('cost_usd', sa.Float(), nullable=True),
    sa.Column('priced_on', sa.Text(), nullable=True),
    sa.Column('is_estimate', sa.Boolean(), server_default=sa.false(), nullable=False),
    *(sa.Column(column, sa.Integer(), nullable=True) for column, _table in LINKS),
    sa.Column('backfill_key', sa.Text(), nullable=True),
    *_checks('api_calls'),
    *(
        sa.ForeignKeyConstraint(
            [column], [f'{table}.id'],
            name=op.f(f'fk_api_calls_{column}_{table}'), ondelete='SET NULL',
        )
        for column, table in LINKS
    ),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_api_calls')),
    sa.UniqueConstraint('backfill_key', name=op.f('uq_api_calls_backfill_key'))
    )
    with op.batch_alter_table('api_calls', schema=None) as batch_op:
        for column, _table in LINKS:
            batch_op.create_index(batch_op.f(f'ix_api_calls_{column}'), [column], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('api_calls', schema=None) as batch_op:
        for column, _table in LINKS:
            batch_op.drop_index(batch_op.f(f'ix_api_calls_{column}'))
    op.drop_table('api_calls')
