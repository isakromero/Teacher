"""Add user accounts: users and user_sessions.

Revision ID: 0017
Revises: 0016
Create Date: 2026-11-25 12:00:00.000000

New tables only; no existing table or row is changed. No account is created here: the owner
registers one after deploying (`teacher.web.auth`) or adds it with `teacher users add`.

- users: id, email (UNIQUE, stored stripped and lowercased by the app; CHECK _email: 3 to 254
  characters), password_hash (`teacher.passwords`' scrypt string), created_at
  (CURRENT_TIMESTAMP server default), last_login_at and disabled_at (nullable).
- user_sessions: id, user_id (FK -> users.id ON DELETE CASCADE, indexed), token_sha256 (UNIQUE;
  the hex SHA-256 of the session token, which is never stored; CHECK _token_sha256: 64
  characters), created_at and last_seen_at (CURRENT_TIMESTAMP server defaults), expires_at, label
  (nullable; CHECK _label: 1 to 100 characters). CHECK _expiry: expires_at > created_at.

On PostgreSQL both tables get row level security enabled, with no policy: Supabase's Data API
(PostgREST, as the `anon` and `authenticated` roles) then sees no row of them, while the app,
which connects as the role that owns the tables, is not affected (RLS does not apply to a
table's owner). SQLite has no such thing.

The check texts are frozen here as written; while 0017 is the head they match
`teacher.db.models`. Downgrade drops both tables (every account and session is lost).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0017'
down_revision: Union[str, Sequence[str], None] = '0016'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TIMESTAMP = sa.text('(CURRENT_TIMESTAMP)')
CHECKS = {
    'users': (
        ('email', 'length(email) BETWEEN 3 AND 254'),
    ),
    'user_sessions': (
        ('token_sha256', 'length(token_sha256) = 64'),
        ('expiry', 'expires_at > created_at'),
        ('label', 'length(label) BETWEEN 1 AND 100'),
    ),
}
RLS_TABLES = ('users', 'user_sessions')


def _checks(table: str) -> list[sa.CheckConstraint]:
    return [
        sa.CheckConstraint(sqltext, name=op.f(f'ck_{table}_{name}'))
        for name, sqltext in CHECKS[table]
    ]


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('users',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('email', sa.Text(), nullable=False),
    sa.Column('password_hash', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(), server_default=TIMESTAMP, nullable=False),
    sa.Column('last_login_at', sa.DateTime(), nullable=True),
    sa.Column('disabled_at', sa.DateTime(), nullable=True),
    *_checks('users'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_users')),
    sa.UniqueConstraint('email', name=op.f('uq_users_email'))
    )
    op.create_table('user_sessions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('token_sha256', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(), server_default=TIMESTAMP, nullable=False),
    sa.Column('last_seen_at', sa.DateTime(), server_default=TIMESTAMP, nullable=False),
    sa.Column('expires_at', sa.DateTime(), nullable=False),
    sa.Column('label', sa.Text(), nullable=True),
    *_checks('user_sessions'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_user_sessions_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_user_sessions')),
    sa.UniqueConstraint('token_sha256', name=op.f('uq_user_sessions_token_sha256'))
    )
    with op.batch_alter_table('user_sessions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_user_sessions_user_id'), ['user_id'], unique=False)
    if op.get_bind().dialect.name == 'postgresql':
        for table in RLS_TABLES:
            op.execute(f'ALTER TABLE {table} ENABLE ROW LEVEL SECURITY')


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('user_sessions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_user_sessions_user_id'))
    op.drop_table('user_sessions')
    op.drop_table('users')
