"""ConceptJudgementManager: the relation judge's memo of judged concept pairs.

`teacher.concept_relating` asks Claude whether two concepts are related; every answer (a
relation added, `none`, or a relation the graph refused) is kept here, one row per unordered
pair (`ConceptPairJudgement`), with the SHA-256 of both concepts' texts
(`teacher.embeddings.concept_text`) at judging time. A pair counts as judged only while both
texts are unchanged (`current`); a new verdict replaces the pair's row (`record`). Deleting a
concept deletes its rows (database cascade); deleting a relation leaves the row with
`relation_id` NULL (so a relation a person removed is not proposed again while the texts stay
the same); `ConceptManager.merge` drops the rows of the merged-away concept
(`delete_for_concept`).
"""

from __future__ import annotations

import builtins
from collections.abc import Sequence

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from teacher.config import Settings
from teacher.db.models import (
    JUDGEMENT_OUTCOMES,
    JUDGEMENT_VERDICTS,
    ConceptPairJudgement,
)
from teacher.embeddings import text_sha256
from teacher.managers.concept_relations import validate_rationale


def _pair(a_id: int, b_id: int) -> tuple[int, int]:
    for value in (a_id, b_id):
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"concept ids must be integers, got {value!r}")
    if a_id == b_id:
        raise ValueError(f"a concept is never judged against itself (id {a_id})")
    return min(a_id, b_id), max(a_id, b_id)


class ConceptJudgementManager:
    """Look up, record and drop the judge's verdicts on concept pairs."""

    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings

    def get(self, a_id: int, b_id: int) -> ConceptPairJudgement | None:
        """The row for the pair (either order), current or not, or None."""
        low, high = _pair(a_id, b_id)
        return self.session.scalar(
            select(ConceptPairJudgement).where(
                ConceptPairJudgement.low_id == low, ConceptPairJudgement.high_id == high
            )
        )

    def current(
        self, a_id: int, a_text: str, b_id: int, b_text: str
    ) -> ConceptPairJudgement | None:
        """The pair's row if both concepts still have the texts it was judged with, else None."""
        row = self.get(a_id, b_id)
        if row is None:
            return None
        hashes = {a_id: text_sha256(a_text), b_id: text_sha256(b_text)}
        if (row.low_text_sha256, row.high_text_sha256) != (hashes[row.low_id], hashes[row.high_id]):
            return None
        return row

    def for_concept(self, concept_id: int) -> builtins.list[ConceptPairJudgement]:
        """Every row with `concept_id` in its pair, in id order."""
        return list(
            self.session.scalars(
                select(ConceptPairJudgement)
                .where(
                    or_(
                        ConceptPairJudgement.low_id == concept_id,
                        ConceptPairJudgement.high_id == concept_id,
                    )
                )
                .order_by(ConceptPairJudgement.id)
            )
        )

    def list(self) -> builtins.list[ConceptPairJudgement]:
        """Every row, in id order."""
        return list(
            self.session.scalars(select(ConceptPairJudgement).order_by(ConceptPairJudgement.id))
        )

    def record(
        self,
        *,
        focus_id: int,
        focus_text: str,
        other_id: int,
        other_text: str,
        found_by: Sequence[str],
        similarity: float | None,
        verdict: str,
        outcome: str,
        rationale: str,
        model: str,
        refusal: str | None = None,
        relation_id: int | None = None,
    ) -> ConceptPairJudgement:
        """Store a verdict on (focus, other), replacing any earlier row for the pair. The texts
        are the two concepts' `concept_text` as the judge saw them.

        ValueError for an unknown verdict or outcome, an outcome that does not fit the verdict
        (`none` exactly for verdict none), a refusal reason without outcome `refused` (or the
        reverse), a relation id without outcome `added`, an empty `found_by` or a rationale
        that is not one sentence. Nothing changes on error."""
        low, high = _pair(focus_id, other_id)
        if verdict not in JUDGEMENT_VERDICTS:
            raise ValueError(f"invalid verdict {verdict!r}: expected one of {JUDGEMENT_VERDICTS}")
        if outcome not in JUDGEMENT_OUTCOMES:
            raise ValueError(f"invalid outcome {outcome!r}: expected one of {JUDGEMENT_OUTCOMES}")
        if (verdict == "none") != (outcome == "none"):
            raise ValueError(f"outcome {outcome!r} does not fit verdict {verdict!r}")
        if (outcome == "refused") != (refusal is not None):
            raise ValueError("a refusal reason is required exactly for outcome 'refused'")
        if refusal is not None and not refusal.strip():
            raise ValueError("the refusal reason is blank")
        if relation_id is not None and outcome != "added":
            raise ValueError(f"a relation id needs outcome 'added', not {outcome!r}")
        tags = list(found_by)
        if not tags or not all(isinstance(tag, str) and tag.strip() for tag in tags):
            raise ValueError(f"found_by must list how the candidate was found, got {tags!r}")
        rationale = validate_rationale(rationale)
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model must be a non-empty string")
        hashes = {focus_id: text_sha256(focus_text), other_id: text_sha256(other_text)}
        row = ConceptPairJudgement(
            low_id=low,
            high_id=high,
            focus_id=focus_id,
            low_text_sha256=hashes[low],
            high_text_sha256=hashes[high],
            found_by=tags,
            similarity=similarity,
            verdict=verdict,
            outcome=outcome,
            rationale=rationale,
            refusal=refusal.strip() if refusal is not None else None,
            relation_id=relation_id,
            model=model,
        )
        with self.session.begin_nested():
            self.session.execute(
                delete(ConceptPairJudgement).where(
                    ConceptPairJudgement.low_id == low, ConceptPairJudgement.high_id == high
                )
            )
            self.session.add(row)
            self.session.flush()
        return row

    def delete_for_concept(self, concept_id: int) -> int:
        """Delete every row with `concept_id` in its pair; returns how many."""
        rows = self.for_concept(concept_id)
        if rows:
            with self.session.begin_nested():
                for row in rows:
                    self.session.delete(row)
                self.session.flush()
        return len(rows)


__all__ = ["ConceptJudgementManager"]
