"""SourceConceptManager: the links from a knowledge source to the concepts it introduces, uses or
assumes.

Concept extraction owns these links: it replaces all of a source's links in one go
(`replace`), inside the transaction that also creates the concepts it links, so a source's links
are always those of exactly one extraction run. `ConceptManager.merge` moves the links of a
merged-away concept onto the concept it is merged into; deleting a source or a concept deletes
its links (database cascade).
"""

from __future__ import annotations

import builtins
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from teacher.concepts import SOURCE_ROLES, validate_link_resolution, validate_source_role
from teacher.config import Settings
from teacher.db.models import Concept, KnowledgeSource, SourceConcept
from teacher.managers.artifacts import _is_unique_violation, _require_persistent, _validate_text
from teacher.managers.errors import DuplicateError


@dataclass(frozen=True, slots=True)
class LinkSpec:
    """One link to write: `concept` (persistent), how the source relates to it (`role`), how
    the run found it (`resolution`), the name the extraction gave it, the judge's reason
    (exactly for `judged`), and a short phrase from the source (or None)."""

    concept: Concept
    role: str
    resolution: str
    candidate_name: str
    judge_reason: str | None = None
    evidence: str | None = None


def _optional_text(value: object, name: str) -> str | None:
    if value is None:
        return None
    return _validate_text(value, name).strip()


class SourceConceptManager:
    """Replace and list the concept links of a source, and list the sources of a concept."""

    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings

    def replace(
        self, source: KnowledgeSource, links: Sequence[LinkSpec]
    ) -> tuple[int, builtins.list[SourceConcept]]:
        """Make `links` the source's whole set of concept links; returns (how many links were
        removed, the new rows in the order given).

        Every link is checked first: a known role and resolution, a non-blank candidate name, a
        judge reason exactly when the resolution is `judged`, a persistent concept, and no
        concept twice (DuplicateError). Nothing changes on error. An empty `links` removes all
        of the source's links.
        """
        _require_persistent(source, "source")
        rows: list[SourceConcept] = []
        seen: dict[int, int] = {}
        for number, link in enumerate(links, start=1):
            if not isinstance(link, LinkSpec):
                raise TypeError(f"link {number} must be a LinkSpec, got {link.__class__.__name__}")
            _require_persistent(link.concept, f"the concept of link {number}")
            validate_source_role(link.role)
            validate_link_resolution(link.resolution)
            reason = _optional_text(link.judge_reason, f"link {number}: judge_reason")
            if (link.resolution == "judged") != (reason is not None):
                raise ValueError(
                    f"link {number} ({link.concept.slug!r}): a judge reason is required for a "
                    "judged link and not allowed otherwise"
                )
            if link.concept.id in seen:
                raise DuplicateError(
                    f"link {number} repeats concept {link.concept.slug!r} of link "
                    f"{seen[link.concept.id]}; a source links a concept once"
                )
            seen[link.concept.id] = number
            rows.append(
                SourceConcept(
                    source_id=source.id,
                    concept_id=link.concept.id,
                    role=link.role,
                    resolution=link.resolution,
                    candidate_name=_validate_text(
                        link.candidate_name, f"link {number}: candidate_name"
                    ).strip(),
                    judge_reason=reason,
                    evidence=_optional_text(link.evidence, f"link {number}: evidence"),
                )
            )
        try:
            with self.session.begin_nested():
                removed = self.session.execute(
                    delete(SourceConcept).where(SourceConcept.source_id == source.id)
                ).rowcount
                self.session.add_all(rows)
                self.session.flush()
        except IntegrityError as err:
            if _is_unique_violation(err):
                raise DuplicateError(f"source {source.slug!r}: duplicate concept link") from err
            raise
        return removed, rows

    def for_source(self, source: KnowledgeSource) -> builtins.list[SourceConcept]:
        """The source's links with their concepts, by role (introduces, uses, assumes), then
        concept name."""
        _require_persistent(source, "source")
        rows = list(
            self.session.scalars(
                select(SourceConcept)
                .join(Concept, Concept.id == SourceConcept.concept_id)
                .where(SourceConcept.source_id == source.id)
                .order_by(Concept.name_norm, Concept.slug)
            )
        )
        return sorted(rows, key=_role_order)

    def for_concept(self, concept: Concept) -> builtins.list[SourceConcept]:
        """The links to the concept, by role, then source title."""
        _require_persistent(concept, "concept")
        rows = list(
            self.session.scalars(
                select(SourceConcept)
                .join(KnowledgeSource, KnowledgeSource.id == SourceConcept.source_id)
                .where(SourceConcept.concept_id == concept.id)
                .order_by(KnowledgeSource.title, KnowledgeSource.slug)
            )
        )
        return sorted(rows, key=_role_order)


def _role_order(row: SourceConcept) -> int:
    return SOURCE_ROLES.index(row.role)


__all__ = ["LinkSpec", "SourceConceptManager"]
