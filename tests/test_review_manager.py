"""The review queue's manager (`teacher.managers.review`): items derived from page answers, the
queue's scope, order and cap, presentations, answers and rescheduling."""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from teacher.config import Settings
from teacher.db.engine import session_scope
from teacher.db.models import ArtifactBlock, ReviewAttempt, ReviewItem, ReviewVariant
from teacher.grading import offline_graders
from teacher.managers import (
    ArtifactBlockManager,
    ArtifactManager,
    BlockConceptManager,
    BlockResponseManager,
    ConceptManager,
)
from teacher.managers.errors import NotFoundError
from teacher.managers.review import ReviewConflict, ReviewManager

T0 = datetime(2026, 3, 1, 9, 0, 0)
DAY = timedelta(days=1)


def mc(question: str, correct: int = 0) -> dict[str, Any]:
    options = ["red option", "blue option", "green option"]
    return {
        "question": question,
        "options": options,
        "private": {
            "correct_index": correct,
            "explanation": f"It is the {options[correct]}.",
            "option_explanations": [f"About the {o}." for o in options],
        },
    }


BLURB = {
    "prompt": "Explain the idea.",
    "min_words": 3,
    "private": {
        "model_answer": "The idea is this.",
        "rubric": [{"criterion": "Names the idea", "required": True}],
    },
}
PREDICTION = {
    "prompt": "What happens next?",
    "options": ["this", "that"],
    "private": {"correct_index": 0, "resolution": "This happens."},
}


@dataclass
class Library:
    settings: Settings
    blocks: dict[str, int]  # "<page>/<key>" -> block id


def make_page(session: Any, settings: Settings, slug: str, blocks: list[tuple[str, dict]]) -> dict[str, int]:
    page = ArtifactManager(session, settings).create(
        slug=slug, kind="study_page", title=f"Page {slug}", generator="test"
    )
    manager = ArtifactBlockManager(session, settings)
    rows = [manager.add(page, type=t, content=c, is_gate=t != "prose") for t, c in blocks]
    ArtifactManager(session, settings).mark_ready(page)
    return {f"{slug}/{b.key}": b.id for b in rows}


@pytest.fixture
def lib(settings: Settings) -> Library:
    """Page `one`: b01 prose, b02 mc, b03 mc, b04 blurb, b05 prediction; page `two`: b01 mc,
    b02 mc. Nothing answered."""
    with session_scope(settings) as session:
        blocks = make_page(session, settings, "one", [
            ("prose", {"markdown": "# One"}), ("multiple_choice", mc("Q one-2")),
            ("multiple_choice", mc("Q one-3", 1)), ("blurb", BLURB), ("prediction", PREDICTION),
        ])
        blocks |= make_page(session, settings, "two", [
            ("multiple_choice", mc("Q two-1", 2)), ("multiple_choice", mc("Q two-2")),
        ])
    return Library(settings, blocks)


def answer(
    session: Any, lib: Library, key: str, at: datetime, *verdicts: str, confidence: str | None = None
) -> None:
    block = session.get(ArtifactBlock, lib.blocks[key])
    for verdict in verdicts:
        row = BlockResponseManager(session, lib.settings).record(
            block,
            response={"text": "some words here"} if block.type == "blurb" else {"choice": 0},
            passed=verdict == "passed",
            grader="test",
            verdict=verdict,
            missing=("Names the idea",) if verdict == "partial" else (),
            confidence=confidence if block.type != "prediction" else None,
        )
        row.created_at = at


def items_by_key(session: Any, lib: Library) -> dict[str, ReviewItem]:
    ids = {v: k for k, v in lib.blocks.items()}
    return {ids[i.block_id]: i for i in session.scalars(select(ReviewItem))}


def queue(session: Any, lib: Library, now: datetime, **kwargs: Any) -> Any:
    manager = ReviewManager(session, lib.settings)
    manager.sync(max_attempts=2)
    return manager.queue(now=now, day_start=now - timedelta(hours=1), **kwargs)


def test_items_are_derived_from_the_page_answers(lib: Library) -> None:
    with session_scope(lib.settings) as session:
        answer(session, lib, "one/b02", T0, "passed")  # a clean first pass
        answer(session, lib, "one/b03", T0, "failed", "passed", confidence="high")
        answer(session, lib, "one/b04", T0 + timedelta(hours=1), "partial")
        answer(session, lib, "one/b04", T0 + timedelta(hours=2), "failed")  # revealed (2 of 2)
        answer(session, lib, "one/b05", T0, "passed")  # a prediction: never an item
        answer(session, lib, "two/b01", T0, "failed")  # still open: not cleared
        assert ReviewManager(session, lib.settings).sync(max_attempts=2) == 3
        assert ReviewManager(session, lib.settings).sync(max_attempts=2) == 0  # idempotent
        got = items_by_key(session, lib)
        assert set(got) == {"one/b02", "one/b03", "one/b04"}
        b02, b03, b04 = got["one/b02"], got["one/b03"], got["one/b04"]
        assert (b02.first_clean, b02.interval_days, b02.due_at) == (True, 3.0, T0 + 3 * DAY)
        assert (b03.first_clean, b03.interval_days, b03.due_at) == (False, 1.0, T0 + DAY)
        assert (b03.last_verdict, b03.last_confidence) == ("passed", "high")
        assert b04.cleared_at == T0 + timedelta(hours=2)  # the attempt that revealed the answer
        assert (b04.due_at, b04.last_verdict) == (T0 + timedelta(hours=2) + DAY, "failed")
        # with a higher attempt limit the blurb would not have been revealed yet
        session.delete(b04)
        session.flush()
        assert ReviewManager(session, lib.settings).sync(max_attempts=3) == 0


def test_a_superseded_or_deleted_page_leaves_the_queue(lib: Library) -> None:
    with session_scope(lib.settings) as session:
        answer(session, lib, "one/b02", T0, "passed")
        answer(session, lib, "two/b01", T0, "passed")
        assert queue(session, lib, T0 + 4 * DAY).items_total == 2
        one = ArtifactManager(session, lib.settings).get_by_slug("one")
        newer = ArtifactManager(session, lib.settings).create(
            slug="one-2", kind="study_page", title="Page one", generator="test", supersedes=one
        )
        assert queue(session, lib, T0 + 4 * DAY).items_total == 2  # still generating: kept
        ArtifactManager(session, lib.settings).mark_failed(newer)
        assert queue(session, lib, T0 + 4 * DAY).items_total == 2  # failed: kept
        ArtifactManager(session, lib.settings).mark_ready(newer)
        q = queue(session, lib, T0 + 4 * DAY)
        assert [v.artifact.slug for v in q.ordered] == ["two"]
        assert len(items_by_key(session, lib)) == 2  # the superseded page's item is history
        ArtifactManager(session, lib.settings).delete(ArtifactManager(session, lib.settings).get_by_slug("two"))
        assert list(items_by_key(session, lib)) == ["one/b02"]  # gone with its page


def test_queue_order_confident_errors_then_overdue_interleaved_and_capped(lib: Library) -> None:
    with session_scope(lib.settings) as session:
        answer(session, lib, "one/b02", T0, "failed", "failed", confidence="medium")  # due T0+1d
        answer(session, lib, "one/b03", T0, "passed", confidence="high")  # due T0+3d
        answer(session, lib, "two/b01", T0 + timedelta(hours=1), "failed", "failed", confidence="high")
        answer(session, lib, "two/b02", T0 - DAY, "failed", "passed")  # due T0
        q = queue(session, lib, T0 + 2 * DAY)
        # two/b01 is a confident error; then most overdue: two/b02 (T0), one/b02 (T0+1d), but
        # two/b02 would follow two/b01 from the same page, so one/b02 comes in between
        names = [f"{v.artifact.slug}/{v.block.key}" for v in q.ordered]
        assert names == ["two/b01", "one/b02", "two/b02"]
        assert (q.due, q.items_total, q.next_due_at) == (3, 4, T0 + 3 * DAY)
        assert [v.confident_error for v in q.ordered] == [True, False, False]
        assert len(q.served(10)) == 3
        assert len(queue(session, lib, T0 + 2 * DAY, cap=2).served(10)) == 2


def test_present_and_answer_a_variant_with_shuffled_options(lib: Library) -> None:
    with session_scope(lib.settings) as session:
        answer(session, lib, "one/b03", T0, "passed")  # correct is option 1: "blue option"
        block_id = lib.blocks["one/b03"]
        variant = mc("A fresh form of one-3", 2)  # correct is "green option"
        session.add(ReviewVariant(block_id=block_id, position=1, type="multiple_choice",
                                  content=variant, model="m"))
        session.flush()
        manager = ReviewManager(session, lib.settings)
        q = queue(session, lib, T0 + 4 * DAY)
        (view,) = q.ordered
        shown = manager.present(view, random.Random(3), now=T0 + 4 * DAY)
        assert (shown.form, shown.variant_position) == ("variant", 1)
        assert shown.shown["question"] == "A fresh form of one-3"
        assert sorted(shown.order or []) == [0, 1, 2]
        assert shown.shown["options"] == [variant["options"][i] for i in shown.order or []]
        assert "private" not in shown.shown
        again = manager.present(view, random.Random(99), now=T0 + 4 * DAY)  # a reload
        assert (again.token, again.order) == (shown.token, shown.order)

        wrong_position = shown.shown["options"].index("red option")
        with pytest.raises(ReviewConflict, match="not the one shown"):
            manager.answer(view.item.id, presentation="stale", response={"choice": 0},
                           confidence="high", graders=offline_graders(), now=T0 + 4 * DAY)
        with pytest.raises(ValueError, match="choice must be"):
            manager.answer(view.item.id, presentation=shown.token, response={"choice": 7},
                           confidence="high", graders=offline_graders(), now=T0 + 4 * DAY)
        with pytest.raises(ValueError, match="confidence"):
            manager.answer(view.item.id, presentation=shown.token, response={"choice": 0},
                           confidence="sure", graders=offline_graders(), now=T0 + 4 * DAY)
        outcome = manager.answer(view.item.id, presentation=shown.token,
                                 response={"choice": wrong_position}, confidence="high",
                                 graders=offline_graders(), now=T0 + 4 * DAY)
        assert outcome.result.verdict == "failed"
        assert outcome.result.feedback == "Not quite. About the red option."
        # the revealed answer is in the order shown
        assert shown.shown["options"][outcome.revealed["correct_index"]] == "green option"
        attempt = outcome.attempt
        assert (attempt.form, attempt.variant_position, attempt.option_order) == ("variant", 1, shown.order)
        assert attempt.response == {"choice": 0}  # the variant's own index of "red option"
        assert attempt.asked["options"] == shown.shown["options"]
        assert (attempt.interval_before, attempt.interval_after) == (3.0, 1.0)
        item = session.get(ReviewItem, view.item.id)
        assert (item.reviews, item.interval_days, item.due_at) == (1, 1.0, T0 + 5 * DAY)
        assert (item.last_verdict, item.last_confidence, item.presentation) == ("failed", "high", None)
        with pytest.raises(ReviewConflict):  # answered: the token is spent
            manager.answer(view.item.id, presentation=shown.token, response={"choice": 0},
                           confidence="high", graders=offline_graders(), now=T0 + 4 * DAY)

        # review 1 asks the original, in a fresh order; passing grows the interval
        q = queue(session, lib, T0 + 5 * DAY)
        (view,) = q.ordered
        shown2 = manager.present(view, random.Random(5), now=T0 + 5 * DAY)
        assert (shown2.form, shown2.shown["question"]) == ("original", "Q one-3")
        right = shown2.shown["options"].index("blue option")
        outcome = manager.answer(view.item.id, presentation=shown2.token, response={"choice": right},
                                 confidence="medium", graders=offline_graders(), now=T0 + 5 * DAY)
        assert outcome.result.verdict == "passed"
        item = session.get(ReviewItem, view.item.id)
        assert item.interval_days == pytest.approx(2.2)
        assert ReviewManager(session, lib.settings).done_since(T0 + 4 * DAY) == 2


def test_a_rebuilt_variant_makes_the_shown_question_stale(lib: Library) -> None:
    with session_scope(lib.settings) as session:
        answer(session, lib, "one/b02", T0, "passed")
        session.add(ReviewVariant(block_id=lib.blocks["one/b02"], position=1,
                                  type="multiple_choice", content=mc("Another form"), model="m"))
        session.flush()
        manager = ReviewManager(session, lib.settings)
        (view,) = queue(session, lib, T0 + 4 * DAY).ordered
        shown = manager.present(view, random.Random(1), now=T0 + 4 * DAY)
        for v in session.scalars(select(ReviewVariant)):
            session.delete(v)
        session.flush()
        session.refresh(view.item)
        with pytest.raises(ReviewConflict, match="rebuilt"):
            manager.answer(view.item.id, presentation=shown.token, response={"choice": 0},
                           confidence="low", graders=offline_graders(), now=T0 + 4 * DAY)
        again = manager.present(view, random.Random(1), now=T0 + 4 * DAY)  # chosen anew
        assert again.form == "original" and again.token != shown.token


def test_blurb_reviews_keep_the_interval_when_partly_right(lib: Library) -> None:
    class Partial:
        name = "partial-grader"

        def grade(self, content: dict, response: dict) -> Any:
            from teacher.grading import GradeResult

            return GradeResult("partial", feedback="Half.", grader=self.name, missing=("Names the idea",))

    with session_scope(lib.settings) as session:
        answer(session, lib, "one/b04", T0, "passed")
        manager = ReviewManager(session, lib.settings)
        (view,) = queue(session, lib, T0 + 3 * DAY).ordered
        shown = manager.present(view, random.Random(1), now=T0 + 3 * DAY)
        assert shown.order is None and shown.shown["prompt"] == "Explain the idea."
        outcome = manager.answer(view.item.id, presentation=shown.token,
                                 response={"text": "an answer of words"}, confidence="medium",
                                 graders={"blurb": Partial()}, now=T0 + 3 * DAY)
        assert (outcome.result.verdict, outcome.attempt.missing) == ("partial", ["Names the idea"])
        assert outcome.revealed["model_answer"] == "The idea is this."
        assert session.get(ReviewItem, view.item.id).interval_days == 3.0


def test_concept_scope_and_counts(lib: Library) -> None:
    with session_scope(lib.settings) as session:
        concepts = ConceptManager(session, lib.settings)
        softmax = concepts.create("softmax", "Weights.", "user")
        other = concepts.create("other", "Else.", "user")
        tags = BlockConceptManager(session, lib.settings)
        tags.add(session.get(ArtifactBlock, lib.blocks["one/b02"]), softmax, "main", "user")
        tags.add(session.get(ArtifactBlock, lib.blocks["two/b01"]), softmax, "incidental", "user")
        tags.add(session.get(ArtifactBlock, lib.blocks["two/b01"]), other, "main", "user")
        answer(session, lib, "one/b02", T0, "passed")
        answer(session, lib, "two/b01", T0, "failed", "failed")
        q = queue(session, lib, T0 + 2 * DAY, concept=softmax)
        assert (q.items_total, q.due, q.next_due_at) == (1, 0, T0 + 3 * DAY)
        counts = ReviewManager(session, lib.settings).concept_counts(now=T0 + 2 * DAY)
        assert counts == {softmax.id: (1, 0, T0 + 3 * DAY), other.id: (1, 1, None)}


def test_an_unknown_item_is_not_found(lib: Library) -> None:
    with session_scope(lib.settings) as session, pytest.raises(NotFoundError):
        ReviewManager(session, lib.settings).get(999)
    with session_scope(lib.settings) as session:
        assert session.scalars(select(ReviewAttempt)).all() == []
