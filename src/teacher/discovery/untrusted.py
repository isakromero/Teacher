"""Fencing text that strangers wrote (fetched pages) before a model reads it.

`fence(text, title=, url=, label=)` returns the text between a start and an end marker that
carry a random id made fresh for every call (`secrets.token_hex(8)`):

    <<<UNTRUSTED_SOURCE_TEXT id="3f9c0d2a6b1e4f70">>>
    Candidate: C1
    URL: https://example.org/notes
    Title: Lecture notes
    ---
    <the text>
    <<<END_UNTRUSTED_SOURCE_TEXT id="3f9c0d2a6b1e4f70">>>

The caller puts the fenced text in a user message (never the system prompt), says in its
trusted instructions that what is between the markers is data, and gives that call no tools.

Before fencing, `sanitize(text)` makes sure the text cannot close the fence or pose as one:
it is NFKC-normalised (which folds full-width and other look-alike letters and brackets), zero
width characters are removed, every run of three or more `<` or `>` is broken up, any spelling
of the marker name is replaced by `[marker removed]`, and chat-template control tokens
(`<|im_start|>`, `[INST]`, ...) are replaced by `[token removed]`. The title, URL and label are
also put on a single line, so none of them can start a line of their own.
"""

from __future__ import annotations

import re
import secrets
import unicodedata

MARKER = "UNTRUSTED_SOURCE_TEXT"
END_MARKER = f"END_{MARKER}"

_ZERO_WIDTH = re.compile("[​‌‍⁠﻿­]")
_ANGLE_RUNS = re.compile(r"<{3,}|>{3,}")
_MARKER_NAME = re.compile(r"untrusted[\s_\-]*source[\s_\-]*text", re.IGNORECASE)
_SPECIAL_TOKENS = re.compile(
    r"<\|[a-z_]{2,30}\|>|\[/?INST\]|<</?SYS>>|</?s>|<(?:start|end)_of_turn>",
    re.IGNORECASE,
)


def sanitize(text: str) -> str:
    """`text` with everything that could forge or close a fence neutralised (see above)."""
    text = unicodedata.normalize("NFKC", text)
    text = _ZERO_WIDTH.sub("", text)
    text = _SPECIAL_TOKENS.sub("[token removed]", text)
    text = _MARKER_NAME.sub("[marker removed]", text)
    return _ANGLE_RUNS.sub(lambda m: " ".join(m.group(0)), text)


def single_line(text: str) -> str:
    return " ".join(sanitize(text).split())


def fence(text: str, *, label: str, title: str | None = None, url: str | None = None) -> str:
    """`text` fenced as untrusted data (see the module docstring)."""
    marker_id = secrets.token_hex(8)
    head = [f"Candidate: {single_line(label)}"]
    if url:
        head.append(f"URL: {single_line(url)}")
    if title:
        head.append(f"Title: {single_line(title)}")
    return "\n".join(
        [
            f'<<<{MARKER} id="{marker_id}">>>',
            *head,
            "---",
            sanitize(text),
            f'<<<{END_MARKER} id="{marker_id}">>>',
        ]
    )


__all__ = ["END_MARKER", "MARKER", "fence", "sanitize", "single_line"]
