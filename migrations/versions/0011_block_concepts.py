"""Add the block concept tags: block_concepts and artifacts.block_concepts_tagged_at.

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-22 15:00:00.000000

Existing rows are untouched; no page is tagged by the migration (`teacher concepts tag-blocks`
does that).
- artifacts.block_concepts_tagged_at: nullable DATETIME, NULL for every existing artifact (its
  blocks have not been tagged). Added with a plain ALTER TABLE ADD COLUMN (no table rebuild).
- block_concepts: FK block_id -> artifact_blocks.id ON DELETE CASCADE and FK concept_id ->
  concepts.id ON DELETE CASCADE (indexed); UNIQUE (block_id, concept_id); CHECKs
  ck_block_concepts_role (main/incidental), ck_block_concepts_origin (model/user),
  ck_block_concepts_rationale (a model tag has a rationale) and ck_block_concepts_model (a model
  tag, and only a model tag, names its model); CURRENT_TIMESTAMP server default on created_at.

Downgrade drops both (the tags are lost; nothing else is).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0011'
down_revision: Union[str, Sequence[str], None] = '0010'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('artifacts', schema=None) as batch_op:
        batch_op.add_column(sa.Column('block_concepts_tagged_at', sa.DateTime(), nullable=True))

    op.create_table('block_concepts',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('block_id', sa.Integer(), nullable=False),
    sa.Column('concept_id', sa.Integer(), nullable=False),
    sa.Column('role', sa.Text(), nullable=False),
    sa.Column('origin', sa.Text(), nullable=False),
    sa.Column('rationale', sa.Text(), nullable=True),
    sa.Column('model', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.CheckConstraint("role IN ('main', 'incidental')", name=op.f('ck_block_concepts_role')),
    sa.CheckConstraint("origin IN ('model', 'user')", name=op.f('ck_block_concepts_origin')),
    sa.CheckConstraint("origin = 'user' OR rationale IS NOT NULL", name=op.f('ck_block_concepts_rationale')),
    sa.CheckConstraint("(origin = 'model') = (model IS NOT NULL)", name=op.f('ck_block_concepts_model')),
    sa.ForeignKeyConstraint(['block_id'], ['artifact_blocks.id'], name=op.f('fk_block_concepts_block_id_artifact_blocks'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['concept_id'], ['concepts.id'], name=op.f('fk_block_concepts_concept_id_concepts'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_block_concepts')),
    sa.UniqueConstraint('block_id', 'concept_id', name=op.f('uq_block_concepts_block_id_concept_id'))
    )
    with op.batch_alter_table('block_concepts', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_block_concepts_concept_id'), ['concept_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('block_concepts', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_block_concepts_concept_id'))

    op.drop_table('block_concepts')
    with op.batch_alter_table('artifacts', schema=None) as batch_op:
        batch_op.drop_column('block_concepts_tagged_at')
