"""Add highlights (passages of a study page a reader highlighted, with an optional note) and
offline_review_answers (the idempotency record of review answers uploaded from a phone).

Revision ID: 0019
Revises: 0018
Create Date: 2026-11-26 12:00:00.000000

Two new tables only; no existing table or row is changed, nothing is backfilled.

- highlights: id, uid (UNIQUE; the public id, a UUID in canonical lowercase form, made by the
  server or a phone; CHECK _uid: 36 characters), user_id (FK -> users.id ON DELETE CASCADE,
  NULL when the app runs without accounts; indexed), artifact_id (FK -> artifacts.id ON DELETE
  CASCADE; indexed), block_key (CHECK _block_key: not empty), quote (CHECK _quote: not empty),
  prefix and suffix (the context around the quote, possibly empty), start_offset and
  end_offset (CHECK _offsets: 0 <= start < end), note (NULL or not empty: CHECK _note),
  created_at and updated_at (CURRENT_TIMESTAMP server default), deleted_at (a tombstone; CHECK
  _deleted_note: a deleted highlight keeps no note). See `teacher.managers.highlights`.
- offline_review_answers: id, client_id (UNIQUE; the UUID the phone generated for the answer,
  canonical lowercase form; CHECK _client_id: 36 characters), attempt_id (UNIQUE; FK ->
  review_attempts.id ON DELETE CASCADE: the stored review answer), made_at (when it was made on
  the phone, naive UTC), device_grader (CHECK _device_grader: starts with "device:" and names
  something after it), device_verdict (CHECK _device_verdict: passed, partial or failed),
  received_at (CURRENT_TIMESTAMP server default). See `teacher.web.routers.offline_review`.

On PostgreSQL both tables get row level security enabled, with no policy, as 0017 and 0018 do:
Supabase's Data API roles see none of their rows, while the app (the tables' owner) is not
affected. SQLite has no such thing.

The check texts are frozen here as written; while 0019 is the head they match
`teacher.db.models`. Downgrade drops both tables (every highlight and note is lost, and the
record of which review answers came from a phone; the answers themselves stay in
review_attempts).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0019'
down_revision: Union[str, Sequence[str], None] = '0018'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TIMESTAMP = sa.text('(CURRENT_TIMESTAMP)')
CHECKS = {
    'highlights': (
        ('uid', 'length(uid) = 36'),
        ('block_key', 'length(block_key) > 0'),
        ('quote', 'length(quote) > 0'),
        ('offsets', 'start_offset >= 0 AND end_offset > start_offset'),
        ('note', 'note IS NULL OR length(note) > 0'),
        ('deleted_note', 'deleted_at IS NULL OR note IS NULL'),
    ),
    'offline_review_answers': (
        ('client_id', 'length(client_id) = 36'),
        (
            'device_grader',
            "substr(device_grader, 1, 7) = 'device:' AND length(device_grader) > 7",
        ),
        ('device_verdict', "device_verdict IN ('passed', 'partial', 'failed')"),
    ),
}
RLS_TABLES = ('highlights', 'offline_review_answers')


def _checks(table: str) -> list[sa.CheckConstraint]:
    return [
        sa.CheckConstraint(sqltext, name=op.f(f'ck_{table}_{name}'))
        for name, sqltext in CHECKS[table]
    ]


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('highlights',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('uid', sa.Text(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=True),
    sa.Column('artifact_id', sa.Integer(), nullable=False),
    sa.Column('block_key', sa.Text(), nullable=False),
    sa.Column('quote', sa.Text(), nullable=False),
    sa.Column('prefix', sa.Text(), nullable=False),
    sa.Column('suffix', sa.Text(), nullable=False),
    sa.Column('start_offset', sa.Integer(), nullable=False),
    sa.Column('end_offset', sa.Integer(), nullable=False),
    sa.Column('note', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(), server_default=TIMESTAMP, nullable=False),
    sa.Column('updated_at', sa.DateTime(), server_default=TIMESTAMP, nullable=False),
    sa.Column('deleted_at', sa.DateTime(), nullable=True),
    *_checks('highlights'),
    sa.ForeignKeyConstraint(['artifact_id'], ['artifacts.id'], name=op.f('fk_highlights_artifact_id_artifacts'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_highlights_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_highlights')),
    sa.UniqueConstraint('uid', name=op.f('uq_highlights_uid'))
    )
    with op.batch_alter_table('highlights', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_highlights_artifact_id'), ['artifact_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_highlights_user_id'), ['user_id'], unique=False)
    op.create_table('offline_review_answers',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('client_id', sa.Text(), nullable=False),
    sa.Column('attempt_id', sa.Integer(), nullable=False),
    sa.Column('made_at', sa.DateTime(), nullable=False),
    sa.Column('device_grader', sa.Text(), nullable=False),
    sa.Column('device_verdict', sa.Text(), nullable=False),
    sa.Column('received_at', sa.DateTime(), server_default=TIMESTAMP, nullable=False),
    *_checks('offline_review_answers'),
    sa.ForeignKeyConstraint(['attempt_id'], ['review_attempts.id'], name=op.f('fk_offline_review_answers_attempt_id_review_attempts'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_offline_review_answers')),
    sa.UniqueConstraint('attempt_id', name=op.f('uq_offline_review_answers_attempt_id')),
    sa.UniqueConstraint('client_id', name=op.f('uq_offline_review_answers_client_id'))
    )
    if op.get_bind().dialect.name == 'postgresql':
        for table in RLS_TABLES:
            op.execute(f'ALTER TABLE {table} ENABLE ROW LEVEL SECURITY')


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('offline_review_answers')
    with op.batch_alter_table('highlights', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_highlights_user_id'))
        batch_op.drop_index(batch_op.f('ix_highlights_artifact_id'))
    op.drop_table('highlights')
