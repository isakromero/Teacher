"""teacher.discovery.registry: canonical addresses, landing-page rewrites, identifiers, merging
across providers, the kept set, and matching against the library."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from teacher.config import Settings
from teacher.discovery.registry import (
    LibraryCatalog,
    candidate_id,
    canonical_url,
    full_text_url,
    identifiers_of,
    merge_hits,
    select_candidates,
)
from teacher.managers import KnowledgeSourceManager
from tests.discovery_fakes import hit

# -- canonical addresses ------------------------------------------------------------------------


def test_canonical_url_normalises_what_does_not_change_the_page() -> None:
    assert canonical_url("HTTP://WWW.Example.org:80/a/b/?utm_source=x&b=2&a=1#frag") == (
        "https://example.org/a/b?a=1&b=2"
    )
    assert canonical_url("https://example.org/a?fbclid=1&gclid=2") == "https://example.org/a"
    assert canonical_url("https://example.org:8443/a") == "https://example.org:8443/a"
    # The path keeps its case: two paths that differ in case can be two pages.
    assert canonical_url("https://example.org/Notes") != canonical_url("https://example.org/notes")


@pytest.mark.parametrize("bad", ["ftp://example.org/x", "mailto:a@b.c", "https:///nohost", "notaurl"])
def test_canonical_url_refuses_what_is_not_a_web_address(bad: str) -> None:
    with pytest.raises(ValueError):
        canonical_url(bad)


# -- landing pages to full text -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("landing", "full"),
    [
        ("https://arxiv.org/abs/1706.03762", "https://arxiv.org/pdf/1706.03762"),
        ("http://arxiv.org/abs/1706.03762v5", "https://arxiv.org/pdf/1706.03762v5"),
        ("https://export.arxiv.org/abs/hep-th/9711200v1", "https://arxiv.org/pdf/hep-th/9711200v1"),
        ("https://arxiv.org/abs/1706.03762?context=cs", "https://arxiv.org/pdf/1706.03762"),
        ("https://arxiv.org/html/2401.00001v2", "https://arxiv.org/pdf/2401.00001v2"),
        ("https://arxiv.org/pdf/1706.03762.pdf", "https://arxiv.org/pdf/1706.03762"),
        ("https://eprint.iacr.org/2023/1234", "https://eprint.iacr.org/2023/1234.pdf"),
        ("https://eprint.iacr.org/2005/187/", "https://eprint.iacr.org/2005/187.pdf"),
        ("https://openreview.net/forum?id=rJXMpikCZ", "https://openreview.net/pdf?id=rJXMpikCZ"),
        ("https://aclanthology.org/2020.acl-main.1/", "https://aclanthology.org/2020.acl-main.1.pdf"),
        ("https://aclanthology.org/P19-1001", "https://aclanthology.org/P19-1001.pdf"),
        (
            "https://proceedings.neurips.cc/paper/2017/hash/3f5ee243547dee91fbd053c1c4a845aa-Abstract.html",
            "https://proceedings.neurips.cc/paper/2017/file/3f5ee243547dee91fbd053c1c4a845aa-Paper.pdf",
        ),
        (
            "https://proceedings.neurips.cc/paper_files/paper/2023/hash/"
            "3f5ee243547dee91fbd053c1c4a845aa-Abstract-Conference.html",
            "https://proceedings.neurips.cc/paper_files/paper/2023/file/"
            "3f5ee243547dee91fbd053c1c4a845aa-Paper-Conference.pdf",
        ),
    ],
)
def test_known_landing_pages_are_rewritten_to_their_full_text(landing: str, full: str) -> None:
    assert full_text_url(landing) == full


@pytest.mark.parametrize(
    "url",
    [
        "https://arxiv.org/pdf/1706.03762",  # already the full text
        "https://arxiv.org/list/cs.LG/recent",  # a listing, not a paper
        "https://arxiv.org/abs/not-an-id",
        "https://eprint.iacr.org/2023/",  # the year's list
        "https://eprint.iacr.org/2023/1234.pdf",
        "https://openreview.net/group?id=ICLR.cc",
        "https://aclanthology.org/events/acl-2020/",  # an event page, not a paper
        "https://aclanthology.org/2020.acl-main.1.pdf",
        "https://proceedings.neurips.cc/paper/2017",
        "https://example.org/abs/1706.03762",  # another host
        "ftp://arxiv.org/abs/1706.03762",
    ],
)
def test_other_addresses_are_not_rewritten(url: str) -> None:
    assert full_text_url(url) is None


def test_identifiers_from_addresses_and_given_values() -> None:
    assert identifiers_of("https://arxiv.org/abs/1706.03762v7").arxiv_id == "1706.03762"
    assert identifiers_of("https://arxiv.org/html/2401.00001v2").arxiv_id == "2401.00001"
    assert identifiers_of("https://doi.org/10.1145/3065386").doi == "10.1145/3065386"
    arxiv_doi = identifiers_of(None, doi="10.48550/arXiv.1706.03762")
    assert (arxiv_doi.doi, arxiv_doi.arxiv_id) == ("10.48550/arxiv.1706.03762", "1706.03762")
    assert identifiers_of("https://example.org/x", arxiv_id="arXiv:1706.03762v2").arxiv_id == "1706.03762"
    nothing = identifiers_of("https://example.org/paper")
    assert (nothing.arxiv_id, nothing.doi) == (None, None)


# -- merging ------------------------------------------------------------------------------------


def test_hits_of_the_same_work_merge_across_providers_keeping_each_provider() -> None:
    merged = merge_hits(
        [
            hit("web", 1, "https://arxiv.org/abs/1706.03762", "Attention (web title)", reason="seminal"),
            hit("arxiv", 1, "https://arxiv.org/abs/1706.03762", "Attention Is All You Need",
                arxiv_id="1706.03762", authors=("Vaswani",), year=2017, reason="arXiv match"),
            hit("web", 2, "https://www.example.org/notes/?utm_source=feed#top", "Notes"),
            hit("arxiv", 2, "https://arxiv.org/pdf/1706.03762v7", "Attention again"),
            hit("web", 3, "https://example.org/notes", "Notes again"),
        ]
    )  # fmt: skip
    assert len(merged) == 2
    paper, notes = merged
    assert paper.providers == ("web", "arxiv")
    assert [(f.provider, f.rank, f.reason) for f in paper.found_by] == [
        ("web", 1, "seminal"), ("arxiv", 1, "arXiv match"),
    ]  # fmt: skip
    assert paper.url == "https://arxiv.org/pdf/1706.03762"
    assert paper.landing_url == "https://arxiv.org/abs/1706.03762"
    assert paper.title == "Attention (web title)"  # the first sighting's
    assert (paper.arxiv_id, paper.authors, paper.year) == ("1706.03762", ("Vaswani",), 2017)
    assert paper.id == candidate_id("arxiv:1706.03762")
    assert notes.providers == ("web",)
    assert notes.url == "https://www.example.org/notes/?utm_source=feed"  # fetched as given
    assert notes.landing_url is None


def test_candidate_ids_are_stable_per_work() -> None:
    first = merge_hits([hit("web", 1, "https://example.org/a/")])[0]
    second = merge_hits([hit("arxiv", 5, "https://www.example.org/a?utm_medium=x")])[0]
    assert first.id == second.id == candidate_id("url:https://example.org/a")
    assert first.id.startswith("c_") and len(first.id) == 14


def test_a_doi_merges_a_doi_link_with_a_page_naming_it() -> None:
    merged = merge_hits(
        [
            hit("web", 1, "https://doi.org/10.1145/3065386"),
            hit("arxiv", 1, "https://publisher.example/article/3065386", doi="10.1145/3065386"),
        ]
    )
    assert len(merged) == 1
    assert merged[0].providers == ("web", "arxiv")
    assert merged[0].doi == "10.1145/3065386"


def test_the_kept_set_prefers_works_found_by_more_providers_then_rank() -> None:
    merged = merge_hits(
        [
            hit("web", 1, "https://a.example/1"),
            hit("arxiv", 1, "https://arxiv.org/abs/2101.00001"),
            hit("web", 2, "https://a.example/2"),
            hit("arxiv", 2, "https://arxiv.org/abs/2101.00002"),
            hit("web", 3, "https://arxiv.org/abs/2101.00002"),  # found by both
        ]
    )
    kept, rest = select_candidates(merged, 3)
    assert [c.url for c in kept] == [
        "https://arxiv.org/pdf/2101.00002",
        "https://a.example/1",
        "https://arxiv.org/pdf/2101.00001",
    ]
    assert [c.url for c in rest] == ["https://a.example/2"]


# -- the library --------------------------------------------------------------------------------


def _add(session: Session, settings: Settings, tmp_path: Path, slug: str, **kw: object) -> None:
    path = tmp_path / f"{slug}.md"
    path.write_text(f"# {slug}\n", encoding="utf-8")
    KnowledgeSourceManager(session, settings).add(
        path, slug=slug, kind="web_page", title=f"Title {slug}", **kw  # type: ignore[arg-type]
    )


def test_candidates_already_in_the_library_are_matched(
    session: Session, settings: Settings, tmp_path: Path
) -> None:
    _add(session, settings, tmp_path, "notes", origin_url="https://example.org/notes/",
         metadata={"requested_url": "https://example.org/old-notes"})
    _add(session, settings, tmp_path, "attention", origin_url="https://arxiv.org/pdf/1706.03762")
    _add(session, settings, tmp_path, "bert", identifiers={"arxiv": "arXiv:1810.04805v2"})
    _add(session, settings, tmp_path, "alexnet", identifiers={"doi": "10.1145/3065386"})
    _add(session, settings, tmp_path, "iacr", origin_url="https://eprint.iacr.org/2023/1234")
    session.flush()
    catalog = LibraryCatalog.load(session)

    def matches(url: str, **kw: object) -> list[tuple[str, str]]:
        candidate = merge_hits([hit("web", 1, url, **kw)])[0]  # type: ignore[arg-type]
        return [(m.slug, m.method) for m in catalog.match(candidate)]

    assert matches("https://www.example.org/notes?utm_campaign=x") == [("notes", "url")]
    assert matches("https://example.org/old-notes") == [("notes", "url")]
    assert matches("https://arxiv.org/abs/1706.03762v3") == [("attention", "arxiv")]
    assert matches("https://arxiv.org/abs/1810.04805") == [("bert", "arxiv")]
    assert matches("https://doi.org/10.1145/3065386") == [("alexnet", "doi")]
    assert matches("https://publisher.example/x", doi="10.1145/3065386") == [("alexnet", "doi")]
    # The library holds the landing page; the candidate is its full text (and the other way).
    assert matches("https://eprint.iacr.org/2023/1234.pdf") == [("iacr", "url")]
    assert matches("https://example.org/other") == []
    assert matches("https://arxiv.org/abs/2101.00001") == []
