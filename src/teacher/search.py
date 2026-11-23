"""Search across the library: study pages, labs and sources, by plain local text matching.

No model call and no index: every query scans the library, which is small, and only reads what
is stored (the database and the study pages' block files). `search(session, settings, query,
...)` returns a `SearchResults`; the API (`GET /api/search`) turns it into JSON.

What is searched, per item (`FIELDS`, with the weight a match in each field adds):

| field      | study page                               | lab                 | source        |
|------------|------------------------------------------|---------------------|---------------|
| `title`    | its title                                | its title           | its title     |
| `concept`  | concepts tagged on its blocks (names and aliases) | -          | those linked to it |
| `authors`  | its sources' authors                     | its sources'        | its authors   |
| `glossary` | its glossary terms (term, expansion, aliases) | -              | -             |
| `heading`  | the section headings of its prose blocks | -                   | -             |
| `summary`  | its summary                              | its summary         | its summary   |
| `body`     | the text of its blocks (prose, questions and their options, chart titles, labels and captions, widget titles and instructions, figure labels and both captions) and glossary definitions | - | - |

Matching. The query and the texts are split into words (runs of letters and digits; case and
accents folded, so "Bézier" matches "bezier"). Every query word must match a word somewhere in
the item, in any of its fields. A query word matches a word of the text when they are equal; the
last query word also matches the start of a word (the reader may still be typing it), unless
the query ends with a space.

Ranking. Each query word adds the weight of the best field it matched in (a prefix match counts
`PREFIX_FACTOR` of it); each further field with a match adds `FIELD_BONUS`; a title holding
every query word adds `TITLE_ALL_BONUS`, and one holding the query as a phrase
`TITLE_PHRASE_BONUS`. An older version (superseded) and an artifact that is
not ready (failed or still generating) are scaled down. Ties: study pages, labs, then sources;
then the most recently studied; then title.

Grouping. There is one result per study page, lab or source, never one per block. A result's
`match` is the place in it that best shows why it matched: the most query words, then one the
reader can see, then running text (a section, the summary, the glossary) over a name (the title,
an author, a concept), then the highest weight, then the earliest. Its `snippet` is that text
with the matched words marked (for a section heading, the start of the section's text; for a
glossary term, the term and its definition); when
only the title matched, the snippet is the start of the summary. `fields` lists every field
with a match. Text comes back
as segments (`{"text", "mark"}`), never as HTML, so the page can build it safely.

Gates. Only the public part of a block is ever searched (never the correct option, an
explanation or a model answer). A block the reader cannot see yet (after an uncleared gate) is
matched, so the page can still be found by what it covers, but its text is not quoted: its
match says `locked`, with no snippet and no link to the block.

Errors. A query with no word, one shorter than `MIN_QUERY_CHARS`, or longer than
`MAX_QUERY_CHARS` or `MAX_QUERY_WORDS` is a `QueryError` (a ValueError: the API's 422). A block
file that cannot be read raises the manager's IntegrityCheckError: the search fails with the
reason instead of leaving the page out.
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from teacher.blocks import markdown_sections, plain_text, public_content
from teacher.config import Settings
from teacher.db.models import (
    Artifact,
    ArtifactBlock,
    ArtifactSource,
    AttentionSpan,
    BlockConcept,
    Concept,
    GlossaryTerm,
    KnowledgeSource,
    SourceConcept,
    StudyEvent,
)
from teacher.labs import LAB_KIND, STUDY_PAGE_PARAM
from teacher.managers.artifact_blocks import ArtifactBlockManager
from teacher.managers.study_history import StudyHistoryManager, iso
from teacher.reading import ReadingState, reading_state

MIN_QUERY_CHARS = 2  # letters and digits in the whole query
MAX_QUERY_CHARS = 200
MAX_QUERY_WORDS = 12
DEFAULT_LIMIT = 20
MAX_LIMIT = 50

FIELDS: dict[str, float] = {
    "title": 100.0,
    "concept": 60.0,
    "authors": 45.0,
    "glossary": 40.0,
    "heading": 35.0,
    "summary": 25.0,
    "body": 10.0,
}
PREFIX_FACTOR = 0.8
FIELD_BONUS = 3.0
NAME_FIELDS = frozenset({"title", "concept", "authors"})  # short names, not running text
TITLE_ALL_BONUS = 40.0
TITLE_PHRASE_BONUS = 30.0
SUPERSEDED_FACTOR = 0.6
NOT_READY_FACTOR = 0.5

KINDS: tuple[str, ...] = ("study_page", "lab", "source")
STUDY_STATES: tuple[str, ...] = ("not_ready", "not_opened", "in_progress", "complete", "opened")

SNIPPET_CHARS = 180  # about; the window is widened to whole words
SNIPPET_LEAD = 50  # characters of context before the first marked word


class QueryError(ValueError):
    """The query cannot be searched for (empty, too short or too long)."""


# -- words --------------------------------------------------------------------------------------


_WORD = re.compile(r"[^\W_]+")


def _fold_char(ch: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", ch) if not unicodedata.combining(c)
    ).casefold()


@lru_cache(maxsize=8192)
def _words(text: str) -> tuple[tuple[str, int, int], ...]:
    """The folded words of `text` with their (start, end) offsets in `text` itself."""
    if text.isascii():
        folded, index = text.lower(), None
    else:
        parts: list[str] = []
        positions: list[int] = []
        for i, ch in enumerate(text):
            for c in _fold_char(ch):
                parts.append(c)
                positions.append(i)
        positions.append(len(text))
        folded, index = "".join(parts), positions
    out = []
    for m in _WORD.finditer(folded):
        if index is None:
            start, end = m.start(), m.end()
        else:
            start = index[m.start()]
            end = index[m.end() - 1] + 1
        out.append((m.group(), start, end))
    return tuple(out)


@lru_cache(maxsize=8192)
def _word_set(text: str) -> frozenset[str]:
    return frozenset(w for w, _, _ in _words(text))


@dataclass(frozen=True, slots=True)
class Query:
    """A parsed query: its words (folded, in order, without repeats) and whether the last one
    also matches as a prefix."""

    text: str
    words: tuple[str, ...]
    prefix_last: bool

    def matches(self, word_set: frozenset[str]) -> dict[str, bool]:
        """The query words found in `word_set`, each with whether it matched exactly."""
        found: dict[str, bool] = {}
        last = len(self.words) - 1
        for i, q in enumerate(self.words):
            if q in word_set:
                found[q] = True
            elif i == last and self.prefix_last and any(w.startswith(q) for w in word_set):
                found[q] = False
        return found

    def marks(self, text: str) -> list[tuple[int, int]]:
        """The (start, end) spans of the words of `text` that match a query word."""
        last = self.words[-1]
        exact = set(self.words)
        spans = []
        for w, start, end in _words(text):
            if w in exact or (self.prefix_last and w.startswith(last)):
                spans.append((start, end))
        return spans


def parse_query(text: str) -> Query:
    """The query `text` as a Query; QueryError when there is nothing to search for."""
    if not isinstance(text, str):
        raise QueryError("the query must be text")
    stripped = text.strip()
    if not stripped:
        raise QueryError("Type something to search for.")
    if len(text) > MAX_QUERY_CHARS:
        raise QueryError(f"The search is too long: at most {MAX_QUERY_CHARS} characters.")
    words: list[str] = []
    for w, _, _ in _words(stripped):
        if w not in words:
            words.append(w)
    if not words:
        raise QueryError("Search for words: the query has no letters or digits.")
    if sum(len(w) for w in words) < MIN_QUERY_CHARS:
        raise QueryError(f"Type at least {MIN_QUERY_CHARS} letters or digits to search.")
    if len(words) > MAX_QUERY_WORDS:
        raise QueryError(f"The search has too many words: at most {MAX_QUERY_WORDS}.")
    # A query ending in a space or punctuation has a finished last word.
    last = _fold_char(text[-1])
    prefix_last = bool(last) and _WORD.fullmatch(last[-1]) is not None
    return Query(text=stripped, words=tuple(words), prefix_last=prefix_last)


# -- the items ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Unit:
    """One searchable piece of an item: a field's text, and where it is."""

    field: str
    text: str
    order: int  # reading order within the item
    block: str | None = None  # the block key, for heading and body text of a study page
    section: str | None = None  # the heading the text is under
    locked: bool = False  # behind an uncleared gate: matched, never quoted
    term: str | None = None  # the glossary term (glossary)
    concept: str | None = None  # the concept's name (concept)
    context: str | None = None  # quoted with it: a heading's section text, a term's definition


@dataclass(slots=True)
class Item:
    kind: str
    slug: str
    title: str
    url: str
    summary: str | None
    authors: list[str]
    year: str | None
    status: str | None
    superseded: bool
    study: dict[str, Any]
    units: list[Unit] = field(default_factory=list)


def _add(units: list[Unit], field_name: str, text: str | None, **where: Any) -> None:
    if text and text.strip():
        units.append(Unit(field=field_name, text=text.strip(), order=len(units), **where))


def _concept_units(units: list[Unit], concepts: Iterable[Concept]) -> None:
    seen: set[int] = set()
    for concept in concepts:
        if concept.id in seen:
            continue
        seen.add(concept.id)
        _add(units, "concept", concept.name, concept=concept.name)
        for alias in concept.aliases:
            _add(units, "concept", alias.alias, concept=concept.name)


def _block_units(
    units: list[Unit], block: ArtifactBlock, content: dict[str, Any], section: str | None,
    locked: bool,
) -> str | None:
    """Add a block's public text; return the section heading in effect after it."""
    public = public_content(content)
    where = {"block": block.key, "locked": locked}
    if block.type == "prose":
        for heading, text in markdown_sections(str(public.get("markdown", ""))):
            if heading is not None:
                section = heading
                _add(units, "heading", heading, section=section, context=text or None, **where)
            _add(units, "body", text, section=section, **where)
        return section
    if block.type in ("multiple_choice", "prediction"):
        question = public.get("question" if block.type == "multiple_choice" else "prompt", "")
        _add(units, "body", plain_text(str(question)), section=section, **where)
        for option in public.get("options") or []:
            _add(units, "body", plain_text(str(option)), section=section, **where)
    elif block.type == "blurb":
        _add(units, "body", plain_text(str(public.get("prompt", ""))), section=section, **where)
    elif block.type == "chart":
        for key in ("title", "x_label", "y_label", "caption"):
            _add(units, "body", plain_text(str(public.get(key) or "")), section=section, **where)
    elif block.type == "interactable":
        for key in ("title", "instructions"):
            _add(units, "body", plain_text(str(public.get(key) or "")), section=section, **where)
    elif block.type == "figure":
        # What the reader sees: the label, the writer's caption and the source's own caption
        # (shown on request); the image itself is not searchable.
        for key in ("label", "caption", "source_caption"):
            _add(units, "body", plain_text(str(public.get(key) or "")), section=section, **where)
    else:
        raise ValueError(f"block {block.key}: search does not know block type {block.type!r}")
    return section


def _year(source: KnowledgeSource) -> str | None:
    value = source.originated_on
    return value[:4] if value and value[:4].isdigit() else value


class _Library:
    """Everything the search needs, loaded once per query in a fixed number of queries (plus
    each ready study page's reading state and block files)."""

    def __init__(self, session: Session, settings: Settings, max_attempts: int) -> None:
        self.session = session
        self.settings = settings
        self.max_attempts = max_attempts
        self.sources = list(session.scalars(select(KnowledgeSource).order_by(KnowledgeSource.id)))
        self.source_by_id = {s.id: s for s in self.sources}
        self.artifacts = list(session.scalars(select(Artifact).order_by(Artifact.id)))
        self.artifact_by_slug = {a.slug: a for a in self.artifacts}
        self.superseded = {
            a.supersedes_id for a in self.artifacts if a.supersedes_id is not None
        }
        self.sources_of: dict[int, list[KnowledgeSource]] = defaultdict(list)
        self.artifacts_of_source: dict[int, list[Artifact]] = defaultdict(list)
        by_id = {a.id: a for a in self.artifacts}
        for artifact_id, source_id in session.execute(
            select(ArtifactSource.artifact_id, ArtifactSource.source_id).order_by(
                ArtifactSource.source_id
            )
        ):
            self.sources_of[artifact_id].append(self.source_by_id[source_id])
            self.artifacts_of_source[source_id].append(by_id[artifact_id])
        self.source_concepts: dict[int, list[Concept]] = defaultdict(list)
        for link, concept in session.execute(
            select(SourceConcept, Concept)
            .join(Concept, Concept.id == SourceConcept.concept_id)
            .order_by(Concept.name_norm)
        ):
            self.source_concepts[link.source_id].append(concept)
        self.block_concepts: dict[int, list[Concept]] = defaultdict(list)  # by artifact id
        for artifact_id, concept in session.execute(
            select(ArtifactBlock.artifact_id, Concept)
            .join(BlockConcept, BlockConcept.block_id == ArtifactBlock.id)
            .join(Concept, Concept.id == BlockConcept.concept_id)
            .order_by(ArtifactBlock.position, Concept.name_norm)
        ):
            self.block_concepts[artifact_id].append(concept)
        self.glossary: dict[int, list[GlossaryTerm]] = defaultdict(list)
        for term in session.scalars(
            select(GlossaryTerm).order_by(GlossaryTerm.artifact_id, GlossaryTerm.position)
        ):
            self.glossary[term.artifact_id].append(term)
        history = StudyHistoryManager(session, settings)
        self.last_studied = history.last_studied()
        self.seconds = history.seconds_on_artifacts()
        self.source_opened: dict[int, datetime] = {}
        for source_id, at in session.execute(
            select(StudyEvent.source_id, func.max(StudyEvent.occurred_at))
            .where(StudyEvent.kind == "source_opened", StudyEvent.source_id.is_not(None))
            .group_by(StudyEvent.source_id)
        ):
            self.source_opened[source_id] = at
        self.source_seconds: dict[int, float] = {
            source_id: float(seconds or 0.0)
            for source_id, seconds in session.execute(
                select(AttentionSpan.source_id, func.sum(AttentionSpan.active_seconds))
                .where(AttentionSpan.surface == "source", AttentionSpan.source_id.is_not(None))
                .group_by(AttentionSpan.source_id)
            )
        }

    # -- per kind ------------------------------------------------------------------------------

    def _common(self, units: list[Unit], artifact: Artifact, sources: Sequence[KnowledgeSource]) -> None:
        _add(units, "title", artifact.title)
        _add(units, "summary", artifact.summary)
        for source in sources:
            for name in source.creators or []:
                _add(units, "authors", name)

    def _artifact_study(self, artifact: Artifact, state: ReadingState | None) -> dict[str, Any]:
        last = self.last_studied.get(artifact.id)
        gates_total = state.gates_total if state is not None else None
        gates_cleared = state.gates_cleared if state is not None else None
        if artifact.status != "ready":
            status = "not_ready"
        elif last is None:
            status = "not_opened"
        elif state is not None and state.gates_total and state.complete:
            status = "complete"
        else:
            status = "in_progress"
        return {
            "state": status,
            "active_seconds": self.seconds.get(artifact.id, 0.0),
            "last_studied_at": iso(last),
            "gates_total": gates_total,
            "gates_cleared": gates_cleared,
        }

    def study_page(self, artifact: Artifact) -> Item:
        sources = self.sources_of.get(artifact.id, [])
        units: list[Unit] = []
        self._common(units, artifact, sources)
        _concept_units(units, self.block_concepts.get(artifact.id, []))
        for term in self.glossary.get(artifact.id, []):
            names: list[str] = []
            for name in [term.term, term.expansion, *(term.aliases or [])]:
                if name and name.strip() and name.strip().casefold() not in {n.casefold() for n in names}:
                    names.append(name.strip())
            definition = plain_text(term.definition)
            _add(units, "glossary", ", ".join(names), term=term.term, context=definition or None)
            _add(units, "body", definition, term=term.term)
        state = None
        if artifact.status == "ready":
            state = reading_state(self.session, self.settings, artifact,
                                  max_attempts=self.max_attempts)
            blocks = ArtifactBlockManager(self.session, self.settings)
            section: str | None = None
            for block_state in state.blocks:
                content = blocks.read_content(block_state.block)
                section = _block_units(units, block_state.block, content, section,
                                       locked=not block_state.visible)
        return Item(
            kind="study_page",
            slug=artifact.slug,
            title=artifact.title,
            url=f"/artifacts/{artifact.slug}",
            summary=artifact.summary,
            authors=[n for s in sources for n in (s.creators or [])],
            year=next((_year(s) for s in sources if s.originated_on), None),
            status=artifact.status,
            superseded=artifact.id in self.superseded,
            study=self._artifact_study(artifact, state),
            units=units,
        )

    def lab(self, artifact: Artifact) -> Item:
        sources = list(self.sources_of.get(artifact.id, []))
        page_slug = (artifact.params or {}).get(STUDY_PAGE_PARAM)
        page = self.artifact_by_slug.get(page_slug) if isinstance(page_slug, str) else None
        if not sources and page is not None:
            sources = list(self.sources_of.get(page.id, []))
        units: list[Unit] = []
        self._common(units, artifact, sources)
        return Item(
            kind="lab",
            slug=artifact.slug,
            title=artifact.title,
            url=f"/labs/{artifact.slug}",
            summary=artifact.summary,
            authors=[n for s in sources for n in (s.creators or [])],
            year=next((_year(s) for s in sources if s.originated_on), None),
            status=artifact.status,
            superseded=artifact.id in self.superseded,
            study=self._artifact_study(artifact, None),
            units=units,
        )

    def source(self, source: KnowledgeSource) -> Item:
        units: list[Unit] = []
        _add(units, "title", source.title)
        _add(units, "summary", source.summary)
        for name in source.creators or []:
            _add(units, "authors", name)
        _concept_units(units, self.source_concepts.get(source.id, []))
        opened = self.source_opened.get(source.id)
        pages = [a for a in self.artifacts_of_source.get(source.id, []) if a.kind != LAB_KIND]
        return Item(
            kind="source",
            slug=source.slug,
            title=source.title,
            url=f"/sources/{source.slug}",
            summary=source.summary,
            authors=list(source.creators or []),
            year=_year(source),
            status=None,
            superseded=False,
            study={
                "state": "opened" if opened is not None else "not_opened",
                "active_seconds": self.source_seconds.get(source.id, 0.0),
                "last_studied_at": iso(opened),
                "study_pages": len(pages),
            },
            units=units,
        )

    def items(self) -> list[Item]:
        out = [
            self.lab(a) if a.kind == LAB_KIND else self.study_page(a) for a in self.artifacts
        ]
        out.extend(self.source(s) for s in self.sources)
        return out


# -- scoring and results --------------------------------------------------------------------------


def segments(text: str, spans: Sequence[tuple[int, int]]) -> list[dict[str, Any]]:
    """`text` as `{"text", "mark"}` segments, the `spans` marked (they must not overlap)."""
    out: list[dict[str, Any]] = []
    at = 0
    for start, end in spans:
        if start > at:
            out.append({"text": text[at:start], "mark": False})
        out.append({"text": text[start:end], "mark": True})
        at = end
    if at < len(text):
        out.append({"text": text[at:], "mark": False})
    return out


def snippet(text: str, query: Query, *, chars: int = SNIPPET_CHARS) -> list[dict[str, Any]]:
    """A window of about `chars` characters of `text` around its first matched word, cut at
    word boundaries (with an ellipsis where text is left out), as marked segments."""
    spans = query.marks(text)
    if len(text) <= chars:
        return segments(text, spans)
    first_start, first_end = spans[0] if spans else (0, 0)
    start = max(0, first_start - SNIPPET_LEAD)
    end = min(len(text), start + chars)
    start = max(0, end - chars)  # near the end of the text: still a full window
    if start > 0:  # begin at a word: after the first space before the first marked word
        space = text.find(" ", start, first_start + 1)
        if space != -1:
            start = space + 1
    if end < len(text):  # end at a word, never inside the first marked word
        space = text.rfind(" ", max(start, first_end), end + 1)
        if space != -1:
            end = space
    inner = [(s - start, e - start) for s, e in spans if s >= start and e <= end]
    out = segments(text[start:end], inner)
    if start > 0:
        out.insert(0, {"text": "… ", "mark": False})
    if end < len(text):
        out.append({"text": " …", "mark": False})
    return out


@dataclass(slots=True)
class Result:
    item: Item
    score: float
    fields: list[str]
    best: Unit

    def record(self, query: Query) -> dict[str, Any]:
        item, best = self.item, self.best
        title_spans = query.marks(item.title)
        if best.field == "title":
            text = item.summary or ""
            snip = snippet(plain_text(text), query) if text.strip() else []
        elif best.locked:
            snip = []
        elif best.field == "heading" and best.context:
            snip = snippet(best.context, query)
        elif best.field == "glossary" and best.context:
            snip = snippet(f"{best.text}: {best.context}", query)
        else:
            snip = snippet(best.text, query)
        href = None
        if best.block is not None and not best.locked and item.kind == "study_page":
            href = f"{item.url}#block-{best.block}"
        return {
            "kind": item.kind,
            "slug": item.slug,
            "title": item.title,
            "title_segments": segments(item.title, title_spans),
            "url": item.url,
            "authors": item.authors,
            "year": item.year,
            "status": item.status,
            "superseded": item.superseded,
            "score": round(self.score, 3),
            "fields": self.fields,
            "match": {
                "field": best.field,
                "section": best.section if not best.locked else None,
                "block": best.block if not best.locked else None,
                "term": best.term,
                "concept": best.concept,
                "locked": best.locked,
                "href": href,
            },
            "snippet": snip,
            "study": item.study,
        }


def score_item(item: Item, query: Query) -> Result | None:
    """The item's Result for `query`, or None unless every query word matches in it."""
    best_weight: dict[str, float] = {}
    fields: set[str] = set()
    candidates: list[tuple[tuple[Any, ...], Unit]] = []
    for unit in item.units:
        found = query.matches(_word_set(unit.text))
        if not found:
            continue
        weight = FIELDS[unit.field]
        fields.add(unit.field)
        for word, exact in found.items():
            value = weight if exact else weight * PREFIX_FACTOR
            if value > best_weight.get(word, 0.0):
                best_weight[word] = value
        # the place that best shows the match (see the module docstring)
        candidates.append(
            ((-len(found), unit.locked, unit.field in NAME_FIELDS, -weight, unit.order), unit)
        )
    if len(best_weight) < len(query.words):
        return None
    score = sum(best_weight.values()) + FIELD_BONUS * (len(fields) - 1)
    title_words = query.matches(_word_set(item.title))
    if len(title_words) == len(query.words):
        score += TITLE_ALL_BONUS
        folded_title = " ".join(w for w, _, _ in _words(item.title))
        if " ".join(query.words) in folded_title:
            score += TITLE_PHRASE_BONUS
    if item.superseded:
        score *= SUPERSEDED_FACTOR
    if item.status is not None and item.status != "ready":
        score *= NOT_READY_FACTOR
    candidates.sort(key=lambda c: c[0])
    best = candidates[0][1]
    ordered = [f for f in FIELDS if f in fields]
    return Result(item=item, score=score, fields=ordered, best=best)


@dataclass(frozen=True, slots=True)
class SearchResults:
    query: Query
    total: int
    limit: int
    results: list[dict[str, Any]]

    def record(self) -> dict[str, Any]:
        return {
            "query": self.query.text,
            "words": list(self.query.words),
            "prefix": self.query.prefix_last,
            "total": self.total,
            "limit": self.limit,
            "results": self.results,
        }


def search(
    session: Session, settings: Settings, text: str, *, max_attempts: int,
    limit: int = DEFAULT_LIMIT,
) -> SearchResults:
    """Search the library for `text` (see the module docstring); the best `limit` results."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
        raise QueryError(f"limit must be an integer from 1 to {MAX_LIMIT}")
    query = parse_query(text)
    library = _Library(session, settings, max_attempts)
    found = [r for r in (score_item(item, query) for item in library.items()) if r is not None]
    kind_order = {k: i for i, k in enumerate(KINDS)}
    found.sort(key=lambda r: (r.item.title.casefold(),))
    found.sort(key=lambda r: r.item.study.get("last_studied_at") or "", reverse=True)
    found.sort(key=lambda r: (-round(r.score, 6), kind_order[r.item.kind]))
    return SearchResults(
        query=query,
        total=len(found),
        limit=limit,
        results=[r.record(query) for r in found[:limit]],
    )


__all__ = [
    "DEFAULT_LIMIT",
    "FIELDS",
    "FIELD_BONUS",
    "KINDS",
    "MAX_LIMIT",
    "MAX_QUERY_CHARS",
    "MAX_QUERY_WORDS",
    "MIN_QUERY_CHARS",
    "Query",
    "QueryError",
    "SearchResults",
    "parse_query",
    "search",
    "segments",
    "snippet",
]
