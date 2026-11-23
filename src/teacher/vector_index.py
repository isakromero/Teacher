"""An approximate nearest-neighbour index (HNSW, `usearch`) of the concept embeddings of one
embedding model and dimensionality, kept on disk under `Settings.derived_dir`.

The `concept_embeddings` table is the only source of truth: this index is derived data that
`ConceptEmbeddingManager` reconciles with the table before every query and can always rebuild
from it (`teacher concepts reindex --rebuild-index`). This module knows nothing about the
database; it holds vectors by integer key (the concept id) and, per key, the `Entry` (the
row's `text_hash` and `embedded_at`) of the vector it holds, which is what reconciliation
compares with the table.

Files, in `derived/_vector_index/concept_embeddings/<model>-<hash8>-<dims>d/` (a leading `_`
is never part of a source slug, so the folder cannot collide with `derived/<source-slug>/`):

- `manifest.json`: format and library versions, model, dimensions, metric, scalar type, HNSW
  parameters, the name, size and SHA-256 of the index file, and `entries` (concept id ->
  `[text_hash, embedded_at]`).
- `index-<sha12>.usearch`: the usearch index (named by its own checksum).
- `.lock`: taken with `flock`, shared to load and exclusive to save, so processes never see a
  half-written pair.

A save writes the new index file under its new name, then atomically replaces the manifest,
then deletes index files the manifest no longer names; a crash at any point leaves the old
consistent pair (or an orphan file that the next save deletes). No manifest at all means the
index was never built: `VectorIndex.open` returns a new, empty index, which reconciliation
fills (first use builds the index; this is construction, not a fallback). Anything else that is
wrong -- a manifest that is unreadable, of another format, library version, model,
dimensionality, metric or scalar type, an index file that is missing, fails its checksum,
cannot be read, or holds other keys than the manifest lists -- is a `VectorIndexFileError`
naming the problem. The exact usearch version is required: a library upgrade means a rebuild.

`cached(settings, model, dimensions)` keeps one opened index per (database, model,
dimensions) in the process. Each `VectorIndex` has an `RLock` (`lock`); its callers hold it
across reconcile + search, so concurrent threads never see a half-applied change.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import threading
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, NamedTuple

import numpy as np
import usearch
from usearch.index import Index, MetricKind, ScalarKind

from teacher.config import Settings
from teacher.db.models import utcnow

LIBRARY = "usearch"
LIBRARY_VERSION: str = usearch.__version__
FORMAT = "teacher-concept-vector-index"
FORMAT_VERSION = 1

# HNSW parameters, chosen on 1536-d synthetic data (see the tests and the module report):
# M=32 / ef_construction=200 keeps recall@10 >= 0.99 on clustered vectors up to 20,000;
# ef_search=128 (raised to the number of candidates asked for) costs ~1-4 ms a query.
METRIC = "cos"
METRIC_KIND = MetricKind.Cos
# Vectors are held as float16: half the memory of float32. Callers re-score the candidates
# exactly from the stored float32 vectors, so this only affects which candidates are found.
SCALAR = "f16"
SCALAR_KIND = ScalarKind.F16
CONNECTIVITY = 32
EXPANSION_ADD = 200
EXPANSION_SEARCH = 128
# Inserting many vectors concurrently into a small graph links it poorly (recall@10 0.90-0.98
# at 200-2,000 vectors, and not deterministic); one thread gives 1.0. So vectors are inserted
# on one thread while the graph holds at most SINGLE_THREADED_UP_TO, on all cores after that.
SINGLE_THREADED_UP_TO = 2048

MANIFEST_FILE = "manifest.json"
LOCK_FILE = ".lock"
INDEX_FILE_PATTERN = re.compile(r"^index-[0-9a-f]{12}\.usearch$")
INDEX_ROOT = Path("_vector_index") / "concept_embeddings"

# Persisting: after a reconcile, the in-memory index is saved when it was never saved, or once
# the changes not yet saved reach max(SAVE_MIN_CHANGES, SAVE_CHANGE_FRACTION * size). The file
# only makes start-up cheap: every process reconciles with the table before using it.
SAVE_MIN_CHANGES = 64
SAVE_CHANGE_FRACTION = 0.05

REBUILD_BATCH = 2048  # vectors loaded and added at a time by a rebuild
_CHUNK = 1 << 20  # bytes per read when hashing


class VectorIndexFileError(Exception):
    """The on-disk index cannot be used (missing file, unreadable, corrupt, or mismatched)."""


class Entry(NamedTuple):
    """What a vector in the index was made from: its row's `text_hash` and `embedded_at`
    (ISO format). A vector whose entry differs from the table's row is replaced. (A tuple, so
    comparing two indexes' entries is one C-level dict comparison.)"""

    text_hash: str
    embedded_at: str


def index_dir(settings: Settings, model: str, dimensions: int) -> Path:
    """The folder of the index for `model` at `dimensions` (see the module docstring)."""
    readable = re.sub(r"[^A-Za-z0-9.]+", "-", model).strip("-.")[:60] or "model"
    digest = hashlib.sha256(model.encode("utf-8")).hexdigest()[:8]
    return settings.derived_dir / INDEX_ROOT / f"{readable}-{digest}-{dimensions}d"


@contextmanager
def _flock(directory: Path, *, exclusive: bool) -> Iterator[None]:
    directory.mkdir(parents=True, exist_ok=True)
    fd = os.open(directory / LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        yield
    finally:
        os.close(fd)  # releases the lock


def _sha256(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _fsync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _new_usearch(dimensions: int) -> Index:
    return Index(
        ndim=dimensions,
        metric=METRIC,
        dtype=SCALAR,
        connectivity=CONNECTIVITY,
        expansion_add=EXPANSION_ADD,
        expansion_search=EXPANSION_SEARCH,
    )


def _keys(index: Index) -> set[int]:
    # One bulk export: iterating `index.keys` exports one key per call, each a scan (O(n^2)).
    return set(np.asarray(index.keys[:], dtype=np.uint64).tolist())


class VectorIndex:
    """The index of one model + dimensionality: usearch vectors by concept id, and the `Entry`
    of each. Not thread-safe by itself: hold `lock` around every use (it is reentrant)."""

    def __init__(self, directory: Path, model: str, dimensions: int) -> None:
        """A new, empty index for `directory` (nothing is read or written)."""
        if isinstance(dimensions, bool) or not isinstance(dimensions, int) or dimensions < 1:
            raise ValueError(f"dimensions must be a positive integer, got {dimensions!r}")
        self.directory = directory
        self.model = model
        self.dimensions = dimensions
        self.lock = threading.RLock()
        self._index = _new_usearch(dimensions)
        self._entries: dict[int, Entry] = {}
        self.saved = False  # does the directory hold a manifest written from this index?
        self.unsaved_changes = 0
        self.poisoned = False  # an update failed half-way: never use this instance again

    # -- loading ----------------------------------------------------------------------------

    @property
    def manifest_path(self) -> Path:
        return self.directory / MANIFEST_FILE

    @classmethod
    def open(cls, directory: Path, model: str, dimensions: int) -> VectorIndex:
        """The index saved in `directory`, or a new empty one if it has no manifest (it was
        never built). VectorIndexFileError if what is there cannot be used."""
        loaded = cls.load(directory, model, dimensions)
        return loaded if loaded is not None else cls(directory, model, dimensions)

    @classmethod
    def load(cls, directory: Path, model: str, dimensions: int) -> VectorIndex | None:
        """The index saved in `directory`; None if there is no manifest."""
        if not (directory / MANIFEST_FILE).exists():
            return None
        self = cls(directory, model, dimensions)
        with _flock(directory, exclusive=False):
            manifest = self._read_manifest()
            index_path = directory / manifest["index_file"]
            if not index_path.is_file():
                raise VectorIndexFileError(
                    f"the vector index file {index_path} named by {self.manifest_path} is missing"
                )
            digest, size = _sha256(index_path)
            if (digest, size) != (manifest["index_sha256"], manifest["index_bytes"]):
                raise VectorIndexFileError(
                    f"the vector index file {index_path} does not match the size and SHA-256 "
                    f"recorded in {self.manifest_path}: it is corrupt or was changed"
                )
            try:
                index = Index.restore(str(index_path))
            except Exception as exc:  # usearch raises ValueError/RuntimeError
                raise VectorIndexFileError(
                    f"usearch could not read the vector index file {index_path}: {exc}"
                ) from exc
        if index is None:
            raise VectorIndexFileError(f"{index_path} is not a usearch index")
        found = (index.ndim, index.dtype, index.metric_kind)
        if found != (dimensions, SCALAR_KIND, METRIC_KIND):
            raise VectorIndexFileError(
                f"the vector index file {index_path} holds {index.ndim}-dimensional "
                f"{index.dtype} vectors by {index.metric_kind}, expected {dimensions}-dimensional "
                f"{SCALAR_KIND} by {METRIC_KIND}"
            )
        entries = {int(key): Entry(*value) for key, value in manifest["entries"].items()}
        keys = _keys(index)
        if keys != set(entries):
            raise VectorIndexFileError(
                f"the vector index file {index_path} holds {len(keys)} vectors but "
                f"{self.manifest_path} lists {len(entries)}, and their concept ids differ"
            )
        index.expansion_search = EXPANSION_SEARCH  # not stored in the file
        self._index = index
        self._entries = entries
        self.saved = True
        return self

    def _read_manifest(self) -> dict[str, Any]:
        path = self.manifest_path
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise VectorIndexFileError(
                f"the vector index manifest {path} is unreadable: {exc}"
            ) from exc
        expected = {
            "format": FORMAT,
            "format_version": FORMAT_VERSION,
            "library": LIBRARY,
            "library_version": LIBRARY_VERSION,
            "model": self.model,
            "dimensions": self.dimensions,
            "metric": METRIC,
            "scalar": SCALAR,
        }
        if not isinstance(manifest, dict):
            raise VectorIndexFileError(f"the vector index manifest {path} is not a JSON object")
        for key, value in expected.items():
            if manifest.get(key) != value:
                raise VectorIndexFileError(
                    f"the vector index manifest {path} has {key} {manifest.get(key)!r}, "
                    f"expected {value!r}"
                )
        try:
            ok = (
                isinstance(manifest["index_file"], str)
                and INDEX_FILE_PATTERN.match(manifest["index_file"]) is not None
                and isinstance(manifest["index_sha256"], str)
                and isinstance(manifest["index_bytes"], int)
                and isinstance(manifest["entries"], dict)
                and all(
                    key.isdigit()
                    and isinstance(value, list)
                    and len(value) == 2
                    and all(isinstance(v, str) for v in value)
                    for key, value in manifest["entries"].items()
                )
            )
        except KeyError as exc:
            raise VectorIndexFileError(f"the vector index manifest {path} lacks {exc}") from exc
        if not ok:
            raise VectorIndexFileError(f"the vector index manifest {path} is malformed")
        return manifest

    # -- contents ---------------------------------------------------------------------------

    def __len__(self) -> int:
        return len(self._entries)

    @property
    def entries(self) -> Mapping[int, Entry]:
        """Concept id -> Entry of the vector held (read-only view; hold `lock`)."""
        return self._entries

    def apply(
        self, remove: Sequence[int], add: Mapping[int, tuple[Entry, np.ndarray]]
    ) -> None:
        """Remove the vectors of `remove`, then add `add` (id -> (entry, unit vector)). An id
        to replace must be in `remove`. The vectors must be finite and `dimensions` long."""
        missing = [key for key in remove if key not in self._entries]
        if missing:
            raise KeyError(f"the vector index holds no vector for concept ids {missing[:5]}")
        removing = set(remove)
        if len(removing) != len(remove):
            raise ValueError("concept ids to remove must be distinct")
        present = [key for key in add if key in self._entries and key not in removing]
        if present:
            raise KeyError(f"the vector index already holds concept ids {present[:5]}")
        matrix = None
        if add:
            matrix = np.vstack([vector for _entry, vector in add.values()]).astype(np.float32)
            if matrix.shape[1] != self.dimensions or not np.isfinite(matrix).all():
                raise ValueError(
                    f"vectors to add must be finite and {self.dimensions}-dimensional"
                )
        try:
            if remove:
                removed = self._index.remove(np.asarray(remove, dtype=np.uint64))
                if removed != len(remove):
                    raise VectorIndexFileError(
                        f"usearch removed {removed} of {len(remove)} vectors"
                    )
                for key in remove:
                    del self._entries[key]
            if matrix is not None:
                keys = np.fromiter(add, dtype=np.uint64, count=len(add))
                seed = max(0, min(len(keys), SINGLE_THREADED_UP_TO - len(self._index)))
                if seed:
                    self._index.add(keys[:seed], matrix[:seed], threads=1)
                if seed < len(keys):
                    self._index.add(keys[seed:], matrix[seed:], threads=0)
                for key, (entry, _vector) in add.items():
                    self._entries[key] = entry
        except BaseException:
            self.poisoned = True
            raise
        self.unsaved_changes += len(remove) + len(add)

    def search(self, query: np.ndarray, count: int) -> list[int]:
        """Up to `count` ids nearest to `query` by the HNSW graph search (approximate; it may
        return fewer than `count` when `count` is close to the size)."""
        if len(self) == 0 or count < 1:
            return []
        count = min(count, len(self))
        self._index.expansion_search = max(EXPANSION_SEARCH, count)
        matches = self._index.search(np.asarray(query, dtype=np.float32), count, threads=1)
        return [int(key) for key in matches.keys]

    # -- saving -----------------------------------------------------------------------------

    def should_save(self) -> bool:
        if not self._entries:
            return False
        if not self.saved:
            return True
        threshold = max(SAVE_MIN_CHANGES, int(len(self) * SAVE_CHANGE_FRACTION))
        return self.unsaved_changes >= threshold

    def save(self) -> None:
        """Write the index and its manifest (see the module docstring for the order)."""
        with self.lock, _flock(self.directory, exclusive=True):
            token = f"{os.getpid()}-{threading.get_ident()}"
            tmp_index = self.directory / f".index-{token}.tmp"
            tmp_manifest = self.directory / f".manifest-{token}.tmp"
            try:
                self._index.save(str(tmp_index))
                digest, size = _sha256(tmp_index)
                name = f"index-{digest[:12]}.usearch"
                manifest = {
                    "format": FORMAT,
                    "format_version": FORMAT_VERSION,
                    "library": LIBRARY,
                    "library_version": LIBRARY_VERSION,
                    "model": self.model,
                    "dimensions": self.dimensions,
                    "metric": METRIC,
                    "scalar": SCALAR,
                    "connectivity": CONNECTIVITY,
                    "expansion_add": EXPANSION_ADD,
                    "count": len(self._entries),
                    "saved_at": utcnow().isoformat(),
                    "index_file": name,
                    "index_sha256": digest,
                    "index_bytes": size,
                    "entries": {
                        str(key): [entry.text_hash, entry.embedded_at]
                        for key, entry in sorted(self._entries.items())
                    },
                }
                with tmp_manifest.open("w", encoding="utf-8") as handle:
                    json.dump(manifest, handle, separators=(",", ":"))
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp_index, self.directory / name)
                _fsync_dir(self.directory)
                os.replace(tmp_manifest, self.manifest_path)
                _fsync_dir(self.directory)
            finally:
                tmp_index.unlink(missing_ok=True)
                tmp_manifest.unlink(missing_ok=True)
            for path in self.directory.iterdir():
                if INDEX_FILE_PATTERN.match(path.name) and path.name != name:
                    path.unlink()
            self.saved = True
            self.unsaved_changes = 0

    def memory_bytes(self) -> int:
        """usearch's own estimate of the memory it uses."""
        return int(self._index.memory_usage)


# -- the per-process cache ------------------------------------------------------------------

_cache_lock = threading.Lock()
_cache: dict[tuple[str, str, int], VectorIndex] = {}


def cached(settings: Settings, model: str, dimensions: int) -> VectorIndex:
    """The process's opened index for (database, model, dimensions), opening it on first use.
    Opening another (model, dimensions) for the same database drops the old one from memory.
    An index whose update failed half-way is dropped and opened again from disk."""
    key = (str(settings.db_path), model, dimensions)
    with _cache_lock:
        index = _cache.get(key)
        if index is None or index.poisoned:
            _cache.pop(key, None)
            index = VectorIndex.open(index_dir(settings, model, dimensions), model, dimensions)
            for other in [k for k in _cache if k[0] == key[0]]:
                del _cache[other]
            _cache[key] = index
        return index


def install(settings: Settings, index: VectorIndex) -> None:
    """Make `index` the cached index for its (database, model, dimensions) (after a rebuild)."""
    key = (str(settings.db_path), index.model, index.dimensions)
    with _cache_lock:
        for other in [k for k in _cache if k[0] == key[0]]:
            del _cache[other]
        _cache[key] = index


def clear_cache() -> None:
    """Forget every opened index (the next use reads the files again, like a new process)."""
    with _cache_lock:
        _cache.clear()


__all__ = [
    "CONNECTIVITY",
    "EXPANSION_ADD",
    "EXPANSION_SEARCH",
    "LIBRARY_VERSION",
    "Entry",
    "VectorIndex",
    "VectorIndexFileError",
    "cached",
    "clear_cache",
    "index_dir",
    "install",
]
