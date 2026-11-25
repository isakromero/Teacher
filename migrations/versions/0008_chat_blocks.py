"""Add the blocks the tutor shows in the side chat: chat_blocks and chat_block_responses.

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-21 14:00:00.000000

New tables only; existing rows are untouched (earlier chat messages simply have no blocks).
- chat_blocks: an exercise, chart or interactable inside an assistant chat message. FK
  message_id -> chat_messages.id ON DELETE CASCADE; UNIQUE (message_id, position); a json_valid()
  CHECK on content; CURRENT_TIMESTAMP server default on created_at.
- chat_block_responses: the reader's attempts at a chat exercise (like block_responses). FK
  block_id -> chat_blocks.id ON DELETE CASCADE; UNIQUE (block_id, attempt); FK
  reported_message_id -> chat_messages.id ON DELETE SET NULL (indexed): the user message that
  told the tutor about the attempt; json_valid() CHECKs on response and details;
  CURRENT_TIMESTAMP server default on created_at.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0008'
down_revision: Union[str, Sequence[str], None] = '0007'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('chat_blocks',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('message_id', sa.Integer(), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.Column('text_offset', sa.Integer(), nullable=False),
    sa.Column('type', sa.Text(), nullable=False),
    sa.Column('is_gate', sa.Boolean(), nullable=False),
    sa.Column('content', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.CheckConstraint('json_valid(content)', name=op.f('ck_chat_blocks_content_json')).ddl_if(dialect='sqlite'),
    sa.ForeignKeyConstraint(['message_id'], ['chat_messages.id'], name=op.f('fk_chat_blocks_message_id_chat_messages'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_chat_blocks')),
    sa.UniqueConstraint('message_id', 'position', name=op.f('uq_chat_blocks_message_id_position'))
    )
    op.create_table('chat_block_responses',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('block_id', sa.Integer(), nullable=False),
    sa.Column('attempt', sa.Integer(), nullable=False),
    sa.Column('response', sa.JSON(), nullable=False),
    sa.Column('passed', sa.Boolean(), nullable=False),
    sa.Column('feedback', sa.Text(), nullable=True),
    sa.Column('grader', sa.Text(), nullable=False),
    sa.Column('details', sa.JSON(none_as_null=True), nullable=True),
    sa.Column('reported_message_id', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.CheckConstraint('json_valid(response)', name=op.f('ck_chat_block_responses_response_json')).ddl_if(dialect='sqlite'),
    sa.CheckConstraint('json_valid(details)', name=op.f('ck_chat_block_responses_details_json')).ddl_if(dialect='sqlite'),
    sa.ForeignKeyConstraint(['block_id'], ['chat_blocks.id'], name=op.f('fk_chat_block_responses_block_id_chat_blocks'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['reported_message_id'], ['chat_messages.id'], name=op.f('fk_chat_block_responses_reported_message_id_chat_messages'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_chat_block_responses')),
    sa.UniqueConstraint('block_id', 'attempt', name=op.f('uq_chat_block_responses_block_id_attempt'))
    )
    with op.batch_alter_table('chat_block_responses', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_chat_block_responses_reported_message_id'), ['reported_message_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('chat_block_responses', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_chat_block_responses_reported_message_id'))

    op.drop_table('chat_block_responses')
    op.drop_table('chat_blocks')
