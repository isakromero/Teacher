"""Add offline_attempts: the idempotency record of attempts uploaded from a phone.

Revision ID: 0018
Revises: 0017
Create Date: 2026-11-25 18:00:00.000000

A new table only; no existing table or row is changed, nothing is backfilled.

- offline_attempts: id, client_id (UNIQUE; the UUID the phone generated for the attempt, in
  canonical lowercase form; CHECK _client_id: 36 characters), response_id (UNIQUE; FK ->
  block_responses.id ON DELETE CASCADE: the stored attempt), made_at (when it was made on the
  phone, naive UTC), device_grader (CHECK _device_grader: starts with "device:" and names
  something after it), device_verdict (CHECK _device_verdict: passed, partial or failed),
  received_at (CURRENT_TIMESTAMP server default). See `teacher.web.routers.offline`.

On PostgreSQL the table gets row level security enabled, with no policy, as 0017 does for the
account tables: Supabase's Data API roles see none of its rows, while the app (the table's
owner) is not affected. SQLite has no such thing.

The check texts are frozen here as written; while 0018 is the head they match
`teacher.db.models`. Downgrade drops the table (the record of which phone attempts were
uploaded is lost; the attempts themselves stay in block_responses).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0018'
down_revision: Union[str, Sequence[str], None] = '0017'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TIMESTAMP = sa.text('(CURRENT_TIMESTAMP)')
CHECKS = {
    'offline_attempts': (
        ('client_id', 'length(client_id) = 36'),
        (
            'device_grader',
            "substr(device_grader, 1, 7) = 'device:' AND length(device_grader) > 7",
        ),
        ('device_verdict', "device_verdict IN ('passed', 'partial', 'failed')"),
    ),
}
RLS_TABLES = ('offline_attempts',)


def _checks(table: str) -> list[sa.CheckConstraint]:
    return [
        sa.CheckConstraint(sqltext, name=op.f(f'ck_{table}_{name}'))
        for name, sqltext in CHECKS[table]
    ]


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('offline_attempts',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('client_id', sa.Text(), nullable=False),
    sa.Column('response_id', sa.Integer(), nullable=False),
    sa.Column('made_at', sa.DateTime(), nullable=False),
    sa.Column('device_grader', sa.Text(), nullable=False),
    sa.Column('device_verdict', sa.Text(), nullable=False),
    sa.Column('received_at', sa.DateTime(), server_default=TIMESTAMP, nullable=False),
    *_checks('offline_attempts'),
    sa.ForeignKeyConstraint(['response_id'], ['block_responses.id'], name=op.f('fk_offline_attempts_response_id_block_responses'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_offline_attempts')),
    sa.UniqueConstraint('client_id', name=op.f('uq_offline_attempts_client_id')),
    sa.UniqueConstraint('response_id', name=op.f('uq_offline_attempts_response_id'))
    )
    if op.get_bind().dialect.name == 'postgresql':
        for table in RLS_TABLES:
            op.execute(f'ALTER TABLE {table} ENABLE ROW LEVEL SECURITY')


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('offline_attempts')
