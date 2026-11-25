"""Add the tutor side chat: chat_threads and chat_messages.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-19 18:00:00.000000

New tables only; existing rows are untouched.
- chat_threads: one conversation per row, FK artifacts.id ON DELETE CASCADE (indexed).
- chat_messages: FK chat_threads.id ON DELETE CASCADE; UNIQUE (thread_id, position);
  CHECK ck_chat_messages_role (user/assistant) and ck_chat_messages_kind
  (message/check_understanding); json_valid() CHECKs on "references" (quoted: a reserved word),
  api_content and context_blocks; server defaults kind 'message', references '[]',
  context_blocks '[]' and CURRENT_TIMESTAMP on both created_at columns.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0003'
down_revision: Union[str, Sequence[str], None] = '0002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('chat_threads',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('artifact_id', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.ForeignKeyConstraint(['artifact_id'], ['artifacts.id'], name=op.f('fk_chat_threads_artifact_id_artifacts'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_chat_threads'))
    )
    with op.batch_alter_table('chat_threads', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_chat_threads_artifact_id'), ['artifact_id'], unique=False)

    op.create_table('chat_messages',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('thread_id', sa.Integer(), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.Column('role', sa.Text(), nullable=False),
    sa.Column('kind', sa.Text(), server_default='message', nullable=False),
    sa.Column('text', sa.Text(), nullable=False),
    sa.Column('references', sa.JSON(), server_default='[]', nullable=False),
    sa.Column('api_content', sa.JSON(), nullable=False),
    sa.Column('context_blocks', sa.JSON(), server_default='[]', nullable=False),
    sa.Column('model', sa.Text(), nullable=True),
    sa.Column('input_tokens', sa.Integer(), nullable=True),
    sa.Column('output_tokens', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.CheckConstraint("role IN ('user', 'assistant')", name=op.f('ck_chat_messages_role')),
    sa.CheckConstraint("kind IN ('message', 'check_understanding')", name=op.f('ck_chat_messages_kind')),
    sa.CheckConstraint('json_valid("references")', name=op.f('ck_chat_messages_references_json')).ddl_if(dialect='sqlite'),
    sa.CheckConstraint('json_valid(api_content)', name=op.f('ck_chat_messages_api_content_json')).ddl_if(dialect='sqlite'),
    sa.CheckConstraint('json_valid(context_blocks)', name=op.f('ck_chat_messages_context_blocks_json')).ddl_if(dialect='sqlite'),
    sa.ForeignKeyConstraint(['thread_id'], ['chat_threads.id'], name=op.f('fk_chat_messages_thread_id_chat_threads'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_chat_messages')),
    sa.UniqueConstraint('thread_id', 'position', name=op.f('uq_chat_messages_thread_id_position'))
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('chat_messages')
    with op.batch_alter_table('chat_threads', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_chat_threads_artifact_id'))

    op.drop_table('chat_threads')
