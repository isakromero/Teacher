"""Add labs: lab_workspaces and lab_runs.

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-22 12:00:00.000000

New tables only; existing rows are untouched. A lab is an `artifacts` row with kind 'lab' (its
spec lives in `lab.json` in the artifact's folder), so no artifact column changes.
- lab_workspaces: the reader's current files of one lab. artifact_id is the primary key and an
  FK -> artifacts.id ON DELETE CASCADE; a json_valid() CHECK on files; solution_revealed_at is
  NULL until the reader asked for the reference solution; CURRENT_TIMESTAMP server default on
  updated_at.
- lab_runs: every run and check the reader made in the browser. FK artifact_id -> artifacts.id
  ON DELETE CASCADE (indexed); CHECK kind IN ('run', 'check'); CHECK a check names its step
  (step_index NOT NULL) and step_index is never negative; a json_valid() CHECK on files;
  CURRENT_TIMESTAMP server default on created_at.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0009'
down_revision: Union[str, Sequence[str], None] = '0008'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('lab_workspaces',
    sa.Column('artifact_id', sa.Integer(), nullable=False),
    sa.Column('files', sa.JSON(), nullable=False),
    sa.Column('solution_revealed_at', sa.DateTime(), nullable=True),
    sa.Column('updated_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.CheckConstraint('json_valid(files)', name=op.f('ck_lab_workspaces_files_json')),
    sa.ForeignKeyConstraint(['artifact_id'], ['artifacts.id'], name=op.f('fk_lab_workspaces_artifact_id_artifacts'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('artifact_id', name=op.f('pk_lab_workspaces'))
    )
    op.create_table('lab_runs',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('artifact_id', sa.Integer(), nullable=False),
    sa.Column('kind', sa.Text(), nullable=False),
    sa.Column('step_index', sa.Integer(), nullable=True),
    sa.Column('passed', sa.Boolean(), nullable=False),
    sa.Column('files', sa.JSON(), nullable=False),
    sa.Column('output', sa.Text(), nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.CheckConstraint("kind IN ('run', 'check')", name=op.f('ck_lab_runs_kind')),
    sa.CheckConstraint("kind != 'check' OR step_index IS NOT NULL", name=op.f('ck_lab_runs_check_step')),
    sa.CheckConstraint('step_index IS NULL OR step_index >= 0', name=op.f('ck_lab_runs_step_index')),
    sa.CheckConstraint('json_valid(files)', name=op.f('ck_lab_runs_files_json')),
    sa.ForeignKeyConstraint(['artifact_id'], ['artifacts.id'], name=op.f('fk_lab_runs_artifact_id_artifacts'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_lab_runs'))
    )
    with op.batch_alter_table('lab_runs', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_lab_runs_artifact_id'), ['artifact_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('lab_runs', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_lab_runs_artifact_id'))

    op.drop_table('lab_runs')
    op.drop_table('lab_workspaces')
