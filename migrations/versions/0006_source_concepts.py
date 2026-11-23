"""Add source_concepts: the links from a knowledge source to the concepts it introduces, uses or
assumes (written by concept extraction).

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-20 23:00:00.000000

A new table only; existing rows are untouched and no existing source has links yet.
- source_concepts: FK source_id -> knowledge_sources.id ON DELETE CASCADE and FK concept_id ->
  concepts.id ON DELETE CASCADE (indexed); UNIQUE (source_id, concept_id), which also serves
  lookups by source. CHECKs ck_source_concepts_role (introduces/uses/assumes),
  _resolution (created/exact/judged) and _judge_reason (a reason exactly for judged links).
  CURRENT_TIMESTAMP server defaults on created_at/updated_at.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0006'
down_revision: Union[str, Sequence[str], None] = '0005'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('source_concepts',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('source_id', sa.Integer(), nullable=False),
    sa.Column('concept_id', sa.Integer(), nullable=False),
    sa.Column('role', sa.Text(), nullable=False),
    sa.Column('resolution', sa.Text(), nullable=False),
    sa.Column('candidate_name', sa.Text(), nullable=False),
    sa.Column('judge_reason', sa.Text(), nullable=True),
    sa.Column('evidence', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('updated_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.CheckConstraint("role IN ('introduces', 'uses', 'assumes')", name=op.f('ck_source_concepts_role')),
    sa.CheckConstraint("resolution IN ('created', 'exact', 'judged')", name=op.f('ck_source_concepts_resolution')),
    sa.CheckConstraint("(resolution = 'judged') = (judge_reason IS NOT NULL)", name=op.f('ck_source_concepts_judge_reason')),
    sa.ForeignKeyConstraint(['source_id'], ['knowledge_sources.id'], name=op.f('fk_source_concepts_source_id_knowledge_sources'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['concept_id'], ['concepts.id'], name=op.f('fk_source_concepts_concept_id_concepts'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_source_concepts')),
    sa.UniqueConstraint('source_id', 'concept_id', name=op.f('uq_source_concepts_source_id_concept_id'))
    )
    with op.batch_alter_table('source_concepts', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_source_concepts_concept_id'), ['concept_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('source_concepts', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_source_concepts_concept_id'))

    op.drop_table('source_concepts')
