"""Add the concept graph: concepts, aliases, relations, embeddings and the two search indexes.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-20 21:00:00.000000

New tables only; existing rows are untouched.
- concepts: UNIQUE slug, indexed name_norm, CHECK ck_concepts_origin (paper/model/user),
  nullable reviewed_at, CURRENT_TIMESTAMP server defaults on created_at/updated_at. The trigger
  trg_concepts_slug_immutable refuses any UPDATE that changes a slug. (A future batch rebuild of
  `concepts` drops triggers, so such a migration must re-create it.)
- concept_aliases: FK concepts.id ON DELETE CASCADE; UNIQUE (concept_id, alias_norm); indexed
  alias_norm (aliases are not unique across concepts).
- concept_relations: FKs from_id/to_id -> concepts.id ON DELETE CASCADE (both indexed);
  low_id/high_id = min/max of the two ids (CHECK ck_concept_relations_pair) with UNIQUE
  (low_id, high_id): at most one relation per unordered pair, whatever the type. CHECKs
  ck_concept_relations_type (builds_on/different_version_of/relates_to), _origin, _not_self
  (from_id != to_id) and _symmetric_order (symmetric types stored with from_id < to_id).
- concept_embeddings: PK/FK concept_id -> concepts.id ON DELETE CASCADE; CHECKs
  ck_concept_embeddings_dimensions (> 0) and _vector_size (length(vector) = 4 * dimensions).
- concept_search: the FTS5 word index (rowid = concepts.id; name, aliases, summary; tokenizer
  unicode61 remove_diacritics 2; prefix indexes for 2, 3 and 4 characters).
- concept_trigram: the FTS5 trigram index over names and aliases, one row per name or alias
  (rowid = concept_id * 65536 + slot; text, compact, label UNINDEXED; tokenizer trigram
  case_sensitive 1, which needs SQLite >= 3.34).
  Both are created with raw SQL (see teacher.db.fts) and kept in sync by the concept manager.
  They are created first, so a SQLite without FTS5 or the trigram tokenizer fails before
  anything else changes. Existing databases have no concepts, so both start empty.

On PostgreSQL: the search indexes are plain tables (see teacher.db.fts and
teacher.db.search.postgres), created first as on SQLite:
- the pg_trgm extension, unless installed already: in the `extensions` schema when there is one
  (Supabase), else in the current schema (`teacher.db.fts.require_pg_trgm`; it needs CREATE
  privilege on the database, which Supabase's postgres role has);
- concept_search (rowid bigint PK, name, aliases, summary);
- concept_search_tokens (rowid, col, pos, token COLLATE "C"; PK (rowid, col, pos); CHECK col in
  0..2) with the b-tree ix_concept_search_tokens_key on substr(token, 1, 200);
- concept_trigram (rowid bigint PK, text, compact, label) with the GIN indexes
  ix_concept_trigram_text and ix_concept_trigram_compact (gin_trgm_ops, schema-qualified).
The downgrade drops the three tables and leaves pg_trgm installed (it may predate this
migration or serve others). The slug trigger is a PL/pgSQL function
`trg_concepts_slug_immutable()` with a trigger of the same name and the same message; the
ck_concept_relations_pair CHECK uses LEAST/GREATEST (SQLite's two-argument min/max).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from teacher.db.fts import require_concept_search, require_pg_trgm


# revision identifiers, used by Alembic.
revision: str = '0005'
down_revision: Union[str, Sequence[str], None] = '0004'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    sqlite = op.get_context().dialect.name == 'sqlite'
    if sqlite:
        require_concept_search(op.get_bind())
        op.execute(
            "CREATE VIRTUAL TABLE concept_search USING fts5("
            "name, aliases, summary, tokenize = 'unicode61 remove_diacritics 2', prefix = '2 3 4')"
        )
        op.execute(
            "CREATE VIRTUAL TABLE concept_trigram USING fts5("
            "text, compact, label UNINDEXED, tokenize = 'trigram case_sensitive 1')"
        )
    else:
        trgm = op.get_bind().dialect.identifier_preparer.quote(require_pg_trgm(op.get_bind()))
        op.execute(
            "CREATE TABLE concept_search (rowid bigint NOT NULL, name text NOT NULL, "
            "aliases text NOT NULL, summary text NOT NULL, "
            "CONSTRAINT pk_concept_search PRIMARY KEY (rowid))"
        )
        op.execute(
            "CREATE TABLE concept_search_tokens (rowid bigint NOT NULL, col smallint NOT NULL, "
            'pos integer NOT NULL, token text COLLATE "C" NOT NULL, '
            "CONSTRAINT pk_concept_search_tokens PRIMARY KEY (rowid, col, pos), "
            "CONSTRAINT ck_concept_search_tokens_col CHECK (col IN (0, 1, 2)))"
        )
        op.execute(
            "CREATE INDEX ix_concept_search_tokens_key ON concept_search_tokens "
            "(substr(token, 1, 200))"
        )
        op.execute(
            "CREATE TABLE concept_trigram (rowid bigint NOT NULL, text text NOT NULL, "
            "compact text NOT NULL, label text NOT NULL, "
            "CONSTRAINT pk_concept_trigram PRIMARY KEY (rowid))"
        )
        op.execute(
            "CREATE INDEX ix_concept_trigram_text ON concept_trigram "
            f"USING gin (text {trgm}.gin_trgm_ops)"
        )
        op.execute(
            "CREATE INDEX ix_concept_trigram_compact ON concept_trigram "
            f"USING gin (compact {trgm}.gin_trgm_ops)"
        )

    op.create_table('concepts',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('slug', sa.Text(), nullable=False),
    sa.Column('name', sa.Text(), nullable=False),
    sa.Column('name_norm', sa.Text(), nullable=False),
    sa.Column('summary', sa.Text(), nullable=False),
    sa.Column('origin', sa.Text(), nullable=False),
    sa.Column('reviewed_at', sa.DateTime(), nullable=True),
    sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('updated_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.CheckConstraint("origin IN ('paper', 'model', 'user')", name=op.f('ck_concepts_origin')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_concepts')),
    sa.UniqueConstraint('slug', name=op.f('uq_concepts_slug'))
    )
    with op.batch_alter_table('concepts', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_concepts_name_norm'), ['name_norm'], unique=False)
    if sqlite:
        op.execute(
            "CREATE TRIGGER trg_concepts_slug_immutable BEFORE UPDATE OF slug ON concepts "
            "WHEN NEW.slug IS NOT OLD.slug "
            "BEGIN SELECT RAISE(ABORT, 'concepts.slug is immutable'); END"
        )
    else:
        op.execute(
            "CREATE FUNCTION trg_concepts_slug_immutable() RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN RAISE EXCEPTION 'concepts.slug is immutable' USING ERRCODE = 'integrity_constraint_violation'; END $$"
        )
        op.execute(
            "CREATE TRIGGER trg_concepts_slug_immutable BEFORE UPDATE OF slug ON concepts "
            "FOR EACH ROW WHEN (NEW.slug IS DISTINCT FROM OLD.slug) "
            "EXECUTE FUNCTION trg_concepts_slug_immutable()"
        )

    op.create_table('concept_aliases',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('concept_id', sa.Integer(), nullable=False),
    sa.Column('alias', sa.Text(), nullable=False),
    sa.Column('alias_norm', sa.Text(), nullable=False),
    sa.ForeignKeyConstraint(['concept_id'], ['concepts.id'], name=op.f('fk_concept_aliases_concept_id_concepts'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_concept_aliases')),
    sa.UniqueConstraint('concept_id', 'alias_norm', name=op.f('uq_concept_aliases_concept_id_alias_norm'))
    )
    with op.batch_alter_table('concept_aliases', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_concept_aliases_alias_norm'), ['alias_norm'], unique=False)

    op.create_table('concept_embeddings',
    sa.Column('concept_id', sa.Integer(), nullable=False),
    sa.Column('model', sa.Text(), nullable=False),
    sa.Column('dimensions', sa.Integer(), nullable=False),
    sa.Column('vector', sa.LargeBinary(), nullable=False),
    sa.Column('text_hash', sa.Text(), nullable=False),
    sa.Column('embedded_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.CheckConstraint('dimensions > 0', name=op.f('ck_concept_embeddings_dimensions')),
    sa.CheckConstraint('length(vector) = 4 * dimensions', name=op.f('ck_concept_embeddings_vector_size')),
    sa.ForeignKeyConstraint(['concept_id'], ['concepts.id'], name=op.f('fk_concept_embeddings_concept_id_concepts'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('concept_id', name=op.f('pk_concept_embeddings'))
    )

    op.create_table('concept_relations',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('type', sa.Text(), nullable=False),
    sa.Column('from_id', sa.Integer(), nullable=False),
    sa.Column('to_id', sa.Integer(), nullable=False),
    sa.Column('low_id', sa.Integer(), nullable=False),
    sa.Column('high_id', sa.Integer(), nullable=False),
    sa.Column('rationale', sa.Text(), nullable=False),
    sa.Column('origin', sa.Text(), nullable=False),
    sa.Column('reviewed_at', sa.DateTime(), nullable=True),
    sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('updated_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.CheckConstraint("type IN ('builds_on', 'different_version_of', 'relates_to')", name=op.f('ck_concept_relations_type')),
    sa.CheckConstraint("origin IN ('paper', 'model', 'user')", name=op.f('ck_concept_relations_origin')),
    sa.CheckConstraint('from_id != to_id', name=op.f('ck_concept_relations_not_self')),
    sa.CheckConstraint("type = 'builds_on' OR from_id < to_id", name=op.f('ck_concept_relations_symmetric_order')),
    sa.CheckConstraint('low_id = min(from_id, to_id) AND high_id = max(from_id, to_id)', name=op.f('ck_concept_relations_pair')).ddl_if(dialect='sqlite'),
    sa.CheckConstraint('low_id = LEAST(from_id, to_id) AND high_id = GREATEST(from_id, to_id)', name=op.f('ck_concept_relations_pair')).ddl_if(dialect='postgresql'),
    sa.ForeignKeyConstraint(['from_id'], ['concepts.id'], name=op.f('fk_concept_relations_from_id_concepts'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['to_id'], ['concepts.id'], name=op.f('fk_concept_relations_to_id_concepts'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_concept_relations')),
    sa.UniqueConstraint('low_id', 'high_id', name=op.f('uq_concept_relations_low_id_high_id'))
    )
    with op.batch_alter_table('concept_relations', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_concept_relations_from_id'), ['from_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_concept_relations_to_id'), ['to_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    sqlite = op.get_context().dialect.name == 'sqlite'
    with op.batch_alter_table('concept_relations', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_concept_relations_to_id'))
        batch_op.drop_index(batch_op.f('ix_concept_relations_from_id'))

    op.drop_table('concept_relations')
    op.drop_table('concept_embeddings')
    with op.batch_alter_table('concept_aliases', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_concept_aliases_alias_norm'))

    op.drop_table('concept_aliases')
    if sqlite:
        op.execute("DROP TRIGGER trg_concepts_slug_immutable")
    else:
        op.execute("DROP TRIGGER trg_concepts_slug_immutable ON concepts")
        op.execute("DROP FUNCTION trg_concepts_slug_immutable()")
    with op.batch_alter_table('concepts', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_concepts_name_norm'))

    op.drop_table('concepts')
    if sqlite:
        op.execute("DROP TABLE concept_trigram")  # also drops its FTS5 shadow tables
        op.execute("DROP TABLE concept_search")  # likewise
    else:
        op.execute("DROP TABLE concept_trigram")  # with its indexes; pg_trgm stays installed
        op.execute("DROP TABLE concept_search_tokens")
        op.execute("DROP TABLE concept_search")
