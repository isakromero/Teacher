"""SourceConceptManager (replace, the checks, order), the cascades from deleting a source or a
concept, and ConceptManager.merge moving and combining source links."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from teacher.concepts import stronger_role
from teacher.config import Settings
from teacher.db.models import Concept, KnowledgeSource, SourceConcept
from teacher.managers import (
    ConceptManager,
    DuplicateError,
    KnowledgeSourceManager,
    LinkSpec,
    SourceConceptManager,
)


@pytest.fixture
def concepts(session: Session, settings: Settings) -> ConceptManager:
    return ConceptManager(session, settings)


@pytest.fixture
def links(session: Session, settings: Settings) -> SourceConceptManager:
    return SourceConceptManager(session, settings)


@pytest.fixture
def add_source(session: Session, settings: Settings, tmp_path: Path):
    def add(slug: str, title: str | None = None) -> KnowledgeSource:
        path = tmp_path / f"{slug}.txt"
        path.write_text(f"text of {slug}\n", encoding="utf-8")
        return KnowledgeSourceManager(session, settings).add(
            path, slug=slug, kind="paper", title=title or slug.title()
        )

    return add


def make(concepts: ConceptManager, name: str) -> Concept:
    return concepts.create(name, f"About {name}.", "paper")


def rows(session: Session) -> list[tuple]:
    return sorted(
        session.execute(
            text("SELECT source_id, concept_id, role, resolution, candidate_name, judge_reason, "
                 "evidence FROM source_concepts")
        ).all()
    )


def test_stronger_role() -> None:
    assert stronger_role("uses", "introduces") == "introduces"
    assert stronger_role("uses", "assumes") == "uses"
    assert stronger_role("assumes", "assumes") == "assumes"
    with pytest.raises(ValueError, match="invalid source role 'mentions'"):
        stronger_role("mentions", "uses")


def test_replace_writes_and_replaces(
    session: Session, concepts: ConceptManager, links: SourceConceptManager, add_source
) -> None:
    paper = add_source("paper")
    a, b, c = make(concepts, "beta"), make(concepts, "alpha"), make(concepts, "gamma")
    removed, written = links.replace(paper, [
        LinkSpec(a, "uses", "created", " beta ", evidence=" we use beta "),
        LinkSpec(b, "assumes", "exact", "alpha"),
        LinkSpec(c, "introduces", "judged", "Gamma", judge_reason="Same idea."),
    ])
    assert removed == 0 and len(written) == 3
    # by role (introduces, uses, assumes), then name
    assert [(l.concept.slug, l.role) for l in links.for_source(paper)] == [
        ("gamma", "introduces"), ("beta", "uses"), ("alpha", "assumes"),
    ]
    assert rows(session)[0][2:] == ("uses", "created", "beta", None, "we use beta")
    assert [l.source.slug for l in links.for_concept(a)] == ["paper"]

    removed, _ = links.replace(paper, [LinkSpec(b, "uses", "exact", "alpha")])
    assert removed == 3
    assert [(l.concept.slug, l.role) for l in links.for_source(paper)] == [("alpha", "uses")]
    assert links.for_concept(a) == []
    assert concepts.get_by_slug("beta") is a  # a concept is never deleted with its link
    removed, _ = links.replace(paper, [])
    assert removed == 1 and links.for_source(paper) == []


def test_for_concept_orders_by_role_then_title(
    concepts: ConceptManager, links: SourceConceptManager, add_source
) -> None:
    x = make(concepts, "x")
    for slug, title, role in [("b", "Beta", "uses"), ("a", "Alpha", "uses"),
                              ("c", "Gamma", "introduces")]:
        links.replace(add_source(slug, title), [LinkSpec(x, role, "exact", "x")])
    assert [(l.source.title, l.role) for l in links.for_concept(x)] == [
        ("Gamma", "introduces"), ("Alpha", "uses"), ("Beta", "uses"),
    ]


@pytest.mark.parametrize(
    ("spec", "error", "fragment"),
    [
        (dict(role="mentions"), ValueError, "invalid source role 'mentions'"),
        (dict(resolution="guessed"), ValueError, "invalid link resolution 'guessed'"),
        (dict(resolution="judged"), ValueError, "a judge reason is required for a judged link"),
        (dict(judge_reason="Why."), ValueError, "not allowed otherwise"),
        (dict(candidate_name=" "), ValueError, "candidate_name must be a non-empty string"),
        (dict(evidence=" "), ValueError, "evidence must be a non-empty string"),
    ],
)
def test_replace_checks_every_link_and_changes_nothing(
    session: Session, concepts: ConceptManager, links: SourceConceptManager, add_source,
    spec: dict, error: type, fragment: str,
) -> None:
    paper = add_source("paper")
    a, b = make(concepts, "a"), make(concepts, "b")
    links.replace(paper, [LinkSpec(a, "uses", "created", "a")])
    before = rows(session)
    values = dict(concept=b, role="uses", resolution="exact", candidate_name="b")
    values.update(spec)
    with pytest.raises(error, match=fragment):
        links.replace(paper, [LinkSpec(**values)])
    assert rows(session) == before


def test_replace_refuses_a_concept_twice(
    session: Session, concepts: ConceptManager, links: SourceConceptManager, add_source
) -> None:
    paper = add_source("paper")
    a = make(concepts, "a")
    with pytest.raises(DuplicateError, match="link 2 repeats concept 'a' of link 1"):
        links.replace(paper, [LinkSpec(a, "uses", "exact", "a"),
                              LinkSpec(a, "assumes", "exact", "A")])
    assert rows(session) == []


def test_database_constraints(
    session: Session, concepts: ConceptManager, add_source
) -> None:
    paper = add_source("paper")
    a = make(concepts, "a")
    session.flush()
    insert = ("INSERT INTO source_concepts (source_id, concept_id, role, resolution, "
              "candidate_name, judge_reason) VALUES (:s, :c, :role, :res, 'a', :why)")
    for values, constraint in [
        ({"role": "mentions", "res": "exact", "why": None}, "ck_source_concepts_role"),
        ({"role": "uses", "res": "guess", "why": None}, "ck_source_concepts_resolution"),
        ({"role": "uses", "res": "judged", "why": None}, "ck_source_concepts_judge_reason"),
        ({"role": "uses", "res": "exact", "why": "x"}, "ck_source_concepts_judge_reason"),
    ]:
        with pytest.raises(IntegrityError, match=constraint), session.begin_nested():
            session.execute(text(insert), {"s": paper.id, "c": a.id, **values})
    session.execute(text(insert), {"s": paper.id, "c": a.id, "role": "uses", "res": "exact",
                                   "why": None})
    with pytest.raises(IntegrityError, match="UNIQUE"), session.begin_nested():
        session.execute(text(insert), {"s": paper.id, "c": a.id, "role": "uses",
                                       "res": "exact", "why": None})


def test_deleting_a_concept_or_a_source_deletes_its_links(
    session: Session, settings: Settings, concepts: ConceptManager,
    links: SourceConceptManager, add_source,
) -> None:
    paper, other = add_source("paper"), add_source("other")
    a, b = make(concepts, "a"), make(concepts, "b")
    links.replace(paper, [LinkSpec(a, "uses", "exact", "a"), LinkSpec(b, "uses", "exact", "b")])
    links.replace(other, [LinkSpec(a, "assumes", "exact", "a")])
    session.commit()

    concepts.delete(a)
    session.commit()
    assert [(l.concept.slug) for l in links.for_source(paper)] == ["b"]
    assert links.for_source(other) == []

    KnowledgeSourceManager(session, settings).delete(paper)
    session.commit()
    assert session.scalar(text("SELECT count(*) FROM source_concepts")) == 0
    assert concepts.get_by_slug("b") is b  # the concept stays


def test_merge_moves_and_combines_source_links(
    session: Session, concepts: ConceptManager, links: SourceConceptManager, add_source
) -> None:
    only_source, both, only_into, weaker = (
        add_source("only-source"), add_source("both"), add_source("only-into"), add_source("weaker")
    )
    source, into, other = make(concepts, "REINFORCE"), make(concepts, "policy gradient"), make(
        concepts, "other"
    )
    links.replace(only_source, [LinkSpec(source, "assumes", "created", "REINFORCE",
                                         evidence="s1"), LinkSpec(other, "uses", "exact", "o")])
    links.replace(both, [
        LinkSpec(source, "introduces", "created", "REINFORCE", evidence="ours"),
        LinkSpec(into, "uses", "judged", "PG", judge_reason="Same.", evidence="theirs"),
    ])
    links.replace(weaker, [
        LinkSpec(source, "assumes", "created", "REINFORCE"),
        LinkSpec(into, "uses", "exact", "policy gradient", evidence="kept"),
    ])
    links.replace(only_into, [LinkSpec(into, "uses", "exact", "policy gradient")])
    session.commit()

    result = concepts.merge(source, into)
    session.commit()

    assert result.moved_links == ("only-source",)
    assert result.combined_links == ("both", "weaker")
    by_source = {l.source.slug: l for l in links.for_concept(into)}
    assert sorted(by_source) == ["both", "only-into", "only-source", "weaker"]
    moved = by_source["only-source"]
    assert (moved.role, moved.resolution, moved.candidate_name, moved.evidence) == (
        "assumes", "created", "REINFORCE", "s1"
    )
    combined = by_source["both"]  # into's link, with the stronger role and its evidence
    assert (combined.role, combined.resolution, combined.candidate_name, combined.judge_reason,
            combined.evidence) == ("introduces", "judged", "PG", "Same.", "ours")
    kept = by_source["weaker"]  # into's role was already the stronger one
    assert (kept.role, kept.evidence) == ("uses", "kept")
    assert [l.concept.slug for l in links.for_source(only_source)] == ["other", "policy-gradient"]
    assert session.scalar(text("SELECT count(*) FROM source_concepts")) == 5  # 7 less 2 merged
    assert session.scalar(
        text("SELECT count(*) FROM source_concepts WHERE concept_id = :id"), {"id": source.id}
    ) == 0


def test_a_failed_merge_leaves_the_links(
    session: Session, concepts: ConceptManager, links: SourceConceptManager, add_source,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paper = add_source("paper")
    source, into = make(concepts, "s"), make(concepts, "i")
    links.replace(paper, [LinkSpec(source, "uses", "exact", "s")])
    session.commit()
    before = rows(session)

    def boom(self: ConceptManager, concept: Concept) -> None:
        raise RuntimeError("index unavailable")

    monkeypatch.setattr(ConceptManager, "_sync_search", boom)
    with pytest.raises(RuntimeError):
        concepts.merge(source, into)
    session.rollback()
    assert rows(session) == before
    assert session.get(SourceConcept, 1).concept_id == source.id
