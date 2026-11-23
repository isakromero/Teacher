"""SourceReferenceManager: what each knowledge source cites, matched against the library.

A source's reference list is extracted by `teacher.reference_extraction` and stored here as
`source_references` rows (in printed order), with the state of the extraction in
`source_reference_extractions` (no row: never run). This module does no model call.

Matching (`LibraryIndex.match`) resolves one reference to a library source, trying in order:

1. DOI: the reference's DOI (and a DOI in its URL) against the library's `doi` identifiers (and
   a doi.org address in a source's `origin_url`). DOIs are compared case-insensitively, without
   a `https://doi.org/` or `doi:` prefix and without trailing `.`, `,` or `;`.
2. arXiv id: the reference's arXiv id (also one read from an arXiv DOI `10.48550/arXiv.<id>`
   or an arxiv.org address) against the library's `arxiv` identifiers (and arXiv DOIs and
   arxiv.org addresses in `origin_url`), with any version suffix (`v2`) and `arXiv:` prefix
   removed.
3. Title: the normalised titles must be equal (see `title_key`). Only titles of at least
   `MIN_TITLE_WORDS` words and `MIN_TITLE_CHARS` letters and digits are compared (a short title
   such as "Deep Learning" names several works), the library must hold exactly one source with
   that title, and when both years are known they may differ by at most `MAX_YEAR_GAP` (a
   preprint and its publication).

A key that the library holds for two different sources is ambiguous and matches nothing (the
next method is tried). Library identifiers that are not a valid DOI or arXiv id are not used.
A wrong match is worse than a missed one, so there is no fuzzy matching.

Matching is re-run (`rematch_all`) for every stored reference whenever the library changes in
a way that can change a match: a source is added (upload or URL), deleted, or its title, date
or origin URL is edited (`teacher.web.routers.references.rematch_library`, called by the sources
router), so old references resolve to newly added sources. A new extraction matches its own
references when it stores them.

`citation_edges()` gives the resolved citations between library sources, (citing source id,
cited source id), for the source graph.
"""

from __future__ import annotations

import builtins
import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import NamedTuple
from urllib.parse import quote

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from teacher.config import Settings
from teacher.db.models import (
    REFERENCE_FOUND_IN,
    KnowledgeSource,
    KnowledgeSourceIdentifier,
    SourceReference,
    SourceReferenceExtraction,
    utcnow,
)
from teacher.managers.errors import ManagerError, NotFoundError

# A run still `running` after this long was interrupted (the server stopped during the model
# call): it is reported as such and a new run may take its place. The extractor's own timeout,
# with the SDK's retries, stays well inside it.
STALE_RUN_AFTER = timedelta(minutes=30)

MIN_TITLE_WORDS = 3
MIN_TITLE_CHARS = 12
MAX_YEAR_GAP = 1

# The states the API reports (`extraction_state`).
EXTRACTION_STATES: tuple[str, ...] = ("never_run", "running", "interrupted", "done", "failed")


class ExtractionBusyError(ManagerError):
    """An extraction of this source's references is already running (HTTP 409)."""


class ExtractionSupersededError(ManagerError):
    """The run being recorded is no longer the source's current run (it was taken over after
    it went stale); nothing was recorded."""


# -- identifiers and titles -------------------------------------------------------------------

_SPACE = re.compile(r"\s+")
_DOI_PREFIX = re.compile(r"^(?:https?://(?:dx\.)?doi\.org/|doi\s*:\s*)", re.I)
_DOI = re.compile(r"^10\.\d{4,9}/\S+$")
_DOI_IN_URL = re.compile(r"^https?://(?:dx\.)?doi\.org/(10\.\d{4,9}/\S+)$", re.I)
_ARXIV_PREFIX = re.compile(
    r"^(?:https?://(?:www\.|export\.)?arxiv\.org/(?:abs|pdf)/|arxiv\s*:\s*)", re.I
)
_ARXIV_URL = re.compile(r"^https?://(?:www\.|export\.)?arxiv\.org/(?:abs|pdf)/(\S+)$", re.I)
_ARXIV_VERSION = re.compile(r"v\d+$", re.I)
_ARXIV_ID = re.compile(r"^(?:\d{4}\.\d{4,5}|[a-z][a-z\-]*(?:\.[A-Z]{2})?/\d{7})$")
_ARXIV_DOI = re.compile(r"^10\.48550/arxiv\.(.+)$", re.I)
_NOT_WORD = re.compile(r"[\W_]+")
_YEAR = re.compile(r"^(\d{4})")


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    value = _SPACE.sub(" ", value).strip()
    return value or None


def parse_doi(value: str | None) -> str | None:
    """`value` as a bare, lower-case DOI (`10.1145/3065386`), or None if it is not one. A
    `https://doi.org/` or `doi:` prefix and trailing `.`, `,` and `;` are removed."""
    value = _clean(value)
    if value is None:
        return None
    value = _DOI_PREFIX.sub("", value).strip().rstrip(".,;").lower()
    return value if _DOI.fullmatch(value) else None


def parse_arxiv_id(value: str | None) -> str | None:
    """`value` as a bare arXiv id without version (`1706.03762`, `hep-th/9711200`), or None if
    it is not one. An `arXiv:` prefix, an arxiv.org address, `.pdf` and a version suffix are
    removed."""
    value = _clean(value)
    if value is None:
        return None
    value = _ARXIV_PREFIX.sub("", value).strip().rstrip("/")
    value = _ARXIV_VERSION.sub("", value.removesuffix(".pdf"))
    return value if _ARXIV_ID.fullmatch(value) else None


def doi_from_url(url: str | None) -> str | None:
    """The DOI of a doi.org address, or None."""
    url = _clean(url)
    if url is None:
        return None
    found = _DOI_IN_URL.match(url)
    return parse_doi(found.group(1)) if found else None


def arxiv_from_url(url: str | None) -> str | None:
    """The arXiv id of an arxiv.org abs/pdf address, or None."""
    url = _clean(url)
    if url is None:
        return None
    found = _ARXIV_URL.match(url)
    return parse_arxiv_id(found.group(1)) if found else None


def arxiv_from_doi(doi: str | None) -> str | None:
    """The arXiv id of an arXiv DOI (`10.48550/arXiv.1706.03762`), or None."""
    if doi is None:
        return None
    found = _ARXIV_DOI.match(doi)
    return parse_arxiv_id(found.group(1)) if found else None


def title_key(title: str | None) -> str | None:
    """The normalised form two titles are compared in, or None when the title is too short to
    be compared safely.

    Normalisation: Unicode compatibility decomposition (NFKD) with combining marks (accents)
    removed, case folded, every run of characters that are not letters or digits (punctuation,
    hyphens, quotes, dashes, LaTeX braces, underscores) replaced by one space, and the ends
    trimmed. "BERT: Pre-training of Deep Bidirectional Transformers" becomes "bert pre training
    of deep bidirectional transformers". A title with fewer than `MIN_TITLE_WORDS` words or
    `MIN_TITLE_CHARS` letters and digits gives None.
    """
    title = _clean(title)
    if title is None:
        return None
    decomposed = unicodedata.normalize("NFKD", title)
    plain = "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()
    words = _NOT_WORD.sub(" ", plain).split()
    if len(words) < MIN_TITLE_WORDS or sum(len(w) for w in words) < MIN_TITLE_CHARS:
        return None
    return " ".join(words)


@dataclass(frozen=True)
class ReferenceKeys:
    """The keys one reference can be matched or grouped by."""

    dois: tuple[str, ...]
    arxiv_ids: tuple[str, ...]
    title: str | None
    year: int | None


def reference_keys(
    *, doi: str | None, arxiv_id: str | None, url: str | None, title: str | None, year: int | None
) -> ReferenceKeys:
    dois = [d for d in (parse_doi(doi), doi_from_url(url)) if d]
    arxiv = [a for a in (parse_arxiv_id(arxiv_id), arxiv_from_url(url)) if a]
    arxiv += [a for a in (arxiv_from_doi(d) for d in dois) if a]
    return ReferenceKeys(
        tuple(dict.fromkeys(dois)), tuple(dict.fromkeys(arxiv)), title_key(title), year
    )


def keys_of(ref: SourceReference) -> ReferenceKeys:
    return reference_keys(
        doi=ref.doi, arxiv_id=ref.arxiv_id, url=ref.url, title=ref.title, year=ref.year
    )


def add_url(*, doi: str | None, arxiv_id: str | None, url: str | None) -> tuple[str, str] | None:
    """The best address to add a cited work from, and what it is: the arXiv PDF ("arxiv", a
    PDF Teacher stores as a paper), else the doi.org link ("doi", the publisher's page), else
    the address printed in the reference ("url"); None when none is known."""
    keys = reference_keys(doi=doi, arxiv_id=arxiv_id, url=url, title=None, year=None)
    if keys.arxiv_ids:
        return f"https://arxiv.org/pdf/{keys.arxiv_ids[0]}", "arxiv"
    if keys.dois:
        return f"https://doi.org/{quote(keys.dois[0], safe='/:;()._-')}", "doi"
    if url:
        return url, "url"
    return None


# -- the library ------------------------------------------------------------------------------


def _year_of(originated_on: str | None) -> int | None:
    found = _YEAR.match(originated_on or "")
    return int(found.group(1)) if found else None


@dataclass
class LibraryIndex:
    """The library's DOIs, arXiv ids and titles, each with the ids of the sources holding it."""

    dois: dict[str, set[int]] = field(default_factory=lambda: defaultdict(set))
    arxiv_ids: dict[str, set[int]] = field(default_factory=lambda: defaultdict(set))
    titles: dict[str, list[tuple[int, int | None]]] = field(
        default_factory=lambda: defaultdict(list)
    )

    @classmethod
    def load(cls, session: Session) -> LibraryIndex:
        index = cls()
        rows = session.execute(
            select(
                KnowledgeSource.id,
                KnowledgeSource.title,
                KnowledgeSource.originated_on,
                KnowledgeSource.origin_url,
            )
        ).all()
        for source_id, title, originated_on, origin_url in rows:
            index.add_source(source_id, title, originated_on, origin_url)
        identifiers = session.execute(
            select(
                KnowledgeSourceIdentifier.source_id,
                KnowledgeSourceIdentifier.scheme,
                KnowledgeSourceIdentifier.value,
            )
        ).all()
        for source_id, scheme, value in identifiers:
            index.add_identifier(source_id, scheme, value)
        return index

    def add_source(
        self, source_id: int, title: str, originated_on: str | None, origin_url: str | None
    ) -> None:
        key = title_key(title)
        if key is not None:
            self.titles[key].append((source_id, _year_of(originated_on)))
        doi = doi_from_url(origin_url)
        if doi is not None:
            self.add_identifier(source_id, "doi", doi)
        arxiv = arxiv_from_url(origin_url)
        if arxiv is not None:
            self.arxiv_ids[arxiv].add(source_id)

    def add_identifier(self, source_id: int, scheme: str, value: str) -> None:
        scheme = scheme.strip().lower()
        if scheme == "doi":
            doi = parse_doi(value)
            if doi is not None:
                self.dois[doi].add(source_id)
                arxiv = arxiv_from_doi(doi)
                if arxiv is not None:
                    self.arxiv_ids[arxiv].add(source_id)
        elif scheme == "arxiv":
            arxiv = parse_arxiv_id(value)
            if arxiv is not None:
                self.arxiv_ids[arxiv].add(source_id)

    def match(self, keys: ReferenceKeys) -> tuple[int, str] | None:
        """(library source id, method) for a reference, or None (see the module docstring)."""
        for method, table, values in (
            ("doi", self.dois, keys.dois),
            ("arxiv", self.arxiv_ids, keys.arxiv_ids),
        ):
            for value in values:
                holders = table.get(value, set())
                if len(holders) == 1:
                    return next(iter(holders)), method
        if keys.title is not None:
            holders = self.titles.get(keys.title, [])
            if len(holders) == 1:
                source_id, year = holders[0]
                if keys.year is None or year is None or abs(keys.year - year) <= MAX_YEAR_GAP:
                    return source_id, "title"
        return None


# -- what is stored ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReferenceSpec:
    """One reference as an extraction produced it (already validated and normalised)."""

    raw_text: str
    title: str | None = None
    authors: tuple[str, ...] = ()
    year: int | None = None
    venue: str | None = None
    doi: str | None = None
    arxiv_id: str | None = None
    url: str | None = None


@dataclass(frozen=True)
class Claim:
    """A run of the extraction that this caller holds: record it with `store` or `fail`."""

    source_id: int
    slug: str
    run_number: int
    started_at: datetime
    trigger: str
    extractor: str
    model: str


class CitationEdge(NamedTuple):
    """A resolved citation between two library sources (ids of `knowledge_sources`)."""

    citing_id: int
    cited_id: int


@dataclass(frozen=True)
class RematchResult:
    total: int  # references looked at
    matched: int  # of them matched now
    changed: int  # whose match changed


@dataclass
class FrontierItem:
    """One cited work that is not in the library, with every reference to it."""

    key: str
    references: builtins.list[SourceReference]
    citing: builtins.list[KnowledgeSource]

    @property
    def best(self) -> SourceReference:
        """The reference that says the most about the work (the earliest of equals)."""
        return max(
            self.references,
            key=lambda r: (
                r.title is not None, r.doi is not None, r.arxiv_id is not None,
                r.url is not None, r.year is not None, bool(r.authors), -r.id,
            ),
        )  # fmt: skip

    def first_value(self, name: str) -> object:
        """The first non-empty value of `name` among the references, best first."""
        best = self.best
        for ref in [best, *(r for r in self.references if r is not best)]:
            value = getattr(ref, name)
            if value:
                return value
        return None


def extraction_state(row: SourceReferenceExtraction | None, now: datetime) -> str:
    """One of `EXTRACTION_STATES` for a source's extraction row (None: never run)."""
    if row is None:
        return "never_run"
    if row.status == "running":
        return "interrupted" if now - row.started_at > STALE_RUN_AFTER else "running"
    if row.status == "succeeded":
        return "done"
    if row.status == "failed":
        return "failed"
    raise ValueError(f"unknown reference extraction status {row.status!r}")


class _Groups:
    """Union-find over reference ids."""

    def __init__(self) -> None:
        self.parent: dict[int, int] = {}

    def find(self, item: int) -> int:
        self.parent.setdefault(item, item)
        while self.parent[item] != item:
            self.parent[item] = self.parent[self.parent[item]]
            item = self.parent[item]
        return item

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


class SourceReferenceManager:
    """Stores extracted references, matches them to the library, and answers what cites what."""

    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings

    # -- the extraction state -------------------------------------------------------------------

    def extraction(self, source: KnowledgeSource) -> SourceReferenceExtraction | None:
        """The source's extraction row, read fresh (None: never run)."""
        return self.session.get(
            SourceReferenceExtraction, source.id, populate_existing=True
        )

    def claim(
        self,
        source: KnowledgeSource,
        *,
        trigger: str,
        extractor: str,
        model: str,
        now: datetime | None = None,
    ) -> Claim:
        """Start a run for `source`: its row becomes `running` with the next run number.
        ExtractionBusyError when a run is already running (and not stale). The first statement
        is the write, so the check and the claim happen under SQLite's write lock: two callers
        can never both claim."""
        for name, value in (("trigger", trigger), ("extractor", extractor), ("model", model)):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        now = now or utcnow()
        table = SourceReferenceExtraction
        result = self.session.execute(
            update(table)
            .where(
                table.source_id == source.id,
                or_(table.status != "running", table.started_at < now - STALE_RUN_AFTER),
            )
            .values(
                status="running", run_number=table.run_number + 1, trigger=trigger,
                extractor=extractor, model=model, started_at=now, finished_at=None,
                error=None, input_tokens=None, output_tokens=None,
            )  # fmt: skip
            .execution_options(synchronize_session=False)
        )
        row = self.session.get(table, source.id, populate_existing=True)
        if result.rowcount != 1:
            if row is not None:
                raise ExtractionBusyError(
                    f"the references of {source.slug!r} are already being extracted (started "
                    f"{row.started_at.isoformat(timespec='seconds')} UTC); wait for that run "
                    "to finish"
                )
            row = table(
                source_id=source.id, status="running", run_number=1, trigger=trigger,
                extractor=extractor, model=model, started_at=now,
            )  # fmt: skip
            try:
                with self.session.begin_nested():
                    self.session.add(row)
                    self.session.flush()
            except IntegrityError as exc:
                raise ExtractionBusyError(
                    f"the references of {source.slug!r} are already being extracted"
                ) from exc
        return Claim(
            source_id=source.id, slug=source.slug, run_number=row.run_number,
            started_at=row.started_at, trigger=trigger, extractor=extractor, model=model,
        )  # fmt: skip

    def _finish(self, claim: Claim, **values: object) -> bool:
        table = SourceReferenceExtraction
        result = self.session.execute(
            update(table)
            .where(
                table.source_id == claim.source_id,
                table.status == "running",
                table.run_number == claim.run_number,
            )
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        return result.rowcount == 1

    def store(
        self,
        claim: Claim,
        references: Sequence[ReferenceSpec],
        *,
        found_in: str,
        model: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        now: datetime | None = None,
    ) -> int:
        """Record the claimed run as succeeded: replace the source's references with
        `references` (positions 1..n in order) and match them. Returns how many matched.
        NotFoundError if the source is gone; ExtractionSupersededError if the run is no longer
        the current one (nothing is changed then)."""
        if found_in not in REFERENCE_FOUND_IN:
            raise ValueError(f"found_in must be one of {REFERENCE_FOUND_IN}, got {found_in!r}")
        if found_in == "none" and references:
            raise ValueError("found_in is 'none' but references were given")
        for number, spec in enumerate(references, start=1):
            if not isinstance(spec.raw_text, str) or not spec.raw_text.strip():
                raise ValueError(f"reference {number} has no text")
        now = now or utcnow()
        values: dict[str, object] = {
            "status": "succeeded", "finished_at": now, "succeeded_at": now,
            "found_in": found_in, "reference_count": len(references),
            "input_tokens": input_tokens, "output_tokens": output_tokens,
        }  # fmt: skip
        if model is not None:
            values["model"] = model
        if not self._finish(claim, **values):
            self._missing_or_superseded(claim)
        self.session.execute(
            delete(SourceReference)
            .where(SourceReference.source_id == claim.source_id)
            .execution_options(synchronize_session=False)
        )
        rows = [
            SourceReference(
                source_id=claim.source_id, position=position, raw_text=spec.raw_text,
                title=spec.title, authors=list(spec.authors), year=spec.year,
                venue=spec.venue, doi=spec.doi, arxiv_id=spec.arxiv_id, url=spec.url,
            )  # fmt: skip
            for position, spec in enumerate(references, start=1)
        ]
        self.session.add_all(rows)
        self.session.flush()
        return self._match(rows).matched

    def fail(
        self,
        claim: Claim,
        error: str,
        *,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        now: datetime | None = None,
    ) -> bool:
        """Record the claimed run as failed with `error`; the stored references stay as they
        were. False (nothing recorded) when the source is gone or the run is no longer the
        current one."""
        if not isinstance(error, str) or not error.strip():
            raise ValueError("error must be a non-empty string")
        return self._finish(
            claim, status="failed", finished_at=now or utcnow(), error=error,
            input_tokens=input_tokens, output_tokens=output_tokens,
        )  # fmt: skip

    def _missing_or_superseded(self, claim: Claim) -> None:
        if self.session.get(KnowledgeSource, claim.source_id) is None:
            raise NotFoundError(
                f"knowledge source {claim.slug!r} was deleted while its references were "
                "extracted"
            )
        raise ExtractionSupersededError(
            f"the extraction run {claim.run_number} of {claim.slug!r} is no longer the current "
            "one (it was taken over after it stopped responding); its result was not stored"
        )

    def counts(self, now: datetime | None = None) -> dict[str, int]:
        """How many library sources are in each of `EXTRACTION_STATES`, plus `sources`."""
        now = now or utcnow()
        total = self.session.scalar(select(func.count(KnowledgeSource.id))) or 0
        counts = dict.fromkeys(EXTRACTION_STATES, 0)
        for row in self.session.scalars(select(SourceReferenceExtraction)):
            counts[extraction_state(row, now)] += 1
        counts["never_run"] = total - sum(counts.values())
        counts["sources"] = total
        return counts

    # -- references and matches -------------------------------------------------------------

    def references(self, source: KnowledgeSource) -> builtins.list[SourceReference]:
        """The source's stored references in printed order."""
        return builtins.list(
            self.session.scalars(
                select(SourceReference)
                .where(SourceReference.source_id == source.id)
                .order_by(SourceReference.position)
            )
        )

    def sources_by_id(self, ids: Iterable[int]) -> dict[int, KnowledgeSource]:
        wanted = sorted(set(ids))
        if not wanted:
            return {}
        return {
            s.id: s
            for s in self.session.scalars(
                select(KnowledgeSource).where(KnowledgeSource.id.in_(wanted))
            )
        }

    def cited_by(
        self, source: KnowledgeSource
    ) -> builtins.list[tuple[KnowledgeSource, builtins.list[int]]]:
        """The other library sources whose references matched `source`, each with the
        positions of those references, by title."""
        rows = self.session.execute(
            select(SourceReference.source_id, SourceReference.position)
            .where(
                SourceReference.matched_source_id == source.id,
                SourceReference.source_id != source.id,
            )
            .order_by(SourceReference.source_id, SourceReference.position)
        ).all()
        positions: dict[int, builtins.list[int]] = defaultdict(builtins.list)
        for citing_id, position in rows:
            positions[citing_id].append(position)
        sources = self.sources_by_id(positions)
        return sorted(
            ((sources[i], p) for i, p in positions.items()),
            key=lambda pair: (pair[0].title.casefold(), pair[0].id),
        )

    def _match(
        self, refs: Iterable[SourceReference], index: LibraryIndex | None = None
    ) -> RematchResult:
        index = index if index is not None else LibraryIndex.load(self.session)
        total = matched = changed = 0
        for ref in refs:
            total += 1
            found = index.match(keys_of(ref))
            target, method = found if found is not None else (None, None)
            if target is not None:
                matched += 1
            if (ref.matched_source_id, ref.match_method) != (target, method):
                ref.matched_source_id, ref.match_method = target, method
                changed += 1
        self.session.flush()
        return RematchResult(total, matched, changed)

    def rematch_all(self) -> RematchResult:
        """Match every stored reference against the library as it is now (see the module
        docstring for when the app calls this). Clears a match whose source is gone."""
        return self._match(self.session.scalars(select(SourceReference)))

    def citation_edges(self) -> builtins.list[CitationEdge]:
        """Every resolved citation between two different library sources, once, in id order."""
        rows = self.session.execute(
            select(SourceReference.source_id, SourceReference.matched_source_id)
            .where(
                SourceReference.matched_source_id.is_not(None),
                SourceReference.matched_source_id != SourceReference.source_id,
            )
            .distinct()
            .order_by(SourceReference.source_id, SourceReference.matched_source_id)
        ).all()
        return [CitationEdge(citing, cited) for citing, cited in rows]

    # -- the reading frontier -----------------------------------------------------------------

    def frontier(self) -> builtins.list[FrontierItem]:
        """The cited works that are not in the library: unmatched references grouped so that
        references sharing a DOI, an arXiv id, a normalised title (`title_key`) or exactly the
        same printed address are one work; a reference with none of these is a work of its own. Ranked by how many library sources
        cite the work, then how many references name it, then title."""
        refs = builtins.list(
            self.session.scalars(
                select(SourceReference)
                .where(SourceReference.matched_source_id.is_(None))
                .order_by(SourceReference.id)
            )
        )
        groups = _Groups()
        first_with: dict[str, int] = {}
        key_of: dict[int, str] = {}
        for ref in refs:
            groups.find(ref.id)
            keys = keys_of(ref)
            names = [f"doi:{d}" for d in keys.dois] + [f"arxiv:{a}" for a in keys.arxiv_ids]
            if keys.title is not None:
                names.append(f"title:{keys.title}")
            if ref.url:
                names.append(f"url:{ref.url}")
            key_of[ref.id] = names[0] if names else f"reference:{ref.id}"
            for name in names:
                if name in first_with:
                    groups.union(first_with[name], ref.id)
                else:
                    first_with[name] = ref.id
        members: dict[int, builtins.list[SourceReference]] = defaultdict(builtins.list)
        for ref in refs:
            members[groups.find(ref.id)].append(ref)
        sources = self.sources_by_id(ref.source_id for ref in refs)
        items = []
        for root, group in members.items():
            citing_ids = sorted({ref.source_id for ref in group})
            citing = sorted(
                (sources[i] for i in citing_ids), key=lambda s: (s.title.casefold(), s.id)
            )
            items.append(FrontierItem(key=key_of[root], references=group, citing=citing))
        items.sort(
            key=lambda item: (
                -len(item.citing), -len(item.references),
                (item.best.title or item.best.raw_text).casefold(), item.key,
            )
        )  # fmt: skip
        return items


__all__ = [
    "EXTRACTION_STATES",
    "MAX_YEAR_GAP",
    "MIN_TITLE_CHARS",
    "MIN_TITLE_WORDS",
    "STALE_RUN_AFTER",
    "CitationEdge",
    "Claim",
    "ExtractionBusyError",
    "ExtractionSupersededError",
    "FrontierItem",
    "LibraryIndex",
    "ReferenceKeys",
    "ReferenceSpec",
    "RematchResult",
    "SourceReferenceManager",
    "add_url",
    "arxiv_from_doi",
    "arxiv_from_url",
    "doi_from_url",
    "extraction_state",
    "keys_of",
    "parse_arxiv_id",
    "parse_doi",
    "reference_keys",
    "title_key",
]
