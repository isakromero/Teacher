"""Add the review queue: review_variants, review_items and review_attempts.

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-22 20:00:00.000000

No existing row is changed and nothing is backfilled here: the review items of questions
answered before this revision are derived from their answers the first time the queue is read
(`teacher.managers.review.ReviewManager.sync`), so no one-off step is needed.

- review_variants: fresh forms of a question (FK block_id -> artifact_blocks.id ON DELETE
  CASCADE; FK run_id -> generation_runs.id ON DELETE SET NULL, indexed); UNIQUE (block_id,
  position); CHECKs ck_review_variants_type (multiple_choice/blurb), _content_json, _position.
- review_items: one per cleared question (FK block_id -> artifact_blocks.id ON DELETE CASCADE,
  UNIQUE; FK presented_variant_id -> review_variants.id ON DELETE SET NULL, indexed; due_at
  indexed); CHECKs on the interval, the review count, the last verdict and confidence, the
  presented form, the presented order's JSON and the presentation columns being set together.
- review_attempts: one per review answer (FK item_id -> review_items.id ON DELETE CASCADE,
  indexed; FK variant_id -> review_variants.id ON DELETE SET NULL, indexed); CHECKs on the
  form and variant position, the JSON columns, the verdict and its missing criteria (as on
  block_responses), the confidence (required) and the intervals.
CURRENT_TIMESTAMP server defaults on every created_at; '[]' on review_attempts.missing; 0 on
review_items.reviews. The check texts are frozen here as written; while 0013 is the head they
match `teacher.db.models`. Downgrade drops the three tables (the review history is lost).

On PostgreSQL `POSTGRES_CHECKS` replaces the SQLite-only texts: no json_valid() CHECK (the json
type refuses invalid JSON) and json_typeof() for SQLite's json_type().
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0013'
down_revision: Union[str, Sequence[str], None] = '0012'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TIMESTAMP = sa.text('(CURRENT_TIMESTAMP)')
CHECKS = {
    'review_variants': (
        ('type', "type IN ('multiple_choice', 'blurb')"),
        ('content_json', 'json_valid(content)'),
        ('position', 'position >= 1'),
    ),
    'review_items': (
        ('interval_days', 'interval_days > 0'),
        ('reviews', 'reviews >= 0'),
        ('last_verdict', "last_verdict IN ('passed', 'partial', 'failed')"),
        ('last_confidence', "last_confidence IN ('low', 'medium', 'high')"),
        ('presented_form', "presented_form IN ('original', 'variant')"),
        ('presented_order_json', 'json_valid(presented_order)'),
        (
            'presentation',
            '(presentation IS NULL) = (presented_form IS NULL) '
            'AND (presentation IS NULL) = (presented_at IS NULL)',
        ),
    ),
    'review_attempts': (
        ('form', "form IN ('original', 'variant')"),
        ('variant', "(form = 'variant') = (variant_position IS NOT NULL)"),
        ('asked_json', 'json_valid(asked)'),
        ('option_order_json', 'json_valid(option_order)'),
        ('response_json', 'json_valid(response)'),
        ('details_json', 'json_valid(details)'),
        ('verdict', "verdict IN ('passed', 'partial', 'failed')"),
        ('missing_json', 'json_valid(missing)'),
        ('missing_array', "json_type(missing) = 'array'"),
        ('missing_partial', "(verdict = 'partial') = (json_array_length(missing) > 0)"),
        ('confidence', "confidence IN ('low', 'medium', 'high')"),
        ('intervals', 'interval_before > 0 AND interval_after > 0'),
    ),
}


# The PostgreSQL text of each check whose SQLite text does not run there (None: no such check).
POSTGRES_CHECKS = {
    'content_json': None,
    'presented_order_json': None,
    'asked_json': None,
    'option_order_json': None,
    'response_json': None,
    'details_json': None,
    'missing_json': None,
    'missing_array': "json_typeof(missing) = 'array'",
}


def _checks(table: str) -> list[sa.CheckConstraint]:
    """The table's checks; one whose text is SQLite-only is created only there, and its
    PostgreSQL form (if any) only on PostgreSQL."""
    checks = []
    for name, sqltext in CHECKS[table]:
        check = sa.CheckConstraint(sqltext, name=op.f(f'ck_{table}_{name}'))
        if name not in POSTGRES_CHECKS:
            checks.append(check)
            continue
        checks.append(check.ddl_if(dialect='sqlite'))
        if POSTGRES_CHECKS[name] is not None:
            checks.append(
                sa.CheckConstraint(POSTGRES_CHECKS[name], name=op.f(f'ck_{table}_{name}'))
                .ddl_if(dialect='postgresql')
            )
    return checks


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('review_variants',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('block_id', sa.Integer(), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.Column('type', sa.Text(), nullable=False),
    sa.Column('content', sa.JSON(), nullable=False),
    sa.Column('model', sa.Text(), nullable=False),
    sa.Column('run_id', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(), server_default=TIMESTAMP, nullable=False),
    *_checks('review_variants'),
    sa.ForeignKeyConstraint(['block_id'], ['artifact_blocks.id'], name=op.f('fk_review_variants_block_id_artifact_blocks'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['run_id'], ['generation_runs.id'], name=op.f('fk_review_variants_run_id_generation_runs'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_review_variants')),
    sa.UniqueConstraint('block_id', 'position', name=op.f('uq_review_variants_block_id_position'))
    )
    with op.batch_alter_table('review_variants', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_review_variants_run_id'), ['run_id'], unique=False)

    op.create_table('review_items',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('block_id', sa.Integer(), nullable=False),
    sa.Column('cleared_at', sa.DateTime(), nullable=False),
    sa.Column('first_clean', sa.Boolean(), nullable=False),
    sa.Column('interval_days', sa.Float(), nullable=False),
    sa.Column('due_at', sa.DateTime(), nullable=False),
    sa.Column('reviews', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('last_verdict', sa.Text(), nullable=False),
    sa.Column('last_confidence', sa.Text(), nullable=True),
    sa.Column('last_answered_at', sa.DateTime(), nullable=False),
    sa.Column('presentation', sa.Text(), nullable=True),
    sa.Column('presented_form', sa.Text(), nullable=True),
    sa.Column('presented_variant_id', sa.Integer(), nullable=True),
    sa.Column('presented_order', sa.JSON(none_as_null=True), nullable=True),
    sa.Column('presented_at', sa.DateTime(), nullable=True),
    sa.Column('created_at', sa.DateTime(), server_default=TIMESTAMP, nullable=False),
    *_checks('review_items'),
    sa.ForeignKeyConstraint(['block_id'], ['artifact_blocks.id'], name=op.f('fk_review_items_block_id_artifact_blocks'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['presented_variant_id'], ['review_variants.id'], name=op.f('fk_review_items_presented_variant_id_review_variants'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_review_items')),
    sa.UniqueConstraint('block_id', name=op.f('uq_review_items_block_id'))
    )
    with op.batch_alter_table('review_items', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_review_items_due_at'), ['due_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_review_items_presented_variant_id'), ['presented_variant_id'], unique=False)

    op.create_table('review_attempts',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('item_id', sa.Integer(), nullable=False),
    sa.Column('form', sa.Text(), nullable=False),
    sa.Column('variant_id', sa.Integer(), nullable=True),
    sa.Column('variant_position', sa.Integer(), nullable=True),
    sa.Column('asked', sa.JSON(), nullable=False),
    sa.Column('option_order', sa.JSON(none_as_null=True), nullable=True),
    sa.Column('response', sa.JSON(), nullable=False),
    sa.Column('verdict', sa.Text(), nullable=False),
    sa.Column('missing', sa.JSON(), server_default='[]', nullable=False),
    sa.Column('confidence', sa.Text(), nullable=False),
    sa.Column('feedback', sa.Text(), nullable=True),
    sa.Column('grader', sa.Text(), nullable=False),
    sa.Column('details', sa.JSON(none_as_null=True), nullable=True),
    sa.Column('interval_before', sa.Float(), nullable=False),
    sa.Column('interval_after', sa.Float(), nullable=False),
    sa.Column('due_before', sa.DateTime(), nullable=False),
    sa.Column('due_after', sa.DateTime(), nullable=False),
    sa.Column('created_at', sa.DateTime(), server_default=TIMESTAMP, nullable=False),
    *_checks('review_attempts'),
    sa.ForeignKeyConstraint(['item_id'], ['review_items.id'], name=op.f('fk_review_attempts_item_id_review_items'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['variant_id'], ['review_variants.id'], name=op.f('fk_review_attempts_variant_id_review_variants'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_review_attempts'))
    )
    with op.batch_alter_table('review_attempts', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_review_attempts_item_id'), ['item_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_review_attempts_variant_id'), ['variant_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('review_attempts', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_review_attempts_variant_id'))
        batch_op.drop_index(batch_op.f('ix_review_attempts_item_id'))
    op.drop_table('review_attempts')
    with op.batch_alter_table('review_items', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_review_items_presented_variant_id'))
        batch_op.drop_index(batch_op.f('ix_review_items_due_at'))
    op.drop_table('review_items')
    with op.batch_alter_table('review_variants', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_review_variants_run_id'))
    op.drop_table('review_variants')
