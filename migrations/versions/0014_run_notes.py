"""Add a note to generation runs: generation_runs.note.

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-22 21:00:00.000000

- note: nullable TEXT, something the owner should see with a stage that succeeded without
  failing the generation (the question check's closed-book flags that are still there after
  the last revision, `teacher.page_revision`). NULL for every existing run.

A plain nullable column: SQLite adds it in place, without a table rebuild. Downgrade drops it
(the notes are lost; the runs are kept).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0014'
down_revision: Union[str, Sequence[str], None] = '0013'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('generation_runs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('note', sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('generation_runs', schema=None) as batch_op:
        batch_op.drop_column('note')
