"""Add block_responses.details: the grader's private verdict as nullable JSON.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-19 16:00:00.000000

Existing rows get SQL NULL (no details). The column carries the same named json_valid() CHECK
as the other JSON columns (ck_block_responses_details_json); json_valid(NULL) is NULL, which a
CHECK accepts. On SQLite the constraint needs a table rebuild, which batch mode performs. On
PostgreSQL there is no json_valid() CHECK (here or on any JSON column): the json type itself
refuses invalid JSON.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0002'
down_revision: Union[str, Sequence[str], None] = '0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    sqlite = op.get_context().dialect.name == 'sqlite'
    with op.batch_alter_table('block_responses', schema=None) as batch_op:
        batch_op.add_column(sa.Column('details', sa.JSON(), nullable=True))
        if sqlite:
            batch_op.create_check_constraint(
                op.f('ck_block_responses_details_json'), 'json_valid(details)'
            )


def downgrade() -> None:
    """Downgrade schema."""
    sqlite = op.get_context().dialect.name == 'sqlite'
    with op.batch_alter_table('block_responses', schema=None) as batch_op:
        if sqlite:
            batch_op.drop_constraint(op.f('ck_block_responses_details_json'), type_='check')
        batch_op.drop_column('details')
