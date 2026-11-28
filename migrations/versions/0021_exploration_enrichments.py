"""Add enrichments of explorations: ingesting their approved sources, writing their outline to the
concept graph and checking its grounding.

Revision ID: 0021
Revises: 0020
Create Date: 2026-11-27 18:00:00.000000

One new table, and three tables changed; no existing row is changed, nothing is backfilled. See
`teacher.exploration.enrichment` (the run) and `teacher.managers.explorations` (its rows).

- exploration_enrichments: one enrichment run of an exploration: id, exploration_id (FK ->
  explorations.id ON DELETE CASCADE; indexed, and a partial UNIQUE index
  ix_exploration_enrichments_one_running over the rows with status 'running', so at most one
  enrichment of an exploration runs at a time), status (server default 'running'; CHECK
  _status: running, completed, failed), stage (NULL or ingest, graph, grounding: CHECK _stage),
  progress_done and progress_total (server default 0; CHECK _progress: 0 <= done <= total),
  error (CHECK _error: set exactly when failed), plan (JSON: what the reader confirmed), summary
  (JSON, CHECK _summary: set exactly when completed), stages (JSON array, '[]'), created_at and
  updated_at (CURRENT_TIMESTAMP server default), finished_at (CHECK _finished: NULL exactly
  while running).
- exploration_candidates: ingest_error (text; CHECK _ingest_error: never set on an ingested
  candidate) and ingest_detail (JSON, nullable): the last enrichment's attempt.
- exploration_concepts: graph_outcome (NULL or matched, found, created: CHECK _graph_outcome),
  graph_reason (text), grounding and prerequisite_links (JSON arrays, '[]' server default).
- api_calls: exploration_enrichment_id (FK -> exploration_enrichments.id ON DELETE SET NULL;
  indexed): the enrichment a call served (its exploration_id is set too). Existing rows get
  NULL. On SQLite adding the foreign key rebuilds the table (batch mode keeps its indexes,
  foreign keys and checks), as adding the checks rebuilds the two exploration tables.

The JSON CHECKs (json_valid) exist on SQLite only; on PostgreSQL the json type refuses invalid
JSON. On PostgreSQL the new table gets row level security enabled, with no policy, as 0017 to
0020 do for account-owned data. The check texts are frozen here as written; while 0021 is the
head they match `teacher.db.models`.

Downgrade drops the table (every enrichment's record and summary is lost) and the new columns
(each candidate's last ingest error and detail, each concept's graph outcome, grounding and
prerequisite links). What enrichments added to the library and the concept graph stays: those
are ordinary sources, concepts and relations, and candidates stay `ingested`.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0021'
down_revision: Union[str, Sequence[str], None] = '0020'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TIMESTAMP = sa.text('(CURRENT_TIMESTAMP)')
RUNNING = sa.text("status = 'running'")


def _in(column: str, values: Sequence[str]) -> str:
    return f'{column} IN (' + ', '.join(f"'{v}'" for v in values) + ')'


CHECKS = {
    'exploration_enrichments': (
        ('status', _in('status', ('running', 'completed', 'failed'))),
        ('stage', 'stage IS NULL OR ' + _in('stage', ('ingest', 'graph', 'grounding'))),
        ('progress', 'progress_done >= 0 AND progress_total >= progress_done'),
        ('error', "(status = 'failed') = (error IS NOT NULL)"),
        ('finished', "(status = 'running') = (finished_at IS NULL)"),
        ('summary', "(status = 'completed') = (summary IS NOT NULL)"),
        ('plan_json', 'json_valid(plan)'),
        ('summary_json', 'json_valid(summary)'),
        ('stages_json', 'json_valid(stages)'),
    ),
}
# The checks added to existing tables.
ADDED_CHECKS = {
    'exploration_candidates': (
        ('ingest_error', "ingest_error IS NULL OR decision != 'ingested'"),
        ('ingest_detail_json', 'json_valid(ingest_detail)'),
    ),
    'exploration_concepts': (
        ('graph_outcome', 'graph_outcome IS NULL OR ' + _in('graph_outcome', ('matched', 'found', 'created'))),
        ('grounding_json', 'json_valid(grounding)'),
        ('prerequisite_links_json', 'json_valid(prerequisite_links)'),
    ),
}
NEW_TABLES = tuple(CHECKS)


def _sqlite() -> bool:
    return op.get_bind().dialect.name == 'sqlite'


def _checks(table: str) -> list[sa.CheckConstraint]:
    checks = []
    for name, sqltext in CHECKS[table]:
        check = sa.CheckConstraint(sqltext, name=op.f(f'ck_{table}_{name}'))
        checks.append(check.ddl_if(dialect='sqlite') if name.endswith('_json') else check)
    return checks


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('exploration_enrichments',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('exploration_id', sa.Integer(), nullable=False),
    sa.Column('status', sa.Text(), server_default='running', nullable=False),
    sa.Column('stage', sa.Text(), nullable=True),
    sa.Column('progress_done', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('progress_total', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('plan', sa.JSON(), nullable=False),
    sa.Column('summary', sa.JSON(none_as_null=True), nullable=True),
    sa.Column('stages', sa.JSON(), server_default='[]', nullable=False),
    sa.Column('created_at', sa.DateTime(), server_default=TIMESTAMP, nullable=False),
    sa.Column('updated_at', sa.DateTime(), server_default=TIMESTAMP, nullable=False),
    sa.Column('finished_at', sa.DateTime(), nullable=True),
    *_checks('exploration_enrichments'),
    sa.ForeignKeyConstraint(
        ['exploration_id'], ['explorations.id'],
        name=op.f('fk_exploration_enrichments_exploration_id_explorations'), ondelete='CASCADE',
    ),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_exploration_enrichments'))
    )
    with op.batch_alter_table('exploration_enrichments', schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f('ix_exploration_enrichments_exploration_id'), ['exploration_id'], unique=False
        )
        batch_op.create_index(
            'ix_exploration_enrichments_one_running', ['exploration_id'], unique=True,
            sqlite_where=RUNNING, postgresql_where=RUNNING,
        )

    with op.batch_alter_table('exploration_candidates', schema=None) as batch_op:
        batch_op.add_column(sa.Column('ingest_error', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('ingest_detail', sa.JSON(none_as_null=True), nullable=True))
    with op.batch_alter_table('exploration_concepts', schema=None) as batch_op:
        batch_op.add_column(sa.Column('graph_outcome', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('graph_reason', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('grounding', sa.JSON(), server_default='[]', nullable=False))
        batch_op.add_column(
            sa.Column('prerequisite_links', sa.JSON(), server_default='[]', nullable=False)
        )
    for table, checks in ADDED_CHECKS.items():
        with op.batch_alter_table(table, schema=None) as batch_op:
            for name, sqltext in checks:
                if name.endswith('_json') and not _sqlite():
                    continue
                batch_op.create_check_constraint(op.f(f'ck_{table}_{name}'), sqltext)

    with op.batch_alter_table('api_calls', schema=None) as batch_op:
        batch_op.add_column(sa.Column('exploration_enrichment_id', sa.Integer(), nullable=True))
    with op.batch_alter_table('api_calls', schema=None) as batch_op:
        batch_op.create_foreign_key(
            op.f('fk_api_calls_exploration_enrichment_id_exploration_enrichments'),
            'exploration_enrichments', ['exploration_enrichment_id'], ['id'], ondelete='SET NULL',
        )
        batch_op.create_index(
            batch_op.f('ix_api_calls_exploration_enrichment_id'), ['exploration_enrichment_id'],
            unique=False,
        )

    if not _sqlite():
        for table in NEW_TABLES:
            op.execute(f'ALTER TABLE {table} ENABLE ROW LEVEL SECURITY')


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('api_calls', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_api_calls_exploration_enrichment_id'))
        batch_op.drop_constraint(
            op.f('fk_api_calls_exploration_enrichment_id_exploration_enrichments'),
            type_='foreignkey',
        )
    with op.batch_alter_table('api_calls', schema=None) as batch_op:
        batch_op.drop_column('exploration_enrichment_id')

    for table, checks in ADDED_CHECKS.items():
        with op.batch_alter_table(table, schema=None) as batch_op:
            for name, _sqltext in checks:
                if name.endswith('_json') and not _sqlite():
                    continue
                batch_op.drop_constraint(op.f(f'ck_{table}_{name}'), type_='check')
    with op.batch_alter_table('exploration_concepts', schema=None) as batch_op:
        batch_op.drop_column('prerequisite_links')
        batch_op.drop_column('grounding')
        batch_op.drop_column('graph_reason')
        batch_op.drop_column('graph_outcome')
    with op.batch_alter_table('exploration_candidates', schema=None) as batch_op:
        batch_op.drop_column('ingest_detail')
        batch_op.drop_column('ingest_error')

    with op.batch_alter_table('exploration_enrichments', schema=None) as batch_op:
        batch_op.drop_index('ix_exploration_enrichments_one_running')
        batch_op.drop_index(batch_op.f('ix_exploration_enrichments_exploration_id'))
    op.drop_table('exploration_enrichments')
