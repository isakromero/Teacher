"""Add the study-page glossary: glossary_terms and artifacts.glossary_built_at.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-19 21:00:00.000000

Existing rows are untouched.
- artifacts.glossary_built_at: nullable DATETIME, NULL for every existing artifact (no glossary
  has been built for it yet). Added with a plain ALTER TABLE ADD COLUMN (no table rebuild).
- glossary_terms: FK artifacts.id ON DELETE CASCADE; UNIQUE (artifact_id, term) and UNIQUE
  (artifact_id, position); CHECK ck_glossary_terms_kind (acronym/term/symbol) and
  ck_glossary_terms_aliases_json (json_valid); server default '[]' on aliases.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0004'
down_revision: Union[str, Sequence[str], None] = '0003'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('artifacts', schema=None) as batch_op:
        batch_op.add_column(sa.Column('glossary_built_at', sa.DateTime(), nullable=True))

    op.create_table('glossary_terms',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('artifact_id', sa.Integer(), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.Column('term', sa.Text(), nullable=False),
    sa.Column('kind', sa.Text(), nullable=False),
    sa.Column('expansion', sa.Text(), nullable=True),
    sa.Column('definition', sa.Text(), nullable=False),
    sa.Column('aliases', sa.JSON(), server_default='[]', nullable=False),
    sa.CheckConstraint("kind IN ('acronym', 'term', 'symbol')", name=op.f('ck_glossary_terms_kind')),
    sa.CheckConstraint('json_valid(aliases)', name=op.f('ck_glossary_terms_aliases_json')).ddl_if(dialect='sqlite'),
    sa.ForeignKeyConstraint(['artifact_id'], ['artifacts.id'], name=op.f('fk_glossary_terms_artifact_id_artifacts'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_glossary_terms')),
    sa.UniqueConstraint('artifact_id', 'position', name=op.f('uq_glossary_terms_artifact_id_position')),
    sa.UniqueConstraint('artifact_id', 'term', name=op.f('uq_glossary_terms_artifact_id_term'))
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('glossary_terms')
    with op.batch_alter_table('artifacts', schema=None) as batch_op:
        batch_op.drop_column('glossary_built_at')
