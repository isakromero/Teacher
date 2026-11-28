"""The source registry of a discovery run: what the providers found, normalised, deduplicated and
matched against the library.

`canonical_url(url)` is the form two addresses are compared in: scheme `https`, host lower-case
without `www.` and without a default port, no fragment, no trailing slash, no tracking
parameters (`utm_*`, `fbclid`, `gclid`, ...), the remaining query parameters sorted. It is a
comparison key, never fetched.

`full_text_url(url)` rewrites a landing page whose full text has a known, stable address to that
address, and returns None for anything else (so most addresses are fetched as found). The
rewrites, each only for exactly this shape:

- arXiv `arxiv.org/abs/<id>` (also `export.`, `www.` and `/html/<id>`) -> `https://arxiv.org/pdf/<id>`
  (version suffix kept when given, `.pdf` and query dropped);
- IACR ePrint `eprint.iacr.org/<year>/<number>` -> `https://eprint.iacr.org/<year>/<number>.pdf`;
- OpenReview `openreview.net/forum?id=<id>` -> `https://openreview.net/pdf?id=<id>`;
- ACL Anthology `aclanthology.org/<paper id>/` -> `https://aclanthology.org/<paper id>.pdf`;
- NeurIPS proceedings `.../hash/<hash>-Abstract[-<track>].html` ->
  `.../file/<hash>-Paper[-<track>].pdf`.

`identifiers_of(url, arxiv_id=, doi=)` reads an arXiv id (from the address, an arXiv DOI or the
given id) and a DOI (from a doi.org address or the given DOI), with the same parsing the
reference matcher uses (`teacher.managers.source_references`).

`merge_hits(outputs)` folds the providers' hits into `MergedCandidate`s. Two hits are the same
candidate when they share an arXiv id, else a DOI, else the canonical form of their full-text
address. A merged candidate keeps every provider that found it with that provider's reason and
rank, the first title and the first value of each detail. Its `id` is stable for the same work
across runs: `c_` and 12 hex digits of the SHA-256 of its key (`arxiv:<id>`, `doi:<doi>` or
`url:<canonical url>`).

`LibraryCatalog.load(session)` indexes the library's sources by canonical origin URL (and the
address asked for when a URL source was added, `metadata.requested_url`, and the full-text
rewrite of both), by `arxiv` and `doi` identifier, and by the arXiv id or DOI an origin URL
names. `match(candidate)` returns every library source the candidate's identifiers or addresses
point at, with how it matched; a candidate that is already in the library is marked, never
dropped.
"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from teacher.db.models import KnowledgeSource, KnowledgeSourceIdentifier
from teacher.managers.source_references import (
    arxiv_from_doi,
    arxiv_from_url,
    doi_from_url,
    parse_arxiv_id,
    parse_doi,
)

# Query parameters that only track where a click came from.
TRACKING_PARAMS = frozenset(
    {"fbclid", "gclid", "dclid", "msclkid", "mc_cid", "mc_eid", "igshid", "yclid", "_hsenc",
     "_hsmi", "ref_src", "spm"}
)  # fmt: skip
_DEFAULT_PORTS = {"http": 80, "https": 443}


def canonical_url(url: str) -> str:
    """The comparison key of an http(s) address (see the module docstring). ValueError for an
    address that is not http(s) or has no host."""
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"{url!r} is not an http(s) address")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise ValueError(f"{url!r} has no host")
    if host.startswith("www."):
        host = host[4:]
    port = parts.port
    netloc = host if port in (None, _DEFAULT_PORTS[scheme]) else f"{host}:{port}"
    query = sorted(
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in TRACKING_PARAMS
    )
    path = parts.path.rstrip("/")
    return urlunsplit(("https", netloc, path, urlencode(query), ""))


def without_fragment(url: str) -> str:
    """`url` as given, without its `#fragment` (a fetch never sends it)."""
    parts = urlsplit(url.strip())
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))


_ARXIV_HOSTS = frozenset({"arxiv.org", "www.arxiv.org", "export.arxiv.org"})
_ARXIV_PATH = re.compile(r"^/(?:abs|pdf|html)/(.+?)(?:\.pdf)?/?$")
_ARXIV_VERSIONED = re.compile(r"^(.+?)(v\d+)?$")
_IACR_PATH = re.compile(r"^/(\d{4})/(\d{1,5})/?$")
# An ACL Anthology paper id: new style `2020.acl-main.1`, old style `P19-1001`.
_ACL_PATH = re.compile(r"^/(\d{4}\.[a-z0-9\-]+\.\d+|[A-Z]\d{2}-\d{4})/?$")
_NEURIPS_PATH = re.compile(r"^(?P<base>/.+)/hash/(?P<hash>[0-9a-f]{32})-Abstract(?P<track>-[A-Za-z]+)?\.html$")


def full_text_url(url: str) -> str | None:
    """The full-text address of a known landing page (see the module docstring), or None."""
    try:
        parts = urlsplit(url.strip())
        host = (parts.hostname or "").lower()
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https"):
        return None
    if host in _ARXIV_HOSTS:
        found = _ARXIV_PATH.match(parts.path)
        if found is None:
            return None
        base, version = _ARXIV_VERSIONED.match(found.group(1)).groups()  # type: ignore[union-attr]
        if parse_arxiv_id(base) is None:
            return None
        target = f"https://arxiv.org/pdf/{base}{version or ''}"
        return None if target == url.strip() else target
    if host == "eprint.iacr.org":
        found = _IACR_PATH.match(parts.path)
        if found is None:
            return None
        return f"https://eprint.iacr.org/{found.group(1)}/{found.group(2)}.pdf"
    if host in ("openreview.net", "www.openreview.net") and parts.path.rstrip("/") == "/forum":
        paper = dict(parse_qsl(parts.query)).get("id")
        if not paper or not re.fullmatch(r"[A-Za-z0-9_\-]+", paper):
            return None
        return f"https://openreview.net/pdf?id={paper}"
    if host == "aclanthology.org":
        found = _ACL_PATH.match(parts.path)
        if found is None:
            return None
        return f"https://aclanthology.org/{found.group(1)}.pdf"
    if host in ("proceedings.neurips.cc", "papers.nips.cc"):
        found = _NEURIPS_PATH.match(parts.path)
        if found is None:
            return None
        track = found.group("track") or ""
        return (
            f"https://{host}{found.group('base')}/file/{found.group('hash')}-Paper{track}.pdf"
        )
    return None


@dataclass(frozen=True)
class Identifiers:
    arxiv_id: str | None = None
    doi: str | None = None


def identifiers_of(url: str | None, *, arxiv_id: str | None = None, doi: str | None = None) -> Identifiers:
    """The arXiv id and DOI a hit names (see the module docstring); None where it names none."""
    found_doi = parse_doi(doi) or doi_from_url(url)
    found_arxiv = (
        parse_arxiv_id(arxiv_id) or arxiv_from_url(_arxiv_abs_form(url)) or arxiv_from_doi(found_doi)
    )
    return Identifiers(arxiv_id=found_arxiv, doi=found_doi)


def _arxiv_abs_form(url: str | None) -> str | None:
    """An arXiv `/html/<id>` or `export.` address as the `/abs/` form `arxiv_from_url` reads."""
    if not url:
        return None
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    if (parts.hostname or "").lower() not in _ARXIV_HOSTS:
        return url
    path = re.sub(r"^/html/", "/abs/", parts.path)
    return f"https://arxiv.org{path}"


# -- what the providers found -----------------------------------------------------------------


@dataclass(frozen=True)
class ProviderHit:
    """One result of one provider, as the provider reported it. `rank` is its position in the
    provider's list (1 = first). `reason` is one line on why the provider found it."""

    provider: str
    rank: int
    url: str
    title: str
    reason: str
    authors: tuple[str, ...] = ()
    year: int | None = None
    summary: str | None = None
    arxiv_id: str | None = None
    doi: str | None = None


@dataclass(frozen=True)
class FoundBy:
    """A provider that found a candidate, with its reason and rank."""

    provider: str
    rank: int
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {"provider": self.provider, "rank": self.rank, "reason": self.reason}


@dataclass
class MergedCandidate:
    """One distinct work after deduplication. `url` is what is fetched (the full-text address
    when `full_text_url` knows one, else the first hit's address without its fragment);
    `landing_url` the address it was rewritten from (None when not rewritten)."""

    id: str
    key: str
    url: str
    landing_url: str | None
    title: str
    found_by: list[FoundBy]
    arxiv_id: str | None = None
    doi: str | None = None
    authors: tuple[str, ...] = ()
    year: int | None = None
    summary: str | None = None
    addresses: list[str] = field(default_factory=list)  # every address a hit gave, as given

    @property
    def providers(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(f.provider for f in self.found_by))

    @property
    def best_rank(self) -> int:
        return min(f.rank for f in self.found_by)


def candidate_id(key: str) -> str:
    return "c_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def _hit_keys(hit: ProviderHit, fetch_url: str) -> list[str]:
    ids = identifiers_of(hit.url, arxiv_id=hit.arxiv_id, doi=hit.doi)
    keys = []
    if ids.arxiv_id:
        keys.append(f"arxiv:{ids.arxiv_id}")
    if ids.doi:
        keys.append(f"doi:{ids.doi}")
    keys.append(f"url:{canonical_url(fetch_url)}")
    keys.append(f"url:{canonical_url(hit.url)}")
    return list(dict.fromkeys(keys))


def merge_hits(hits: Iterable[ProviderHit]) -> list[MergedCandidate]:
    """Deduplicate `hits` (in the order given) into candidates, in order of first sighting.
    A hit whose address is not http(s) is a ValueError (providers check their own output)."""
    merged: list[MergedCandidate] = []
    by_key: dict[str, MergedCandidate] = {}
    for hit in hits:
        rewritten = full_text_url(hit.url)
        fetch_url = rewritten or without_fragment(hit.url)
        keys = _hit_keys(hit, fetch_url)
        ids = identifiers_of(hit.url, arxiv_id=hit.arxiv_id, doi=hit.doi)
        existing = next((by_key[k] for k in keys if k in by_key), None)
        found = FoundBy(hit.provider, hit.rank, hit.reason)
        if existing is None:
            primary = keys[0]
            candidate = MergedCandidate(
                id=candidate_id(primary),
                key=primary,
                url=fetch_url,
                landing_url=without_fragment(hit.url) if rewritten else None,
                title=hit.title,
                found_by=[found],
                arxiv_id=ids.arxiv_id,
                doi=ids.doi,
                authors=hit.authors,
                year=hit.year,
                summary=hit.summary,
                addresses=[hit.url],
            )
            merged.append(candidate)
        else:
            candidate = existing
            if not any(f.provider == found.provider for f in candidate.found_by):
                candidate.found_by.append(found)
            if hit.url not in candidate.addresses:
                candidate.addresses.append(hit.url)
            candidate.arxiv_id = candidate.arxiv_id or ids.arxiv_id
            candidate.doi = candidate.doi or ids.doi
            candidate.authors = candidate.authors or hit.authors
            candidate.year = candidate.year if candidate.year is not None else hit.year
            candidate.summary = candidate.summary or hit.summary
        for key in keys:
            by_key.setdefault(key, candidate)
        if candidate.arxiv_id:
            by_key.setdefault(f"arxiv:{candidate.arxiv_id}", candidate)
        if candidate.doi:
            by_key.setdefault(f"doi:{candidate.doi}", candidate)
    return merged


def select_candidates(
    merged: Sequence[MergedCandidate], limit: int
) -> tuple[list[MergedCandidate], list[MergedCandidate]]:
    """(kept, not kept): the `limit` candidates found by the most providers, then by their best
    rank in any provider, then in order of first sighting (which interleaves the providers)."""
    order = sorted(
        range(len(merged)),
        key=lambda i: (-len(merged[i].providers), merged[i].best_rank, i),
    )
    kept = [merged[i] for i in order[:limit]]
    rest = [merged[i] for i in order[limit:]]
    return kept, rest


# -- the library ------------------------------------------------------------------------------


@dataclass(frozen=True)
class LibraryMatch:
    """A library source a candidate already is: its slug, title and how it matched (`arxiv`,
    `doi` or `url`)."""

    slug: str
    title: str
    method: str

    def to_dict(self) -> dict[str, Any]:
        return {"slug": self.slug, "title": self.title, "method": self.method}


@dataclass
class LibraryCatalog:
    """The library's addresses and identifiers (see the module docstring)."""

    sources: dict[int, tuple[str, str]] = field(default_factory=dict)  # id -> (slug, title)
    urls: dict[str, set[int]] = field(default_factory=lambda: defaultdict(set))
    arxiv_ids: dict[str, set[int]] = field(default_factory=lambda: defaultdict(set))
    dois: dict[str, set[int]] = field(default_factory=lambda: defaultdict(set))

    @classmethod
    def load(cls, session: Session) -> LibraryCatalog:
        catalog = cls()
        rows = session.execute(
            select(
                KnowledgeSource.id,
                KnowledgeSource.slug,
                KnowledgeSource.title,
                KnowledgeSource.origin_url,
                KnowledgeSource.metadata_,
            )
        ).all()
        for source_id, slug, title, origin_url, metadata in rows:
            requested = (metadata or {}).get("requested_url")
            catalog.add_source(
                source_id,
                slug,
                title,
                [u for u in (origin_url, requested) if isinstance(u, str) and u.strip()],
            )
        identifiers = session.execute(
            select(
                KnowledgeSourceIdentifier.source_id,
                KnowledgeSourceIdentifier.scheme,
                KnowledgeSourceIdentifier.value,
            )
        ).all()
        for source_id, scheme, value in identifiers:
            catalog.add_identifier(source_id, scheme, value)
        return catalog

    def add_source(self, source_id: int, slug: str, title: str, urls: Sequence[str]) -> None:
        self.sources[source_id] = (slug, title)
        for url in urls:
            for form in (url, full_text_url(url)):
                if form is None:
                    continue
                try:
                    self.urls[canonical_url(form)].add(source_id)
                except ValueError:
                    continue  # an origin that is not a web address names nothing to match
            ids = identifiers_of(url)
            if ids.arxiv_id:
                self.arxiv_ids[ids.arxiv_id].add(source_id)
            if ids.doi:
                self.dois[ids.doi].add(source_id)

    def add_identifier(self, source_id: int, scheme: str, value: str) -> None:
        scheme = scheme.strip().lower()
        if scheme == "arxiv":
            arxiv = parse_arxiv_id(value)
            if arxiv:
                self.arxiv_ids[arxiv].add(source_id)
        elif scheme == "doi":
            doi = parse_doi(value)
            if doi:
                self.dois[doi].add(source_id)
                arxiv = arxiv_from_doi(doi)
                if arxiv:
                    self.arxiv_ids[arxiv].add(source_id)

    def match(self, candidate: MergedCandidate) -> tuple[LibraryMatch, ...]:
        """Every library source `candidate` already is, each once, by the first method that
        found it (arXiv id, then DOI, then address)."""
        found: dict[int, str] = {}
        if candidate.arxiv_id:
            for source_id in self.arxiv_ids.get(candidate.arxiv_id, ()):
                found.setdefault(source_id, "arxiv")
        if candidate.doi:
            for source_id in self.dois.get(candidate.doi, ()):
                found.setdefault(source_id, "doi")
        for address in [candidate.url, *candidate.addresses]:
            try:
                key = canonical_url(address)
            except ValueError:
                continue
            for source_id in self.urls.get(key, ()):
                found.setdefault(source_id, "url")
        return tuple(
            LibraryMatch(*self.sources[source_id], method)
            for source_id, method in sorted(found.items())
        )


__all__ = [
    "TRACKING_PARAMS",
    "FoundBy",
    "Identifiers",
    "LibraryCatalog",
    "LibraryMatch",
    "MergedCandidate",
    "ProviderHit",
    "candidate_id",
    "canonical_url",
    "full_text_url",
    "identifiers_of",
    "merge_hits",
    "select_candidates",
    "without_fragment",
]
