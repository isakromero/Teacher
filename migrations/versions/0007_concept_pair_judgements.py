"""Add concept_pair_judgements: the relation judge's memo of the concept pairs it has judged
(written by `teacher.concept_relating`).

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-21 10:00:00.000000

A new table only; existing rows are untouched and no pair has been judged yet.
- concept_pair_judgements: FKs low_id/high_id -> concepts.id ON DELETE CASCADE (high_id
  indexed; UNIQUE (low_id, high_id) serves lookups by low_id) and relation_id ->
  concept_relations.id ON DELETE SET NULL (indexed). CHECKs ck_concept_pair_judgements_pair_order
  (low_id < high_id), _focus (focus_id is one of the pair), _verdict (focus_builds_on_candidate/
  candidate_builds_on_focus/different_version_of/relates_to/none), _outcome (added/none/refused),
  _none_outcome (outcome none exactly for verdict none), _refusal (a refusal reason exactly for
  refused), _relation_outcome (a relation only for added) and _found_by_json. CURRENT_TIMESTAMP
  server default on judged_at.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0007'
down_revision: Union[str, Sequence[str], None] = '0006'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('concept_pair_judgements',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('low_id', sa.Integer(), nullable=False),
    sa.Column('high_id', sa.Integer(), nullable=False),
    sa.Column('focus_id', sa.Integer(), nullable=False),
    sa.Column('low_text_sha256', sa.Text(), nullable=False),
    sa.Column('high_text_sha256', sa.Text(), nullable=False),
    sa.Column('found_by', sa.JSON(), nullable=False),
    sa.Column('similarity', sa.Float(), nullable=True),
    sa.Column('verdict', sa.Text(), nullable=False),
    sa.Column('outcome', sa.Text(), nullable=False),
    sa.Column('rationale', sa.Text(), nullable=False),
    sa.Column('refusal', sa.Text(), nullable=True),
    sa.Column('relation_id', sa.Integer(), nullable=True),
    sa.Column('model', sa.Text(), nullable=False),
    sa.Column('judged_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.CheckConstraint('low_id < high_id', name=op.f('ck_concept_pair_judgements_pair_order')),
    sa.CheckConstraint('focus_id IN (low_id, high_id)', name=op.f('ck_concept_pair_judgements_focus')),
    sa.CheckConstraint("verdict IN ('focus_builds_on_candidate', 'candidate_builds_on_focus', 'different_version_of', 'relates_to', 'none')", name=op.f('ck_concept_pair_judgements_verdict')),
    sa.CheckConstraint("outcome IN ('added', 'none', 'refused')", name=op.f('ck_concept_pair_judgements_outcome')),
    sa.CheckConstraint("(verdict = 'none') = (outcome = 'none')", name=op.f('ck_concept_pair_judgements_none_outcome')),
    sa.CheckConstraint("(outcome = 'refused') = (refusal IS NOT NULL)", name=op.f('ck_concept_pair_judgements_refusal')),
    sa.CheckConstraint("relation_id IS NULL OR outcome = 'added'", name=op.f('ck_concept_pair_judgements_relation_outcome')),
    sa.CheckConstraint('json_valid(found_by)', name=op.f('ck_concept_pair_judgements_found_by_json')),
    sa.ForeignKeyConstraint(['low_id'], ['concepts.id'], name=op.f('fk_concept_pair_judgements_low_id_concepts'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['high_id'], ['concepts.id'], name=op.f('fk_concept_pair_judgements_high_id_concepts'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['relation_id'], ['concept_relations.id'], name=op.f('fk_concept_pair_judgements_relation_id_concept_relations'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_concept_pair_judgements')),
    sa.UniqueConstraint('low_id', 'high_id', name=op.f('uq_concept_pair_judgements_low_id_high_id'))
    )
    with op.batch_alter_table('concept_pair_judgements', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_concept_pair_judgements_high_id'), ['high_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_concept_pair_judgements_relation_id'), ['relation_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('concept_pair_judgements', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_concept_pair_judgements_relation_id'))
        batch_op.drop_index(batch_op.f('ix_concept_pair_judgements_high_id'))

    op.drop_table('concept_pair_judgements')
