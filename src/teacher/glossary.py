"""Glossary of a study page: the terms a newcomer may want to hover, and how they are matched.

This module holds the parts that do not call the model (the model call is
`teacher.claude_generator.GlossaryBuilder`):

- `GlossaryEntry` and `KINDS`: one term as stored (`teacher.managers.GlossaryManager`).
- Matching: `find_occurrences`, `occurs`, `visible_entries`. A form of a term (the term or one of
  its aliases) matches as a whole word: a match may not be preceded or followed by a letter,
  digit, combining mark or underscore when the form itself starts or ends with one (so "GAN"
  does not match inside "ORGAN", but matches in "GAN-based" or "(GAN)"). Whitespace inside a
  form matches any run of whitespace. Acronyms and symbols match case-sensitively, other terms
  case-insensitively. `static/js/glossary_match.js` implements the same rules for the page.
- `block_fields(content)` / `block_texts(content)`: every piece of text of a block's content
  (public and, under `private`, hidden-until-answered), in order, leaving out the strings that
  are not text (`teacher.blocks.NON_TEXT_KEYS`: a chart's `kind`, a widget's `html`).
- `select_terms(proposed, texts)`: the checks applied to the model's proposal (normalise,
  merge case-insensitive duplicates, drop terms that do not occur in the page text) and
  `check_selection`, which fails when too much had to be dropped.
- Errors: `GlossaryError` and its subclasses, mapped to HTTP statuses in `teacher.web.errors`.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from teacher.blocks import NON_TEXT_KEYS, PRIVATE_KEY

KINDS: tuple[str, ...] = ("acronym", "term", "symbol")
CASE_SENSITIVE_KINDS = frozenset({"acronym", "symbol"})

# A page with at least this many words of text certainly uses some terms worth explaining, so
# an empty glossary for it means the step went wrong rather than that there is nothing to list.
PROSE_MIN_WORDS = 150

NOT_ON_PAGE = "does not occur in the study page text"


# -- errors ---------------------------------------------------------------------------------


class GlossaryError(Exception):
    """The glossary could not be built. The message says why and never contains a secret."""


class GlossaryUnavailableError(GlossaryError):
    """The model could not be used: no credentials, an API error, a refusal, a truncated or
    unexpected reply (HTTP 503)."""


class GlossaryOutputError(GlossaryError):
    """The model replied, but the reply is unusable: invalid against the schema, too many terms
    that do not occur in the page, or no terms for a page that plainly has prose (HTTP 502)."""


class GlossaryInputError(GlossaryError, ValueError):
    """The request cannot be built: no linked source, an unreadable or oversized source file,
    no blocks (HTTP 422, like any ValueError)."""


# -- entries --------------------------------------------------------------------------------


@dataclass(frozen=True)
class GlossaryEntry:
    """One glossary term. `term` is shown as written and must occur in the page text (or one of
    `aliases` must); `expansion` is the spelled-out form of an acronym, or None."""

    term: str
    kind: str
    definition: str
    expansion: str | None = None
    aliases: tuple[str, ...] = field(default_factory=tuple)

    @property
    def case_sensitive(self) -> bool:
        return self.kind in CASE_SENSITIVE_KINDS

    @property
    def forms(self) -> tuple[str, ...]:
        """The term and its aliases: every surface form that marks the term on the page."""
        return (self.term, *self.aliases)

    def to_dict(self) -> dict[str, Any]:
        return {
            "term": self.term,
            "kind": self.kind,
            "expansion": self.expansion,
            "definition": self.definition,
            "aliases": list(self.aliases),
        }


def normalise_space(text: str) -> str:
    """`text` on one line: runs of whitespace become one space, ends trimmed."""
    return " ".join(text.split())


# -- matching -------------------------------------------------------------------------------


def is_word_char(char: str) -> bool:
    """Letters, digits, combining marks and the underscore: what a whole-word match may not
    touch (the same set as `[\\p{L}\\p{N}\\p{M}_]` in the page script)."""
    return char == "_" or unicodedata.category(char)[0] in "LNM"


def _pattern(form: str, case_sensitive: bool) -> re.Pattern[str]:
    body = r"\s+".join(re.escape(part) for part in form.split())
    return re.compile(body, 0 if case_sensitive else re.IGNORECASE)


def find_occurrences(text: str, form: str, *, case_sensitive: bool) -> Iterator[tuple[int, int]]:
    """`(start, end)` of every whole-word occurrence of `form` in `text` (they may overlap)."""
    form = normalise_space(form)
    if not form:
        return
    pattern = _pattern(form, case_sensitive)
    check_start, check_end = is_word_char(form[0]), is_word_char(form[-1])
    position = 0
    while True:
        match = pattern.search(text, position)
        if match is None:
            return
        start, end = match.span()
        if not (check_start and start > 0 and is_word_char(text[start - 1])) and not (
            check_end and end < len(text) and is_word_char(text[end])
        ):
            yield start, end
        position = start + 1


def form_occurs(form: str, texts: Iterable[str], *, case_sensitive: bool) -> bool:
    return any(
        next(find_occurrences(text, form, case_sensitive=case_sensitive), None) is not None
        for text in texts
    )


def occurs(entry: GlossaryEntry, texts: Sequence[str]) -> bool:
    """Whether the term or one of its aliases occurs (as a whole word) in any of `texts`."""
    return any(form_occurs(form, texts, case_sensitive=entry.case_sensitive) for form in entry.forms)


def visible_entries(entries: Iterable[GlossaryEntry], texts: Sequence[str]) -> list[GlossaryEntry]:
    """The entries that occur in `texts`, each with only the aliases that occur there.

    Used with the text of the blocks the reader can currently see, so the glossary neither lists
    terms from locked blocks nor reveals surface forms used only there.
    """
    result = []
    for entry in entries:
        if not occurs(entry, texts):
            continue
        aliases = tuple(
            alias
            for alias in entry.aliases
            if form_occurs(alias, texts, case_sensitive=entry.case_sensitive)
        )
        result.append(replace(entry, aliases=aliases))
    return result


# -- block text -----------------------------------------------------------------------------


def block_fields(content: Mapping[str, Any]) -> list[tuple[str, str, bool]]:
    """`(field name, text, private)` for every piece of reader-visible text in a block's
    content, in order: every non-blank string except those under a key in
    `teacher.blocks.NON_TEXT_KEYS` (at any depth), which are not prose (a chart's `kind`, an
    interactable's widget source in `html`).

    The walk is otherwise generic (block types store their text as strings, their numbers and
    flags as other types), so a new block type is covered without changes here as long as any
    string it stores that is not text has its key in `NON_TEXT_KEYS`. List items take the name
    of the list's key; `private` is True under the content's `private` key.
    """
    found: list[tuple[str, str, bool]] = []

    def walk(value: Any, name: str, private: bool) -> None:
        if isinstance(value, str):
            if value.strip():
                found.append((name, value, private))
        elif isinstance(value, Mapping):
            for key, item in value.items():
                if key in NON_TEXT_KEYS:
                    continue
                walk(item, str(key), private or key == PRIVATE_KEY)
        elif isinstance(value, list | tuple):
            for item in value:
                walk(item, name, private)

    walk(content, "text", False)
    return found


def block_texts(content: Mapping[str, Any] | None) -> list[str]:
    """All the text of a block's content (see `block_fields`)."""
    if not content:
        return []
    return [text for _name, text, _private in block_fields(content)]


def word_count(texts: Iterable[str]) -> int:
    return sum(len(text.split()) for text in texts)


# -- checking the model's proposal ----------------------------------------------------------


@dataclass
class Selection:
    """What `select_terms` kept and why it changed or removed anything.

    `proposed` counts the distinct terms the model proposed (after merging duplicates);
    `dropped` lists the removed terms with reasons; `merged` the duplicates folded into an
    earlier term; `dropped_aliases` the aliases removed from kept terms.
    """

    kept: list[GlossaryEntry]
    proposed: int
    dropped: list[dict[str, str]] = field(default_factory=list)
    merged: list[dict[str, str]] = field(default_factory=list)
    dropped_aliases: list[dict[str, str]] = field(default_factory=list)

    def report(self) -> dict[str, Any]:
        return {
            "proposed": self.proposed,
            "kept": len(self.kept),
            "dropped": self.dropped,
            "merged": self.merged,
            "dropped_aliases": self.dropped_aliases,
        }


def _same(a: str, b: str, case_sensitive: bool) -> bool:
    return a == b if case_sensitive else a.casefold() == b.casefold()


def _unique_aliases(entry: GlossaryEntry, aliases: Iterable[str]) -> tuple[str, ...]:
    result: list[str] = []
    for alias in aliases:
        if _same(alias, entry.term, entry.case_sensitive):
            continue
        if any(_same(alias, seen, entry.case_sensitive) for seen in result):
            continue
        result.append(alias)
    return tuple(result)


def select_terms(proposed: Sequence[GlossaryEntry], texts: Sequence[str]) -> Selection:
    """Clean the model's proposed terms against the page text; never raises.

    1. Whitespace is normalised; a term or definition left blank is dropped; a blank expansion
       becomes None; blank aliases are removed.
    2. Terms equal apart from case are merged into the first: its wording, kind and definition
       are kept, the aliases are combined, and a duplicate written differently becomes an alias.
       A merge is recorded, not an error: the model listing a term twice is harmless.
    3. An alias equal to the term, repeated, or equal to another term is removed.
    4. A term none of whose forms occurs in `texts` is dropped (it could never be marked on the
       page); aliases that do not occur are removed from the terms that are kept.

    Whether too much was dropped is decided by `check_selection`.
    """
    dropped: list[dict[str, str]] = []
    cleaned: list[GlossaryEntry] = []
    for entry in proposed:
        term = normalise_space(entry.term)
        definition = normalise_space(entry.definition)
        if not term:
            dropped.append({"term": entry.term, "reason": "the term is blank"})
            continue
        if not definition:
            dropped.append({"term": term, "reason": "the definition is blank"})
            continue
        expansion = normalise_space(entry.expansion) if entry.expansion is not None else ""
        aliases = tuple(a for a in (normalise_space(a) for a in entry.aliases) if a)
        cleaned.append(
            GlossaryEntry(
                term=term,
                kind=entry.kind,
                definition=definition,
                expansion=expansion or None,
                aliases=aliases,
            )
        )

    merged_entries: dict[str, GlossaryEntry] = {}
    merged: list[dict[str, str]] = []
    for entry in cleaned:
        key = entry.term.casefold()
        first = merged_entries.get(key)
        if first is None:
            merged_entries[key] = entry
            continue
        extra = (entry.term,) if entry.term != first.term else ()
        merged_entries[key] = replace(
            first,
            expansion=first.expansion or entry.expansion,
            aliases=(*first.aliases, *extra, *entry.aliases),
        )
        merged.append({"term": entry.term, "into": first.term})

    terms = set(merged_entries)
    dropped_aliases: list[dict[str, str]] = []
    kept: list[GlossaryEntry] = []
    for key, entry in merged_entries.items():
        aliases = []
        for alias in _unique_aliases(entry, entry.aliases):
            if alias.casefold() in terms and alias.casefold() != key:
                dropped_aliases.append(
                    {"term": entry.term, "alias": alias, "reason": "is another glossary term"}
                )
            else:
                aliases.append(alias)
        entry = replace(entry, aliases=tuple(aliases))
        if not occurs(entry, texts):
            dropped.append({"term": entry.term, "reason": NOT_ON_PAGE})
            continue
        present = []
        for alias in entry.aliases:
            if form_occurs(alias, texts, case_sensitive=entry.case_sensitive):
                present.append(alias)
            else:
                dropped_aliases.append({"term": entry.term, "alias": alias, "reason": NOT_ON_PAGE})
        kept.append(replace(entry, aliases=tuple(present)))

    blank = sum(1 for item in dropped if item["reason"].endswith("is blank"))
    return Selection(
        kept=kept,
        proposed=len(merged_entries) + blank,
        dropped=dropped,
        merged=merged,
        dropped_aliases=dropped_aliases,
    )


def check_selection(selection: Selection, texts: Sequence[str], *, prefix: str = "") -> None:
    """GlossaryOutputError when the proposal was mostly unusable.

    Dropping a term that is not on the page is tolerated (the model sometimes lists a term from
    the paper that the page does not use), but if more than a third of the proposed terms had to
    be dropped, the reply did not follow the page and the result cannot be trusted. Likewise a
    page with at least `PROSE_MIN_WORDS` words of text that ends up with no terms at all.
    """
    dropped = len(selection.dropped)
    if dropped and dropped * 3 > selection.proposed:
        listed = "; ".join(f"{item['term']!r} ({item['reason']})" for item in selection.dropped[:12])
        more = f"; and {dropped - 12} more" if dropped > 12 else ""
        raise GlossaryOutputError(
            f"{prefix}{dropped} of the {selection.proposed} terms Claude proposed had to be "
            f"dropped, more than a third, so the reply does not fit the page: {listed}{more}"
        )
    words = word_count(texts)
    if not selection.kept and words >= PROSE_MIN_WORDS:
        raise GlossaryOutputError(
            f"{prefix}Claude proposed no usable terms for a page with {words} words of text"
        )


__all__ = [
    "CASE_SENSITIVE_KINDS",
    "KINDS",
    "NOT_ON_PAGE",
    "PROSE_MIN_WORDS",
    "GlossaryEntry",
    "GlossaryError",
    "GlossaryInputError",
    "GlossaryOutputError",
    "GlossaryUnavailableError",
    "Selection",
    "block_fields",
    "block_texts",
    "check_selection",
    "find_occurrences",
    "form_occurs",
    "is_word_char",
    "normalise_space",
    "occurs",
    "select_terms",
    "visible_entries",
    "word_count",
]
