"""Add graded verdicts and confidence to answers: block_responses.verdict, .missing and
.confidence, and chat_block_responses.verdict and .missing.

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-22 18:00:00.000000

- verdict (both tables): nullable TEXT, "passed", "partial" (a written answer that meets some
  but not all required criteria) or "failed". CHECK ck_<table>_verdict (the three values) and
  ck_<table>_verdict_passed (it agrees with `passed`). Every existing row is filled in from
  `passed` ('passed' or 'failed': there were no partial verdicts before this revision); NULL is
  left only for rows written outside the managers later.
- missing (both tables): JSON array of the unmet required criteria a partial answer is shown,
  NOT NULL with server default '[]' (what every existing row gets). CHECKs
  ck_<table>_missing_json (json_valid), ck_<table>_missing_array (an array) and
  ck_<table>_missing_partial (non-empty exactly when the verdict is partial).
- block_responses.confidence: nullable TEXT, "low", "medium" or "high" (CHECK
  ck_block_responses_confidence); NULL for every existing row (nobody was asked).

The CHECKs need a table rebuild on SQLite, which batch mode performs (indexes and foreign keys
are kept). The check texts are frozen here as written; while 0012 is the head they match
`teacher.db.models`. Downgrade drops the columns and their checks; the answers are kept.

On PostgreSQL `POSTGRES_CHECKS` replaces the SQLite-only texts: no json_valid() CHECK (the json
type refuses invalid JSON) and json_typeof() for SQLite's json_type().
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0012'
down_revision: Union[str, Sequence[str], None] = '0011'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

VERDICT_CHECK = "verdict IN ('passed', 'partial', 'failed')"
VERDICT_PASSED_CHECK = "verdict IS NULL OR (verdict = 'passed') = passed"
MISSING_ARRAY_CHECK = "json_type(missing) = 'array'"
MISSING_PARTIAL_CHECK = "(verdict = 'partial') = (json_array_length(missing) > 0)"
CONFIDENCE_CHECK = "confidence IN ('low', 'medium', 'high')"
TABLES = ('block_responses', 'chat_block_responses')
CHECKS = (
    ('verdict', VERDICT_CHECK),
    ('verdict_passed', VERDICT_PASSED_CHECK),
    ('missing_json', 'json_valid(missing)'),
    ('missing_array', MISSING_ARRAY_CHECK),
    ('missing_partial', MISSING_PARTIAL_CHECK),
)
# The PostgreSQL text of each check whose SQLite text does not run there (None: no such check).
POSTGRES_CHECKS = {
    'missing_json': None,
    'missing_array': "json_typeof(missing) = 'array'",
}


def _dialect_checks() -> list[tuple[str, str]]:
    """CHECKS as this database spells them."""
    if op.get_context().dialect.name == 'sqlite':
        return list(CHECKS)
    checks = [(check, POSTGRES_CHECKS.get(check, sqltext)) for check, sqltext in CHECKS]
    return [(check, sqltext) for check, sqltext in checks if sqltext is not None]


def upgrade() -> None:
    """Upgrade schema."""
    for name in TABLES:
        with op.batch_alter_table(name, schema=None) as batch_op:
            batch_op.add_column(sa.Column('verdict', sa.Text(), nullable=True))
            batch_op.add_column(
                sa.Column('missing', sa.JSON(), server_default='[]', nullable=False)
            )
            if name == 'block_responses':
                batch_op.add_column(sa.Column('confidence', sa.Text(), nullable=True))
        op.execute(
            f"UPDATE {name} SET verdict = CASE WHEN passed THEN 'passed' ELSE 'failed' END"
        )
        with op.batch_alter_table(name, schema=None) as batch_op:
            for check, sqltext in _dialect_checks():
                batch_op.create_check_constraint(op.f(f'ck_{name}_{check}'), sqltext)
            if name == 'block_responses':
                batch_op.create_check_constraint(
                    op.f('ck_block_responses_confidence'), CONFIDENCE_CHECK
                )


def downgrade() -> None:
    """Downgrade schema."""
    for name in TABLES:
        with op.batch_alter_table(name, schema=None) as batch_op:
            if name == 'block_responses':
                batch_op.drop_constraint(op.f('ck_block_responses_confidence'), type_='check')
            for check, _sqltext in reversed(_dialect_checks()):
                batch_op.drop_constraint(op.f(f'ck_{name}_{check}'), type_='check')
        with op.batch_alter_table(name, schema=None) as batch_op:
            if name == 'block_responses':
                batch_op.drop_column('confidence')
            batch_op.drop_column('missing')
            batch_op.drop_column('verdict')
