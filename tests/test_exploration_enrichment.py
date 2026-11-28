"""teacher.exploration.enrichment: the preview (nothing written), ingesting the approved
candidates only (added through the Add page's path, or linked when the library holds them, with
concept extraction only and no study page), one source's failure not stopping the others (it
stays ticked and is tried again), the outline written to the graph (matched, found again after
the ingest, or created unreviewed), the prerequisite links as builds_on with the skipped cases
(another relation on the pair, a cycle), grounding, an idempotent second run, the guard against
two at once, interruption, edits refused while one runs, a changed plan refused, and every model
call in the cost ledger with the exploration and the enrichment. Claude, the embedder and the
fetcher are fakes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from teacher.config import Settings
from teacher.db.engine import session_scope
from teacher.db.models import (
    ApiCall,
    Artifact,
    Concept,
    ConceptAlias,
    ConceptEmbedding,
    ConceptRelation,
    ExplorationEnrichment,
    KnowledgeSource,
    SourceConcept,
    StudyEvent,
)
from teacher.exploration import (
    INTERRUPTED_ENRICHMENT_ERROR,
    enrichment_preview,
    exploration_detail,
    exploration_roadmap,
    fail_interrupted_enrichments,
    run_enrichment,
    start_enrichment,
)
from teacher.exploration.enrichment import EnrichmentUnavailableError
from teacher.managers import (
    ConceptManager,
    ConceptRelationManager,
    ExplorationManager,
    KnowledgeSourceManager,
    StateConflictError,
)
from teacher.metadata import MetadataExtractor
from tests.enrichment_fakes import (
    FakeMetadata,
    approve,
    cand,
    candidate,
    completed_exploration,
    drop,
    enricher,
    outline,
    page,
    pdf,
)
from tests.exploration_fakes import graph

DLOG_URL = "https://a.example/dlog"
DH_URL = "https://a.example/dh"
HELD_URL = "https://a.example/held"
OTHER_URL = "https://a.example/other"
CURVES_URL = "https://a.example/curves"

CONCEPTS = [
    outline("modular-arithmetic", "Modular arithmetic", 1),
    outline("discrete-logarithm", "Discrete logarithm", 2, ["modular-arithmetic"]),
    outline("diffie-hellman-key-exchange", "Diffie-Hellman key exchange", 3,
            ["modular-arithmetic", "discrete-logarithm"], aliases=["DH"]),  # fmt: skip
    outline("shared-secret", "Shared secret", 3),
    outline("elliptic-curves", "Elliptic curves", 3),
]


@pytest.fixture
def embedder(fake_embedder: Any) -> Any:
    return fake_embedder()


@pytest.fixture
def world(settings: Settings, session: Any, embedder: Any, tmp_path: Path) -> dict[str, Any]:
    """"Modular arithmetic" in the graph, used (not introduced) by a library source that is the
    candidate HELD_URL; an exploration whose concepts found: dlog (approved), dh (approved),
    held (approved), other (proposed, for DH) and curves (approved, only for the concept the
    tests drop)."""
    made = graph(session, settings, embedder, "Modular arithmetic")
    path = tmp_path / "held.md"
    path.write_text("# Held notes\n\nAbout clocks.\n", encoding="utf-8")
    held = KnowledgeSourceManager(session, settings).add(
        path, slug="held-notes", kind="web_page", title="Held notes", origin_url=HELD_URL
    )
    session.add(SourceConcept(source_id=held.id, concept_id=made["Modular arithmetic"].id,
                              role="uses", resolution="exact", candidate_name="Modular arithmetic"))  # fmt: skip
    session.commit()
    eid = completed_exploration(
        settings, CONCEPTS,
        {
            "discrete-logarithm": [candidate("c_dlog", DLOG_URL, title="Discrete logs")],
            "diffie-hellman-key-exchange": [
                candidate("c_dh", DH_URL, title="The DH protocol"),
                candidate("c_other", OTHER_URL, title="Another DH text", overall=3.0),
            ],
            "modular-arithmetic": [candidate("c_held", HELD_URL, title="Held notes")],
            "elliptic-curves": [candidate("c_curves", CURVES_URL, title="Curves")],
        },
        matched={"modular-arithmetic": made["Modular arithmetic"].id},
    )  # fmt: skip
    approve(settings, eid, "c_dlog", "c_dh", "c_held", "c_curves")
    drop(settings, eid, "elliptic-curves")
    return {"id": eid, "ma": made["Modular arithmetic"].id, "held": held.id}


PAGES = {
    DLOG_URL: page(DLOG_URL, "Discrete logarithms in groups, built on modular arithmetic."),
    DH_URL: page(DH_URL, "Diffie-Hellman key exchange agrees a shared key in public."),
    CURVES_URL: page(CURVES_URL, "Elliptic curves."),
}


def replies(fake: Any) -> Any:
    """Concept extraction for the two sources to add: dlog introduces Discrete logarithm and
    Modular arithmetic; dh only uses Diffie-Hellman key exchange (so it is not grounded)."""
    return fake(
        fake.concepts_reply([cand("Discrete logarithm"), cand("Modular arithmetic")]),
        fake.concepts_reply([cand("Diffie-Hellman key exchange", "uses", aliases=["DH"])]),
    )


def make(fake_anthropic: Any, embedder: Any, extract: Any = None, **kw: Any) -> Any:
    the_enricher, fetcher = enricher(
        pages=kw.pop("pages", PAGES), extract_client=extract or replies(fake_anthropic),
        judge_client=kw.pop("judge", None) or fake_anthropic(), embedder=embedder, **kw,
    )  # fmt: skip
    return the_enricher, fetcher


def preview(settings: Settings, eid: int) -> dict[str, Any]:
    with session_scope(settings) as session:
        row = ExplorationManager(session, settings, owner=None).get(eid)
        return enrichment_preview(session, settings, row)


def enrich(settings: Settings, the_enricher: Any, eid: int, **kw: Any) -> int:
    enrichment_id = start_enrichment(settings, the_enricher, eid, owner=None,
                                     digest=preview(settings, eid)["digest"])  # fmt: skip
    run_enrichment(settings, enrichment_id, the_enricher, **kw)
    return enrichment_id


def detail(settings: Settings, eid: int) -> dict[str, Any]:
    with session_scope(settings) as session:
        row = ExplorationManager(session, settings, owner=None).get(eid)
        return exploration_detail(session, settings, row)


def by_key(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {c["key"]: c for s in data["steps"] for c in s["concepts"]}


def candidates(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out = {}
    for c in by_key(data).values():
        for cand_ in c["candidates"]:
            out[cand_["title"]] = cand_
    return out


def counts(settings: Settings) -> dict[str, int]:
    with session_scope(settings) as session:
        return {
            model.__tablename__: session.scalar(select(func.count()).select_from(model)) or 0
            for model in (KnowledgeSource, Concept, ConceptAlias, ConceptRelation, Artifact)
        }


# -- the preview ------------------------------------------------------------------------------


def test_the_preview_says_what_will_happen_and_writes_nothing(
    settings: Settings, world: dict[str, Any]
) -> None:
    before = counts(settings)
    p = preview(settings, world["id"])
    assert p["can_start"] is True and p["blocked"] is None and len(p["digest"]) == 32
    # Approved and linked to a concept that is not dropped: curves is left out.
    assert [(s["title"], s["action"]) for s in p["sources"]["items"]] == [
        ("Discrete logs", "add"), ("The DH protocol", "add"), ("Held notes", "link")]
    held = p["sources"]["items"][2]
    assert held["held"] == {"slug": "held-notes", "title": "Held notes", "method": "url"}
    assert held["extraction"] == "done_before"
    assert (p["sources"]["to_add"], p["sources"]["to_link"]) == (2, 1)
    assert [(c["name"], c["action"]) for c in p["concepts"]["items"]] == [
        ("Modular arithmetic", "use"), ("Discrete logarithm", "resolve"),
        ("Diffie-Hellman key exchange", "resolve"), ("Shared secret", "resolve")]  # fmt: skip
    assert p["dropped"] == ["Elliptic curves"]
    assert [(link["from"], link["to"], link["expected"]) for link in p["links"]["items"]] == [
        ("Discrete logarithm", "Modular arithmetic", "add"),
        ("Diffie-Hellman key exchange", "Modular arithmetic", "add"),
        ("Diffie-Hellman key exchange", "Discrete logarithm", "add"),
    ]
    assert "no price estimate" in p["model_calls"]
    assert counts(settings) == before
    with session_scope(settings) as session:
        assert session.scalar(select(func.count(ExplorationEnrichment.id))) == 0


# -- a whole enrichment -----------------------------------------------------------------------


def test_an_enrichment_ingests_the_approved_sources_and_writes_the_outline(
    settings: Settings, world: dict[str, Any], embedder: Any, fake_anthropic: Any
) -> None:
    the_enricher, fetcher = make(fake_anthropic, embedder)
    stages: list[str] = []
    enrichment_id = enrich(settings, the_enricher, world["id"], on_stage=stages.append)
    assert stages == ["ingest", "graph", "grounding"]
    data = detail(settings, world["id"])
    e = data["enrichment"]
    assert e["id"] == enrichment_id and e["status"] == "completed", e["error"]
    assert data["enriching"] is False and data["editable"] is True and data["can_enrich"] is True
    assert [(s["name"], s["status"]) for s in e["stages"]] == [
        ("ingest", "succeeded"), ("graph", "succeeded"), ("grounding", "succeeded")]
    # Only the approved candidates of concepts that are not dropped were fetched.
    assert sorted(fetcher.calls) == [DH_URL, DLOG_URL]

    s = e["summary"]
    assert (s["sources"]["added"], s["sources"]["held"], s["sources"]["failed"]) == (2, 1, 0)
    assert [(i["title"], i["outcome"], i["slug"]) for i in s["sources"]["items"]] == [
        ("Discrete logs", "added", "discrete-logs"), ("The DH protocol", "added", "the-dh-protocol"),
        ("Held notes", "held", "held-notes")]  # fmt: skip
    cands = candidates(data)
    assert {t: c["decision"] for t, c in cands.items()} == {
        "Discrete logs": "ingested", "The DH protocol": "ingested", "Held notes": "ingested",
        "Another DH text": "proposed", "Curves": "approved"}  # fmt: skip
    assert cands["Held notes"]["knowledge_source"]["slug"] == "held-notes"  # linked, not added
    assert cands["Held notes"]["ingest"]["extraction"] == "done_before"
    assert cands["Discrete logs"]["ingest"]["extraction"] == "ran"
    assert cands["Discrete logs"]["ingest"]["details"]["title"] == "search result"

    with session_scope(settings) as session:
        sources = {src.slug: src for src in session.scalars(select(KnowledgeSource))}
        assert sorted(sources) == ["discrete-logs", "held-notes", "the-dh-protocol"]
        added = sources["discrete-logs"]
        assert (added.kind, added.origin_url, added.title) == ("web_page", DLOG_URL, "Discrete logs")
        assert added.metadata_["requested_url"] == DLOG_URL
        assert added.metadata_["exploration"] == {
            "id": world["id"], "candidate_id": cands["Discrete logs"]["id"],
            "enrichment_id": enrichment_id}  # fmt: skip
        assert added.creators == ["A. Writer"] and added.originated_on == "2020"
        # Concept extraction only: no study page.
        assert session.scalar(select(func.count(Artifact.id))) == 0
        assert [e.payload for e in session.scalars(select(StudyEvent).where(StudyEvent.kind == "source_added"))] == [
            {"via": "exploration"}, {"via": "exploration"}]  # fmt: skip

    s_concepts = {i["key"]: (i["outcome"], i["slug"]) for i in s["concepts"]["items"]}
    assert s_concepts == {
        "modular-arithmetic": ("matched", "modular-arithmetic"),
        "discrete-logarithm": ("found", "discrete-logarithm"),  # created by dlog's extraction
        "diffie-hellman-key-exchange": ("found", "diffie-hellman-key-exchange"),
        "shared-secret": ("created", "shared-secret"),
    }
    assert (s["concepts"]["matched"], s["concepts"]["found"], s["concepts"]["created"]) == (1, 2, 1)
    with session_scope(settings) as session:
        created = ConceptManager(session, settings).get_by_slug("shared-secret")
        assert (created.origin, created.reviewed_at, created.summary) == ("model", None, "What Shared secret is.")
        assert session.get(ConceptEmbedding, created.id) is not None  # the index stays complete
        relations = sorted(
            (r.from_concept.slug, r.type, r.to_concept.slug, r.origin, r.reviewed_at)
            for r in ConceptRelationManager(session, settings).list()
        )
    assert relations == [
        ("diffie-hellman-key-exchange", "builds_on", "discrete-logarithm", "model", None),
        ("diffie-hellman-key-exchange", "builds_on", "modular-arithmetic", "model", None),
        ("discrete-logarithm", "builds_on", "modular-arithmetic", "model", None),
    ]
    assert (s["relations"]["added"], s["relations"]["present"], s["relations"]["skipped"]) == (3, 0, 0)

    # Grounding: introduced by a library source. DH is only used by its source.
    concepts = by_key(data)
    assert {k: c["status"] for k, c in concepts.items()} == {
        "modular-arithmetic": "grounded", "discrete-logarithm": "grounded",
        "diffie-hellman-key-exchange": "planned", "shared-secret": "planned",
        "elliptic-curves": "dropped"}  # fmt: skip
    ma = concepts["modular-arithmetic"]
    assert [(g["slug"], g["role"], g["grounds"]) for g in ma["grounding"]] == [
        ("discrete-logs", "introduces", True), ("held-notes", "uses", False)]
    dh = concepts["diffie-hellman-key-exchange"]
    assert [(g["slug"], g["grounds"]) for g in dh["grounding"]] == [("the-dh-protocol", False)]
    assert dh["graph"] == {"outcome": "found", "reason": "exact name match: 'Diffie-Hellman key exchange'"}
    assert [(link["key"], link["outcome"]) for link in dh["links"]] == [
        ("modular-arithmetic", "added"), ("discrete-logarithm", "added")]
    assert concepts["elliptic-curves"]["graph"] is None  # dropped: not written
    g = s["grounding"]
    assert (g["grounded"], g["ungrounded"]) == (2, 2)
    assert {i["key"]: (i["grounded"], i["proposed"]) for i in g["items"]}["diffie-hellman-key-exchange"] == (False, 1)
    # No judge call: every concept was found exactly or had nothing close.
    assert the_enricher.matcher._client.calls == []


def test_a_failing_source_does_not_stop_the_others_and_is_tried_again(
    settings: Settings, world: dict[str, Any], embedder: Any, fake_anthropic: Any
) -> None:
    extract = fake_anthropic(
        fake_anthropic.reply(text="x", stop_reason="refusal", model="claude-sonnet-5-5"),  # dh
    )
    pages = {DH_URL: PAGES[DH_URL]}  # dlog cannot be fetched
    the_enricher, _ = make(fake_anthropic, embedder, extract, pages=pages)
    first = enrich(settings, the_enricher, world["id"])
    data = detail(settings, world["id"])
    assert data["enrichment"]["status"] == "completed"
    s = data["enrichment"]["summary"]["sources"]
    assert (s["added"], s["held"], s["failed"]) == (0, 1, 2)
    cands = candidates(data)
    dlog, dh = cands["Discrete logs"], cands["The DH protocol"]
    # Both stay ticked, with their errors, to be tried again.
    assert dlog["decision"] == "approved" and dlog["ingest_error"].startswith("fetching it: ")
    assert "could not be fetched" in dlog["ingest_error"] and dlog["knowledge_source"] is None
    assert dh["decision"] == "approved" and dh["ingest_error"].startswith("concept extraction: ")
    assert dh["ingest"]["extraction"] == "failed"
    assert dh["knowledge_source"]["slug"] == "the-dh-protocol"  # stored; its extraction failed
    assert cands["Held notes"]["decision"] == "ingested"

    # Again, with both working: dlog is added, dh's stored source is linked and extracted.
    the_enricher, fetcher = make(fake_anthropic, embedder)
    second = enrich(settings, the_enricher, world["id"])
    assert second != first
    data = detail(settings, world["id"])
    s = data["enrichment"]["summary"]["sources"]
    assert [(i["title"], i["outcome"]) for i in s["items"]] == [
        ("Discrete logs", "added"), ("The DH protocol", "held")]
    assert fetcher.calls == [DLOG_URL]  # dh was not fetched again
    cands = candidates(data)
    assert cands["The DH protocol"]["ingest"]["method"] == "earlier attempt"
    assert cands["The DH protocol"]["ingest_error"] is None
    with session_scope(settings) as session:
        assert sorted(session.scalars(select(KnowledgeSource.slug))) == [
            "discrete-logs", "held-notes", "the-dh-protocol"]


def test_a_second_run_is_idempotent(
    settings: Settings, world: dict[str, Any], embedder: Any, fake_anthropic: Any
) -> None:
    the_enricher, _ = make(fake_anthropic, embedder)
    enrich(settings, the_enricher, world["id"])
    before = counts(settings)
    with session_scope(settings) as session:
        links_before = session.scalar(select(func.count(SourceConcept.id)))
    p = preview(settings, world["id"])
    assert p["sources"]["total"] == 0
    assert {c["action"] for c in p["concepts"]["items"]} == {"use"}
    assert {link["expected"] for link in p["links"]["items"]} == {"present"}
    the_enricher, fetcher = make(fake_anthropic, embedder, fake_anthropic())  # no call allowed
    enrich(settings, the_enricher, world["id"])
    assert counts(settings) == before and fetcher.calls == []
    with session_scope(settings) as session:
        assert session.scalar(select(func.count(SourceConcept.id))) == links_before
    s = detail(settings, world["id"])["enrichment"]["summary"]
    assert (s["sources"]["added"], s["sources"]["held"], s["sources"]["failed"]) == (0, 0, 0)
    assert (s["relations"]["added"], s["relations"]["present"]) == (0, 3)
    assert {i["outcome"] for i in s["concepts"]["items"]} == {"matched", "found", "created"}
    assert (s["grounding"]["grounded"], s["grounding"]["ungrounded"]) == (2, 2)


def test_a_new_concept_is_judged_against_the_graph_before_it_is_created(
    settings: Settings, session: Any, embedder: Any, fake_anthropic: Any
) -> None:
    """A concept extraction did not create is looked up with the resolver; renamed, it is
    created with its drafted name as an alias."""
    embedder.table.update({"key agreement": [1.0, 0.0], "key agreement scheme": [0.95, 0.31]})
    made = graph(session, settings, embedder, ("Key agreement", ["key exchange"]))
    eid = completed_exploration(settings, [outline("key-exchange", "Key agreement scheme", 1),
                                           outline("shared-key", "Shared key", 1)])  # fmt: skip
    with session_scope(settings) as s:
        m = ExplorationManager(s, settings, owner=None)
        row = m.get(eid)
        m.rename_concept(row, next(c for c in m.concepts(row) if c.key == "shared-key"), "Common key")
    judge = fake_anthropic(fake_anthropic.judge_reply([
        {"candidate": 1, "verdict": "same_as", "concept_id": made["Key agreement"].id,
         "reason": "Both agree a key in public."},
    ]))  # fmt: skip
    the_enricher, _ = make(fake_anthropic, embedder, fake_anthropic(), judge=judge)
    enrich(settings, the_enricher, eid)
    data = detail(settings, eid)
    concepts = by_key(data)
    assert concepts["key-exchange"]["graph"] == {"outcome": "found",
                                                 "reason": "judged the same: Both agree a key in public."}  # fmt: skip
    assert concepts["key-exchange"]["known"]["concept"]["slug"] == "key-agreement"
    assert concepts["shared-key"]["graph"] == {"outcome": "created",
                                               "reason": "no concept of the graph comes close"}  # fmt: skip
    with session_scope(settings) as s:
        created = ConceptManager(s, settings).get_by_slug("common-key")
        assert [a.alias for a in created.aliases] == ["Shared key"]
        # The judged concept was not changed: no alias added, still reviewed as it was.
        kept = ConceptManager(s, settings).get_by_slug("key-agreement")
        assert [a.alias for a in kept.aliases] == ["key exchange"]
    assert len(judge.calls) == 1


# -- relations ----------------------------------------------------------------------------------


def test_links_that_cannot_be_written_are_skipped_with_the_reason(
    settings: Settings, session: Any, embedder: Any, fake_anthropic: Any
) -> None:
    made = graph(session, settings, embedder, "X", "Y", "Z", "W")
    relations = ConceptRelationManager(session, settings)
    relations.add("relates_to", made["X"], made["Y"], "They are related.", "user")
    relations.add("builds_on", made["Y"], made["Z"], "Y needs Z.", "user")
    relations.add("builds_on", made["Z"], made["W"], "Z needs W.", "user")
    session.commit()
    eid = completed_exploration(
        settings,
        [outline("x", "X", 1), outline("w", "W", 1), outline("z", "Z", 2),
         outline("y", "Y", 3, ["x", "z"]), outline("v", "V", 3, ["y"]),
         outline("w2", "W again", 3, ["y"])],
        matched={"x": made["X"].id, "y": made["Y"].id, "z": made["Z"].id, "w": made["W"].id,
                 "w2": made["W"].id},
    )  # fmt: skip
    p = preview(settings, eid)
    assert [(link["from_key"], link["to_key"], link["expected"], link["code"]) for link in p["links"]["items"]] == [
        ("y", "x", "skip", "related"), ("y", "z", "present", None),
        ("v", "y", "add", None), ("w2", "y", "skip", "cycle"),
    ]  # fmt: skip
    the_enricher, _ = make(fake_anthropic, embedder, fake_anthropic())
    enrich(settings, the_enricher, eid)
    data = detail(settings, eid)
    rel = data["enrichment"]["summary"]["relations"]
    assert (rel["added"], rel["present"], rel["skipped"]) == (1, 1, 2)
    items = {(i["from_key"], i["to_key"]): i for i in rel["items"]}
    assert items[("y", "x")]["code"] == "related"
    assert "already related as" in items[("y", "x")]["reason"] and "relates_to" in items[("y", "x")]["reason"]
    assert items[("y", "z")]["outcome"] == "present"
    assert items[("w2", "y")]["code"] == "cycle" and "cyclic" in items[("w2", "y")]["reason"]
    assert items[("v", "y")]["outcome"] == "added"
    y = by_key(data)["y"]
    assert [(link["key"], link["outcome"], link["code"]) for link in y["links"]] == [
        ("x", "skipped", "related"), ("z", "present", None)]
    with session_scope(settings) as s:
        found = sorted((r.from_concept.slug, r.type, r.to_concept.slug, r.origin)
                       for r in ConceptRelationManager(s, settings).list())  # fmt: skip
    # The existing relations are untouched; one was added (V, created, builds on Y).
    assert found == [("v", "builds_on", "y", "model"), ("x", "relates_to", "y", "user"),
                     ("y", "builds_on", "z", "user"), ("z", "builds_on", "w", "user")]  # fmt: skip


# -- grounding and the roadmap ---------------------------------------------------------------------


def test_the_roadmap_recommends_the_best_grounding_source(
    settings: Settings, world: dict[str, Any], embedder: Any, fake_anthropic: Any, session: Any
) -> None:
    the_enricher, _ = make(fake_anthropic, embedder)
    enrich(settings, the_enricher, world["id"])
    with session_scope(settings) as s:
        row = ExplorationManager(s, settings, owner=None).get(world["id"])
        steps = [step.to_dict() for step in exploration_roadmap(s, settings, row)]
    assert [(st["number"], [c["key"] for c in st["concepts"]]) for st in steps] == [
        (1, ["modular-arithmetic"]), (2, ["discrete-logarithm"]),
        (3, ["diffie-hellman-key-exchange", "shared-secret"])]  # the dropped one is left out
    concepts = {c["key"]: c for st in steps for c in st["concepts"]}
    dl = concepts["discrete-logarithm"]
    assert dl["grounded"] is True and dl["concept"]["slug"] == "discrete-logarithm"
    assert dl["primary_source"]["slug"] == "discrete-logs" and dl["primary_source"]["overall"] == 4.0
    assert dl["prerequisites"] == ["modular-arithmetic"]
    ma = concepts["modular-arithmetic"]
    assert ma["primary_source"]["slug"] == "discrete-logs"  # introduces it; held-notes only uses it
    assert concepts["diffie-hellman-key-exchange"]["primary_source"] is None
    assert concepts["shared-secret"]["sources"] == [] and concepts["shared-secret"]["grounded"] is False


def test_a_concept_whose_grounding_source_is_gone_is_no_longer_grounded(
    settings: Settings, world: dict[str, Any], embedder: Any, fake_anthropic: Any
) -> None:
    the_enricher, _ = make(fake_anthropic, embedder)
    enrich(settings, the_enricher, world["id"])
    with session_scope(settings) as s:
        manager = KnowledgeSourceManager(s, settings)
        manager.delete(manager.get_by_slug("discrete-logs"))
    the_enricher, _ = make(fake_anthropic, embedder, fake_anthropic())
    enrich(settings, the_enricher, world["id"])
    concepts = by_key(detail(settings, world["id"]))
    assert concepts["modular-arithmetic"]["status"] == "known"  # matched before: known again
    assert concepts["discrete-logarithm"]["status"] == "planned"


# -- the guards ---------------------------------------------------------------------------------


def test_only_one_enrichment_at_a_time_and_edits_wait(
    settings: Settings, world: dict[str, Any], embedder: Any, fake_anthropic: Any
) -> None:
    the_enricher, _ = make(fake_anthropic, embedder)
    digest = preview(settings, world["id"])["digest"]
    enrichment_id = start_enrichment(settings, the_enricher, world["id"], owner=None, digest=digest)
    with pytest.raises(StateConflictError, match="is being enriched|An enrichment of this exploration is running"):
        start_enrichment(settings, the_enricher, world["id"], owner=None, digest=digest)
    assert preview(settings, world["id"])["can_start"] is False
    with session_scope(settings) as session:
        m = ExplorationManager(session, settings, owner=None)
        row = m.get(world["id"])
        concept, cand_ = m.concepts(row)[0], m.candidates(row)[0]
        for edit in (
            lambda: m.rename_concept(row, concept, "Clock arithmetic"),
            lambda: m.set_dropped(row, concept, True),
            lambda: m.reorder_steps(row, [1, 2, 3]),
            lambda: m.decide(row, cand_, "proposed"),
        ):
            with pytest.raises(StateConflictError, match="is being enriched"):
                edit()
        with pytest.raises(StateConflictError, match="being enriched already"):
            m.create_enrichment(row, {"digest": "x"})
    # The database refuses a second running one too.
    with session_scope(settings) as session:
        with pytest.raises(IntegrityError), session.begin_nested():
            session.add(ExplorationEnrichment(exploration_id=world["id"], status="running", plan={}))
            session.flush()
    assert detail(settings, world["id"])["editable"] is False
    run_enrichment(settings, enrichment_id, the_enricher)
    assert detail(settings, world["id"])["editable"] is True


def test_a_changed_plan_is_refused(settings: Settings, world: dict[str, Any], embedder: Any,
                                   fake_anthropic: Any) -> None:  # fmt: skip
    the_enricher, _ = make(fake_anthropic, embedder)
    digest = preview(settings, world["id"])["digest"]
    approve(settings, world["id"], "c_other")  # ticked after the preview was shown
    with pytest.raises(StateConflictError, match="The plan changed since it was shown"):
        start_enrichment(settings, the_enricher, world["id"], owner=None, digest=digest)
    with pytest.raises(ValueError, match="digest"):
        start_enrichment(settings, the_enricher, world["id"], owner=None, digest=" ")
    with session_scope(settings) as session:
        assert session.scalar(select(func.count(ExplorationEnrichment.id))) == 0


def test_an_enrichment_needs_a_completed_exploration_and_its_models(
    settings: Settings, embedder: Any, fake_anthropic: Any
) -> None:
    with session_scope(settings) as session:
        running = ExplorationManager(session, settings, owner=None).create("DH", "intro").id
    the_enricher, _ = make(fake_anthropic, embedder)
    with pytest.raises(StateConflictError, match="only a completed exploration can be enriched"):
        start_enrichment(settings, the_enricher, running, owner=None, digest="x")
    eid = completed_exploration(settings, [outline("a", "A", 1)])
    embedder.available = False
    with pytest.raises(EnrichmentUnavailableError, match="The enrichment failed: .*no OpenAI API key"):
        start_enrichment(settings, the_enricher, eid, owner=None, digest=preview(settings, eid)["digest"])


def test_an_interrupted_enrichment_ends_failed_and_can_run_again(
    settings: Settings, world: dict[str, Any], embedder: Any, fake_anthropic: Any
) -> None:
    the_enricher, _ = make(fake_anthropic, embedder)
    first = start_enrichment(settings, the_enricher, world["id"], owner=None,
                             digest=preview(settings, world["id"])["digest"])  # fmt: skip
    with session_scope(settings) as session:
        m = ExplorationManager(session, settings, owner=None)
        m.begin_enrichment_stage(session.get(ExplorationEnrichment, first), "ingest", total=3)
    assert fail_interrupted_enrichments(settings) == 1
    e = detail(settings, world["id"])["enrichment"]
    assert e["status"] == "failed" and e["error"] == INTERRUPTED_ENRICHMENT_ERROR.format(stage="ingest")
    assert e["stages"][-1]["status"] == "failed" and e["finished_at"] is not None
    assert fail_interrupted_enrichments(settings) == 0
    second = enrich(settings, the_enricher, world["id"])
    assert detail(settings, world["id"])["enrichment"]["id"] == second
    assert detail(settings, world["id"])["enrichment"]["status"] == "completed"


def test_a_job_marked_failed_meanwhile_stops_instead_of_writing_on(
    settings: Settings, world: dict[str, Any], embedder: Any, fake_anthropic: Any
) -> None:
    the_enricher, _ = make(fake_anthropic, embedder)

    def restart_sweep(stage: str) -> None:
        if stage == "graph":
            fail_interrupted_enrichments(settings)

    enrich(settings, the_enricher, world["id"], on_stage=restart_sweep)
    data = detail(settings, world["id"])
    assert data["enrichment"]["status"] == "failed"
    assert data["enrichment"]["error"] == INTERRUPTED_ENRICHMENT_ERROR.format(stage="graph")
    with session_scope(settings) as session:
        assert session.scalar(select(func.count(ConceptRelation.id))) == 0  # graph stage stopped
    assert by_key(data)["shared-secret"]["graph"] is None


def test_a_failure_of_the_whole_job_is_stored(
    settings: Settings, world: dict[str, Any], embedder: Any, fake_anthropic: Any
) -> None:
    """The embedding of the new concepts fails: the enrichment fails with the error, in the
    graph stage; what the ingest stage did stays."""
    the_enricher, _ = make(fake_anthropic, embedder)
    # Calls: 1 the world's graph, 2 and 3 the two extractions, 4 the resolver's query, 5 the
    # embedding of the concept to create.
    embedder.fail_on = {5}
    enrich(settings, the_enricher, world["id"])
    data = detail(settings, world["id"])
    e = data["enrichment"]
    assert e["status"] == "failed" and e["stage"] == "graph"
    assert e["error"] == (
        "The enrichment failed: the new concepts could not be embedded: Embedding failed: fake "
        "failure on call 5"
    )
    assert e["stages"][-1]["name"] == "graph" and e["stages"][-1]["status"] == "failed"
    assert e["summary"] is None and data["editable"] is True
    assert candidates(data)["Discrete logs"]["decision"] == "ingested"  # the ingest stays
    with session_scope(settings) as session:  # nothing of the failed write
        assert session.scalar(select(func.count(Concept.id)).where(Concept.slug == "shared-secret")) == 0


# -- the ledger -----------------------------------------------------------------------------------


def test_every_model_call_is_in_the_ledger_with_the_exploration_and_the_enrichment(
    settings: Settings, session: Any, embedder: Any, fake_anthropic: Any, make_pdf: Any
) -> None:
    url = "https://arxiv.org/pdf/1706.03762"
    eid = completed_exploration(settings, [outline("attention", "Attention", 1)],
                                {"attention": [candidate("c_paper", url, title="search title")]})  # fmt: skip
    approve(settings, eid, "c_paper")
    metadata = MetadataExtractor(fake_anthropic(fake_anthropic.metadata_reply(
        title="Attention Is All You Need", arxiv_id="1706.03762")))  # fmt: skip
    extract = fake_anthropic(fake_anthropic.concepts_reply([cand("Attention")]))
    the_enricher, _ = make(fake_anthropic, embedder, extract, metadata=metadata,
                           pages={url: pdf(url, make_pdf(2))})  # fmt: skip
    enrichment_id = enrich(settings, the_enricher, eid)
    data = detail(settings, eid)
    assert data["enrichment"]["status"] == "completed", data["enrichment"]["error"]
    with session_scope(settings) as s:
        source = KnowledgeSourceManager(s, settings).get_by_slug("attention-is-all-you-need")
        assert source.kind == "paper" and [(i.scheme, i.value) for i in source.identifiers] == [
            ("arxiv", "1706.03762")]
        rows = [(r.purpose, r.step, r.exploration_id, r.exploration_enrichment_id,
                 r.knowledge_source_id is not None) for r in s.scalars(select(ApiCall).order_by(ApiCall.id))]  # fmt: skip
    assert rows[0] == ("exploration", "metadata", eid, enrichment_id, False)
    assert ("exploration", "concepts", eid, enrichment_id, True) in rows
    assert all(r[0] == "exploration" and r[2] == eid and r[3] == enrichment_id for r in rows)
    assert {r[1] for r in rows} == {"metadata", "concepts"}  # the fake embedder records none
    assert data["enrichment"]["cost"]["calls"] == len(rows)
    parts = {p["key"]: p["label"] for p in data["cost"]["parts"]}
    assert parts["enrichment:metadata"] == "enrichment: source details"
    assert parts["enrichment:concepts"] == "enrichment: concept extraction"
    assert by_key(data)["attention"]["status"] == "grounded"
    cand_ = candidates(data)["search title"]
    assert cand_["ingest"]["details"]["title"] == "detected"


def test_a_source_held_by_content_or_identifier_is_linked(
    settings: Settings, session: Any, embedder: Any, fake_anthropic: Any, tmp_path: Path
) -> None:
    text = "The same text, stored before under another address."
    path = tmp_path / "same.md"
    path.write_text(text + "\n", encoding="utf-8")
    KnowledgeSourceManager(session, settings).add(path, slug="same", kind="web_page", title="Same")
    session.commit()
    url = "https://b.example/copy"
    eid = completed_exploration(settings, [outline("a", "A", 1)],
                                {"a": [candidate("c_copy", url)]})  # fmt: skip
    approve(settings, eid, "c_copy")
    extract = fake_anthropic(fake_anthropic.concepts_reply([cand("A")]))
    the_enricher, fetcher = make(fake_anthropic, embedder, extract, pages={url: page(url, text)})
    assert preview(settings, eid)["sources"]["items"][0]["action"] == "add"  # not known before fetching
    enrich(settings, the_enricher, eid)
    item = detail(settings, eid)["enrichment"]["summary"]["sources"]["items"][0]
    assert (item["outcome"], item["slug"]) == ("held", "same")
    with session_scope(settings) as s:
        assert list(s.scalars(select(KnowledgeSource.slug))) == ["same"]
    cand_ = candidates(detail(settings, eid))["About c_copy"]
    assert cand_["ingest"]["method"] == "content" and cand_["ingest"]["extraction"] == "ran"


def test_a_taken_slug_gets_a_suffix(
    settings: Settings, session: Any, embedder: Any, fake_anthropic: Any, tmp_path: Path
) -> None:
    path = tmp_path / "x.md"
    path.write_text("# Another work\n", encoding="utf-8")
    KnowledgeSourceManager(session, settings).add(path, slug="notes", kind="web_page", title="Notes")
    session.commit()
    url = "https://c.example/notes"
    eid = completed_exploration(settings, [outline("a", "A", 1)],
                                {"a": [candidate("c_notes", url, title="Notes")]})  # fmt: skip
    approve(settings, eid, "c_notes")
    extract = fake_anthropic(fake_anthropic.concepts_reply([cand("A")]))
    the_enricher, _ = make(fake_anthropic, embedder, extract,
                           pages={url: page(url, "Different notes on A.")})  # fmt: skip
    enrich(settings, the_enricher, eid)
    cand_ = candidates(detail(settings, eid))["Notes"]
    assert cand_["knowledge_source"]["slug"] == "notes-2"
    assert cand_["ingest"]["slug_note"] == "slug 'notes' belongs to another source; added as 'notes-2'"


def test_the_enricher_needs_one_embedding_model(fake_embedder: Any) -> None:
    from teacher.exploration import Enricher

    def holder(model: str) -> Any:
        return type("Holder", (), {"embedder": fake_embedder(model=model)})()

    with pytest.raises(ValueError, match="must agree"):
        Enricher(fetcher=object(), metadata_extractor=object(), concept_extractor=holder("a"),
                 matcher=holder("b"))  # fmt: skip
    with pytest.raises(ValueError, match="needs fetcher"):
        Enricher(fetcher=None, metadata_extractor=object(), concept_extractor=holder("a"),
                 matcher=holder("a"))  # fmt: skip


def test_a_bug_with_one_source_is_stored_on_it_and_the_others_go_on(
    settings: Settings, world: dict[str, Any], embedder: Any, fake_anthropic: Any
) -> None:
    extract = fake_anthropic(fake_anthropic.concepts_reply([cand("Diffie-Hellman key exchange", aliases=["DH"])]))
    the_enricher, fetcher = make(fake_anthropic, embedder, extract)
    real = fetcher.fetch

    def fetch(url: str) -> Any:
        if url == DLOG_URL:
            raise RuntimeError("a bug in the fetcher")
        return real(url)

    fetcher.fetch = fetch  # type: ignore[method-assign]
    enrich(settings, the_enricher, world["id"])
    data = detail(settings, world["id"])
    assert data["enrichment"]["status"] == "completed"
    cands = candidates(data)
    assert cands["Discrete logs"]["ingest_error"] == "fetching it: RuntimeError: a bug in the fetcher"
    assert cands["The DH protocol"]["decision"] == "ingested"


def test_the_app_marks_interrupted_enrichments_failed_at_startup(
    settings: Settings, world: dict[str, Any]
) -> None:
    from fastapi.testclient import TestClient

    from teacher.generation import StubGenerator
    from teacher.lab_generator import StubLabGenerator
    from teacher.web.app import create_app

    with session_scope(settings) as session:
        m = ExplorationManager(session, settings, owner=None)
        m.create_enrichment(m.get(world["id"]), {"digest": "x"})
    app = create_app(settings, generator=StubGenerator(), lab_generator=StubLabGenerator())
    with TestClient(app) as client:
        data = client.get(f"/api/explorations/{world['id']}").json()
    assert data["enrichment"]["status"] == "failed"
    assert data["enrichment"]["error"] == INTERRUPTED_ENRICHMENT_ERROR.format(stage="first")
    assert data["editable"] is True and data["can_enrich"] is True
