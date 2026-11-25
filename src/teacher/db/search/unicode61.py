"""SQLite FTS5's `unicode61 remove_diacritics 2` tokenizer, in Python.

The Postgres concept search (`teacher.db.search.postgres`) indexes and queries the same tokens
as SQLite's FTS5 word index, so both find the same concepts. Postgres's own text-search parser
splits text differently (hyphens, numbers, e-mail addresses ...) and folds differently, so the
tokens are made here instead.

unicode61's rules are tables of its own (its Unicode version and choices differ from Python's
`unicodedata`: e.g. most unassigned code points are token characters, some combining marks are
separators, others are diacritics it removes, and only some precomposed letters lose their
accents). So the tables are not re-derived from `unicodedata`: `teacher.db.search.
_unicode61_data` records what FTS5 itself does with every code point, probed from SQLite with
`probe_fts5` (regenerate with `python -m teacher.db.search.unicode61 <path>`), and
tests/test_concept_search.py compares the tables with the local SQLite's FTS5 on every run.

What FTS5 does with a code point is context-free: it is a separator, or a token character that
folds to one character or to nothing (a removed diacritic). A token is a maximal run of token
characters, folded and concatenated; a run that folds to nothing is not a token. Like FTS5, a
token is cut to `MAX_TOKEN_BYTES` bytes of UTF-8 (FTS5 may cut inside a character; this cuts
before it, which can only matter for a "word" of more than 32,768 bytes).
"""

from __future__ import annotations

import sqlite3
import sys
from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path

from teacher.db.search._unicode61_data import FOLDS, SEPARATOR_RANGES

MAX_TOKEN_BYTES = 32768  # FTS5_MAX_TOKEN_SIZE
_MAX_CODE_POINT = 0x10FFFF


def _expand(ranges: str) -> frozenset[str]:
    chars: set[str] = set()
    for part in ranges.split():
        start, _, end = part.partition("-")
        first = int(start, 16)
        last = int(end, 16) if end else first
        chars.update(chr(cp) for cp in range(first, last + 1))
    return frozenset(chars)


def _fold_table(pairs: str) -> dict[str, str]:
    table: dict[str, str] = {}
    for part in pairs.split():
        source, _, target = part.partition(":")
        table[chr(int(source, 16))] = chr(int(target, 16)) if target else ""
    return table


_SEPARATORS = _expand(SEPARATOR_RANGES)
_FOLDS = _fold_table(FOLDS)


def _cut(token: str) -> str:
    encoded = token.encode("utf-8")
    if len(encoded) <= MAX_TOKEN_BYTES:
        return token
    return encoded[:MAX_TOKEN_BYTES].decode("utf-8", errors="ignore")


def tokenize(text: str) -> list[tuple[str, int]]:
    """The tokens of `text` with the index of the character each starts at, in order."""
    out: list[tuple[str, int]] = []
    current: list[str] = []
    start = -1
    for i, ch in enumerate(text):
        if ch in _SEPARATORS:
            if start >= 0:
                token = "".join(current)
                if token:
                    out.append((_cut(token), start))
                current, start = [], -1
            continue
        if start < 0:
            start = i
        current.append(_FOLDS.get(ch, ch))
    if start >= 0:
        token = "".join(current)
        if token:
            out.append((_cut(token), start))
    return out


@lru_cache(maxsize=4096)
def tokens(text: str) -> tuple[str, ...]:
    """The tokens of `text` (`tokenize` without the positions)."""
    return tuple(token for token, _ in tokenize(text))


# -- the tables, probed from SQLite ------------------------------------------------------------


def probe_fts5(connection: sqlite3.Connection) -> tuple[list[tuple[int, int]], dict[int, str]]:
    """What the FTS5 of `connection` does with every code point (surrogates excepted):
    `(separator ranges, folds)`, the ranges inclusive and the folds only those that change the
    code point (to one character, or to "" when it is removed). Uses temp tables only."""
    connection.execute(
        "CREATE VIRTUAL TABLE temp._teacher_unicode61_probe USING fts5("
        "x, tokenize = 'unicode61 remove_diacritics 2')"
    )
    connection.execute(
        "CREATE VIRTUAL TABLE temp._teacher_unicode61_vocab "
        "USING fts5vocab(temp, _teacher_unicode61_probe, 'instance')"
    )
    separators: list[int] = []
    folds: dict[int, str] = {}
    code_points = [cp for cp in range(_MAX_CODE_POINT + 1) if not 0xD800 <= cp <= 0xDFFF]
    chunk_size = 4000
    try:
        for begin in range(0, len(code_points), chunk_size):
            chunk = code_points[begin : begin + chunk_size]
            # "q<c>q" per code point: a separator gives the tokens "q", "q"; a token character
            # one token "q<folded>q".
            document = " ".join("q" + chr(cp) + "q" for cp in chunk)
            connection.execute("DELETE FROM temp._teacher_unicode61_probe")
            connection.execute(
                "INSERT INTO temp._teacher_unicode61_probe (rowid, x) VALUES (1, ?)", (document,)
            )
            terms = [
                term
                for (term,) in connection.execute(
                    "SELECT term FROM temp._teacher_unicode61_vocab ORDER BY offset"
                )
            ]
            i = 0
            for cp in chunk:
                if terms[i] == "q":
                    if terms[i + 1] != "q":
                        raise RuntimeError(f"unexpected FTS5 tokens around U+{cp:04X}")
                    separators.append(cp)
                    i += 2
                    continue
                term = terms[i]
                if not (len(term) >= 2 and term[0] == term[-1] == "q"):
                    raise RuntimeError(f"unexpected FTS5 token {term!r} for U+{cp:04X}")
                if term[1:-1] != chr(cp):
                    folds[cp] = term[1:-1]
                i += 1
            if i != len(terms):
                raise RuntimeError("FTS5 returned more tokens than the probe expected")
    finally:
        connection.execute("DROP TABLE temp._teacher_unicode61_vocab")
        connection.execute("DROP TABLE temp._teacher_unicode61_probe")
    ranges: list[tuple[int, int]] = []
    for cp in separators:
        if ranges and ranges[-1][1] == cp - 1:
            ranges[-1] = (ranges[-1][0], cp)
        else:
            ranges.append((cp, cp))
    return ranges, folds


def _wrap(parts: Iterable[str], width: int = 96) -> str:
    lines: list[str] = []
    line = ""
    for part in parts:
        if line and len(line) + 1 + len(part) > width:
            lines.append(line)
            line = part
        else:
            line = f"{line} {part}" if line else part
    if line:
        lines.append(line)
    return "\n".join(lines)


def render_tables(ranges: list[tuple[int, int]], folds: dict[int, str]) -> tuple[str, str]:
    """The probed tables in `_unicode61_data`'s notation: (SEPARATOR_RANGES, FOLDS)."""
    range_parts = (f"{a:X}" if a == b else f"{a:X}-{b:X}" for a, b in ranges)
    fold_parts = (f"{cp:X}:{ord(t):X}" if t else f"{cp:X}:" for cp, t in sorted(folds.items()))
    return _wrap(range_parts), _wrap(fold_parts)


def write_data_module(path: Path) -> None:
    """Probe the local SQLite and write the `_unicode61_data` module to `path`."""
    connection = sqlite3.connect(":memory:")
    try:
        ranges, folds = probe_fts5(connection)
    finally:
        connection.close()
    separator_ranges, fold_pairs = render_tables(ranges, folds)
    path.write_text(
        '"""What SQLite FTS5\'s `unicode61 remove_diacritics 2` tokenizer does with each code '
        "point.\n\nGenerated by `python -m teacher.db.search.unicode61` from SQLite "
        f"{sqlite3.sqlite_version} (see\n`teacher.db.search.unicode61`); do not edit by hand.\n\n"
        "- SEPARATOR_RANGES: the code points that separate tokens, as hex ranges (inclusive).\n"
        "- FOLDS: `<code point>:<folded code point>` for every token character FTS5 changes;\n"
        '  an empty target means the character is removed (a diacritic).\n"""\n\n'
        f'SQLITE_VERSION = "{sqlite3.sqlite_version}"\n\n'
        f'SEPARATOR_RANGES = """\n{separator_ranges}\n"""\n\n'
        f'FOLDS = """\n{fold_pairs}\n"""\n',
        encoding="utf-8",
    )


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: python -m teacher.db.search.unicode61 <path of _unicode61_data.py>")
    write_data_module(Path(sys.argv[1]))
