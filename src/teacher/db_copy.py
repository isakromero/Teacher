"""Copying everything from one database to the other: `teacher db copy --from X --to Y`.

The two databases are the local SQLite file and Supabase's Postgres (`teacher.db.providers`);
both live under the same `$TEACHER_HOME` and differ in provider and in `Settings.data_root`
(`root` for SQLite, `root/supabase` for Supabase). The copy seeds production from the local
data, or pulls production down into a local database to test against. `copy_database(source,
target)` does the work and reports each step through `out`; `plan_copy` is the dry run.

Preconditions, each a `DbCopyError` before anything is changed (nothing is ever copied in part):

1. Source and target are different databases (`cache_key`), and none of the copied folders of
   one lies inside one of the other's.
2. The source exists and is at the head revision. It is never migrated here: the error says how
   to upgrade it. Its schema has every column the models name, and no table this command does
   not know (anything but the models' tables, `alembic_version` and the concept search tables
   would be lost). A SQLite source must pass `PRAGMA foreign_key_check`. Every source's and
   artifact's folder (`dir_path`) must exist under the source's data root (a database whose
   files are on another machine is refused, not copied without them).
3. The target's `knowledge_sources/` and `artifacts/` (under its data root) are absent or
   empty, and its `derived/` holds nothing but `pyodide/` (the lab runtime).
4. The target is migrated to head (a SQLite file is created if needed; the dry run only says
   so) and then has no row in any application table; otherwise the error names the tables that
   have rows.

Then, in this order:

1. Files: `knowledge_sources/`, `artifacts/` and `derived/` (but see below) are copied from the
   source's data root to the target's, keeping modification times (the cost backfill dates the
   run records by them), and every copied file is compared with its original (size and
   SHA-256). `backups/` is never copied.
2. Rows: every table of `Base.metadata` except the concept search tables, in foreign-key
   dependency order, with SQLAlchemy Core select/insert in batches of `BATCH_ROWS`, primary
   keys preserved, so the dialects convert the types (JSON, booleans, naive-UTC datetimes,
   LargeBinary). JSON columns are read with an `IS NULL` flag and written back with `null()`
   (SQL NULL) or `JSON.NULL` (the JSON value null), so the two stay apart in both directions.
   The source is read in one snapshot (a SQLite read transaction; on Postgres REPEATABLE READ,
   READ ONLY); the target is written in one transaction. On Postgres both transactions start
   with `SET LOCAL extra_float_digits = 3`, so a float8 is read with all its digits whatever the
   server's setting (at 0, 0.30000000000000004 reads as 0.3). Self-referencing foreign keys
   (`artifacts.supersedes_id`, `source_segments.parent_id`) and any foreign key in a cycle are
   inserted as NULL and set once every row is in (they must be nullable, or the copy refuses),
   so the order of the rows does not matter. `alembic_version` is not copied (the migration
   wrote the target's).
3. Postgres target: every sequence owned by a copied table's column is set to the column's
   max (`setval(..., max, true)`; an empty table restarts at 1), so the next insert does not
   collide; a sequence owned by anything else is an error. Postgres refuses NUL characters in
   text: every text value holding one is reported (table, column, row key) as a DbCopyError
   instead of the driver's error. SQLite target: no Teacher table uses AUTOINCREMENT; if one
   ever does, `sqlite_sequence` (which SQLite raises to an explicit id itself) is checked to be
   at least each table's max id.
4. Verification, in the target's transaction: per table the row count (`SELECT count(*)`) and
   a content digest must equal the source's. The digest covers every column of every row: the
   SHA-256 of each row's type-tagged canonical values (SQL NULL, JSON text with its key order,
   bool, int, float as hex, text, bytes, datetime in ISO form with microseconds and any time
   zone), summed over the rows so the order rows come back in does not matter. A type-conversion
   bug (a lost microsecond, a JSON null turned into SQL NULL, a boolean read as an integer, a
   changed byte) changes it. Any mismatch is a DbCopyError: the target's transaction is rolled
   back and the copied files are removed, so the target is as empty as before.
5. Commit, then a separate final step: the concept search index is rebuilt on the target
   (`ConceptManager.rebuild_search_index()`, then `check()` must find nothing) and the vector
   index of each embedding model is rebuilt from `concept_embeddings`
   (`ConceptEmbeddingManager.rebuild_index`). Its failure is a DbCopyError that says the copy
   itself is committed and verified, and how to retry (`teacher db rebuild-search`, `teacher
   concepts reindex --rebuild-index`).

`derived/`, per entry: `_vector_index/` is not copied but rebuilt (step 5: it is derived from
`concept_embeddings`, keyed by concept id, and reconciled with the table anyway); `pyodide/` is
not copied (the pinned lab runtime, not data: `teacher labs setup` installs it); everything else
is copied: the command line's run records (`_concept_relations/`, `_block_concepts/`,
`<source-slug>/concepts.json`), which cannot be rebuilt and which the cost backfill reads.

Files are copied as they are while the database is read in a snapshot: do not use the app on
either database while copying. Databases are named by the provider's `describe()` only: a
Supabase connection string is never printed or logged.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from datetime import time as dt_time
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import (
    JSON,
    Column,
    Connection,
    Engine,
    ForeignKeyConstraint,
    String,
    Table,
    bindparam,
    func,
    inspect,
    null,
    select,
    text,
    update,
)
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import sort_tables_and_constraints

from teacher.config import Settings
from teacher.db import models  # noqa: F401  (registers every table on Base.metadata)
from teacher.db.base import Base
from teacher.db.fts import is_concept_search_table
from teacher.db.migrate import current_revision, head_revision, upgrade_to_head
from teacher.db.providers import DbProvider, create_db_provider

BATCH_ROWS = 500
#: Folders under the data root copied as a whole; the target's must be absent or empty.
COPIED_FOLDERS = ("knowledge_sources", "artifacts")
DERIVED = "derived"
#: Entries of `derived/` that are not copied, and why (see the module docstring).
DERIVED_REBUILT = {"_vector_index": "rebuilt from concept_embeddings after the copy"}
DERIVED_NOT_COPIED = {"pyodide": "the lab runtime, not data: run `teacher labs setup` for the target"}
#: What the target's `derived/` may already hold.
DERIVED_ALLOWED_IN_TARGET = frozenset({"pyodide"})
NOT_COPIED_TABLES = ("alembic_version",)
NUL_REPORT_LIMIT = 20

Out = Callable[[str], None]


class DbCopyError(RuntimeError):
    """The copy cannot be done or failed; the message says why (never with a secret)."""


# -- the plan --------------------------------------------------------------------------------


@dataclass(frozen=True)
class FolderEntry:
    """A file tree under a data root: `path` relative to it, what happens to it, its size."""

    path: str
    action: str  # "copy", "rebuild" or "skip"
    files: int
    bytes: int
    reason: str = ""


@dataclass
class CopyPlan:
    source: str  # describe()
    target: str
    target_state: str  # what the target database is / will be before rows go in
    tables: list[tuple[str, int]]  # (table, source rows), in copy order
    folders: list[FolderEntry]
    deferred: list[str]  # "table.column" of the foreign keys set after the inserts

    def lines(self) -> list[str]:
        width = max([len(name) for name, _ in self.tables] + [5])
        out = [
            f"Source: {self.source}",
            f"Target: {self.target} ({self.target_state})",
            f"Tables to copy ({len(self.tables)}, in this order):",
        ]
        out += [f"  {name:<{width}}  {rows:>9}" for name, rows in self.tables]
        out.append(f"  {'total':<{width}}  {sum(rows for _, rows in self.tables):>9}")
        if self.deferred:
            out.append(
                "Set after the inserts (self-referencing foreign keys): "
                + ", ".join(self.deferred)
            )
        out.append("Files:")
        for entry in self.folders:
            what = {"copy": "copy", "rebuild": "rebuild", "skip": "not copied"}[entry.action]
            reason = f" ({entry.reason})" if entry.reason else ""
            out.append(
                f"  {entry.path + '/':<32} {what:<10} {entry.files:>6} file(s) "
                f"{_size(entry.bytes):>9}{reason}"
            )
        if not any(entry.path.startswith(f"{DERIVED}/") for entry in self.folders):
            out.append(f"  {DERIVED + '/':<32} empty or absent: nothing to copy")
        out.append("Not copied: backups/, alembic_version, the concept search tables (rebuilt).")
        return out


@dataclass
class TableResult:
    name: str
    source_rows: int
    target_rows: int
    source_digest: str
    target_digest: str

    @property
    def ok(self) -> bool:
        return (self.source_rows, self.source_digest) == (self.target_rows, self.target_digest)


@dataclass
class CopyResult:
    plan: CopyPlan
    tables: list[TableResult] = field(default_factory=list)
    files: int = 0
    bytes: int = 0
    sequences: list[str] = field(default_factory=list)
    search_concepts: int | None = None
    vector_indexes: list[str] = field(default_factory=list)
    seconds: dict[str, float] = field(default_factory=dict)


def verification_lines(tables: Sequence[TableResult]) -> list[str]:
    width = max([len(t.name) for t in tables] + [5])
    out = [f"  {'table':<{width}}  {'source':>9}  {'target':>9}  content"]
    for t in tables:
        content = "same" if t.source_digest == t.target_digest else "DIFFERS"
        out.append(f"  {t.name:<{width}}  {t.source_rows:>9}  {t.target_rows:>9}  {content}")
    out.append(
        f"  {'total':<{width}}  {sum(t.source_rows for t in tables):>9}  "
        f"{sum(t.target_rows for t in tables):>9}"
    )
    return out


# -- tables ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class _TableSpec:
    table: Table
    pk: tuple[Column[Any], ...]
    json: frozenset[str]  # column names
    text: tuple[str, ...]  # string columns that are not JSON
    deferred: tuple[str, ...]  # FK columns inserted as NULL and set afterwards


def copied_tables() -> list[Table]:
    """The application tables in foreign-key dependency order (parents first)."""
    return [t for t in Base.metadata.sorted_tables if not is_concept_search_table(t.name)]


def _table_specs() -> list[_TableSpec]:
    tables = copied_tables()
    # Foreign keys the dependency sort cannot satisfy: the ones in a cycle between tables
    # (left over by sort_tables_and_constraints) and every self-referencing one.
    deferred: set[ForeignKeyConstraint] = set()
    for table, leftovers in sort_tables_and_constraints(tables):
        if table is None:
            deferred.update(leftovers)
    for table in tables:
        deferred.update(fk for fk in table.foreign_key_constraints if fk.referred_table is table)
    specs = []
    for table in tables:
        pk = tuple(table.primary_key.columns)
        if not pk:
            raise DbCopyError(f"table {table.name} has no primary key; db copy cannot copy it")
        later = []
        for fk in table.foreign_key_constraints:
            if fk not in deferred:
                continue
            for column in fk.columns:
                if not column.nullable:
                    raise DbCopyError(
                        f"{table.name}.{column.name} is a self-referencing or cyclic foreign "
                        "key that is NOT NULL; db copy sets such keys after the inserts and "
                        "cannot copy this table"
                    )
                later.append(column.name)
        specs.append(
            _TableSpec(
                table=table,
                pk=pk,
                json=frozenset(c.name for c in table.c if isinstance(c.type, JSON)),
                text=tuple(
                    c.name
                    for c in table.c
                    if isinstance(c.type, String) and not isinstance(c.type, JSON)
                ),
                deferred=tuple(later),
            )
        )
    return specs


def _flag(name: str) -> str:
    return f"_dbcopy_null_{name}"


def _select(spec: _TableSpec) -> Any:
    """Every column, plus an `IS NULL` flag per JSON column (SQL NULL vs the JSON null)."""
    flags = [spec.table.c[name].is_(None).label(_flag(name)) for name in sorted(spec.json)]
    return select(*spec.table.c, *flags).order_by(*spec.pk)


def _canonical(value: Any, *, json_column: bool, sql_null: bool) -> bytes:
    """A value as type-tagged bytes for the content digest (see the module docstring)."""
    if json_column:
        if sql_null:
            return b"N"
        return b"J" + json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
    if value is None:
        return b"N"
    if isinstance(value, bool):
        return b"B1" if value else b"B0"
    if isinstance(value, int):
        return b"I" + str(value).encode()
    if isinstance(value, float):
        return b"F" + value.hex().encode()
    if isinstance(value, str):
        return b"S" + value.encode("utf-8", "surrogatepass")
    if isinstance(value, bytes | bytearray | memoryview):
        return b"X" + bytes(value)
    if isinstance(value, datetime | date | dt_time):
        return b"T" + type(value).__name__.encode() + value.isoformat().encode()
    if isinstance(value, Decimal):
        return b"D" + str(value).encode()
    raise DbCopyError(f"db copy cannot compare a value of type {type(value).__name__}")


def _row_digest(spec: _TableSpec, mapping: Any) -> int:
    h = hashlib.sha256()
    for column in spec.table.c:
        is_json = column.name in spec.json
        encoded = _canonical(
            mapping[column.name],
            json_column=is_json,
            sql_null=bool(mapping[_flag(column.name)]) if is_json else False,
        )
        h.update(column.name.encode())
        h.update(len(encoded).to_bytes(8, "big"))
        h.update(encoded)
    return int.from_bytes(h.digest(), "big")


class _Digest:
    """Order-independent digest of a table's rows: the row count and the sum of the rows'
    SHA-256 values modulo 2**256."""

    def __init__(self) -> None:
        self.rows = 0
        self.total = 0

    def add(self, value: int) -> None:
        self.rows += 1
        self.total = (self.total + value) % (1 << 256)

    @property
    def hex(self) -> str:
        return f"{self.total:064x}"


def _insert_params(spec: _TableSpec, mapping: Any) -> dict[str, Any]:
    """The insert parameters of a source row: JSON as `null()` (SQL NULL) or `JSON.NULL` (the
    JSON value null) or the value; deferred foreign keys as NULL."""
    params: dict[str, Any] = {}
    for column in spec.table.c:
        value = mapping[column.name]
        if column.name in spec.json:
            if mapping[_flag(column.name)]:
                value = null()
            elif value is None:
                value = JSON.NULL
        if column.name in spec.deferred:
            value = None
        params[column.name] = value
    return params


def _key(spec: _TableSpec, mapping: Any) -> str:
    return ", ".join(f"{c.name}={mapping[c.name]!r}" for c in spec.pk)


def _rows(conn: Connection, spec: _TableSpec) -> Iterator[Sequence[Any]]:
    result = conn.execution_options(yield_per=BATCH_ROWS).execute(_select(spec))
    for partition in result.partitions():
        yield [row._mapping for row in partition]


def _count(conn: Connection, table: Table) -> int:
    return int(conn.execute(select(func.count()).select_from(table)).scalar_one())


def _first_line(exc: BaseException) -> str:
    orig = getattr(exc, "orig", None)
    return (str(orig if orig is not None else exc).strip().splitlines() or [type(exc).__name__])[0]


# -- connections -----------------------------------------------------------------------------


def _engine(provider: DbProvider) -> Engine:
    return provider.create_engine(poolclass=NullPool)


def _snapshot(conn: Connection, provider: DbProvider) -> Connection:
    """`conn`, set up so one transaction reads one snapshot (see the module docstring)."""
    if provider.dialect == "postgresql":
        return conn.execution_options(isolation_level="REPEATABLE READ", postgresql_readonly=True)
    return conn  # SQLite: a read transaction sees one snapshot (BEGIN from the provider)


def _exact_floats(conn: Connection, provider: DbProvider) -> None:
    """Postgres: have this transaction send every float8 with all its digits. With the server
    setting `extra_float_digits` at 0 or below, 0.30000000000000004 is read as 0.3: the copy
    would lose precision, and the digest of the lossy read would not notice. SET LOCAL, so it
    also holds behind Supabase's transaction pooler; call it first in the transaction."""
    if provider.dialect == "postgresql":
        conn.exec_driver_sql("SET LOCAL extra_float_digits = 3")


# -- preconditions ---------------------------------------------------------------------------


def _check_pair(source: Settings, target: Settings) -> tuple[DbProvider, DbProvider]:
    src, dst = create_db_provider(source), create_db_provider(target)
    if src.cache_key == dst.cache_key:
        raise DbCopyError(
            f"source and target are the same database ({src.describe()}); copy between "
            "sqlite and supabase"
        )
    for a in _copied_roots(source):
        for b in _copied_roots(target):
            if a == b or a in b.parents or b in a.parents:
                raise DbCopyError(f"the folders {a} and {b} overlap; db copy needs separate ones")
    return src, dst


def _copied_roots(settings: Settings) -> list[Path]:
    return [settings.data_root / name for name in (*COPIED_FOLDERS, DERIVED)]


def _upgrade_hint(settings: Settings) -> str:
    return (
        f"upgrade it first, e.g. by starting `teacher --db {settings.db} serve` once (the "
        "server migrates on startup) or with any command that migrates, such as "
        f"`teacher --db {settings.db} costs backfill --dry-run`"
    )


def _check_source(source: Settings, provider: DbProvider) -> None:
    if not provider.database_exists():
        raise DbCopyError(f"the source database {provider.describe()} does not exist")
    current, head = current_revision(source), head_revision(source)
    if current is None:
        raise DbCopyError(
            f"the source database {provider.describe()} has no revision (it was never "
            f"migrated); {_upgrade_hint(source)}"
        )
    if current != head:
        raise DbCopyError(
            f"the source database {provider.describe()} is at revision {current}, not at the "
            f"head revision {head}; db copy never migrates the source: {_upgrade_hint(source)}"
        )


def _check_schema(conn: Connection, provider: DbProvider) -> None:
    """Every copied table exists with exactly the models' columns, and the database has no
    table this command does not know."""
    inspector = inspect(conn)
    names = set(inspector.get_table_names())
    known = {t.name for t in Base.metadata.sorted_tables}
    unknown = sorted(
        n
        for n in names - known - set(NOT_COPIED_TABLES)
        if not is_concept_search_table(n) and not n.startswith("sqlite_")
    )
    if unknown:
        raise DbCopyError(
            f"{provider.describe()} has table(s) db copy does not know: {', '.join(unknown)}; "
            "their rows would be lost, so nothing was copied"
        )
    for table in copied_tables():
        if table.name not in names:
            raise DbCopyError(f"{provider.describe()} has no table {table.name}")
        columns = {c["name"] for c in inspector.get_columns(table.name)}
        wanted = {c.name for c in table.c}
        if columns != wanted:
            extra, missing = sorted(columns - wanted), sorted(wanted - columns)
            raise DbCopyError(
                f"{provider.describe()}: table {table.name} does not match the models "
                f"(extra columns: {', '.join(extra) or 'none'}; missing: "
                f"{', '.join(missing) or 'none'})"
            )


def _check_foreign_keys(conn: Connection, provider: DbProvider) -> None:
    if provider.dialect != "sqlite":
        return  # Postgres enforces them
    broken = conn.exec_driver_sql("PRAGMA foreign_key_check").all()
    if broken:
        shown = "; ".join(
            f"{table} rowid {rowid} -> {parent}" for table, rowid, parent, _fk in broken[:10]
        )
        raise DbCopyError(
            f"the source database {provider.describe()} has {len(broken)} row(s) whose "
            f"foreign key points at no row ({shown}); fix them before copying"
        )


def _check_source_folders(conn: Connection, source: Settings, provider: DbProvider) -> None:
    """Every source's and artifact's folder (`dir_path`) exists under the source's data root:
    otherwise the copy would hold rows whose files are elsewhere (e.g. on another machine)."""
    from teacher.db.models import Artifact, KnowledgeSource
    from teacher.files import to_absolute

    missing = []
    for model in (KnowledgeSource, Artifact):
        for slug, dir_path in conn.execute(select(model.slug, model.dir_path)):
            try:
                folder = to_absolute(dir_path, source.data_root)
            except ValueError as exc:
                missing.append(f"{model.__tablename__} {slug!r}: {exc}")
                continue
            if not folder.is_dir():
                missing.append(f"{model.__tablename__} {slug!r}: no folder {folder}")
    if missing:
        shown = "; ".join(missing[:10])
        more = f" (and {len(missing) - 10} more)" if len(missing) > 10 else ""
        raise DbCopyError(
            f"{len(missing)} row(s) of {provider.describe()} have no folder under its data root "
            f"{source.data_root}: {shown}{more}. Its files must be there to be copied"
        )


def _rows_by_table(conn: Connection, tables: Iterable[Table]) -> dict[str, int]:
    return {t.name: _count(conn, t) for t in tables}


def _check_target_empty(conn: Connection, provider: DbProvider) -> None:
    names = set(inspect(conn).get_table_names())
    counts = _rows_by_table(conn, [t for t in copied_tables() if t.name in names])
    full = {name: rows for name, rows in counts.items() if rows}
    if full:
        listed = ", ".join(f"{name} ({rows})" for name, rows in sorted(full.items()))
        raise DbCopyError(
            f"the target database {provider.describe()} is not empty: these tables have rows: "
            f"{listed}; db copy only fills an empty database"
        )


def _check_target_folders(target: Settings) -> None:
    for name in COPIED_FOLDERS:
        folder = target.data_root / name
        if folder.exists() and (not folder.is_dir() or any(folder.iterdir())):
            raise DbCopyError(
                f"the target folder {folder} is not empty; db copy only fills empty folders"
            )
    derived = target.data_root / DERIVED
    if derived.exists():
        if not derived.is_dir():
            raise DbCopyError(f"{derived} is not a folder")
        present = sorted(p.name for p in derived.iterdir() if p.name not in DERIVED_ALLOWED_IN_TARGET)
        if present:
            raise DbCopyError(
                f"the target folder {derived} is not empty (it holds {', '.join(present)}); "
                "db copy only fills an empty derived/ (pyodide/ may stay)"
            )


# -- files -----------------------------------------------------------------------------------


def _files(path: Path) -> Iterator[Path]:
    if path.is_file():
        yield path
        return
    for dirpath, _dirnames, filenames in os.walk(path):
        for filename in filenames:
            yield Path(dirpath) / filename


def _tree_size(path: Path) -> tuple[int, int]:
    files = list(_files(path))
    return len(files), sum(f.stat().st_size for f in files)


def _folder_plan(source: Settings) -> list[FolderEntry]:
    root = source.data_root
    entries = []
    for name in COPIED_FOLDERS:
        folder = root / name
        if folder.exists() and not folder.is_dir():
            raise DbCopyError(f"{folder} is not a folder")
        count, size = _tree_size(folder) if folder.is_dir() else (0, 0)
        entries.append(FolderEntry(name, "copy", count, size))
    derived = root / DERIVED
    if derived.is_dir():
        for child in sorted(derived.iterdir(), key=lambda p: p.name):
            count, size = _tree_size(child)
            relative = f"{DERIVED}/{child.name}"
            if child.name in DERIVED_REBUILT:
                entries.append(
                    FolderEntry(relative, "rebuild", count, size, DERIVED_REBUILT[child.name])
                )
            elif child.name in DERIVED_NOT_COPIED:
                entries.append(
                    FolderEntry(relative, "skip", count, size, DERIVED_NOT_COPIED[child.name])
                )
            else:
                entries.append(FolderEntry(relative, "copy", count, size, "run records"))
    return entries


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _copy_files(
    source: Settings, target: Settings, plan: Sequence[FolderEntry], created: list[Path]
) -> tuple[int, int]:
    """Copy the plan's "copy" entries (modification times kept) and compare every copy with
    its original; `created` collects what was made, for the clean-up on failure."""
    files = size = 0
    for entry in plan:
        if entry.action != "copy":
            continue
        src = source.data_root / entry.path
        dst = target.data_root / entry.path
        if not src.exists():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            if not dst.exists():
                created.append(dst)
                dst.mkdir()
            for child in sorted(src.iterdir(), key=lambda p: p.name):
                created.append(dst / child.name)
                _copy_entry(child, dst / child.name)
        else:
            created.append(dst)
            _copy_entry(src, dst)
        for original in _files(src):
            copy = dst / original.relative_to(src) if src.is_dir() else dst
            if not copy.is_file() or copy.stat().st_size != original.stat().st_size or (
                _sha256(copy) != _sha256(original)
            ):
                raise DbCopyError(f"the copy of {original} at {copy} differs from the original")
            files += 1
            size += original.stat().st_size
    return files, size


def _copy_entry(src: Path, dst: Path) -> None:
    if src.is_dir():
        shutil.copytree(src, dst, copy_function=shutil.copy2)
    else:
        shutil.copy2(src, dst)


def _remove(paths: Iterable[Path]) -> None:
    for path in reversed(list(paths)):
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        elif path.exists() or path.is_symlink():
            path.unlink()


# -- rows ------------------------------------------------------------------------------------


def _nul_columns(spec: _TableSpec, mapping: Any) -> list[str]:
    return [
        name
        for name in spec.text
        if isinstance(mapping[name], str) and "\x00" in mapping[name]
    ]


def _nul_error(found: Sequence[str], target: DbProvider) -> DbCopyError:
    shown = "; ".join(found[:NUL_REPORT_LIMIT])
    more = f" (and {len(found) - NUL_REPORT_LIMIT} more)" if len(found) > NUL_REPORT_LIMIT else ""
    return DbCopyError(
        f"{len(found)} text value(s) contain a NUL character, which Postgres "
        f"({target.describe()}) cannot store: {shown}{more}. Remove the NUL characters in the "
        "source, then copy again; nothing was copied"
    )


def _scan_nul(conn: Connection, specs: Sequence[_TableSpec]) -> list[str]:
    found = []
    for spec in specs:
        if not spec.text:
            continue
        for batch in _rows(conn, spec):
            for mapping in batch:
                for name in _nul_columns(spec, mapping):
                    found.append(f"{spec.table.name}.{name} at {_key(spec, mapping)}")
    return found


def _copy_rows(
    src_conn: Connection,
    dst_conn: Connection,
    specs: Sequence[_TableSpec],
    target: DbProvider,
    out: Out,
) -> dict[str, _Digest]:
    """Insert every source row into the target (one transaction, the caller's); returns the
    source digests."""
    digests: dict[str, _Digest] = {}
    later: dict[tuple[str, str], list[dict[str, Any]]] = {}
    nul: list[str] = []
    check_nul = target.dialect == "postgresql"
    for spec in specs:
        started = time.monotonic()
        digest = digests[spec.table.name] = _Digest()
        insert = spec.table.insert()
        for batch in _rows(src_conn, spec):
            params = []
            for mapping in batch:
                digest.add(_row_digest(spec, mapping))
                if check_nul:
                    bad = _nul_columns(spec, mapping)
                    if bad:
                        nul.extend(f"{spec.table.name}.{n} at {_key(spec, mapping)}" for n in bad)
                        continue
                params.append(_insert_params(spec, mapping))
                for name in spec.deferred:
                    if mapping[name] is not None:
                        later.setdefault((spec.table.name, name), []).append(
                            {
                                **{f"_dbcopy_pk_{c.name}": mapping[c.name] for c in spec.pk},
                                "_dbcopy_value": mapping[name],
                            }
                        )
            if not params or nul:
                continue  # after a NUL value nothing more is inserted: the copy will fail
            try:
                dst_conn.execute(insert, params)
            except DBAPIError as exc:
                first, last = _key(spec, batch[0]), _key(spec, batch[-1])
                raise DbCopyError(
                    f"inserting {spec.table.name} rows ({first} .. {last}) into "
                    f"{target.describe()} failed: {_first_line(exc)}"
                ) from None
        out(f"  {spec.table.name}: {digest.rows} row(s) in {time.monotonic() - started:.1f} s")
    if nul:
        raise _nul_error(nul, target)
    by_name = {spec.table.name: spec for spec in specs}
    for (table_name, column_name), rows in later.items():
        spec = by_name[table_name]
        table = spec.table
        values: dict[str, Any] = {column_name: bindparam("_dbcopy_value")}
        # Setting a column explicitly keeps its `onupdate` (an updated_at) from firing.
        values.update({c.name: c for c in table.c if c.onupdate is not None})
        statement = update(table).values(values)
        for c in spec.pk:
            statement = statement.where(c == bindparam(f"_dbcopy_pk_{c.name}"))
        try:
            dst_conn.execute(statement, rows)
        except DBAPIError as exc:
            raise DbCopyError(
                f"setting {table_name}.{column_name} in {target.describe()} failed: "
                f"{_first_line(exc)}"
            ) from None
        out(f"  {table_name}.{column_name}: set on {len(rows)} row(s)")
    return digests


def _reset_sequences(conn: Connection, provider: DbProvider) -> list[str]:
    """Postgres: set every sequence owned by a copied table's column to the column's max; a
    sequence owned by anything else is an error. SQLite: check `sqlite_sequence`, if any.
    Returns what was done, one line per sequence."""
    copied = {t.name: t for t in copied_tables()}
    done = []
    if provider.dialect == "postgresql":
        owned = conn.execute(
            text(
                "SELECT s.oid::regclass::text, t.relname, a.attname FROM pg_class s "
                "JOIN pg_namespace n ON n.oid = s.relnamespace "
                "LEFT JOIN pg_depend d ON d.objid = s.oid "
                "  AND d.classid = 'pg_class'::regclass AND d.deptype IN ('a', 'i') "
                "LEFT JOIN pg_class t ON t.oid = d.refobjid "
                "LEFT JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = d.refobjsubid "
                "WHERE s.relkind = 'S' AND n.nspname = current_schema() ORDER BY s.relname"
            )
        ).all()
        quote = conn.dialect.identifier_preparer.quote
        for sequence, table_name, column_name in owned:
            if table_name in copied and column_name in copied[table_name].c:
                top = conn.execute(
                    text(f"SELECT MAX({quote(column_name)}) FROM {quote(table_name)}")
                ).scalar()
                # An empty table: is_called false, so the next value is 1.
                conn.execute(
                    text("SELECT setval(CAST(:seq AS regclass), :value, :called)"),
                    {"seq": sequence, "value": top if top is not None else 1,
                     "called": top is not None},
                )
                where = f"{table_name}.{column_name}"
                done.append(
                    f"{sequence}: next {where} is {top + 1}" if top is not None
                    else f"{sequence}: {table_name} is empty, next {where} is 1"
                )
            elif table_name is not None and is_concept_search_table(table_name):
                continue  # the search index's own; it is rebuilt from scratch
            else:
                owner = f"{table_name}.{column_name}" if table_name else "no column"
                raise DbCopyError(
                    f"{provider.describe()} has a sequence {sequence} owned by {owner}, which "
                    "db copy does not know how to set"
                )
        return done
    has_sequence_table = conn.exec_driver_sql(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'sqlite_sequence'"
    ).first()
    if has_sequence_table is None:
        return ["SQLite: no table uses AUTOINCREMENT (no sqlite_sequence); new ids follow the max"]
    for name, seq in conn.exec_driver_sql("SELECT name, seq FROM sqlite_sequence").all():
        if name in copied:
            top = conn.execute(
                text(f"SELECT MAX(rowid) FROM {conn.dialect.identifier_preparer.quote(name)}")
            ).scalar()
            if top is not None and seq < top:
                raise DbCopyError(f"sqlite_sequence of {name} is {seq}, below the max id {top}")
            done.append(f"sqlite_sequence {name}: {seq}")
    return done


def _target_digests(conn: Connection, specs: Sequence[_TableSpec]) -> dict[str, _Digest]:
    digests = {}
    for spec in specs:
        digest = digests[spec.table.name] = _Digest()
        for batch in _rows(conn, spec):
            for mapping in batch:
                digest.add(_row_digest(spec, mapping))
    return digests


# -- the command -----------------------------------------------------------------------------


def plan_copy(source: Settings, target: Settings) -> CopyPlan:
    """Check every precondition and say what `copy_database` would do; changes nothing."""
    src, dst = _check_pair(source, target)
    _check_source(source, src)
    _check_target_folders(target)
    specs = _table_specs()
    head = head_revision(target)
    if not dst.database_exists():
        state = "does not exist: it would be created and migrated to head"
    else:
        current = current_revision(target)
        if current == head:
            state = f"at the head revision {head}"
        else:
            state = f"at revision {current or 'none'}: it would be migrated to {head}"
        engine = _engine(dst)
        try:
            with engine.connect() as conn:
                _check_target_empty(conn, dst)
        finally:
            engine.dispose()
    engine = _engine(src)
    try:
        with _snapshot(engine.connect(), src) as conn, conn.begin():
            _check_schema(conn, src)
            _check_foreign_keys(conn, src)
            _check_source_folders(conn, source, src)
            tables = [(spec.table.name, _count(conn, spec.table)) for spec in specs]
            if dst.dialect == "postgresql":
                nul = _scan_nul(conn, specs)
                if nul:
                    raise _nul_error(nul, dst)
    finally:
        engine.dispose()
    return CopyPlan(
        source=src.describe(),
        target=dst.describe(),
        target_state=state,
        tables=tables,
        folders=_folder_plan(source),
        deferred=[f"{s.table.name}.{name}" for s in specs for name in s.deferred],
    )


def copy_database(source: Settings, target: Settings, *, out: Out = print) -> CopyResult:
    """Copy every row and file from `source` to `target` (see the module docstring); `out`
    receives the progress and the verification table. Raises DbCopyError."""
    timer = time.monotonic()
    plan = plan_copy(source, target)
    for line in plan.lines():
        out(line)
    result = CopyResult(plan=plan)
    src, dst = create_db_provider(source), create_db_provider(target)
    specs = _table_specs()

    out(f"Migrating {plan.target} to head ...")
    try:
        upgrade_to_head(target)
    except (SQLAlchemyError, OSError) as exc:
        raise DbCopyError(f"migrating {plan.target} failed: {_first_line(exc)}") from None
    if current_revision(target) != head_revision(target):
        raise DbCopyError(f"{plan.target} is not at the head revision after migrating")
    result.seconds["migrate"] = time.monotonic() - timer

    created: list[Path] = []
    src_engine, dst_engine = _engine(src), _engine(dst)
    try:
        step = time.monotonic()
        out("Copying files ...")
        result.files, result.bytes = _copy_files(source, target, plan.folders, created)
        out(f"  {result.files} file(s), {_size(result.bytes)}, each compared with its original")
        result.seconds["files"] = time.monotonic() - step

        with (
            _snapshot(src_engine.connect(), src) as src_conn,
            src_conn.begin(),
            dst_engine.connect() as dst_conn,
            dst_conn.begin(),
        ):
            _exact_floats(src_conn, src)
            _exact_floats(dst_conn, dst)
            _check_schema(dst_conn, dst)
            _check_target_empty(dst_conn, dst)
            step = time.monotonic()
            out("Copying rows ...")
            source_digests = _copy_rows(src_conn, dst_conn, specs, dst, out)
            result.sequences = _reset_sequences(dst_conn, dst)
            result.seconds["rows"] = time.monotonic() - step
            step = time.monotonic()
            target_digests = _target_digests(dst_conn, specs)
            for spec in specs:
                name = spec.table.name
                result.tables.append(
                    TableResult(
                        name=name,
                        source_rows=_count(src_conn, spec.table),
                        target_rows=_count(dst_conn, spec.table),
                        source_digest=source_digests[name].hex,
                        target_digest=target_digests[name].hex,
                    )
                )
            result.seconds["verify"] = time.monotonic() - step
            out("Verification (row counts and a digest of every column of every row):")
            for line in verification_lines(result.tables):
                out(line)
            bad = [t.name for t in result.tables if not t.ok]
            if bad or any(
                t.source_rows != source_digests[t.name].rows for t in result.tables
            ):
                raise DbCopyError(
                    f"the copy differs from the source in: {', '.join(bad) or 'row counts'}; "
                    f"{plan.target} was rolled back and the copied files removed"
                )
            for line in result.sequences:
                out(f"  sequence {line}")
        # Leaving the `with` committed the target's transaction.
    except BaseException:
        _remove(created)
        raise
    finally:
        src_engine.dispose()
        dst_engine.dispose()
    out(f"Committed {sum(t.target_rows for t in result.tables)} row(s) to {plan.target}.")

    step = time.monotonic()
    rebuild_derived(target, result, out)
    result.seconds["rebuild"] = time.monotonic() - step
    result.seconds["total"] = time.monotonic() - timer
    steps = ", ".join(
        f"{name} {result.seconds[name]:.1f} s"
        for name in ("migrate", "files", "rows", "verify", "rebuild")
    )
    out(f"Done in {result.seconds['total']:.1f} s ({steps}).")
    return result


def rebuild_search_index(settings: Settings) -> int:
    """Rebuild the concept search index of `settings`' database from the concepts and check
    it (`ConceptManager.check()` must find nothing); returns the number of concepts."""
    from teacher.db.engine import session_scope
    from teacher.managers import ConceptManager

    with session_scope(settings) as session:
        manager = ConceptManager(session, settings)
        count = manager.rebuild_search_index()
        issues = manager.check()
        if issues:
            listed = "; ".join(f"{i.code} {i.slug}" for i in issues[:10])
            raise DbCopyError(
                f"the rebuilt concept search index does not match the concepts: {listed}"
            )
    return count


def rebuild_derived(settings: Settings, result: CopyResult, out: Out) -> None:
    """The final step: the concept search index, then the vector index of each embedding
    model. A failure says the copy itself is committed and how to retry."""
    from teacher.db.engine import session_scope
    from teacher.db.models import ConceptEmbedding
    from teacher.managers import ConceptEmbeddingManager

    describe = create_db_provider(settings).describe()
    committed = f"The copy to {describe} is committed and verified, but "
    try:
        result.search_concepts = rebuild_search_index(settings)
    except Exception as exc:  # noqa: BLE001 - reported with the retry command, never hidden
        raise DbCopyError(
            f"{committed}rebuilding its concept search index failed: {_first_line(exc)}. "
            f"Concept search fails until it is rebuilt: fix the cause, then run "
            f"`teacher --db {settings.db} db rebuild-search`"
        ) from exc
    out(f"Rebuilt the concept search index ({result.search_concepts} concept(s)).")
    try:
        with session_scope(settings) as session:
            names = sorted(session.scalars(select(ConceptEmbedding.model).distinct()))
            manager = ConceptEmbeddingManager(session, settings)
            for name in names:
                done = manager.rebuild_index(name)
                result.vector_indexes.append(f"{name}: {done.total} vector(s)")
    except Exception as exc:  # noqa: BLE001 - reported with the retry command, never hidden
        raise DbCopyError(
            f"{committed}rebuilding its vector index failed: {_first_line(exc)}. It is rebuilt "
            f"on first use, or run `teacher --db {settings.db} concepts reindex --rebuild-index`"
        ) from exc
    if result.vector_indexes:
        out("Rebuilt the vector index: " + "; ".join(result.vector_indexes) + ".")
    else:
        out("No concept embeddings, so no vector index.")


def _size(count: int) -> str:
    value = float(count)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    raise AssertionError("unreachable")


__all__ = [
    "BATCH_ROWS",
    "CopyPlan",
    "CopyResult",
    "DbCopyError",
    "FolderEntry",
    "TableResult",
    "copied_tables",
    "copy_database",
    "plan_copy",
    "rebuild_derived",
    "rebuild_search_index",
    "verification_lines",
]
