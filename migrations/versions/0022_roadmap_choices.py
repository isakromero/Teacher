"""Add the reader's roadmap choices to exploration concepts: the chosen primary source and the
"I know this" mark.

Revision ID: 0022
Revises: 0021
Create Date: 2026-11-27 22:00:00.000000

One table changed; no row is changed, nothing is backfilled. See `teacher.exploration.roadmap`
(the roadmap, whose progress is computed when read and never stored) and
`teacher.managers.explorations` (`set_primary_source`, `set_marked_known`).

- exploration_concepts: primary_source_id (FK -> knowledge_sources.id ON DELETE SET NULL;
  indexed, ix_exploration_concepts_primary_source_id): the source the reader chose to study the
  concept from, one of its grounding sources (the manager checks that; the database cannot, as
  grounding is read from source_concepts when it is needed). NULL: the recommended source.
  Deleting the source clears the choice. marked_known_at (DateTime, nullable): when the reader
  marked the concept "I know this"; NULL: not marked. Existing rows get NULL for both.

No CHECK is added (so 0020's and 0021's frozen check texts still match the models). On SQLite
adding the foreign key rebuilds the table (batch mode keeps its indexes, foreign keys and
checks). The table already has row level security on PostgreSQL (0020).

Downgrade drops both columns: every chosen primary source and every "I know this" mark is lost
(the roadmap then recommends the best source again and counts those concepts not done).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0022'
down_revision: Union[str, Sequence[str], None] = '0021'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('exploration_concepts', schema=None) as batch_op:
        batch_op.add_column(sa.Column('primary_source_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('marked_known_at', sa.DateTime(), nullable=True))
    with op.batch_alter_table('exploration_concepts', schema=None) as batch_op:
        batch_op.create_foreign_key(
            op.f('fk_exploration_concepts_primary_source_id_knowledge_sources'),
            'knowledge_sources', ['primary_source_id'], ['id'], ondelete='SET NULL',
        )
        batch_op.create_index(
            batch_op.f('ix_exploration_concepts_primary_source_id'), ['primary_source_id'],
            unique=False,
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('exploration_concepts', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_exploration_concepts_primary_source_id'))
        batch_op.drop_constraint(
            op.f('fk_exploration_concepts_primary_source_id_knowledge_sources'),
            type_='foreignkey',
        )
    with op.batch_alter_table('exploration_concepts', schema=None) as batch_op:
        batch_op.drop_column('marked_known_at')
        batch_op.drop_column('primary_source_id')
