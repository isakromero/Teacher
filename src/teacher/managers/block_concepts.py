"""BlockConceptManager: which concepts each study-page block covers (`block_concepts`).

The block tagger (`teacher.block_tagging`) owns the model-made tags of a page: it replaces all of
them in one go (`replace_model_tags`), which also stamps the page's `block_concepts_tagged_at`,
so a page's model tags are always those of exactly one tagging run. Tags with origin `user` are
never touched by a run; a run that names a pair a user tag already covers leaves the user's tag
as it is (reported as `kept_user`). `ConceptManager.merge` moves the tags of a merged-away
concept; deleting a page (its blocks) or a concept deletes its tags (database cascade).

Like every manager it flushes and never commits.
"""

from __future__ import annotations

import builtins
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from teacher.config import Settings
from teacher.db.models import (
    BLOCK_CONCEPT_ORIGINS,
    BLOCK_CONCEPT_ROLES,
    Artifact,
    ArtifactBlock,
    BlockConcept,
    Concept,
    utcnow,
)
from teacher.labs import LAB_KIND
from teacher.managers.artifacts import _is_unique_violation, _require_persistent, _validate_text
from teacher.managers.errors import DuplicateError


def validate_block_role(role: object) -> str:
    if role not in BLOCK_CONCEPT_ROLES:
        raise ValueError(f"unknown block concept role {role!r}: expected one of {BLOCK_CONCEPT_ROLES}")
    return role  # type: ignore[return-value]


def stronger_block_role(a: str, b: str) -> str:
    """`main` beats `incidental`."""
    validate_block_role(a)
    validate_block_role(b)
    return "main" if "main" in (a, b) else "incidental"


@dataclass(frozen=True, slots=True)
class TagSpec:
    """One model tag to write: `block` covers `concept` (both persistent) as `role`, for the
    tagger's one-sentence `rationale`."""

    block: ArtifactBlock
    concept: Concept
    role: str
    rationale: str


@dataclass(frozen=True, slots=True)
class ReplaceResult:
    """What `replace_model_tags` did: how many model tags it `removed`, the new rows `added`
    (in the order given), and the specs it did not write because a user tag already covers
    the pair (`kept_user`)."""

    removed: int
    added: tuple[BlockConcept, ...] = ()
    kept_user: tuple[TagSpec, ...] = field(default_factory=tuple)


def _role_order(tag: BlockConcept) -> int:
    return BLOCK_CONCEPT_ROLES.index(tag.role)


class BlockConceptManager:
    """Write and read the concept tags of study-page blocks."""

    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings

    # -- writes -----------------------------------------------------------------------------

    def _check_page(self, artifact: Artifact) -> None:
        _require_persistent(artifact, "artifact")
        if artifact.kind == LAB_KIND:
            raise ValueError(f"{artifact.slug!r} is a lab; only study-page blocks have concepts")

    def add(
        self,
        block: ArtifactBlock,
        concept: Concept,
        role: str,
        origin: str,
        *,
        rationale: str | None = None,
        model: str | None = None,
    ) -> BlockConcept:
        """Tag `block` with `concept`. A `model` tag needs `model` and a `rationale`; a `user`
        tag takes no model. DuplicateError if the block already has the concept."""
        _require_persistent(block, "block")
        _require_persistent(concept, "concept")
        self._check_page(block.artifact)
        validate_block_role(role)
        if origin not in BLOCK_CONCEPT_ORIGINS:
            raise ValueError(
                f"unknown block concept origin {origin!r}: expected one of {BLOCK_CONCEPT_ORIGINS}"
            )
        if rationale is not None:
            rationale = _validate_text(rationale, "rationale").strip()
        if origin == "model":
            if model is None or rationale is None:
                raise ValueError("a model tag needs the model and a rationale")
            model = _validate_text(model, "model").strip()
        elif model is not None:
            raise ValueError("a user tag names no model")
        row = BlockConcept(
            block_id=block.id,
            concept_id=concept.id,
            role=role,
            origin=origin,
            rationale=rationale,
            model=model,
        )
        try:
            with self.session.begin_nested():
                self.session.add(row)
                self.session.flush()
        except IntegrityError as err:
            if _is_unique_violation(err):
                raise DuplicateError(
                    f"block {block.key} of {block.artifact.slug!r} is already tagged with "
                    f"{concept.slug!r}"
                ) from err
            raise
        return row

    def replace_model_tags(
        self,
        artifact: Artifact,
        specs: Sequence[TagSpec],
        model: str,
        *,
        at: datetime | None = None,
    ) -> ReplaceResult:
        """Make `specs` the page's whole set of model tags (origin `model`, made by `model`) and
        set its `block_concepts_tagged_at` to `at` (default now). Every spec is checked first:
        a block of this page, a persistent concept, a known role, a non-blank rationale, and no
        (block, concept) twice (DuplicateError). User tags stay; a spec on a pair a user tag
        covers is not written (`kept_user`). Nothing changes on error. An empty `specs` removes
        every model tag and still marks the page tagged."""
        self._check_page(artifact)
        model = _validate_text(model, "model").strip()
        user_pairs = {
            (block_id, concept_id)
            for block_id, concept_id in self.session.execute(
                select(BlockConcept.block_id, BlockConcept.concept_id)
                .join(ArtifactBlock, ArtifactBlock.id == BlockConcept.block_id)
                .where(ArtifactBlock.artifact_id == artifact.id, BlockConcept.origin == "user")
            )
        }
        rows: list[BlockConcept] = []
        kept: list[TagSpec] = []
        seen: set[tuple[int, int]] = set()
        for number, spec in enumerate(specs, start=1):
            if not isinstance(spec, TagSpec):
                raise TypeError(f"tag {number} must be a TagSpec, got {spec.__class__.__name__}")
            _require_persistent(spec.block, f"the block of tag {number}")
            _require_persistent(spec.concept, f"the concept of tag {number}")
            if spec.block.artifact_id != artifact.id:
                raise ValueError(
                    f"tag {number}: block {spec.block.key} is not a block of {artifact.slug!r}"
                )
            validate_block_role(spec.role)
            rationale = _validate_text(spec.rationale, f"tag {number}: rationale").strip()
            pair = (spec.block.id, spec.concept.id)
            if pair in seen:
                raise DuplicateError(
                    f"tag {number} repeats concept {spec.concept.slug!r} on block "
                    f"{spec.block.key}; a block is tagged with a concept once"
                )
            seen.add(pair)
            if pair in user_pairs:
                kept.append(spec)
                continue
            rows.append(
                BlockConcept(
                    block_id=spec.block.id,
                    concept_id=spec.concept.id,
                    role=spec.role,
                    origin="model",
                    rationale=rationale,
                    model=model,
                )
            )
        block_ids = select(ArtifactBlock.id).where(ArtifactBlock.artifact_id == artifact.id)
        try:
            with self.session.begin_nested():
                removed = self.session.execute(
                    delete(BlockConcept).where(
                        BlockConcept.block_id.in_(block_ids), BlockConcept.origin == "model"
                    )
                ).rowcount
                self.session.add_all(rows)
                artifact.block_concepts_tagged_at = utcnow() if at is None else at
                self.session.flush()
        except IntegrityError as err:
            if _is_unique_violation(err):
                raise DuplicateError(f"{artifact.slug!r}: duplicate block concept tag") from err
            raise
        return ReplaceResult(removed=removed, added=tuple(rows), kept_user=tuple(kept))

    # -- reads ------------------------------------------------------------------------------

    def for_artifact(
        self, artifact: Artifact
    ) -> builtins.list[tuple[ArtifactBlock, BlockConcept, Concept]]:
        """The page's tags with their blocks and concepts: in block order, main before
        incidental, then by concept slug."""
        _require_persistent(artifact, "artifact")
        rows = list(
            self.session.execute(
                select(ArtifactBlock, BlockConcept, Concept)
                .join(BlockConcept, BlockConcept.block_id == ArtifactBlock.id)
                .join(Concept, Concept.id == BlockConcept.concept_id)
                .where(ArtifactBlock.artifact_id == artifact.id)
                .order_by(ArtifactBlock.position, Concept.slug)
            )
        )
        return sorted(rows, key=lambda r: (r[0].position, _role_order(r[1]), r[2].slug))

    def for_blocks(
        self, block_ids: Iterable[int]
    ) -> dict[int, builtins.list[tuple[BlockConcept, Concept]]]:
        """Per block id (only those with tags): its tags with their concepts, main first, then
        by concept slug."""
        ids = list(block_ids)
        out: dict[int, list[tuple[BlockConcept, Concept]]] = {}
        if not ids:
            return out
        for tag, concept in self.session.execute(
            select(BlockConcept, Concept)
            .join(Concept, Concept.id == BlockConcept.concept_id)
            .where(BlockConcept.block_id.in_(ids))
            .order_by(Concept.slug)
        ):
            out.setdefault(tag.block_id, []).append((tag, concept))
        for tags in out.values():
            tags.sort(key=lambda pair: (_role_order(pair[0]), pair[1].slug))
        return out

    def for_concept(
        self, concept: Concept
    ) -> builtins.list[tuple[BlockConcept, ArtifactBlock, Artifact]]:
        """Every tag of `concept` with its block and page: main first, then by page id and
        block position."""
        _require_persistent(concept, "concept")
        rows = list(
            self.session.execute(
                select(BlockConcept, ArtifactBlock, Artifact)
                .join(ArtifactBlock, ArtifactBlock.id == BlockConcept.block_id)
                .join(Artifact, Artifact.id == ArtifactBlock.artifact_id)
                .where(BlockConcept.concept_id == concept.id)
                .order_by(Artifact.id, ArtifactBlock.position)
            )
        )
        return sorted(rows, key=lambda r: (_role_order(r[0]), r[2].id, r[1].position))

    def all(self) -> builtins.list[tuple[BlockConcept, ArtifactBlock, Artifact, Concept]]:
        """Every tag with its block, page and concept, by page id, block position, role and
        concept slug (the history export)."""
        rows = list(
            self.session.execute(
                select(BlockConcept, ArtifactBlock, Artifact, Concept)
                .join(ArtifactBlock, ArtifactBlock.id == BlockConcept.block_id)
                .join(Artifact, Artifact.id == ArtifactBlock.artifact_id)
                .join(Concept, Concept.id == BlockConcept.concept_id)
            )
        )
        return sorted(
            rows, key=lambda r: (r[2].id, r[1].position, _role_order(r[0]), r[3].slug)
        )


__all__ = [
    "BlockConceptManager",
    "ReplaceResult",
    "TagSpec",
    "stronger_block_role",
    "validate_block_role",
]
