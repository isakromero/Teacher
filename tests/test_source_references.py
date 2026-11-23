"""Citations storage and matching (`teacher.managers.source_references`): identifier and title
normalisation, the library index, claiming and recording runs, re-matching, the frontier, the
citation edges, and the schema's checks and cascades."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from teacher.config import Settings
from teacher.db.engine import session_scope
from teacher.db.models import (
    KnowledgeSource,
    SourceReference,
    SourceReferenceExtraction,
    utcnow,
)
from teacher.managers import KnowledgeSourceManager, NotFoundError, SourceReferenceManager
from teacher.managers.source_references import (
    STALE_RUN_AFTER,
    CitationEdge,
    ExtractionBusyError,
    ExtractionSupersededError,
    LibraryIndex,
    ReferenceSpec,
    add_url,
    arxiv_from_url,
    doi_from_url,
    extraction_state,
    parse_arxiv_id,
    parse_doi,
    reference_keys,
    title_key,
)

# -- normalisation ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("10.1145/3065386", "10.1145/3065386"),
        ("https://doi.org/10.1145/3065386.", "10.1145/3065386"),
        ("http://dx.doi.org/10.1038/NATURE14539", "10.1038/nature14539"),
        ("doi: 10.1038/nature14539,", "10.1038/nature14539"),
        ("10.48550/arXiv.1706.03762", "10.48550/arxiv.1706.03762"),
        ("1706.03762", None),
        ("doi:", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_doi(value: str | None, expected: str | None) -> None:
    assert parse_doi(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1706.03762", "1706.03762"),
        ("arXiv:1706.03762v7", "1706.03762"),
        ("https://arxiv.org/abs/1810.04805v2", "1810.04805"),
        ("https://arxiv.org/pdf/1706.03762v5.pdf", "1706.03762"),
        ("hep-th/9711200v3", "hep-th/9711200"),
        ("math.GT/0309136", "math.GT/0309136"),
        ("1706.037", None),  # 4 or 5 digits after the dot
        ("attention", None),
        (None, None),
    ],
)
def test_parse_arxiv_id(value: str | None, expected: str | None) -> None:
    assert parse_arxiv_id(value) == expected


def test_ids_in_urls_and_arxiv_dois() -> None:
    assert doi_from_url("https://doi.org/10.1145/3065386") == "10.1145/3065386"
    assert doi_from_url("https://example.org/10.1145/3065386") is None
    assert arxiv_from_url("https://arxiv.org/abs/1706.03762v2") == "1706.03762"
    assert arxiv_from_url("https://example.org/abs/1706.03762") is None
    keys = reference_keys(
        doi="10.48550/arXiv.1706.03762", arxiv_id=None, url=None, title=None, year=None
    )
    assert keys.dois == ("10.48550/arxiv.1706.03762",)
    assert keys.arxiv_ids == ("1706.03762",)


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Attention Is All You Need", "attention is all you need"),
        ("Attention is all you need.", "attention is all you need"),
        ("“Attention Is All You Need”", "attention is all you need"),
        (
            "BERT: Pre-training of Deep Bidirectional Transformers",
            "bert pre training of deep bidirectional transformers",
        ),
        ("Ünïcödé   Títles\nwrap over lines", "unicode titles wrap over lines"),
        ("{G}raph {N}eural {N}etworks", "g raph n eural n etworks"),
        ("Deep Learning", None),  # two words: names several works
        ("A B C", None),  # three words but too few letters
        ("", None),
        (None, None),
    ],
)
def test_title_key(title: str | None, expected: str | None) -> None:
    assert title_key(title) == expected


def test_add_url_prefers_the_arxiv_pdf_then_doi_then_the_printed_address() -> None:
    assert add_url(doi="10.1/x", arxiv_id="1706.03762v2", url="https://e.org") == (
        "https://arxiv.org/pdf/1706.03762", "arxiv"
    )
    assert add_url(doi="10.1145/3065386", arxiv_id=None, url="https://e.org") == (
        "https://doi.org/10.1145/3065386", "doi"
    )
    assert add_url(doi=None, arxiv_id=None, url="https://e.org/p") == ("https://e.org/p", "url")
    assert add_url(doi=None, arxiv_id=None, url="https://arxiv.org/abs/1810.04805") == (
        "https://arxiv.org/pdf/1810.04805", "arxiv"
    )
    assert add_url(doi=None, arxiv_id=None, url=None) is None


# -- the library index ----------------------------------------------------------------------------


def _keys(**kw: object):
    fields = {"doi": None, "arxiv_id": None, "url": None, "title": None, "year": None, **kw}
    return reference_keys(**fields)  # type: ignore[arg-type]


def test_match_order_doi_then_arxiv_then_title() -> None:
    index = LibraryIndex()
    index.add_source(1, "Attention Is All You Need", "2017", None)
    index.add_identifier(1, "arxiv", "1706.03762v5")
    index.add_source(2, "ImageNet Classification with Deep CNNs", "2012", None)
    index.add_identifier(2, "doi", "10.1145/3065386")
    index.add_source(3, "Some Paper With A Long Title", None, "https://arxiv.org/pdf/2001.00001v1")

    assert index.match(_keys(doi="10.1145/3065386", arxiv_id="1706.03762")) == (2, "doi")
    assert index.match(_keys(doi="10.9999/nothing", arxiv_id="1706.03762")) == (1, "arxiv")
    assert index.match(_keys(doi="10.48550/arXiv.1706.03762")) == (1, "arxiv")
    assert index.match(_keys(url="https://arxiv.org/abs/1706.03762v1")) == (1, "arxiv")
    assert index.match(_keys(arxiv_id="2001.00001")) == (3, "arxiv")  # from origin_url
    assert index.match(_keys(title="Attention is all you need.", year=2017)) == (1, "title")
    assert index.match(_keys(title="attention is all you need", year=2018)) == (1, "title")
    assert index.match(_keys(title="Attention is all you need")) == (1, "title")
    assert index.match(_keys(title="Attention Is All You Need", year=2019)) is None  # years
    assert index.match(_keys(title="Attention Is All You Need Indeed")) is None  # no fuzz
    assert index.match(_keys()) is None


def test_ambiguous_keys_match_nothing() -> None:
    index = LibraryIndex()
    index.add_source(1, "A Survey of Graph Neural Networks", "2020", None)
    index.add_source(2, "A survey of graph neural networks", "2021", None)
    index.add_identifier(1, "arxiv", "2001.00001")
    index.add_source(2, "unused title here ok", None, "https://arxiv.org/abs/2001.00001")
    assert index.match(_keys(title="A Survey of Graph Neural Networks")) is None
    assert index.match(_keys(arxiv_id="2001.00001")) is None
    # An ambiguous DOI/arXiv id falls through to the next method.
    assert index.match(_keys(arxiv_id="2001.00001", title="unused title here ok")) == (
        2, "title"
    )


def test_short_titles_and_invalid_library_identifiers_are_not_used() -> None:
    index = LibraryIndex()
    index.add_source(1, "Deep Learning", "2016", None)
    index.add_identifier(1, "doi", "not a doi")
    index.add_identifier(1, "isbn", "978-0262035613")
    assert index.match(_keys(title="Deep learning", year=2016)) is None
    assert not index.dois and not index.arxiv_ids


# -- the manager --------------------------------------------------------------------------------


def add_source(
    settings: Settings,
    tmp_path: Path,
    slug: str,
    *,
    title: str | None = None,
    identifiers: dict[str, str] | None = None,
    originated_on: str | None = None,
) -> int:
    path = tmp_path / f"{slug}.md"
    path.write_text(f"# {slug}\n\nText of {slug}.\n", encoding="utf-8")
    with session_scope(settings) as session:
        source = KnowledgeSourceManager(session, settings).add(
            path, slug=slug, kind="paper", title=title or f"The {slug} paper title",
            identifiers=identifiers, originated_on=originated_on,
        )  # fmt: skip
        return source.id


def source(session: Session, slug: str) -> KnowledgeSource:
    return KnowledgeSourceManager(session, Settings(root=Path("unused"))).get_by_slug(slug)


def store(settings: Settings, slug: str, specs: list[ReferenceSpec], found_in: str = "reference_list"):
    with session_scope(settings) as session:
        manager = SourceReferenceManager(session, settings)
        claim = manager.claim(source(session, slug), trigger="cli", extractor="x", model="m")
    with session_scope(settings) as session:
        matched = SourceReferenceManager(session, settings).store(
            claim, specs, found_in=found_in, input_tokens=10, output_tokens=5
        )
    return claim, matched


ATTENTION = ReferenceSpec(
    raw_text="Vaswani et al. Attention is all you need. NeurIPS 2017. arXiv:1706.03762",
    title="Attention is all you need", authors=("Ashish Vaswani",), year=2017,
    venue="NeurIPS", arxiv_id="1706.03762",
)  # fmt: skip
ALEXNET = ReferenceSpec(
    raw_text="Krizhevsky et al. ImageNet classification with deep convolutional neural networks.",
    title="ImageNet classification with deep convolutional neural networks", year=2012,
    doi="10.1145/3065386",
)  # fmt: skip


def test_claim_store_and_read_back(settings: Settings, tmp_path: Path) -> None:
    add_source(settings, tmp_path, "citing")
    claim, matched = store(settings, "citing", [ATTENTION, ALEXNET])
    assert claim.run_number == 1 and matched == 0
    with session_scope(settings) as session:
        manager = SourceReferenceManager(session, settings)
        row = manager.extraction(source(session, "citing"))
        assert row is not None
        assert (row.status, row.reference_count, row.found_in) == ("succeeded", 2, "reference_list")
        assert (row.input_tokens, row.output_tokens, row.error) == (10, 5, None)
        assert row.succeeded_at == row.finished_at
        assert extraction_state(row, utcnow()) == "done"
        refs = manager.references(source(session, "citing"))
        assert [r.position for r in refs] == [1, 2]
        assert refs[0].authors == ["Ashish Vaswani"] and refs[1].doi == "10.1145/3065386"
        assert all(r.matched_source_id is None for r in refs)


def test_never_run_and_the_counts(settings: Settings, tmp_path: Path) -> None:
    add_source(settings, tmp_path, "one")
    add_source(settings, tmp_path, "two")
    with session_scope(settings) as session:
        manager = SourceReferenceManager(session, settings)
        assert manager.extraction(source(session, "one")) is None
        assert extraction_state(None, utcnow()) == "never_run"
        manager.claim(source(session, "one"), trigger="cli", extractor="x", model="m")
        counts = manager.counts()
    assert counts == {"never_run": 1, "running": 1, "interrupted": 0, "done": 0, "failed": 0,
                      "sources": 2}


def test_a_second_claim_is_refused_until_the_first_finishes_or_goes_stale(
    settings: Settings, tmp_path: Path
) -> None:
    add_source(settings, tmp_path, "citing")
    with session_scope(settings) as session:
        first = SourceReferenceManager(session, settings).claim(
            source(session, "citing"), trigger="cli", extractor="x", model="m"
        )
    with session_scope(settings) as session:
        with pytest.raises(ExtractionBusyError, match="already being extracted"):
            SourceReferenceManager(session, settings).claim(
                source(session, "citing"), trigger="on_demand", extractor="x", model="m"
            )
    later = first.started_at + STALE_RUN_AFTER + timedelta(seconds=1)
    with session_scope(settings) as session:
        manager = SourceReferenceManager(session, settings)
        row = manager.extraction(source(session, "citing"))
        assert extraction_state(row, later) == "interrupted"
        second = manager.claim(
            source(session, "citing"), trigger="on_demand", extractor="x", model="m", now=later
        )
    assert second.run_number == 2
    # The stale run's result is not stored over the new one.
    with session_scope(settings) as session:
        with pytest.raises(ExtractionSupersededError):
            SourceReferenceManager(session, settings).store(first, [ATTENTION], found_in="reference_list")
    with session_scope(settings) as session:
        assert not SourceReferenceManager(session, settings).fail(first, "late")
        row = SourceReferenceManager(session, settings).extraction(source(session, "citing"))
        assert (row.status, row.run_number, row.trigger) == ("running", 2, "on_demand")


def test_a_failed_run_keeps_the_earlier_references(settings: Settings, tmp_path: Path) -> None:
    add_source(settings, tmp_path, "citing")
    store(settings, "citing", [ATTENTION])
    with session_scope(settings) as session:
        manager = SourceReferenceManager(session, settings)
        claim = manager.claim(source(session, "citing"), trigger="cli", extractor="x", model="m")
        row = manager.extraction(source(session, "citing"))
        assert row.reference_count == 1 and row.succeeded_at is not None  # still described
    with session_scope(settings) as session:
        assert SourceReferenceManager(session, settings).fail(claim, "boom", input_tokens=3)
    with session_scope(settings) as session:
        manager = SourceReferenceManager(session, settings)
        row = manager.extraction(source(session, "citing"))
        assert (row.status, row.error, row.input_tokens, row.run_number) == ("failed", "boom", 3, 2)
        assert row.reference_count == 1 and row.found_in == "reference_list"
        assert extraction_state(row, utcnow()) == "failed"
        assert len(manager.references(source(session, "citing"))) == 1


def test_found_nothing_is_done_with_zero(settings: Settings, tmp_path: Path) -> None:
    add_source(settings, tmp_path, "page")
    store(settings, "page", [], found_in="none")
    with session_scope(settings) as session:
        row = SourceReferenceManager(session, settings).extraction(source(session, "page"))
        assert (row.status, row.reference_count, row.found_in) == ("succeeded", 0, "none")
    with session_scope(settings) as session:
        manager = SourceReferenceManager(session, settings)
        claim = manager.claim(source(session, "page"), trigger="cli", extractor="x", model="m")
        with pytest.raises(ValueError, match="'none' but references"):
            manager.store(claim, [ATTENTION], found_in="none")
        with pytest.raises(ValueError, match="found_in must be one of"):
            manager.store(claim, [], found_in="nowhere")
        with pytest.raises(ValueError, match="reference 1 has no text"):
            manager.store(claim, [ReferenceSpec(raw_text="  ")], found_in="reference_list")
        with pytest.raises(ValueError, match="error must be"):
            manager.fail(claim, " ")


def test_store_after_the_source_was_deleted(settings: Settings, tmp_path: Path) -> None:
    add_source(settings, tmp_path, "gone")
    with session_scope(settings) as session:
        claim = SourceReferenceManager(session, settings).claim(
            source(session, "gone"), trigger="cli", extractor="x", model="m"
        )
    with session_scope(settings) as session:
        KnowledgeSourceManager(session, settings).delete(source(session, "gone"))
    with session_scope(settings) as session:
        with pytest.raises(NotFoundError, match="was deleted while"):
            SourceReferenceManager(session, settings).store(claim, [], found_in="none")
    with session_scope(settings) as session:
        assert not SourceReferenceManager(session, settings).fail(claim, "x")


def test_store_matches_and_rematch_resolves_sources_added_later(
    settings: Settings, tmp_path: Path
) -> None:
    add_source(settings, tmp_path, "citing")
    attention_id = add_source(
        settings, tmp_path, "attention", title="Attention Is All You Need",
        identifiers={"arxiv": "1706.03762"}, originated_on="2017",
    )  # fmt: skip
    _claim, matched = store(settings, "citing", [ATTENTION, ALEXNET])
    assert matched == 1
    alexnet_id = add_source(
        settings, tmp_path, "alexnet", title="ImageNet classification with deep CNNs",
        identifiers={"doi": "10.1145/3065386"},
    )  # fmt: skip
    with session_scope(settings) as session:
        manager = SourceReferenceManager(session, settings)
        refs = manager.references(source(session, "citing"))
        assert [(r.matched_source_id, r.match_method) for r in refs] == [
            (attention_id, "arxiv"), (None, None),
        ]
        result = manager.rematch_all()
        assert (result.total, result.matched, result.changed) == (2, 2, 1)
        refs = manager.references(source(session, "citing"))
        assert [(r.matched_source_id, r.match_method) for r in refs] == [
            (attention_id, "arxiv"), (alexnet_id, "doi"),
        ]
        citing_id = source(session, "citing").id
        assert manager.citation_edges() == [
            CitationEdge(citing_id, attention_id), CitationEdge(citing_id, alexnet_id)
        ]
        cited = manager.cited_by(source(session, "attention"))
        assert [(s.slug, positions) for s, positions in cited] == [("citing", [1])]
    # Deleting a matched source unmatches its references (SET NULL, then re-matching).
    with session_scope(settings) as session:
        KnowledgeSourceManager(session, settings).delete(source(session, "alexnet"))
        manager = SourceReferenceManager(session, settings)
        manager.rematch_all()
        refs = manager.references(source(session, "citing"))
        assert [(r.matched_source_id, r.match_method) for r in refs][1] == (None, None)


def test_citation_edges_are_distinct_and_skip_self_citations(
    settings: Settings, tmp_path: Path
) -> None:
    a = add_source(settings, tmp_path, "a", title="Attention Is All You Need")
    b = add_source(settings, tmp_path, "b", title="The Annotated Transformer Explained")
    store(settings, "a", [ReferenceSpec(raw_text="self", title="Attention is all you need")])
    store(settings, "b", [
        ReferenceSpec(raw_text="one", title="Attention is all you need"),
        ReferenceSpec(raw_text="two", title="Attention Is All You Need."),
    ])  # fmt: skip
    with session_scope(settings) as session:
        manager = SourceReferenceManager(session, settings)
        assert manager.citation_edges() == [CitationEdge(b, a)]
        assert [(s.slug, p) for s, p in manager.cited_by(source(session, "a"))] == [("b", [1, 2])]


def test_frontier_groups_by_doi_arxiv_and_title_and_ranks_by_citing_sources(
    settings: Settings, tmp_path: Path
) -> None:
    for slug in ("p1", "p2", "p3"):
        add_source(settings, tmp_path, slug)
    resnet = "Deep residual learning for image recognition"
    store(settings, "p1", [
        ReferenceSpec(raw_text="He et al. 2016", title=resnet, year=2016),
        ReferenceSpec(raw_text="Some blog", url="https://example.org/post"),
        ReferenceSpec(raw_text="Smith et al. (2020)"),
    ])  # fmt: skip
    store(settings, "p2", [
        ReferenceSpec(raw_text="He, Zhang. ResNet.", doi="10.1109/cvpr.2016.90",
                      title=resnet.upper()),
        ReferenceSpec(raw_text="BERT", arxiv_id="1810.04805", title="BERT pre-training ok"),
    ])  # fmt: skip
    store(settings, "p3", [
        ReferenceSpec(raw_text="ResNet again", doi="10.1109/CVPR.2016.90"),
        ReferenceSpec(raw_text="BERT v2", url="https://arxiv.org/abs/1810.04805v2"),
    ])  # fmt: skip
    with session_scope(settings) as session:
        items = SourceReferenceManager(session, settings).frontier()
        summary = [(len(i.citing), len(i.references), i.best.raw_text) for i in items]
        assert summary == [
            (3, 3, "He, Zhang. ResNet."),  # title + DOI link all three
            (2, 2, "BERT"),
            (1, 1, "Smith et al. (2020)"),
            (1, 1, "Some blog"),
        ]
        resnet_item = items[0]
        assert [s.slug for s in resnet_item.citing] == ["p1", "p2", "p3"]
        assert resnet_item.first_value("doi") == "10.1109/cvpr.2016.90"
        assert resnet_item.first_value("year") == 2016
        assert items[1].first_value("url") == "https://arxiv.org/abs/1810.04805v2"
    # Adding the cited work removes it from the frontier once matching runs.
    add_source(settings, tmp_path, "resnet", title=resnet, originated_on="2016")
    with session_scope(settings) as session:
        manager = SourceReferenceManager(session, settings)
        manager.rematch_all()
        items = manager.frontier()
        # p1 and p2 name its title; p3 gives only a DOI, which the library source lacks, so that
        # reference stays on the frontier (no guessing).
        assert [(len(i.citing), i.best.raw_text) for i in items] == [
            (2, "BERT"), (1, "ResNet again"), (1, "Smith et al. (2020)"), (1, "Some blog"),
        ]


# -- the schema ---------------------------------------------------------------------------------


def test_schema_checks_and_cascades(settings: Settings, tmp_path: Path) -> None:
    a = add_source(settings, tmp_path, "a")
    b = add_source(settings, tmp_path, "b")
    with session_scope(settings) as session:
        session.add(SourceReference(source_id=a, position=1, raw_text="x", matched_source_id=b,
                                    match_method="doi"))
        session.add(SourceReferenceExtraction(
            source_id=a, status="succeeded", run_number=1, trigger="cli", extractor="x",
            model="m", started_at=utcnow(), finished_at=utcnow(), succeeded_at=utcnow(),
            found_in="reference_list", reference_count=1,
        ))  # fmt: skip
    bad_rows = [
        "INSERT INTO source_references (source_id, position, raw_text) VALUES (:a, 0, 'x')",
        "INSERT INTO source_references (source_id, position, raw_text) VALUES (:a, 1, 'dup')",
        "INSERT INTO source_references (source_id, position, raw_text, match_method) "
        "VALUES (:a, 2, 'x', 'guess')",
        "INSERT INTO source_references (source_id, position, raw_text, authors) "
        "VALUES (:a, 3, 'x', 'not json')",
        "UPDATE source_reference_extractions SET status = 'failed'",  # no error
        "UPDATE source_reference_extractions SET status = 'running'",  # finished_at set
        "UPDATE source_reference_extractions SET found_in = 'none'",  # with one reference
        "UPDATE source_reference_extractions SET reference_count = NULL",
        "UPDATE source_reference_extractions SET status = 'done'",
    ]
    for statement in bad_rows:
        with session_scope(settings) as session, pytest.raises(IntegrityError):
            session.execute(text(statement), {"a": a})
    with session_scope(settings) as session:  # the matched source goes: SET NULL
        session.execute(text("DELETE FROM knowledge_sources WHERE id = :b"), {"b": b})
    with session_scope(settings) as session:
        ref = session.scalar(select(SourceReference))
        assert ref.matched_source_id is None
    with session_scope(settings) as session:  # the citing source goes: CASCADE
        session.execute(text("DELETE FROM knowledge_sources WHERE id = :a"), {"a": a})
    with session_scope(settings) as session:
        assert session.scalar(select(SourceReference)) is None
        assert session.scalar(select(SourceReferenceExtraction)) is None
