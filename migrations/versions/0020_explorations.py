"""Add explorations (a topic outlined into concepts, matched against the graph, with candidate
sources for review) and record source discovery and explorations in the cost ledger.

Revision ID: 0020
Revises: 0019
Create Date: 2026-11-27 12:00:00.000000

Five new tables, and api_calls changed; no other table or row is changed, nothing is
backfilled. See `teacher.exploration` (the run) and `teacher.managers.explorations`.

- explorations: id, user_id (FK -> users.id ON DELETE CASCADE, NULL without accounts;
  indexed), prompt (CHECK _prompt: not empty), level (CHECK _level: intro, intermediate,
  advanced), status (server default 'running'; CHECK _status: running, completed, failed),
  stage (NULL or brief, outline, graph_match, discovery: CHECK _stage), progress_done and
  progress_total (server default 0; CHECK _progress: 0 <= done <= total), error (CHECK _error:
  set exactly when failed), brief and questions (JSON, nullable), steps, stages and notes (JSON
  arrays, '[]' server default), created_at and updated_at (CURRENT_TIMESTAMP server default),
  finished_at (CHECK _finished: NULL exactly while running).
- exploration_concepts: id, exploration_id (FK -> explorations.id ON DELETE CASCADE), key,
  position, step, name, drafted_name, aliases and prerequisites (JSON arrays, '[]'), summary,
  search_query, status (server default 'planned'; CHECK _status: planned, known, grounded,
  dropped), concept_id (FK -> concepts.id ON DELETE SET NULL; indexed), match_method (NULL or
  exact, judged, new), match_reason, match_detail (JSON, nullable), coverage (NULL or new,
  thin, covered), discovery_status (server default 'not_needed'; CHECK: not_needed, pending,
  done, failed), discovery_error (CHECK _discovery_error: set exactly when failed),
  discovery_log (JSON array, '[]'), created_at, updated_at. UNIQUE (exploration_id, key) and
  (exploration_id, position); CHECKs _key and _name (not empty), _position and _step (>= 1).
- exploration_searches: one provider call of a concept's search: id, exploration_id and
  outline_concept_id (FKs ON DELETE CASCADE; both indexed), attempt (CHECK: 1 or 2), query
  (not empty), query_detail (JSON), provider, outcome (CHECK: ok, failed), hits (server default
  0; CHECK >= 0), error (CHECK _error: set exactly when failed), warnings (JSON array, '[]'),
  seconds (CHECK >= 0), created_at.
- exploration_candidates: a candidate source, once per exploration: id, exploration_id (FK
  ON DELETE CASCADE), candidate_key (UNIQUE with exploration_id; not empty), url (not empty),
  canonical_url, landing_url, arxiv_id, doi, title, authors and found_by and library (JSON
  arrays, '[]'), year, summary, verdict, verdict_reason, readable (CHECK _readable: exactly
  when the verdict is 'readable'), probe (JSON), decision (server default 'proposed'; CHECK:
  proposed, approved, rejected, ingested), decided_at, knowledge_source_id (FK ->
  knowledge_sources.id ON DELETE SET NULL; indexed), created_at, updated_at.
- exploration_links: which concepts found a candidate: id, candidate_id (FK ->
  exploration_candidates.id ON DELETE CASCADE), outline_concept_id (FK ->
  exploration_concepts.id ON DELETE CASCADE), rank (CHECK >= 1; UNIQUE with the concept),
  attempt (CHECK: 1 or 2), triage (JSON, nullable), overall (CHECK _scored: NULL together with
  triage), fits (false server default). UNIQUE (candidate_id, outline_concept_id).
- api_calls: purpose's CHECK (_purpose) also allows 'source_discovery' and 'exploration'; new
  columns exploration_id (FK -> explorations.id ON DELETE SET NULL; indexed) and
  web_search_requests (server default 0; CHECK _web_search_requests: >= 0), the server-side
  web searches of a Claude call (billed per search, never priced: no rate in the price table).
  Existing rows get 0 searches and no exploration. On SQLite the CHECK change rebuilds the table
  (batch mode keeps its indexes, foreign keys and other checks).

The JSON CHECKs (json_valid) exist on SQLite only (`SQLITE_ONLY`); on PostgreSQL the json type
refuses invalid JSON. On PostgreSQL the five new tables get row level security enabled, with no
policy, as 0017 to 0019 do for account-owned data. The check texts are frozen here as written;
while 0020 is the head they match `teacher.db.models`.

Downgrade drops the five tables (every exploration is lost) and the two api_calls columns, and
deletes the api_calls rows of the two new purposes before restoring the old purpose CHECK, which
refuses them (those calls' records are lost; their cost was spent).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0020'
down_revision: Union[str, Sequence[str], None] = '0019'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TIMESTAMP = sa.text('(CURRENT_TIMESTAMP)')
OLD_PURPOSES = (
    'generation', 'lab_generation', 'review_bank', 'glossary', 'tutor', 'grading',
    'block_tagging', 'concept_extraction', 'concept_relating', 'metadata',
    'reference_extraction', 'embedding',
)
NEW_PURPOSES = ('source_discovery', 'exploration')


def _in(column: str, values: Sequence[str]) -> str:
    return f'{column} IN (' + ', '.join(f"'{v}'" for v in values) + ')'


PURPOSE_CHECK = _in('purpose', OLD_PURPOSES + NEW_PURPOSES)
OLD_PURPOSE_CHECK = _in('purpose', OLD_PURPOSES)
WEB_SEARCHES_CHECK = 'web_search_requests >= 0'

CHECKS = {
    'explorations': (
        ('prompt', 'length(prompt) > 0'),
        ('level', _in('level', ('intro', 'intermediate', 'advanced'))),
        ('status', _in('status', ('running', 'completed', 'failed'))),
        ('stage', 'stage IS NULL OR ' + _in('stage', ('brief', 'outline', 'graph_match', 'discovery'))),
        ('progress', 'progress_done >= 0 AND progress_total >= progress_done'),
        ('error', "(status = 'failed') = (error IS NOT NULL)"),
        ('finished', "(status = 'running') = (finished_at IS NULL)"),
        ('brief_json', 'json_valid(brief)'),
        ('questions_json', 'json_valid(questions)'),
        ('steps_json', 'json_valid(steps)'),
        ('stages_json', 'json_valid(stages)'),
        ('notes_json', 'json_valid(notes)'),
    ),
    'exploration_concepts': (
        ('key', 'length(key) > 0'),
        ('position', 'position >= 1'),
        ('step', 'step >= 1'),
        ('name', 'length(name) > 0'),
        ('status', _in('status', ('planned', 'known', 'grounded', 'dropped'))),
        ('match_method', 'match_method IS NULL OR ' + _in('match_method', ('exact', 'judged', 'new'))),
        ('coverage', 'coverage IS NULL OR ' + _in('coverage', ('new', 'thin', 'covered'))),
        ('discovery_status', _in('discovery_status', ('not_needed', 'pending', 'done', 'failed'))),
        ('discovery_error', "(discovery_status = 'failed') = (discovery_error IS NOT NULL)"),
        ('aliases_json', 'json_valid(aliases)'),
        ('prerequisites_json', 'json_valid(prerequisites)'),
        ('match_detail_json', 'json_valid(match_detail)'),
        ('discovery_log_json', 'json_valid(discovery_log)'),
    ),
    'exploration_searches': (
        ('attempt', 'attempt IN (1, 2)'),
        ('query', 'length(query) > 0'),
        ('outcome', _in('outcome', ('ok', 'failed'))),
        ('error', "(outcome = 'failed') = (error IS NOT NULL)"),
        ('hits', 'hits >= 0'),
        ('seconds', 'seconds >= 0'),
        ('query_detail_json', 'json_valid(query_detail)'),
        ('warnings_json', 'json_valid(warnings)'),
    ),
    'exploration_candidates': (
        ('candidate_key', 'length(candidate_key) > 0'),
        ('url', 'length(url) > 0'),
        ('readable', "readable = (verdict = 'readable')"),
        ('decision', _in('decision', ('proposed', 'approved', 'rejected', 'ingested'))),
        ('authors_json', 'json_valid(authors)'),
        ('found_by_json', 'json_valid(found_by)'),
        ('probe_json', 'json_valid(probe)'),
        ('library_json', 'json_valid(library)'),
    ),
    'exploration_links': (
        ('rank', 'rank >= 1'),
        ('attempt', 'attempt IN (1, 2)'),
        ('scored', '(triage IS NULL) = (overall IS NULL)'),
        ('triage_json', 'json_valid(triage)'),
    ),
}
NEW_TABLES = tuple(CHECKS)


def _checks(table: str) -> list[sa.CheckConstraint]:
    checks = []
    for name, sqltext in CHECKS[table]:
        check = sa.CheckConstraint(sqltext, name=op.f(f'ck_{table}_{name}'))
        checks.append(check.ddl_if(dialect='sqlite') if name.endswith('_json') else check)
    return checks


def _fk(table: str, column: str, target: str, ondelete: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        [column], [f'{target}.id'], name=op.f(f'fk_{table}_{column}_{target}'), ondelete=ondelete
    )


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('explorations',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=True),
    sa.Column('prompt', sa.Text(), nullable=False),
    sa.Column('level', sa.Text(), nullable=False),
    sa.Column('status', sa.Text(), server_default='running', nullable=False),
    sa.Column('stage', sa.Text(), nullable=True),
    sa.Column('progress_done', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('progress_total', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('brief', sa.JSON(none_as_null=True), nullable=True),
    sa.Column('questions', sa.JSON(none_as_null=True), nullable=True),
    sa.Column('steps', sa.JSON(), server_default='[]', nullable=False),
    sa.Column('stages', sa.JSON(), server_default='[]', nullable=False),
    sa.Column('notes', sa.JSON(), server_default='[]', nullable=False),
    sa.Column('created_at', sa.DateTime(), server_default=TIMESTAMP, nullable=False),
    sa.Column('updated_at', sa.DateTime(), server_default=TIMESTAMP, nullable=False),
    sa.Column('finished_at', sa.DateTime(), nullable=True),
    *_checks('explorations'),
    _fk('explorations', 'user_id', 'users', 'CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_explorations'))
    )
    with op.batch_alter_table('explorations', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_explorations_user_id'), ['user_id'], unique=False)

    op.create_table('exploration_concepts',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('exploration_id', sa.Integer(), nullable=False),
    sa.Column('key', sa.Text(), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.Column('step', sa.Integer(), nullable=False),
    sa.Column('name', sa.Text(), nullable=False),
    sa.Column('drafted_name', sa.Text(), nullable=False),
    sa.Column('aliases', sa.JSON(), server_default='[]', nullable=False),
    sa.Column('summary', sa.Text(), nullable=False),
    sa.Column('prerequisites', sa.JSON(), server_default='[]', nullable=False),
    sa.Column('search_query', sa.Text(), nullable=False),
    sa.Column('status', sa.Text(), server_default='planned', nullable=False),
    sa.Column('concept_id', sa.Integer(), nullable=True),
    sa.Column('match_method', sa.Text(), nullable=True),
    sa.Column('match_reason', sa.Text(), nullable=True),
    sa.Column('match_detail', sa.JSON(none_as_null=True), nullable=True),
    sa.Column('coverage', sa.Text(), nullable=True),
    sa.Column('discovery_status', sa.Text(), server_default='not_needed', nullable=False),
    sa.Column('discovery_error', sa.Text(), nullable=True),
    sa.Column('discovery_log', sa.JSON(), server_default='[]', nullable=False),
    sa.Column('created_at', sa.DateTime(), server_default=TIMESTAMP, nullable=False),
    sa.Column('updated_at', sa.DateTime(), server_default=TIMESTAMP, nullable=False),
    *_checks('exploration_concepts'),
    _fk('exploration_concepts', 'exploration_id', 'explorations', 'CASCADE'),
    _fk('exploration_concepts', 'concept_id', 'concepts', 'SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_exploration_concepts')),
    sa.UniqueConstraint('exploration_id', 'key', name=op.f('uq_exploration_concepts_exploration_id_key')),
    sa.UniqueConstraint('exploration_id', 'position', name=op.f('uq_exploration_concepts_exploration_id_position'))
    )
    with op.batch_alter_table('exploration_concepts', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_exploration_concepts_concept_id'), ['concept_id'], unique=False)

    op.create_table('exploration_searches',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('exploration_id', sa.Integer(), nullable=False),
    sa.Column('outline_concept_id', sa.Integer(), nullable=False),
    sa.Column('attempt', sa.Integer(), nullable=False),
    sa.Column('query', sa.Text(), nullable=False),
    sa.Column('query_detail', sa.JSON(), nullable=False),
    sa.Column('provider', sa.Text(), nullable=False),
    sa.Column('outcome', sa.Text(), nullable=False),
    sa.Column('hits', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('warnings', sa.JSON(), server_default='[]', nullable=False),
    sa.Column('seconds', sa.Float(), nullable=False),
    sa.Column('created_at', sa.DateTime(), server_default=TIMESTAMP, nullable=False),
    *_checks('exploration_searches'),
    _fk('exploration_searches', 'exploration_id', 'explorations', 'CASCADE'),
    _fk('exploration_searches', 'outline_concept_id', 'exploration_concepts', 'CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_exploration_searches'))
    )
    with op.batch_alter_table('exploration_searches', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_exploration_searches_exploration_id'), ['exploration_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_exploration_searches_outline_concept_id'), ['outline_concept_id'], unique=False)

    op.create_table('exploration_candidates',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('exploration_id', sa.Integer(), nullable=False),
    sa.Column('candidate_key', sa.Text(), nullable=False),
    sa.Column('url', sa.Text(), nullable=False),
    sa.Column('canonical_url', sa.Text(), nullable=False),
    sa.Column('landing_url', sa.Text(), nullable=True),
    sa.Column('arxiv_id', sa.Text(), nullable=True),
    sa.Column('doi', sa.Text(), nullable=True),
    sa.Column('title', sa.Text(), nullable=False),
    sa.Column('authors', sa.JSON(), server_default='[]', nullable=False),
    sa.Column('year', sa.Integer(), nullable=True),
    sa.Column('summary', sa.Text(), nullable=True),
    sa.Column('found_by', sa.JSON(), server_default='[]', nullable=False),
    sa.Column('verdict', sa.Text(), nullable=False),
    sa.Column('verdict_reason', sa.Text(), nullable=False),
    sa.Column('readable', sa.Boolean(), nullable=False),
    sa.Column('probe', sa.JSON(), nullable=False),
    sa.Column('library', sa.JSON(), server_default='[]', nullable=False),
    sa.Column('decision', sa.Text(), server_default='proposed', nullable=False),
    sa.Column('decided_at', sa.DateTime(), nullable=True),
    sa.Column('knowledge_source_id', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(), server_default=TIMESTAMP, nullable=False),
    sa.Column('updated_at', sa.DateTime(), server_default=TIMESTAMP, nullable=False),
    *_checks('exploration_candidates'),
    _fk('exploration_candidates', 'exploration_id', 'explorations', 'CASCADE'),
    _fk('exploration_candidates', 'knowledge_source_id', 'knowledge_sources', 'SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_exploration_candidates')),
    sa.UniqueConstraint('exploration_id', 'candidate_key', name=op.f('uq_exploration_candidates_exploration_id_candidate_key'))
    )
    with op.batch_alter_table('exploration_candidates', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_exploration_candidates_knowledge_source_id'), ['knowledge_source_id'], unique=False)

    op.create_table('exploration_links',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('candidate_id', sa.Integer(), nullable=False),
    sa.Column('outline_concept_id', sa.Integer(), nullable=False),
    sa.Column('rank', sa.Integer(), nullable=False),
    sa.Column('attempt', sa.Integer(), nullable=False),
    sa.Column('triage', sa.JSON(none_as_null=True), nullable=True),
    sa.Column('overall', sa.Float(), nullable=True),
    sa.Column('fits', sa.Boolean(), server_default=sa.false(), nullable=False),
    *_checks('exploration_links'),
    _fk('exploration_links', 'candidate_id', 'exploration_candidates', 'CASCADE'),
    _fk('exploration_links', 'outline_concept_id', 'exploration_concepts', 'CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_exploration_links')),
    sa.UniqueConstraint('candidate_id', 'outline_concept_id', name=op.f('uq_exploration_links_candidate_id_outline_concept_id')),
    sa.UniqueConstraint('outline_concept_id', 'rank', name=op.f('uq_exploration_links_outline_concept_id_rank'))
    )

    with op.batch_alter_table('api_calls', schema=None) as batch_op:
        batch_op.add_column(sa.Column('exploration_id', sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column('web_search_requests', sa.Integer(), server_default=sa.text('0'), nullable=False)
        )
    with op.batch_alter_table('api_calls', schema=None) as batch_op:
        batch_op.drop_constraint(op.f('ck_api_calls_purpose'), type_='check')
        batch_op.create_check_constraint(op.f('ck_api_calls_purpose'), PURPOSE_CHECK)
        batch_op.create_check_constraint(
            op.f('ck_api_calls_web_search_requests'), WEB_SEARCHES_CHECK
        )
        batch_op.create_foreign_key(
            op.f('fk_api_calls_exploration_id_explorations'), 'explorations',
            ['exploration_id'], ['id'], ondelete='SET NULL',
        )
        batch_op.create_index(batch_op.f('ix_api_calls_exploration_id'), ['exploration_id'], unique=False)

    if op.get_bind().dialect.name == 'postgresql':
        for table in NEW_TABLES:
            op.execute(f'ALTER TABLE {table} ENABLE ROW LEVEL SECURITY')


def downgrade() -> None:
    """Downgrade schema."""
    op.execute(
        'DELETE FROM api_calls WHERE purpose IN ('
        + ', '.join(f"'{p}'" for p in NEW_PURPOSES) + ')'
    )
    with op.batch_alter_table('api_calls', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_api_calls_exploration_id'))
        batch_op.drop_constraint(op.f('fk_api_calls_exploration_id_explorations'), type_='foreignkey')
        batch_op.drop_constraint(op.f('ck_api_calls_web_search_requests'), type_='check')
        batch_op.drop_constraint(op.f('ck_api_calls_purpose'), type_='check')
        batch_op.create_check_constraint(op.f('ck_api_calls_purpose'), OLD_PURPOSE_CHECK)
    with op.batch_alter_table('api_calls', schema=None) as batch_op:
        batch_op.drop_column('web_search_requests')
        batch_op.drop_column('exploration_id')

    op.drop_table('exploration_links')
    with op.batch_alter_table('exploration_candidates', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_exploration_candidates_knowledge_source_id'))
    op.drop_table('exploration_candidates')
    with op.batch_alter_table('exploration_searches', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_exploration_searches_outline_concept_id'))
        batch_op.drop_index(batch_op.f('ix_exploration_searches_exploration_id'))
    op.drop_table('exploration_searches')
    with op.batch_alter_table('exploration_concepts', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_exploration_concepts_concept_id'))
    op.drop_table('exploration_concepts')
    with op.batch_alter_table('explorations', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_explorations_user_id'))
    op.drop_table('explorations')
