"""Add citations: source_reference_extractions and source_references.

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-22 22:00:00.000000

New tables only; no existing row is changed and nothing is backfilled (no source has had its
references extracted yet, which the app shows as "never run").

- source_reference_extractions: one row per knowledge source whose references were ever
  extracted (PK and FK source_id -> knowledge_sources.id ON DELETE CASCADE). The latest run:
  status (running/succeeded/failed), run_number, trigger, extractor, model, started_at
  (CURRENT_TIMESTAMP server default), finished_at, error, input/output tokens. The stored
  references: succeeded_at, found_in (reference_list/inline_citations/none), reference_count
  (NULL together until a run succeeded). CHECKs: _status, _error (set exactly when failed),
  _finished (NULL exactly while running), _succeeded, _stored, _found_in, _reference_count,
  _none_found (nothing found means no references) and _run_number.
- source_references: one entry of a source's reference list (FK source_id ->
  knowledge_sources.id ON DELETE CASCADE; UNIQUE (source_id, position), which also serves
  lookups by source): raw_text, title, authors (JSON array, '[]' server default), year, venue,
  doi, arxiv_id, url, and the library source it matched (FK matched_source_id ->
  knowledge_sources.id ON DELETE SET NULL, indexed) with match_method (doi/arxiv/title).
  CHECKs _position (>= 1), _authors_json and _match_method. CURRENT_TIMESTAMP on created_at.

The check texts are frozen here as written; while 0015 is the head they match
`teacher.db.models`. Downgrade drops both tables (the extracted references are lost).
The json_valid() CHECK (`SQLITE_ONLY`) is not created on PostgreSQL, where the json type
refuses invalid JSON.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0015'
down_revision: Union[str, Sequence[str], None] = '0014'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TIMESTAMP = sa.text('(CURRENT_TIMESTAMP)')
CHECKS = {
    'source_reference_extractions': (
        ('status', "status IN ('running', 'succeeded', 'failed')"),
        ('error', "(status = 'failed') = (error IS NOT NULL)"),
        ('finished', "(status = 'running') = (finished_at IS NULL)"),
        ('succeeded', "status != 'succeeded' OR succeeded_at IS NOT NULL"),
        (
            'stored',
            '(succeeded_at IS NULL) = (found_in IS NULL) '
            'AND (succeeded_at IS NULL) = (reference_count IS NULL)',
        ),
        ('found_in', "found_in IN ('reference_list', 'inline_citations', 'none')"),
        ('reference_count', 'reference_count >= 0'),
        ('none_found', "found_in != 'none' OR reference_count = 0"),
        ('run_number', 'run_number >= 1'),
    ),
    'source_references': (
        ('position', 'position >= 1'),
        ('authors_json', 'json_valid(authors)'),
        ('match_method', "match_method IN ('doi', 'arxiv', 'title')"),
    ),
}


# Checks that exist only on SQLite (on PostgreSQL the json type refuses invalid JSON).
SQLITE_ONLY = {'authors_json'}


def _checks(table: str) -> list[sa.CheckConstraint]:
    checks = []
    for name, sqltext in CHECKS[table]:
        check = sa.CheckConstraint(sqltext, name=op.f(f'ck_{table}_{name}'))
        checks.append(check.ddl_if(dialect='sqlite') if name in SQLITE_ONLY else check)
    return checks


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('source_reference_extractions',
    sa.Column('source_id', sa.Integer(), nullable=False),
    sa.Column('status', sa.Text(), nullable=False),
    sa.Column('run_number', sa.Integer(), nullable=False),
    sa.Column('trigger', sa.Text(), nullable=False),
    sa.Column('extractor', sa.Text(), nullable=False),
    sa.Column('model', sa.Text(), nullable=False),
    sa.Column('started_at', sa.DateTime(), server_default=TIMESTAMP, nullable=False),
    sa.Column('finished_at', sa.DateTime(), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('input_tokens', sa.Integer(), nullable=True),
    sa.Column('output_tokens', sa.Integer(), nullable=True),
    sa.Column('succeeded_at', sa.DateTime(), nullable=True),
    sa.Column('found_in', sa.Text(), nullable=True),
    sa.Column('reference_count', sa.Integer(), nullable=True),
    *_checks('source_reference_extractions'),
    sa.ForeignKeyConstraint(['source_id'], ['knowledge_sources.id'], name=op.f('fk_source_reference_extractions_source_id_knowledge_sources'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('source_id', name=op.f('pk_source_reference_extractions'))
    )

    op.create_table('source_references',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('source_id', sa.Integer(), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.Column('raw_text', sa.Text(), nullable=False),
    sa.Column('title', sa.Text(), nullable=True),
    sa.Column('authors', sa.JSON(), server_default='[]', nullable=False),
    sa.Column('year', sa.Integer(), nullable=True),
    sa.Column('venue', sa.Text(), nullable=True),
    sa.Column('doi', sa.Text(), nullable=True),
    sa.Column('arxiv_id', sa.Text(), nullable=True),
    sa.Column('url', sa.Text(), nullable=True),
    sa.Column('matched_source_id', sa.Integer(), nullable=True),
    sa.Column('match_method', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(), server_default=TIMESTAMP, nullable=False),
    *_checks('source_references'),
    sa.ForeignKeyConstraint(['source_id'], ['knowledge_sources.id'], name=op.f('fk_source_references_source_id_knowledge_sources'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['matched_source_id'], ['knowledge_sources.id'], name=op.f('fk_source_references_matched_source_id_knowledge_sources'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_source_references')),
    sa.UniqueConstraint('source_id', 'position', name=op.f('uq_source_references_source_id_position'))
    )
    with op.batch_alter_table('source_references', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_source_references_matched_source_id'), ['matched_source_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('source_references', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_source_references_matched_source_id'))

    op.drop_table('source_references')
    op.drop_table('source_reference_extractions')
