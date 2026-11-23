"""ConceptEmbeddingManager: concept embeddings, staleness, and similarity search.

A concept's embedding is of `teacher.embeddings.concept_text(...)` (name, sorted aliases,
summary); its SHA-256 is stored as `text_hash`. An embedding is stale for a model when it is
missing, was made by another model, or its hash no longer matches the concept's text.
`embed_stale` embeds exactly the stale concepts; `store` stores vectors a caller made itself
(concept extraction embeds new concepts before it writes anything).

Similarity search never searches a partial index: if any concept is stale for the model it
raises StaleEmbeddingsError; mixed dimensions or a corrupt vector raise EmbeddingIndexError.

It goes through an approximate nearest-neighbour index (`teacher.vector_index`, HNSW) of the
model's vectors, kept on disk under `derived/_vector_index/` and cached in the process. The
`concept_embeddings` table stays the only source of truth. Before every query the index is
reconciled with the table as this session sees it, inside the session's transaction and under
the index's lock (so concurrent queries never see a half-applied change):

1. One scan of small columns -- concept id, slug, name, summary, aliases, and each embedding's
   model, dimensions, `text_hash` and `embedded_at`, never the vector blobs -- gives the
   staleness check and the wanted entries (id -> (text_hash, embedded_at)) of the model.
   (No blob reaches Python, but SQLite stores a row's columns together and `text_hash` /
   `embedded_at` come after `vector`, so the scan still reads the table's pages: ~80 ms at
   20,000 x 1536 warm. A covering index on (concept_id, model, dimensions, text_hash,
   embedded_at) would make it read only that index; it needs a migration.)
2. Entries the index holds that the table lacks or has otherwise are removed; rows the index
   lacks or holds otherwise are added, loading only those vectors.
3. The candidates the HNSW search returns are re-scored exactly (float64 cosine) from their
   stored vectors, ranked best first with ties by slug, and cut to `limit`. When the query asks
   for at least half the index (`2 * candidates >= size`: tiny graphs), every vector is a
   candidate, so the result is exact.

Rolled-back writes need no hooks: the next query from any session reconciles the index with
what that session sees. The index file is written when it is first built and then once enough
changes are unsaved (`teacher.vector_index.SAVE_*`); `sync_index` (run by `teacher concepts
reindex`) always writes it. A missing index is built on first use (construction, not a
fallback); a corrupt or mismatched one is a VectorIndexError naming `REBUILD_COMMAND`.
`exact=True` on the `similar_*` methods computes the same ranking by brute force over all
stored vectors instead (for verification); it is never chosen implicitly.
"""

from __future__ import annotations

import builtins
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from sqlalchemy import Text, select, type_coerce
from sqlalchemy.orm import Session, selectinload

from teacher import vector_index
from teacher.config import Settings
from teacher.db.models import Concept, ConceptAlias, ConceptEmbedding, utcnow
from teacher.embeddings import Embedder, check_vectors, concept_text, text_sha256
from teacher.managers.artifacts import _require_persistent
from teacher.managers.errors import (
    EmbeddingIndexError,
    Issue,
    NotFoundError,
    StaleEmbeddingsError,
    VectorIndexError,
)
from teacher.vector_index import Entry, VectorIndex, VectorIndexFileError

DEFAULT_SIMILAR_LIMIT = 10
STALE_EXAMPLES = 5  # how many slugs a StaleEmbeddingsError names
VECTOR_DTYPE = np.dtype("<f4")  # float32, little-endian
REBUILD_COMMAND = "teacher concepts reindex --rebuild-index"
# The HNSW search asks for CANDIDATE_FACTOR * wanted + CANDIDATE_EXTRA candidates, which are
# then re-scored exactly: oversampling makes up for the index's approximate float16 ranking.
CANDIDATE_FACTOR = 2
CANDIDATE_EXTRA = 10
_IN_CHUNK = 500  # ids per `IN (...)` when loading vectors
_CONCEPTS = Concept.__table__
_ALIASES = ConceptAlias.__table__
_EMBEDDINGS = ConceptEmbedding.__table__
# `embedded_at` exactly as stored (text), the version token of an index entry: no datetime
# parsing per row, and it changes on every `store` even when the text (hash) does not.
_EMBEDDED_AT = type_coerce(_EMBEDDINGS.c.embedded_at, Text)


def encode_vector(vector: list[float]) -> bytes:
    return np.asarray(vector, dtype=VECTOR_DTYPE).tobytes()


def _decode(blob: bytes, dimensions: int, slug: str) -> np.ndarray:
    if len(blob) != VECTOR_DTYPE.itemsize * dimensions:
        raise EmbeddingIndexError(
            f"the embedding of concept {slug!r} has {len(blob)} bytes, but "
            f"{dimensions} dimensions need {VECTOR_DTYPE.itemsize * dimensions}"
        )
    return np.frombuffer(blob, dtype=VECTOR_DTYPE)


def decode_vector(row: ConceptEmbedding, slug: str) -> np.ndarray:
    """The stored vector as float32; EmbeddingIndexError if its size disagrees with `dimensions`."""
    return _decode(row.vector, row.dimensions, slug)


def _unit(vector: np.ndarray, model: str, slug: str) -> np.ndarray:
    """`vector` as a float64 unit vector; EmbeddingIndexError if it has no direction."""
    vector = vector.astype(np.float64)
    norm = float(np.linalg.norm(vector))
    if not np.isfinite(norm) or norm == 0.0:
        raise EmbeddingIndexError(
            f"the {model!r} embedding of concept {slug!r} has length {norm}, so "
            "it has no direction to compare"
        )
    return vector / norm


def text_of(concept: Concept) -> str:
    """The text embedded for `concept` (see `teacher.embeddings.concept_text`)."""
    return concept_text(concept.name, [a.alias for a in concept.aliases], concept.summary)


def _model_name(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("model must be a non-empty string")
    return value


def _limit(limit: object) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError(f"limit must be a positive integer, got {limit!r}")
    return limit


def _index_errors_message(exc: VectorIndexFileError) -> str:
    return f"{exc}. Rebuild the index from the database with `{REBUILD_COMMAND}`"


@contextmanager
def _index_errors() -> Iterator[None]:
    """VectorIndexFileError (a file problem) as VectorIndexError naming the rebuild command."""
    try:
        yield
    except VectorIndexFileError as exc:
        raise VectorIndexError(_index_errors_message(exc)) from exc


@dataclass(frozen=True)
class _Index:
    concepts: list[Concept]
    matrix: np.ndarray  # one unit-length row per concept (float64)

    @property
    def dimensions(self) -> int:
        return int(self.matrix.shape[1])


@dataclass(frozen=True)
class _Snapshot:
    """What one scan of the small columns found (vectors are not read)."""

    count: int  # concepts
    slugs: dict[int, str]  # concept id -> slug
    stale: list[str]  # slugs of the concepts stale for the model, by slug
    entries: dict[int, Entry]  # the model's rows (of any concept, stale or not)
    dimensions: set[int]  # of the model's rows


@dataclass(frozen=True)
class IndexSync:
    """What `sync_index` / `rebuild_index` did: vectors added (new or replaced) and removed,
    how many the index holds, and its folder."""

    model: str
    dimensions: int | None  # None: the table has no vectors of the model, so no index
    added: int
    removed: int
    total: int
    directory: Path | None


class ConceptEmbeddingManager:
    """Find stale embeddings, embed them, and rank concepts by similarity."""

    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings

    def _concepts(self, *, vectors: bool = False) -> builtins.list[Concept]:
        """Every concept with aliases and embedding; the vector blobs only if `vectors`."""
        embedding = selectinload(Concept.embedding)
        if not vectors:
            embedding = embedding.defer(ConceptEmbedding.vector)
        return list(
            self.session.scalars(
                select(Concept)
                .options(selectinload(Concept.aliases), embedding)
                .order_by(Concept.slug)
            )
        )

    @staticmethod
    def _is_stale(concept: Concept, model: str) -> bool:
        row = concept.embedding
        return row is None or row.model != model or row.text_hash != text_sha256(text_of(concept))

    def stale(self, model: str) -> builtins.list[Concept]:
        """Concepts (by slug) whose `model` embedding is missing, by another model, or out of
        date with the concept's text."""
        _model_name(model)
        return [concept for concept in self._concepts() if self._is_stale(concept, model)]

    def embed_stale(self, embedder: Embedder) -> int:
        """Embed exactly the concepts that are stale for `embedder.model` and store the vectors
        (replacing older ones). Returns how many were embedded; the embedder is not called when
        none is stale. The vectors are checked (one per text, finite, one length, the same
        length as the model's stored vectors) before anything is written."""
        model = _model_name(embedder.model)
        stale = [concept for concept in self._concepts() if self._is_stale(concept, model)]
        if not stale:
            return 0
        texts = [text_of(concept) for concept in stale]
        vectors = check_vectors(embedder.embed(list(texts)), count=len(texts), model=model)
        self.store(list(zip(stale, texts, vectors, strict=True)), model)
        return len(stale)

    def store(
        self, items: builtins.list[tuple[Concept, str, builtins.list[float]]], model: str
    ) -> None:
        """Store vectors made elsewhere: `(concept, text, vector)` per concept, where `text` is
        what was embedded and must be the concept's current text (`text_of`), so the stored
        hash is right. The vectors must be finite, of one length, and of the length of the
        model's other stored vectors; ValueError / EmbeddingIndexError / EmbeddingResponseError
        otherwise, before anything is written."""
        model = _model_name(model)
        if not items:
            return
        for concept, text, _vector in items:
            _require_persistent(concept, "concept")
            if text != text_of(concept):
                raise ValueError(
                    f"the text embedded for concept {concept.slug!r} is not its current text "
                    "(name, aliases and summary); embed it again"
                )
        vectors = check_vectors(
            [vector for _concept, _text, vector in items], count=len(items), model=model
        )
        dimensions = len(vectors[0])
        ids = {concept.id for concept, _text, _vector in items}
        for concept in self._concepts():
            row = concept.embedding
            if concept.id not in ids and row is not None and row.model == model:
                if row.dimensions != dimensions:
                    raise EmbeddingIndexError(
                        f"embedding model {model!r} returned {dimensions}-dimensional vectors, "
                        f"but the stored embedding of {concept.slug!r} has {row.dimensions}"
                    )
        now = utcnow()
        with self.session.begin_nested():
            for (concept, text, _vector), vector in zip(items, vectors, strict=True):
                values = dict(
                    model=model,
                    dimensions=dimensions,
                    vector=encode_vector(vector),
                    text_hash=text_sha256(text),
                    embedded_at=now,
                )
                if concept.embedding is None:
                    concept.embedding = ConceptEmbedding(**values)
                else:
                    for key, value in values.items():
                        setattr(concept.embedding, key, value)
            self.session.flush()

    # -- the exact (brute-force) search, for `exact=True` -----------------------------------

    def _stale_error(self, stale: Sequence[str], count: int, model: str) -> StaleEmbeddingsError:
        examples = ", ".join(repr(slug) for slug in stale[:STALE_EXAMPLES])
        more = f" and {len(stale) - STALE_EXAMPLES} more" if len(stale) > STALE_EXAMPLES else ""
        return StaleEmbeddingsError(
            f"{len(stale)} of {count} concepts have no up-to-date embedding for "
            f"model {model!r} ({examples}{more}); run embed_stale first",
            stale_count=len(stale),
            concept_count=count,
        )

    def _exact_index(self, model: str) -> _Index:
        """Every concept's `model` vector, normalised; raises unless all are up to date."""
        concepts = self._concepts(vectors=True)
        stale = [concept for concept in concepts if self._is_stale(concept, model)]
        if stale:
            raise self._stale_error([c.slug for c in stale], len(concepts), model)
        if not concepts:
            return _Index([], np.zeros((0, 0)))
        dimensions = {c.embedding.dimensions for c in concepts if c.embedding is not None}
        if len(dimensions) != 1:
            raise EmbeddingIndexError(
                f"the {model!r} embeddings have mixed dimensions {sorted(dimensions)}"
            )
        rows = []
        for concept in concepts:
            assert concept.embedding is not None  # not stale
            vector = decode_vector(concept.embedding, concept.slug)
            rows.append(_unit(vector, model, concept.slug))
        return _Index(concepts, np.vstack(rows))

    @staticmethod
    def _rank(
        index: _Index, query: np.ndarray, limit: int, exclude: int | None = None
    ) -> builtins.list[tuple[Concept, float]]:
        scores = index.matrix @ query
        pairs = zip(index.concepts, scores, strict=True)
        ranked = sorted(
            (-float(score), concept.slug, position)
            for position, (concept, score) in enumerate(pairs)
            if concept.id != exclude
        )
        return [(index.concepts[position], -neg) for neg, _slug, position in ranked[:limit]]

    @staticmethod
    def _query_unit(vector: builtins.list[float], dimensions: int, model: str) -> np.ndarray:
        if len(vector) != dimensions:
            raise EmbeddingIndexError(
                f"embedding model {model!r} returned a {len(vector)}-dimensional vector for the "
                f"query, but the stored embeddings have {dimensions}"
            )
        query = np.asarray(vector, dtype=np.float64)
        norm = float(np.linalg.norm(query))
        if norm == 0.0:
            raise EmbeddingIndexError(
                f"embedding model {model!r} returned a zero vector for the query"
            )
        return query / norm

    # -- the index --------------------------------------------------------------------------

    def _flush(self) -> None:
        """Flush pending ORM changes as an ORM query would (the scans below are Core
        statements, which skip autoflush and ORM row processing, for speed)."""
        if self.session.autoflush:
            self.session.flush()

    def _snapshot(self, model: str) -> _Snapshot:
        """Scan the small columns of concepts, aliases and embeddings (not the vectors)."""
        self._flush()
        rows = self.session.execute(
            select(
                _CONCEPTS.c.id,
                _CONCEPTS.c.slug,
                _CONCEPTS.c.name,
                _CONCEPTS.c.summary,
                _EMBEDDINGS.c.model,
                _EMBEDDINGS.c.dimensions,
                _EMBEDDINGS.c.text_hash,
                _EMBEDDED_AT,
            )
            .select_from(
                _CONCEPTS.outerjoin(_EMBEDDINGS, _EMBEDDINGS.c.concept_id == _CONCEPTS.c.id)
            )
            .order_by(_CONCEPTS.c.slug)
        ).all()
        aliases: dict[int, builtins.list[str]] = {}
        for concept_id, alias in self.session.execute(
            select(_ALIASES.c.concept_id, _ALIASES.c.alias)
        ):
            aliases.setdefault(concept_id, []).append(alias)
        slugs: dict[int, str] = {}
        stale: builtins.list[str] = []
        entries: dict[int, Entry] = {}
        dimensions: set[int] = set()
        for concept_id, slug, name, summary, row_model, dims, text_hash, embedded_at in rows:
            slugs[concept_id] = slug
            if row_model == model:
                entries[concept_id] = Entry(text_hash, embedded_at)
                dimensions.add(dims)
            current = text_sha256(concept_text(name, aliases.get(concept_id, []), summary))
            if row_model != model or text_hash != current:
                stale.append(slug)
        return _Snapshot(len(rows), slugs, stale, entries, dimensions)

    def _fresh_snapshot(self, model: str) -> _Snapshot:
        """The snapshot; StaleEmbeddingsError / EmbeddingIndexError unless every concept has an
        up-to-date `model` embedding, all of one dimensionality."""
        snapshot = self._snapshot(model)
        if snapshot.stale:
            raise self._stale_error(snapshot.stale, snapshot.count, model)
        if len(snapshot.dimensions) > 1:
            raise EmbeddingIndexError(
                f"the {model!r} embeddings have mixed dimensions {sorted(snapshot.dimensions)}"
            )
        return snapshot

    def _unit_vectors(
        self, ids: Sequence[int], model: str, snapshot: _Snapshot
    ) -> dict[int, np.ndarray]:
        """The stored `model` vectors of `ids` as float64 unit vectors, checked against the
        snapshot's entries (same transaction, so they always agree unless the session itself
        wrote in between, which is an error)."""
        found: dict[int, np.ndarray] = {}
        self._flush()
        for start in range(0, len(ids), _IN_CHUNK):
            chunk = ids[start : start + _IN_CHUNK]
            rows = self.session.execute(
                select(
                    _EMBEDDINGS.c.concept_id,
                    _EMBEDDINGS.c.model,
                    _EMBEDDINGS.c.dimensions,
                    _EMBEDDINGS.c.vector,
                    _EMBEDDINGS.c.text_hash,
                    _EMBEDDED_AT,
                ).where(_EMBEDDINGS.c.concept_id.in_(chunk))
            )
            for concept_id, row_model, dims, blob, text_hash, embedded_at in rows:
                slug = snapshot.slugs.get(concept_id, f"#{concept_id}")
                if row_model != model or snapshot.entries.get(concept_id) != Entry(
                    text_hash, embedded_at
                ):
                    raise EmbeddingIndexError(
                        f"the embedding of concept {slug!r} changed while it was being read"
                    )
                found[concept_id] = _unit(_decode(blob, dims, slug), model, slug)
        missing = [concept_id for concept_id in ids if concept_id not in found]
        if missing:
            raise EmbeddingIndexError(
                f"the {model!r} embeddings of concept ids {missing[:5]} disappeared while "
                "they were being read"
            )
        return found

    def _reconcile(
        self, index: VectorIndex, model: str, snapshot: _Snapshot
    ) -> tuple[int, int]:
        """Make `index` hold exactly the snapshot's entries; (added, removed). Hold its lock.
        Vectors are loaded and added `REBUILD_BATCH` at a time (bounded memory on a first
        build); after each step the index's entries describe exactly what it holds, so a
        failure part-way leaves it consistent and the next reconcile carries on."""
        have = index.entries
        wanted = snapshot.entries
        if have == wanted:  # the usual case, one C-level comparison
            return 0, 0
        remove = [key for key, entry in have.items() if wanted.get(key) != entry]
        add_ids = sorted(key for key, entry in wanted.items() if have.get(key) != entry)
        if remove:
            index.apply(remove, {})
        for start in range(0, len(add_ids), vector_index.REBUILD_BATCH):
            chunk = add_ids[start : start + vector_index.REBUILD_BATCH]
            vectors = self._unit_vectors(chunk, model, snapshot)
            index.apply([], {key: (wanted[key], vectors[key]) for key in chunk})
        return len(add_ids), len(remove)

    @contextmanager
    def _synced(self, model: str, snapshot: _Snapshot) -> Iterator[VectorIndex]:
        """The cached index of `model`, reconciled with the snapshot; its lock is held while
        the caller uses it. Saved when `VectorIndex.should_save` says so."""
        [dimensions] = snapshot.dimensions
        with _index_errors():
            index = vector_index.cached(self.settings, model, dimensions)
            with index.lock:
                self._reconcile(index, model, snapshot)
                if index.should_save():
                    index.save()
                yield index

    def _search(
        self,
        model: str,
        snapshot: _Snapshot,
        query: np.ndarray,
        limit: int,
        exclude: int | None = None,
    ) -> builtins.list[tuple[Concept, float]]:
        """The `limit` best concepts for the unit vector `query` through the index, re-scored
        exactly from their stored vectors."""
        wanted = min(limit + (exclude is not None), snapshot.count)
        count = CANDIDATE_FACTOR * wanted + CANDIDATE_EXTRA
        with self._synced(model, snapshot) as index:
            if 2 * count >= len(index):
                candidates = list(index.entries)  # a tiny index: every vector, so exact
            else:
                candidates = index.search(query.astype(np.float32), count)
                if len(candidates) < wanted:
                    raise VectorIndexError(
                        f"the vector index of model {model!r} returned {len(candidates)} of the "
                        f"{wanted} nearest neighbours asked for out of {len(index)} vectors; "
                        f"its graph is damaged. Rebuild it with `{REBUILD_COMMAND}`"
                    )
        candidates = [concept_id for concept_id in candidates if concept_id != exclude]
        vectors = self._unit_vectors(candidates, model, snapshot)
        matrix = np.vstack([vectors[concept_id] for concept_id in candidates])
        scores = matrix @ query
        ranked = sorted(
            (-float(score), snapshot.slugs[concept_id], concept_id)
            for concept_id, score in zip(candidates, scores, strict=True)
        )[:limit]
        top = [concept_id for _neg, _slug, concept_id in ranked]
        loaded = {
            concept.id: concept
            for concept in self.session.scalars(
                select(Concept)
                .where(Concept.id.in_(top))
                .options(selectinload(Concept.aliases), selectinload(Concept.embedding))
            )
        }
        return [(loaded[concept_id], -neg) for neg, _slug, concept_id in ranked]

    # -- similarity search ------------------------------------------------------------------

    def similar_to_concept(
        self,
        concept: Concept,
        embedder_or_model: Embedder | str,
        limit: int = DEFAULT_SIMILAR_LIMIT,
        *,
        exact: bool = False,
    ) -> builtins.list[tuple[Concept, float]]:
        """The `limit` other concepts most similar to `concept` as `(concept, cosine)`, best
        first (ties by slug), using its stored vector (no embedding request is made). With
        `exact`, by brute force over every stored vector instead of the index."""
        _require_persistent(concept, "concept")
        model = _model_name(
            embedder_or_model if isinstance(embedder_or_model, str) else embedder_or_model.model
        )
        _limit(limit)
        if exact:
            index = self._exact_index(model)
            positions = [i for i, c in enumerate(index.concepts) if c.id == concept.id]
            if not positions:
                raise NotFoundError(f"concept {concept.slug!r} is not in the {model!r} index")
            return self._rank(index, index.matrix[positions[0]], limit, exclude=concept.id)
        snapshot = self._fresh_snapshot(model)
        if concept.id not in snapshot.entries:
            raise NotFoundError(f"concept {concept.slug!r} is not in the {model!r} index")
        query = self._unit_vectors([concept.id], model, snapshot)[concept.id]
        return self._search(model, snapshot, query, limit, exclude=concept.id)

    def similar_to_text(
        self,
        text: str,
        embedder: Embedder,
        limit: int = DEFAULT_SIMILAR_LIMIT,
        *,
        exact: bool = False,
    ) -> builtins.list[tuple[Concept, float]]:
        """The `limit` concepts most similar to `text` as `(concept, cosine)`, best first (ties
        by slug). The index is checked before `text` is embedded; with no concepts at all the
        result is empty and nothing is embedded. With `exact`, by brute force."""
        if not isinstance(text, str) or not text.strip():
            raise ValueError("text must be a non-blank string")
        model = _model_name(embedder.model)
        _limit(limit)
        if exact:
            index = self._exact_index(model)
            if not index.concepts:
                return []
            [vector] = check_vectors(embedder.embed([text]), count=1, model=model)
            return self._rank(index, self._query_unit(vector, index.dimensions, model), limit)
        snapshot = self._fresh_snapshot(model)
        if not snapshot.count:
            return []
        with self._synced(model, snapshot):  # checked (and brought up to date) first
            pass
        [vector] = check_vectors(embedder.embed([text]), count=1, model=model)
        [dimensions] = snapshot.dimensions
        query = self._query_unit(vector, dimensions, model)
        return self._search(model, snapshot, query, limit)

    def similar_to_vector(
        self,
        vector: builtins.list[float],
        model: str,
        limit: int = DEFAULT_SIMILAR_LIMIT,
        *,
        exact: bool = False,
    ) -> builtins.list[tuple[Concept, float]]:
        """As `similar_to_text`, for a query vector already made with `model` (so a caller can
        embed many queries in one request). Empty with no concepts at all."""
        model = _model_name(model)
        _limit(limit)
        if exact:
            index = self._exact_index(model)
            if not index.concepts:
                return []
            [checked] = check_vectors([vector], count=1, model=model)
            return self._rank(index, self._query_unit(checked, index.dimensions, model), limit)
        snapshot = self._fresh_snapshot(model)
        if not snapshot.count:
            return []
        with self._synced(model, snapshot):  # stored-vector errors before the query's own
            pass
        [checked] = check_vectors([vector], count=1, model=model)
        [dimensions] = snapshot.dimensions
        query = self._query_unit(checked, dimensions, model)
        return self._search(model, snapshot, query, limit)

    # -- index maintenance ------------------------------------------------------------------

    def index_dir(self, model: str, dimensions: int) -> Path:
        """Where the index of `model` at `dimensions` is kept."""
        return vector_index.index_dir(self.settings, _model_name(model), dimensions)

    def _table_dimensions(self, snapshot: _Snapshot, model: str) -> int | None:
        if not snapshot.dimensions:
            return None
        if len(snapshot.dimensions) > 1:
            raise EmbeddingIndexError(
                f"the {model!r} embeddings have mixed dimensions {sorted(snapshot.dimensions)}"
            )
        [dimensions] = snapshot.dimensions
        return dimensions

    def sync_index(self, model: str) -> IndexSync:
        """Reconcile the index of `model` with the table's rows of the model (stale concepts
        included: the index mirrors the table) and write it to disk. Reads the existing index
        file: a corrupt or mismatched one is a VectorIndexError (see `rebuild_index`)."""
        model = _model_name(model)
        snapshot = self._snapshot(model)
        dimensions = self._table_dimensions(snapshot, model)
        if dimensions is None:
            return IndexSync(model, None, 0, 0, 0, None)
        with _index_errors():
            index = vector_index.cached(self.settings, model, dimensions)
            with index.lock:
                added, removed = self._reconcile(index, model, snapshot)
                if index.unsaved_changes or not index.saved:
                    index.save()
                return IndexSync(model, dimensions, added, removed, len(index), index.directory)

    def rebuild_index(self, model: str) -> IndexSync:
        """Build the index of `model` from scratch from the table, without reading the existing
        files, write it (replacing them) and use it in this process from now on."""
        model = _model_name(model)
        snapshot = self._snapshot(model)
        dimensions = self._table_dimensions(snapshot, model)
        if dimensions is None:
            return IndexSync(model, None, 0, 0, 0, None)
        index = VectorIndex(self.index_dir(model, dimensions), model, dimensions)
        with _index_errors(), index.lock:
            added, _removed = self._reconcile(index, model, snapshot)
            index.save()
        vector_index.install(self.settings, index)
        return IndexSync(model, dimensions, added, 0, len(index), index.directory)

    def check_index(self, model: str) -> builtins.list[Issue]:
        """Compare the index file of `model` with the table's rows of the model.

        'index_missing' (no index was ever saved), 'index_unusable' (the files cannot be used:
        the detail says why and names the rebuild command), 'mixed_dimensions' (the table's
        rows of the model are not of one length; the index cannot be checked) -- these three
        with the model as the Issue's slug; 'missing_from_index' (a row the file lacks),
        'stale_in_index' (the file holds another version of the row) and 'orphan_in_index'
        (the file holds a concept id with no row of the model; slug '#<id>'). Drift is normal
        between saves (queries reconcile in memory first); `sync_index` writes it away.
        """
        model = _model_name(model)
        snapshot = self._snapshot(model)
        if len(snapshot.dimensions) > 1:
            return [
                Issue(
                    "mixed_dimensions",
                    model,
                    f"the {model!r} embeddings have mixed dimensions "
                    f"{sorted(snapshot.dimensions)}",
                )
            ]
        if not snapshot.dimensions:
            return []
        [dimensions] = snapshot.dimensions
        directory = self.index_dir(model, dimensions)
        try:
            on_disk = VectorIndex.load(directory, model, dimensions)
        except VectorIndexFileError as exc:
            return [Issue("index_unusable", model, _index_errors_message(exc))]
        if on_disk is None:
            return [
                Issue(
                    "index_missing",
                    model,
                    f"no vector index has been saved in {directory}; run `teacher concepts "
                    "reindex` (or any similarity search) to build it",
                )
            ]
        return self._drift(on_disk.entries, snapshot)

    @staticmethod
    def _drift(have: Mapping[int, Entry], snapshot: _Snapshot) -> builtins.list[Issue]:
        issues: builtins.list[Issue] = []
        by_slug = sorted(snapshot.entries.items(), key=lambda item: snapshot.slugs[item[0]])
        for concept_id, entry in by_slug:
            slug = snapshot.slugs[concept_id]
            if concept_id not in have:
                issues.append(Issue("missing_from_index", slug, "the index has no vector for it"))
            elif have[concept_id] != entry:
                issues.append(
                    Issue(
                        "stale_in_index",
                        slug,
                        f"the index holds the vector embedded at {have[concept_id].embedded_at} "
                        f"(text hash {have[concept_id].text_hash[:12]}), the table the one "
                        f"embedded at {entry.embedded_at} (text hash {entry.text_hash[:12]})",
                    )
                )
        for concept_id in sorted(set(have) - set(snapshot.entries)):
            issues.append(
                Issue(
                    "orphan_in_index",
                    f"#{concept_id}",
                    f"the index holds a vector for concept id {concept_id}, which has no "
                    "embedding of this model",
                )
            )
        return issues


__all__ = [
    "DEFAULT_SIMILAR_LIMIT",
    "REBUILD_COMMAND",
    "ConceptEmbeddingManager",
    "IndexSync",
    "decode_vector",
    "encode_vector",
    "text_of",
]
