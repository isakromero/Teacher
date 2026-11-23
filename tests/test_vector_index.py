"""The approximate (HNSW) vector index behind concept similarity search: recall against the
exact computation, reconciliation with `concept_embeddings` after every kind of change, other
sessions and processes, model and dimension changes, damaged files, `check_index`,
`rebuild_index`, and concurrent queries.

Synthetic data: clustered random vectors (random gaussian centres, each point a centre plus
gaussian noise of the same scale, so related points have cosine ~0.5 like related concepts'
embeddings). Isotropic gaussian vectors in 1536 dimensions have no neighbourhood structure at
all (every cosine is 0 +- 0.03), which no ANN index can rank: there HNSW recall@10 falls to
~0.8 at 20,000 vectors whatever the parameters, so the recall tests use the clustered data.
The 20,000-vector case uses 256 dimensions to keep the suite fast; 200 and 2,000 use the real
1536 (`text-embedding-3-small`).
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from pathlib import Path

import numpy as np
import pytest
from sqlalchemy import insert, select, text
from sqlalchemy.orm import Session

from teacher import vector_index
from teacher.concepts import normalize_name
from teacher.config import Settings
from teacher.db.engine import session_factory, session_scope
from teacher.db.models import Concept, ConceptEmbedding, utcnow
from teacher.embeddings import concept_text, text_sha256
from teacher.managers import (
    ConceptEmbeddingManager,
    ConceptManager,
    EmbeddingIndexError,
    StaleEmbeddingsError,
    VectorIndexError,
)
from teacher.managers.concept_embeddings import REBUILD_COMMAND, encode_vector
from teacher.vector_index import MANIFEST_FILE, VectorIndex, index_dir

MODEL = "bench-embed"


@pytest.fixture(autouse=True)
def _fresh_cache() -> None:
    vector_index.clear_cache()


def clustered(rng: np.random.Generator, n: int, dims: int, per_cluster: int = 20) -> np.ndarray:
    centres = rng.standard_normal((max(n // per_cluster, 2), dims))
    points = centres[rng.integers(0, len(centres), n)] + rng.standard_normal((n, dims))
    return points.astype(np.float32)


def bulk_concepts(session: Session, vectors: np.ndarray, model: str = MODEL) -> list[int]:
    """Insert one concept per row with an up-to-date embedding (no search rows: not needed)."""
    start = session.scalar(select(Concept.id).order_by(Concept.id.desc()).limit(1)) or 0
    names = [f"Concept {start + i:06d}" for i in range(1, len(vectors) + 1)]
    session.execute(
        insert(Concept),
        [
            {
                "slug": f"c-{start + i:06d}",
                "name": name,
                "name_norm": normalize_name(name),
                "summary": "Synthetic.",
                "origin": "user",
            }
            for i, name in enumerate(names, start=1)
        ],
    )
    ids = list(session.scalars(select(Concept.id).where(Concept.id > start).order_by(Concept.id)))
    now = utcnow()
    session.execute(
        insert(ConceptEmbedding),
        [
            {
                "concept_id": concept_id,
                "model": model,
                "dimensions": vectors.shape[1],
                "vector": encode_vector(vector),
                "text_hash": text_sha256(concept_text(name, [], "Synthetic.")),
                "embedded_at": now,
            }
            for concept_id, name, vector in zip(ids, names, vectors, strict=True)
        ],
    )
    return ids


def unit_rows(vectors: np.ndarray) -> np.ndarray:
    rows = vectors.astype(np.float32).astype(np.float64)  # what is stored, then as compared
    return rows / np.linalg.norm(rows, axis=1, keepdims=True)


@pytest.fixture
def manager(session: Session, settings: Settings) -> ConceptEmbeddingManager:
    return ConceptEmbeddingManager(session, settings)


# -- recall and exactness ---------------------------------------------------------------------


@pytest.mark.parametrize(("n", "dims", "queries"), [(200, 1536, 40), (2000, 1536, 40), (20000, 256, 25)])
def test_recall_at_10_against_exact(
    manager: ConceptEmbeddingManager, session: Session, n: int, dims: int, queries: int
) -> None:
    rng = np.random.default_rng(n)
    data = clustered(rng, n + queries, dims)
    vectors, probes = data[:n], data[n:]
    ids = np.array(bulk_concepts(session, vectors))
    matrix = unit_rows(vectors)
    recalls = []
    for probe in probes:
        result = manager.similar_to_vector(probe.tolist(), MODEL, 10)
        query = probe.astype(np.float64) / np.linalg.norm(probe.astype(np.float64))
        truth = set(ids[np.argsort(-(matrix @ query))[:10]].tolist())
        recalls.append(len(truth & {concept.id for concept, _ in result}) / 10)
        scores = [score for _, score in result]
        assert len(result) == 10 and scores == sorted(scores, reverse=True)
        for concept, score in result:  # true cosine values, not the index's approximations
            position = int(np.searchsorted(ids, concept.id))
            assert score == pytest.approx(float(matrix[position] @ query), abs=1e-12)
    assert np.mean(recalls) >= 0.95
    assert len(vector_index.cached(manager.settings, MODEL, dims)) == n


def test_exact_option_agrees_on_a_medium_set(
    manager: ConceptEmbeddingManager, session: Session
) -> None:
    """Above the tiny-set size the index is approximate (and usearch's multi-threaded build
    is not deterministic), so this checks overlap and true scores, not identical lists."""
    rng = np.random.default_rng(7)
    data = clustered(rng, 520, 48)
    ids = bulk_concepts(session, data[:500])
    overlaps = []

    def compare(approx: list, exact: list) -> None:
        exact_scores = {c.id: s for c, s in exact}
        overlaps.append(len(exact_scores.keys() & {c.id for c, _ in approx}) / len(exact))
        assert len(approx) == len(exact)
        for concept, score in approx:  # every score is the true cosine, ranked
            if concept.id in exact_scores:
                assert score == pytest.approx(exact_scores[concept.id], abs=1e-12)
        assert [s for _, s in approx] == sorted((s for _, s in approx), reverse=True)

    for probe in data[500:]:
        compare(
            manager.similar_to_vector(probe.tolist(), MODEL, 5),
            manager.similar_to_vector(probe.tolist(), MODEL, 5, exact=True),
        )
    for concept_id in ids[:20]:
        concept = session.get(Concept, concept_id)
        assert concept is not None
        approx = manager.similar_to_concept(concept, MODEL, 5)
        assert concept.id not in {c.id for c, _ in approx}
        compare(approx, manager.similar_to_concept(concept, MODEL, 5, exact=True))
    assert np.mean(overlaps) >= 0.95


def test_tiny_sets_are_ranked_exactly_with_ties_by_slug(
    manager: ConceptEmbeddingManager, session: Session
) -> None:
    rng = np.random.default_rng(3)
    vectors = rng.standard_normal((40, 8)).astype(np.float32)
    vectors[5] = vectors[9] = vectors[17] = vectors[2]  # four identical vectors: a tie
    ids = bulk_concepts(session, vectors)
    for probe in [vectors[2], *rng.standard_normal((5, 8))]:
        for limit in (1, 3, 10, 40, 100):
            approx = manager.similar_to_vector(list(map(float, probe)), MODEL, limit)
            exact = manager.similar_to_vector(list(map(float, probe)), MODEL, limit, exact=True)
            assert [(c.slug, s) for c, s in approx] == [(c.slug, s) for c, s in exact]
            assert len(approx) == min(limit, 40)
    concept = session.get(Concept, ids[2])
    assert concept is not None
    ranked = manager.similar_to_concept(concept, MODEL, 3)
    assert [c.id for c, _ in ranked] == [ids[5], ids[9], ids[17]]  # the tie, by slug
    assert ranked == manager.similar_to_concept(concept, MODEL, 3, exact=True)


def test_exact_is_an_explicit_option_and_reads_every_vector(
    manager: ConceptEmbeddingManager, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    bulk_concepts(session, np.eye(4, dtype=np.float32))
    calls: list[str] = []
    monkeypatch.setattr(vector_index, "cached", lambda *a: calls.append("index") or None)
    manager.similar_to_vector([1.0, 0.0, 0.0, 0.0], MODEL, exact=True)
    assert calls == []  # the exact search does not touch the index


def test_a_short_answer_from_the_graph_is_an_error(
    manager: ConceptEmbeddingManager, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    bulk_concepts(session, clustered(np.random.default_rng(1), 300, 16))
    manager.similar_to_vector([1.0] * 16, MODEL)
    monkeypatch.setattr(VectorIndex, "search", lambda self, query, count: [])
    with pytest.raises(VectorIndexError, match=rf"returned 0 of the 10 .*`{REBUILD_COMMAND}`"):
        manager.similar_to_vector([1.0] * 16, MODEL)


# -- staying in sync ----------------------------------------------------------------------------

TABLE = {
    "attention": [1.0, 0.0, 0.0],
    "self-attention": [0.9, 0.1, 0.0],
    "softmax": [0.5, 0.5, 0.0],
    "convolution": [0.0, 0.0, 1.0],
    "pooling": [0.0, 0.2, 1.0],
    "probe": [1.0, 0.05, 0.0],
}


@pytest.fixture
def graph(session: Session, settings: Settings, fake_embedder) -> dict:
    concepts = ConceptManager(session, settings)
    made = {
        name: concepts.create(name, f"About {name}.", "user")
        for name in ("attention", "self-attention", "softmax", "convolution")
    }
    embedder = fake_embedder(TABLE)
    ConceptEmbeddingManager(session, settings).embed_stale(embedder)
    session.commit()
    return {"concepts": concepts, "made": made, "embedder": embedder}


def held(settings: Settings, model: str = "fake-embed", dims: int = 64) -> dict[int, str]:
    """concept id -> text hash, of the vectors the process's index holds."""
    index = vector_index.cached(settings, model, dims)
    with index.lock:
        return {key: entry.text_hash for key, entry in index.entries.items()}


def table(session: Session, model: str = "fake-embed") -> dict[int, str]:
    return dict(
        session.execute(
            select(ConceptEmbedding.concept_id, ConceptEmbedding.text_hash).where(
                ConceptEmbedding.model == model
            )
        ).all()
    )


def assert_matches_exact(manager: ConceptEmbeddingManager, embedder, query: str = "probe") -> list:
    approx = manager.similar_to_text(query, embedder)
    exact = manager.similar_to_text(query, embedder, exact=True)
    assert [(c.id, s) for c, s in approx] == [(c.id, s) for c, s in exact]
    return [c.name for c, _ in approx]


def test_sync_after_every_kind_of_change(
    manager: ConceptEmbeddingManager, session: Session, settings: Settings, graph: dict
) -> None:
    concepts, made, embedder = graph["concepts"], graph["made"], graph["embedder"]
    assert assert_matches_exact(manager, embedder)[:2] == ["attention", "self-attention"]
    assert held(settings) == table(session)

    # A new concept, embedded.
    concepts.create("pooling", "About pooling.", "user")
    with pytest.raises(StaleEmbeddingsError, match="'pooling'"):
        manager.similar_to_text("probe", embedder)
    manager.embed_stale(embedder)
    assert "pooling" in assert_matches_exact(manager, embedder)
    assert held(settings) == table(session)

    # A text change, re-embedded (the new first line maps to another vector).
    embedder.table["softmax 2"] = [1.0, 0.04, 0.0]
    concepts.update(made["softmax"], name="softmax 2")
    manager.embed_stale(embedder)
    assert assert_matches_exact(manager, embedder)[0] == "softmax 2"
    assert held(settings) == table(session)

    # An alias change makes the text stale; after re-embedding the index holds the new hash.
    concepts.add_alias(made["convolution"], "conv")
    with pytest.raises(StaleEmbeddingsError, match="'convolution'"):
        manager.similar_to_text("probe", embedder)
    manager.embed_stale(embedder)
    assert_matches_exact(manager, embedder)
    assert held(settings) == table(session)

    # A merge deletes the source's embedding and makes the target stale.
    concepts.merge(made["self-attention"], made["attention"])
    manager.embed_stale(embedder)
    names = assert_matches_exact(manager, embedder)
    assert "self-attention" not in names
    assert held(settings) == table(session)

    # A delete.
    concepts.delete(made["convolution"])
    assert "convolution" not in assert_matches_exact(manager, embedder)
    assert held(settings) == table(session)
    session.commit()


def test_a_rolled_back_transaction_leaves_no_trace(
    session: Session, settings: Settings, graph: dict
) -> None:
    concepts, embedder = graph["concepts"], graph["embedder"]
    manager = ConceptEmbeddingManager(session, settings)
    before = held(settings) if manager.similar_to_text("probe", embedder) else {}
    concepts.create("pooling", "About pooling.", "user")
    concepts.update(graph["made"]["softmax"], summary="Changed.")
    manager.embed_stale(embedder)
    assert "pooling" in assert_matches_exact(manager, embedder)  # the uncommitted view
    assert held(settings) != before
    session.rollback()

    with session_scope(settings) as other:
        names = assert_matches_exact(ConceptEmbeddingManager(other, settings), embedder)
        assert "pooling" not in names
        assert held(settings) == table(other) == before


def test_another_session_sees_committed_changes(
    session: Session, settings: Settings, graph: dict
) -> None:
    embedder = graph["embedder"]
    reader = ConceptEmbeddingManager(session, settings)
    assert "pooling" not in assert_matches_exact(reader, embedder)
    session.commit()  # end the read transaction

    with session_scope(settings) as writer:
        ConceptManager(writer, settings).create("pooling", "About pooling.", "user")
        ConceptEmbeddingManager(writer, settings).embed_stale(embedder)
    assert "pooling" in assert_matches_exact(reader, embedder)
    session.commit()


def test_another_process_sees_changes_and_this_one_sees_its(
    session: Session, settings: Settings, graph: dict, tmp_path: Path
) -> None:
    embedder = graph["embedder"]
    manager = ConceptEmbeddingManager(session, settings)
    assert_matches_exact(manager, embedder)  # builds and saves the index
    session.commit()
    folder = index_dir(settings, "fake-embed", 64)
    saved = json.loads((folder / MANIFEST_FILE).read_text())
    assert len(saved["entries"]) == 4

    script = tmp_path / "child.py"
    script.write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "from teacher.config import Settings\n"
        "from teacher.db.engine import session_scope\n"
        "from teacher.managers import ConceptEmbeddingManager, ConceptManager\n"
        "from teacher.managers.concept_embeddings import text_of\n"
        "settings = Settings(root=Path(sys.argv[1]))\n"
        "with session_scope(settings) as s:\n"
        "    c = ConceptManager(s, settings).create('pooling', 'About pooling.', 'user')\n"
        "    vector = [0.99, 0.0, 0.1] + [0.0] * 61\n"
        "    ConceptEmbeddingManager(s, settings).store([(c, text_of(c), vector)], 'fake-embed')\n"
        "with session_scope(settings) as s:\n"
        "    hits = ConceptEmbeddingManager(s, settings).similar_to_vector(\n"
        "        [1.0] + [0.0] * 63, 'fake-embed', 2)\n"
        "    print(','.join(c.name for c, _ in hits))\n",
        encoding="utf-8",
    )
    child = subprocess.run(
        [sys.executable, str(script), str(settings.root)],
        capture_output=True, text=True, timeout=120, check=False,
    )
    assert child.returncode == 0, child.stderr
    assert child.stdout.strip() == "attention,pooling"
    # This process still has its cached index without 'pooling'; the query reconciles it.
    assert "pooling" in assert_matches_exact(manager, embedder)
    session.commit()


def test_a_model_change_uses_another_index(
    session: Session, settings: Settings, graph: dict, fake_embedder
) -> None:
    manager = ConceptEmbeddingManager(session, settings)
    assert_matches_exact(manager, graph["embedder"])
    other = fake_embedder({name: [*v, 1.0] for name, v in TABLE.items()}, model="fake-other")
    manager.embed_stale(other)
    with pytest.raises(StaleEmbeddingsError, match="4 of 4 concepts"):
        manager.similar_to_text("probe", graph["embedder"])
    assert assert_matches_exact(manager, other)[0] == "attention"
    assert index_dir(settings, "fake-other", 64) != index_dir(settings, "fake-embed", 64)
    assert (index_dir(settings, "fake-other", 64) / MANIFEST_FILE).exists()
    session.commit()


def test_a_dimension_change_uses_another_index(
    manager: ConceptEmbeddingManager, session: Session, settings: Settings
) -> None:
    ids = bulk_concepts(session, np.eye(5, dtype=np.float32))
    manager.similar_to_vector([1.0, 0, 0, 0, 0], MODEL)
    session.execute(text("DELETE FROM concept_embeddings"))
    session.execute(text("DELETE FROM concepts"))
    ids = bulk_concepts(session, np.eye(7, dtype=np.float32))
    [(best, score)] = manager.similar_to_vector([0, 0, 0, 0, 0, 0, 1.0], MODEL, 1)
    assert best.id == ids[6] and score == pytest.approx(1.0)
    assert (index_dir(settings, MODEL, 7) / MANIFEST_FILE).exists()


def test_zero_concepts_and_stale_concepts_never_touch_the_index(
    manager: ConceptEmbeddingManager, session: Session, settings: Settings, fake_embedder
) -> None:
    embedder = fake_embedder(TABLE)
    assert manager.similar_to_text("probe", embedder) == []
    assert manager.similar_to_vector([1.0] * 64, "fake-embed") == []
    assert embedder.calls == []
    ConceptManager(session, settings).create("attention", "About attention.", "user")
    with pytest.raises(StaleEmbeddingsError):
        manager.similar_to_vector([1.0] * 64, "fake-embed")
    assert not settings.derived_dir.exists() or not any(settings.derived_dir.rglob(MANIFEST_FILE))


def test_mixed_dimensions_raise_before_the_index(
    manager: ConceptEmbeddingManager, session: Session
) -> None:
    bulk_concepts(session, np.eye(3, dtype=np.float32))
    bulk_concepts(session, np.eye(4, dtype=np.float32))
    with pytest.raises(EmbeddingIndexError, match=r"mixed dimensions \[3, 4\]"):
        manager.similar_to_vector([1.0, 0.0, 0.0], MODEL)
    assert manager.check_index(MODEL)[0].code == "mixed_dimensions"


# -- the files --------------------------------------------------------------------------------


@pytest.fixture
def built(manager: ConceptEmbeddingManager, session: Session, settings: Settings) -> Path:
    bulk_concepts(session, clustered(np.random.default_rng(5), 120, 16))
    session.commit()
    manager.similar_to_vector([1.0] * 16, MODEL)  # first use builds and saves it
    session.commit()
    vector_index.clear_cache()  # from here on, as a new process
    return index_dir(settings, MODEL, 16)


def index_file(folder: Path) -> Path:
    return folder / json.loads((folder / MANIFEST_FILE).read_text())["index_file"]


def test_first_use_builds_and_saves_the_index(built: Path, settings: Settings) -> None:
    manifest = json.loads((built / MANIFEST_FILE).read_text())
    assert built.parent == settings.derived_dir / "_vector_index" / "concept_embeddings"
    assert built.name.startswith("bench-embed-") and built.name.endswith("-16d")
    assert manifest["library"] == "usearch"
    assert manifest["library_version"] == vector_index.LIBRARY_VERSION
    assert (manifest["model"], manifest["dimensions"], manifest["count"]) == (MODEL, 16, 120)
    assert len(manifest["entries"]) == 120
    assert sorted(p.name for p in built.iterdir() if p.suffix == ".usearch") == [
        manifest["index_file"]
    ]


def test_a_new_process_loads_the_saved_index(
    built: Path, manager: ConceptEmbeddingManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    added: list[int] = []
    original = VectorIndex.apply
    monkeypatch.setattr(
        VectorIndex, "apply", lambda self, r, a: added.append(len(a)) or original(self, r, a)
    )
    assert len(manager.similar_to_vector([1.0] * 16, MODEL)) == 10
    assert added == []  # nothing to reconcile: no vector was loaded from the table


@pytest.mark.parametrize(
    ("damage", "fragment"),
    [
        (lambda f: (f / MANIFEST_FILE).write_text("{oops"), "manifest.json is unreadable"),
        (lambda f: (f / MANIFEST_FILE).write_text("[]"), "is not a JSON object"),
        (lambda f: index_file(f).unlink(), r"index-[0-9a-f]{12}\.usearch named by .* is missing"),
        (lambda f: index_file(f).write_bytes(b"x" * 64), "does not match the size and SHA-256"),
        (
            lambda f: index_file(f).write_bytes(
                (lambda b: b[:200] + bytes(64) + b[264:])(index_file(f).read_bytes())
            ),
            "does not match the size and SHA-256",
        ),
    ],
)
def test_damaged_files_are_loud(
    built: Path, manager: ConceptEmbeddingManager, damage, fragment: str
) -> None:
    damage(built)
    with pytest.raises(VectorIndexError, match=fragment) as caught:
        manager.similar_to_vector([1.0] * 16, MODEL)
    assert f"`{REBUILD_COMMAND}`" in str(caught.value)
    [issue] = manager.check_index(MODEL)
    assert issue.code == "index_unusable" and REBUILD_COMMAND in issue.detail


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("library_version", "0.0.1"),
        ("format_version", 99),
        ("model", "another-model"),
        ("dimensions", 17),
        ("scalar", "f32"),
    ],
)
def test_a_mismatched_manifest_is_loud(
    built: Path, manager: ConceptEmbeddingManager, key: str, value: object
) -> None:
    manifest = json.loads((built / MANIFEST_FILE).read_text())
    manifest[key] = value
    (built / MANIFEST_FILE).write_text(json.dumps(manifest))
    with pytest.raises(VectorIndexError, match=rf"has {key} {value!r}, expected .*`{REBUILD_COMMAND}`"):
        manager.similar_to_vector([1.0] * 16, MODEL)


def test_entries_that_disagree_with_the_index_file_are_loud(
    built: Path, manager: ConceptEmbeddingManager
) -> None:
    manifest = json.loads((built / MANIFEST_FILE).read_text())
    manifest["entries"].popitem()
    (built / MANIFEST_FILE).write_text(json.dumps(manifest))
    with pytest.raises(VectorIndexError, match="holds 120 vectors but .* lists 119"):
        manager.similar_to_vector([1.0] * 16, MODEL)


def test_a_missing_manifest_means_not_built(built: Path, manager: ConceptEmbeddingManager) -> None:
    old = index_file(built)
    (built / MANIFEST_FILE).unlink()
    assert len(manager.similar_to_vector([1.0] * 16, MODEL)) == 10  # built again
    assert (built / MANIFEST_FILE).exists()
    assert [p.name for p in built.glob("*.usearch")] == [index_file(built).name]
    assert old.name == index_file(built).name or not old.exists()


def test_rebuild_ignores_damaged_files(
    built: Path, manager: ConceptEmbeddingManager, session: Session
) -> None:
    (built / MANIFEST_FILE).write_text("{oops")
    done = manager.rebuild_index(MODEL)
    assert (done.dimensions, done.added, done.total, done.directory) == (16, 120, 120, built)
    assert manager.check_index(MODEL) == []
    vector_index.clear_cache()  # the rebuilt files load cleanly in a new process
    query = [1.0] * 16
    approx = manager.similar_to_vector(query, MODEL)
    exact = manager.similar_to_vector(query, MODEL, exact=True)
    assert len(approx) == 10
    assert [c.id for c, _ in approx][:5] == [c.id for c, _ in exact][:5]


def test_check_reports_drift_and_sync_writes_it_away(
    built: Path, manager: ConceptEmbeddingManager, session: Session, settings: Settings
) -> None:
    assert manager.check_index(MODEL) == []
    concepts = ConceptManager(session, settings)
    [new_id] = bulk_concepts(session, np.ones((1, 16), dtype=np.float32))
    first, second = list(session.scalars(select(Concept).order_by(Concept.id).limit(2)))
    session.execute(  # another vector for the same text: a new embedded_at
        text("UPDATE concept_embeddings SET embedded_at = '2030-01-01 00:00:00' WHERE concept_id = :i"),
        {"i": first.id},
    )
    second_id = second.id
    concepts.delete(second)
    session.commit()
    manager.similar_to_vector([1.0] * 16, MODEL)  # reconciled in memory, too few changes to save
    issues = {(issue.code, issue.slug) for issue in manager.check_index(MODEL)}
    assert issues == {
        ("missing_from_index", f"c-{new_id:06d}"),
        ("stale_in_index", first.slug),
        ("orphan_in_index", f"#{second_id}"),
    }
    done = manager.sync_index(MODEL)
    assert (done.added, done.removed, done.total) == (0, 0, 120)  # memory already in sync
    assert manager.check_index(MODEL) == []
    vector_index.clear_cache()
    assert manager.sync_index(MODEL).added == 0  # the saved file is current


def test_check_without_a_saved_index(manager: ConceptEmbeddingManager, session: Session) -> None:
    assert manager.check_index(MODEL) == []  # no vectors, nothing to index
    bulk_concepts(session, np.eye(3, dtype=np.float32))
    [issue] = manager.check_index(MODEL)
    assert issue.code == "index_missing" and "teacher concepts reindex" in issue.detail


def test_saving_waits_for_enough_changes(
    built: Path, manager: ConceptEmbeddingManager, session: Session
) -> None:
    saved_at = json.loads((built / MANIFEST_FILE).read_text())["saved_at"]
    bulk_concepts(session, clustered(np.random.default_rng(9), 3, 16))
    session.commit()
    manager.similar_to_vector([1.0] * 16, MODEL)
    assert json.loads((built / MANIFEST_FILE).read_text())["saved_at"] == saved_at
    bulk_concepts(session, clustered(np.random.default_rng(9), vector_index.SAVE_MIN_CHANGES, 16))
    session.commit()
    manager.similar_to_vector([1.0] * 16, MODEL)
    manifest = json.loads((built / MANIFEST_FILE).read_text())
    assert manifest["saved_at"] != saved_at and manifest["count"] == 123 + vector_index.SAVE_MIN_CHANGES


def test_a_zero_vector_in_the_table_is_loud(
    manager: ConceptEmbeddingManager, session: Session
) -> None:
    vectors = np.eye(4, dtype=np.float32)
    vectors[2] = 0.0
    bulk_concepts(session, vectors)
    with pytest.raises(EmbeddingIndexError, match="has length 0.0, so it has no direction"):
        manager.similar_to_vector([1.0, 0, 0, 0], MODEL)


# -- concurrency ------------------------------------------------------------------------------


def test_concurrent_queries_and_writes(settings: Settings, fake_embedder) -> None:
    """Readers compare the index's answer with the exact one in the same transaction while
    writers commit and roll back embeddings; every answer must be exact (a small graph is
    ranked exactly, so any race in reconciliation would show as a difference)."""
    rng = np.random.default_rng(11)
    with session_scope(settings) as session:
        bulk_concepts(session, rng.standard_normal((30, 8)).astype(np.float32))
    factory = session_factory(settings)
    errors: list[BaseException] = []
    stop = threading.Event()

    def reader(seed: int) -> None:
        local = np.random.default_rng(seed)
        try:
            for _ in range(30):
                with factory() as session:
                    manager = ConceptEmbeddingManager(session, settings)
                    probe = local.standard_normal(8).tolist()
                    approx = manager.similar_to_vector(probe, MODEL, 5)
                    exact = manager.similar_to_vector(probe, MODEL, 5, exact=True)
                    assert [(c.id, s) for c, s in approx] == [(c.id, s) for c, s in exact]
                    session.rollback()
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    def writer() -> None:
        # One writer (SQLite has one): it alternately commits and rolls back, and queries
        # inside each transaction, which pulls its uncommitted view into the shared index.
        # Each transaction re-embeds 3 concepts (new vectors, same text: only `embedded_at`
        # tells), adds one and deletes one, so the graph stays small enough to be exact.
        local = np.random.default_rng(99)
        commit = False
        try:
            while not stop.is_set():
                commit = not commit
                with factory() as session:
                    ids = list(session.scalars(select(Concept.id).order_by(Concept.id)))
                    for concept_id in local.choice(ids, 3, replace=False).tolist():
                        session.execute(
                            text(
                                "UPDATE concept_embeddings SET vector = :v, embedded_at = :t "
                                "WHERE concept_id = :i"
                            ),
                            {
                                "v": encode_vector(local.standard_normal(8).tolist()),
                                "t": utcnow().isoformat(" "),
                                "i": concept_id,
                            },
                        )
                    bulk_concepts(session, local.standard_normal((1, 8)).astype(np.float32))
                    session.execute(text("DELETE FROM concepts WHERE id = :i"), {"i": ids[0]})
                    ConceptEmbeddingManager(session, settings).similar_to_vector(
                        [1.0] * 8, MODEL, 3
                    )
                    session.commit() if commit else session.rollback()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    readers = [threading.Thread(target=reader, args=(i,)) for i in range(4)]
    writers = [threading.Thread(target=writer)]
    for thread in readers + writers:
        thread.start()
    for thread in readers:
        thread.join(120)
    stop.set()
    for thread in writers:
        thread.join(120)
    assert errors == []
    with session_scope(settings) as session:
        manager = ConceptEmbeddingManager(session, settings)
        manager.similar_to_vector([1.0] * 8, MODEL)
        assert {k for k in held(settings, MODEL, 8)} == set(table(session, MODEL))
