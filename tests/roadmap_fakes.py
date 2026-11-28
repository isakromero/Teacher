"""Builders for the roadmap tests (`teacher.exploration.roadmap`). Nothing here reaches the
network or a model: the library, the graph and the exploration are written through the managers.

`build_world(settings, tmp_path, owner=None)` makes:

- graph concepts `modular-arithmetic`, `group`, `discrete-logarithm`, `diffie-hellman`;
- sources (in this order, so by id): `number-theory` (introduces modular arithmetic and group:
  the primary source of both), `dlog-notes` and `dlog-paper` (both introduce discrete
  logarithm: the notes, older, are recommended), `dh-blog` (only uses Diffie-Hellman: it does
  not ground it);
- a completed exploration (`World.eid`) with the outline: step 1 `ma` (Modular arithmetic) and
  `gr` (Group), step 2 `dl` (Discrete logarithm, builds on ma), step 3 `dh` (Diffie-Hellman key
  exchange, builds on dl), `ec` (Elliptic curves: new, no graph concept) and `xx` (Extra:
  dropped). ma, gr, dl and dh are matched to their graph concepts.

`study_page(settings, source, slug, gates=2, tag=None, status="ready")` adds a study page of a
source with `gates` multiple-choice gates (tagged `main` with concept `tag`), `answer(settings,
page, [True, False, ...])` records one attempt per listed gate in order (True passes).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from teacher.config import Settings
from teacher.db.engine import session_scope
from teacher.managers import (
    ArtifactBlockManager,
    ArtifactManager,
    BlockConceptManager,
    BlockResponseManager,
    ConceptManager,
    ExplorationManager,
    KnowledgeSourceManager,
    LinkSpec,
    SourceConceptManager,
)
from tests.enrichment_fakes import completed_exploration, outline

MC = {
    "question": "Which is right?",
    "options": ["This one", "That one", "Neither"],
    "private": {"correct_index": 0, "explanation": "This one is right."},
}
CONCEPTS = {
    "modular-arithmetic": "Modular arithmetic",
    "group": "Group",
    "discrete-logarithm": "Discrete logarithm",
    "diffie-hellman": "Diffie-Hellman key exchange",
}
SOURCES = {  # slug: (title, [(concept slug, role)])
    "number-theory": ("Number theory", [("modular-arithmetic", "introduces"), ("group", "introduces")]),
    "dlog-notes": ("Discrete log notes", [("discrete-logarithm", "introduces"), ("modular-arithmetic", "uses")]),
    "dlog-paper": ("Discrete log paper", [("discrete-logarithm", "introduces")]),
    "dh-blog": ("A Diffie-Hellman blog", [("diffie-hellman", "uses")]),
}
OUTLINE = [
    outline("ma", "Modular arithmetic", 1),
    outline("gr", "Group", 1),
    outline("dl", "Discrete logarithm", 2, ["ma"]),
    outline("dh", "Diffie-Hellman key exchange", 3, ["dl"]),
    outline("ec", "Elliptic curves", 3),
    outline("xx", "Extra", 3),
]


@dataclass(frozen=True)
class World:
    eid: int
    concepts: dict[str, int]  # graph concept slug -> id
    sources: dict[str, int]  # source slug -> id
    rows: dict[str, int]  # outline key -> exploration concept id


def build_world(settings: Settings, tmp_path: Path, *, owner: int | None = None) -> World:
    with session_scope(settings) as session:
        concepts = {
            slug: ConceptManager(session, settings).create(name, f"About {name}.", "paper", slug=slug)
            for slug, name in CONCEPTS.items()
        }
        sources = {}
        for slug, (title, links) in SOURCES.items():
            path = tmp_path / f"{slug}.txt"
            path.write_text(f"Text of {title}.\n", encoding="utf-8")
            source = KnowledgeSourceManager(session, settings).add(path, slug=slug, kind="note", title=title)
            SourceConceptManager(session, settings).replace(
                source,
                [LinkSpec(concepts[c], role, "created", CONCEPTS[c],
                          evidence=None if role == "assumes" else "it says so")
                 for c, role in links],
            )  # fmt: skip
            sources[slug] = source.id
        concept_ids = {slug: c.id for slug, c in concepts.items()}
    matched = {"ma": concept_ids["modular-arithmetic"], "gr": concept_ids["group"],
               "dl": concept_ids["discrete-logarithm"], "dh": concept_ids["diffie-hellman"]}  # fmt: skip
    eid = completed_exploration(settings, OUTLINE, {}, matched=matched, owner=owner)
    with session_scope(settings) as session:
        m = ExplorationManager(session, settings, owner=owner)
        row = m.get(eid)
        m.set_dropped(row, next(c for c in m.concepts(row) if c.key == "xx"), True)
        rows = {c.key: c.id for c in m.concepts(row)}
    return World(eid, concept_ids, sources, rows)


def study_page(
    settings: Settings,
    source: str | Sequence[str],
    slug: str,
    *,
    gates: int = 2,
    tag: str | None = None,
    status: str = "ready",
) -> str:
    with session_scope(settings) as session:
        found = [
            KnowledgeSourceManager(session, settings).get_by_slug(s)
            for s in ([source] if isinstance(source, str) else source)
        ]
        artifacts = ArtifactManager(session, settings)
        page = artifacts.create(slug=slug, kind="study_page", title=f"Study: {slug}",
                                generator="test", sources=found)  # fmt: skip
        blocks = ArtifactBlockManager(session, settings)
        blocks.add(page, type="prose", content={"markdown": "# Intro\n\nSome text."})
        made = [blocks.add(page, type="multiple_choice", content=MC, is_gate=True) for _ in range(gates)]
        if tag is not None:
            concept = ConceptManager(session, settings).get_by_slug(tag)
            for block in made:
                BlockConceptManager(session, settings).add(block, concept, "main", "user")
        artifacts.set_status(page, status)
        return page.slug


def answer(settings: Settings, page: str, results: Sequence[bool | None]) -> None:
    """One attempt on each of the page's gates in order, passing where `results` is True (None:
    no attempt on that gate)."""
    with session_scope(settings) as session:
        artifact = ArtifactManager(session, settings).get_by_slug(page)
        gates = [b for b in artifact.blocks if b.is_gate]
        responses = BlockResponseManager(session, settings)
        for block, passed in zip(gates, results, strict=False):
            if passed is None:
                continue
            responses.record(block, response={"choice": 0 if passed else 1}, passed=passed,
                             grader="exact")  # fmt: skip


__all__ = ["CONCEPTS", "OUTLINE", "SOURCES", "World", "answer", "build_world", "study_page"]
