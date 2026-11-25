"""Small shared file helpers: hashing, slug validation, root-relative paths."""

from __future__ import annotations

import hashlib
import os
import re
import unicodedata
from pathlib import Path, PurePosixPath

SLUG_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"
_SLUG_RE = re.compile(SLUG_PATTERN)
_CHUNK = 1024 * 1024
SLUG_MAX_LENGTH = 80


def sha256_bytes(data: bytes) -> str:
    """Hex SHA-256 of `data`."""
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    """Hex SHA-256 of the file at `path`, read in chunks."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as fh:
        while chunk := fh.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def sync_file(path: Path) -> None:
    """Flush the file at `path` to disk (fsync). The database is on another machine and keeps
    its commits, so a file a row refers to must be on disk before that row is committed: a
    server stopped abruptly otherwise loses the file and keeps the row."""
    with Path(path).open("rb") as fh:
        os.fsync(fh.fileno())


def sync_dir(path: Path) -> None:
    """Flush the directory at `path` to disk, so the entries of files created in it survive
    an abrupt stop (see `sync_file`)."""
    fd = os.open(Path(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def validate_slug(slug: str) -> str:
    """Return `slug` unchanged if it matches `SLUG_PATTERN`, else raise ValueError."""
    if not isinstance(slug, str) or not _SLUG_RE.fullmatch(slug):
        raise ValueError(f"invalid slug {slug!r}: must match {SLUG_PATTERN}")
    return slug


def slugify(text: str, *, max_length: int = SLUG_MAX_LENGTH) -> str:
    """Derive a slug matching `SLUG_PATTERN` from free text ("Café Notes!" -> "cafe-notes").

    Accents are folded to ASCII, every other run of non-alphanumerics becomes one hyphen, and
    the result is cut to `max_length` at a hyphen where possible. Raises ValueError if nothing
    usable is left.
    """
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")
    if len(slug) > max_length:
        cut = slug[:max_length]
        if slug[max_length] != "-" and "-" in cut:
            cut = cut.rsplit("-", 1)[0]
        slug = cut.strip("-")
    if not slug:
        raise ValueError(f"cannot derive a slug from {text!r}")
    return validate_slug(slug)


def to_relative(path: Path, base: Path) -> str:
    """Express `path` as a POSIX string relative to `base` (e.g. Settings.data_root).

    Relative `path`s are taken as relative to `base`. Raises ValueError if it lies outside `base`.
    """
    base = Path(base).resolve()
    path = Path(path)
    full = (path if path.is_absolute() else base / path).resolve()
    try:
        rel = full.relative_to(base)
    except ValueError:
        raise ValueError(f"{path} is not inside {base}") from None
    return rel.as_posix()


def to_absolute(relative: str, base: Path) -> Path:
    """Turn a stored POSIX relative path back into an absolute Path under `base`.

    Raises ValueError for absolute paths or paths that escape `base` (e.g. via '..').
    """
    rel = PurePosixPath(relative)
    if rel.is_absolute() or Path(relative).is_absolute():
        raise ValueError(f"expected a relative path, got {relative!r}")
    base = Path(base).resolve()
    full = (base / Path(*rel.parts)).resolve()
    if not full.is_relative_to(base):
        raise ValueError(f"{relative!r} escapes {base}")
    return full


__all__ = [
    "SLUG_PATTERN",
    "sha256_bytes",
    "sha256_file",
    "slugify",
    "to_absolute",
    "to_relative",
    "validate_slug",
]
