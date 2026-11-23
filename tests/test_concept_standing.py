"""The standing on each concept (`teacher.managers.concept_standing`, computed on read) and the
suggestions built from it and the concept graph (`teacher.suggestions`)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from teacher.db.engine import session_scope
from teacher.db.models import ArtifactBlock, Concept
from teacher.managers import (
    ArtifactManager,
    BlockConceptManager,
    BlockResponseManager,
    ConceptManager,
    ConceptRelationManager,
    ConceptStandingManager,
    StudyHistoryManager,
)
from teacher.managers.concept_standing import (
    STRONG_FROM,
    STRONG_MIN_QUESTIONS,
    WEAK_BELOW,
    status_of,
    thresholds,
)
from teacher.suggestions import REVIEW_AFTER, rules, suggestions

T0 = datetime(2026, 3, 1, 9, 0, 0)


@pytest.mark.parametrize(
    ("answered", "passed", "status"),
    [
        (0, 0, "unstudied"),
        (1, 0, "weak"),
        (1, 1, "developing"),  # one question is not enough for strong
        (2, 1, "developing"),  # 50% is not below WEAK_BELOW
        (2, 2, "strong"),
        (3, 1, "weak"),
        (4, 3, "developing"),  # 75%
        (5, 4, "strong"),  # 80%
    ],
)
def test_status_rule(answered: int, passed: int, status: str) -> None:
    assert status_of(answered, passed) == status


def test_status_rule_constants_and_bad_evidence() -> None:
    assert (WEAK_BELOW, STRONG_FROM, STRONG_MIN_QUESTIONS) == (0.5, 0.8, 2)
    assert thresholds()["good_standing"] == ["developing", "strong"]
    with pytest.raises(ValueError, match="impossible evidence"):
        status_of(1, 2)


def tag(session: Any, lib: Any, key: str, concept: str, role: str) -> None:
    BlockConceptManager(session, lib.settings).add(
        session.get(ArtifactBlock, lib.blocks[key]),
        session.get(Concept, lib.concepts[concept]),
        role,
        "model",
        rationale=f"{key} covers {concept}.",
        model="m",
    )


def answer(session: Any, lib: Any, key: str, at: datetime, *verdicts: bool) -> None:
    responses = BlockResponseManager(session, lib.settings)
    for passed in verdicts:
        row = responses.record(session.get(ArtifactBlock, lib.blocks[key]), response={"choice": 0},
                               passed=passed, grader="test")
        row.created_at = at


@pytest.fixture
def studied(tag_library: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """The tag library tagged (b01 attention; b02 attention + softmax incidental; b03 softmax;
    b04 transformer + attention incidental; b05 softmax), answered (b02 failed then passed; b04
    failed; b05 passed) and read (30 s on b01, 15 s on b03), all from T0 on."""
    lib = tag_library
    clock = {"now": T0}
    monkeypatch.setattr("teacher.managers.study_history.utcnow", lambda: clock["now"])
    with session_scope(lib.settings) as session:
        for key, concept, role in [
            ("b01", "attention", "main"), ("b02", "attention", "main"),
            ("b02", "softmax", "incidental"), ("b03", "softmax", "main"),
            ("b04", "transformer", "main"), ("b04", "attention", "incidental"),
            ("b05", "softmax", "main"),
        ]:
            tag(session, lib, key, concept, role)
        page = ArtifactManager(session, lib.settings).get_by_slug(lib.page)
        history = StudyHistoryManager(session, lib.settings)
        b01 = session.get(ArtifactBlock, lib.blocks["b01"])
        b03 = session.get(ArtifactBlock, lib.blocks["b03"])
        for seconds in (0, 15, 30):
            history.heartbeat("study_page", artifact=page, block=b01, at=T0 + timedelta(seconds=seconds))
        for seconds in (45, 60):  # the time before a heartbeat goes to the block read before it
            history.heartbeat("study_page", artifact=page, block=b03, at=T0 + timedelta(seconds=seconds))
        clock["now"] = T0 + timedelta(minutes=1)
        answer(session, lib, "b02", clock["now"], False, True)
        clock["now"] = T0 + timedelta(minutes=2)
        answer(session, lib, "b04", clock["now"], False)
        clock["now"] = T0 + timedelta(minutes=3)
        answer(session, lib, "b05", clock["now"], True)
    return lib


def standings(lib: Any) -> dict[str, dict[str, Any]]:
    with session_scope(lib.settings) as session:
        return {
            s.concept.slug: s.record()
            for s in ConceptStandingManager(session, lib.settings).standings().values()
        }


def test_standing_from_answers_and_time(studied: Any) -> None:
    got = standings(studied)
    attention = got["attention"]
    assert attention["status"] == "weak"  # its one question failed the first time
    assert {k: attention[k] for k in (
        "questions", "questions_answered", "first_attempt_passed", "first_attempt_pass_rate",
        "attempts", "eventually_passed", "unresolved", "incidental_questions",
        "incidental_questions_answered", "incidental_first_attempt_passed", "blocks",
        "incidental_blocks", "active_seconds", "incidental_seconds", "seen",
    )} == {
        "questions": 1, "questions_answered": 1, "first_attempt_passed": 0,
        "first_attempt_pass_rate": 0.0, "attempts": 2, "eventually_passed": 1, "unresolved": 0,
        "incidental_questions": 1, "incidental_questions_answered": 1,
        "incidental_first_attempt_passed": 0, "blocks": 2, "incidental_blocks": 1,
        "active_seconds": 45.0, "incidental_seconds": 0.0, "seen": True,
    }
    assert attention["first_studied_at"] == "2026-03-01T09:00:00+00:00"
    assert attention["last_studied_at"] == "2026-03-01T09:01:00+00:00"  # the second attempt
    assert attention["pages"] == [{
        "slug": "study-paper", "title": "Study: The Paper", "kind": "study_page", "role": "main",
        "main_blocks": 2, "main_questions": 1, "incidental_blocks": 1,
    }]
    softmax = got["softmax"]  # b05 passed the first time; b02's failure is only incidental
    assert (softmax["status"], softmax["questions_answered"], softmax["first_attempt_passed"],
            softmax["incidental_questions_answered"]) == ("developing", 1, 1, 1)
    assert softmax["active_seconds"] == 15.0  # b03 from 0:45 to 1:00 (before 0:45: b01)
    assert softmax["last_studied_at"] == "2026-03-01T09:03:00+00:00"
    assert got["transformer"]["status"] == "weak" and got["transformer"]["unresolved"] == 1
    dot = got["dot-product"]
    assert (dot["status"], dot["seen"], dot["blocks"], dot["pages"]) == ("unstudied", False, 0, [])


def test_suggestions_fill_the_gap_review_and_ready_next(studied: Any) -> None:
    lib = studied
    with session_scope(lib.settings) as session:
        got = suggestions(session, lib.settings, now=T0 + timedelta(days=1))
    assert [(s["rank"], s["kind"], s["concept"]["slug"]) for s in got] == [
        (1, "fill_gap", "dot-product"),  # needed by attention and (through it) transformer
        (2, "fill_gap", "attention"),  # needed by transformer
        (3, "review", "transformer"),
    ]
    dot, attention, transformer = got
    assert dot["reason"] == (
        "Dot product is a prerequisite of attention and transformer, which you struggled with, "
        "and you have not answered a question on it yet. Filling this gap first should make the "
        "rest easier."
    )
    assert [(w["slug"], w["direct"], w["first_attempt_pass_rate"]) for w in dot["evidence"]["supports"]] == [
        ("attention", True, 0.0), ("transformer", False, 0.0),
    ]
    assert [a["kind"] for a in dot["also"]] == ["ready_next"]  # it builds on nothing
    assert dot["action"]["kind"] == "source"  # linked to the paper, whose page is not tagged
    assert dot["action"]["sources"][0]["state"] == "page_not_tagged"
    assert attention["evidence"]["attempts"] == 2
    assert [a["kind"] for a in attention["also"]] == ["review"]
    assert attention["action"]["kind"] == "study_page"
    assert attention["action"]["pages"][0]["slug"] == "study-paper"
    assert transformer["reason"] == (
        "You struggled with transformer: you passed 0 of 1 question on it at the first attempt "
        "(0%). You never passed it."
    )
    assert rules() == {"kinds": ["fill_gap", "review", "ready_next"],
                       "review_after_days": 14.0, "good_standing": ["developing", "strong"]}


def test_a_good_concept_not_studied_for_a_while_comes_back_for_review(studied: Any) -> None:
    lib = studied
    later = T0 + timedelta(minutes=3) + REVIEW_AFTER
    with session_scope(lib.settings) as session:
        got = suggestions(session, lib.settings, now=later)
    review = [s for s in got if s["kind"] == "review"]
    assert [s["concept"]["slug"] for s in review] == ["transformer", "softmax"]  # weak first
    assert review[1]["evidence"]["days_since"] == 14
    assert "you last studied it 14 days ago" in review[1]["reason"]
    with session_scope(lib.settings) as session:
        sooner = suggestions(session, lib.settings, now=later - timedelta(seconds=1))
    assert "softmax" not in [s["concept"]["slug"] for s in sooner]


def test_ready_next_and_what_the_library_offers(studied: Any) -> None:
    lib = studied
    with session_scope(lib.settings) as session:
        concepts = ConceptManager(session, lib.settings)
        decoder = concepts.create("decoder", "Builds outputs from weights.", "user")
        concepts.create("tax law", "Rules for taxes.", "user")
        ConceptRelationManager(session, lib.settings).add(
            "builds_on", decoder, concepts.get(lib.concepts["softmax"]),
            "The decoder uses the softmax.", "user",
        )
        page = ArtifactManager(session, lib.settings).get_by_slug(lib.page)
        page.block_concepts_tagged_at = T0  # now its tags are complete: it does not cover dot product
    with session_scope(lib.settings) as session:
        got = suggestions(session, lib.settings, now=T0 + timedelta(days=1))
    ready = [s for s in got if s["kind"] == "ready_next"]
    assert [s["concept"]["slug"] for s in ready] == ["decoder", "tax-law"]  # prerequisites met first
    assert ready[0]["evidence"]["prerequisites"][0]["slug"] == "softmax"
    assert "what it builds on is in good standing: softmax (developing)" in ready[0]["reason"]
    assert ready[1]["action"] == {
        "kind": "none", "text": "Nothing in the library covers it yet: add a source about it.",
        "pages": [], "sources": [],
    }
    (dot,) = [s for s in got if s["concept"]["slug"] == "dot-product"]
    assert dot["action"]["sources"][0]["state"] == "page_does_not_cover"


def test_nothing_tagged_means_nothing_studied(tag_library: Any) -> None:
    got = standings(tag_library)
    assert {s["status"] for s in got.values()} == {"unstudied"}
    with session_scope(tag_library.settings) as session:
        ranked = suggestions(session, tag_library.settings)
    # every concept is unstudied: those whose prerequisites are all good (none) are the roots
    assert [(s["kind"], s["concept"]["slug"]) for s in ranked] == [
        ("ready_next", "dot-product"), ("ready_next", "softmax"),
    ]


# -- predictions, partly right first tries and reviews ------------------------------------------


def test_a_prediction_is_not_a_question_even_when_tagged(tag_library: Any) -> None:
    from teacher.managers import ArtifactBlockManager

    lib = tag_library
    with session_scope(lib.settings) as session:
        page = ArtifactManager(session, lib.settings).get_by_slug(lib.page)
        guess = ArtifactBlockManager(session, lib.settings).add(page, type="prediction", content={
            "prompt": "Guess.", "options": ["a", "b"],
            "private": {"correct_index": 0, "resolution": "It is a."},
        }, is_gate=True)
        lib.blocks[guess.key] = guess.id
        tag(session, lib, guess.key, "attention", "main")
        answer(session, lib, guess.key, T0, True)
        concept = session.get(Concept, lib.concepts["attention"])
        assert ConceptStandingManager(session, lib.settings).tagged_questions(concept) == []
    got = standings(lib)["attention"]
    assert (got["status"], got["questions"], got["questions_answered"], got["blocks"]) == (
        "unstudied", 0, 0, 1,
    )


def test_a_partly_right_first_attempt_counts_half(tag_library: Any) -> None:
    lib = tag_library
    with session_scope(lib.settings) as session:
        tag(session, lib, "b04", "transformer", "main")
        tag(session, lib, "b02", "transformer", "main")
        row = BlockResponseManager(session, lib.settings).record(
            session.get(ArtifactBlock, lib.blocks["b04"]), response={"text": "x"}, passed=False,
            grader="test", verdict="partial", missing=("Names attention",),
        )
        row.created_at = T0
        answer(session, lib, "b02", T0, False)
    got = standings(lib)["transformer"]
    assert (got["first_attempt_passed"], got["first_attempt_partial"], got["questions_answered"]) == (0, 1, 2)
    assert got["first_attempt_pass_rate"] == 0.25 and got["status"] == "weak"
    with session_scope(lib.settings) as session:
        row = session.get(ArtifactBlock, lib.blocks["b02"])
        BlockResponseManager(session, lib.settings).record(row, response={"choice": 0}, passed=True, grader="t")
    # a later attempt never changes the first-attempt evidence
    assert standings(lib)["transformer"]["first_attempt_pass_rate"] == 0.25


def test_reviews_are_first_tries_that_move_the_standing(studied: Any) -> None:
    from teacher.db.models import ReviewAttempt, ReviewItem

    lib = studied
    assert standings(lib)["softmax"]["status"] == "developing"  # b05 passed: one question
    reviewed_at = T0 + timedelta(days=4)

    def review(session: Any, item: ReviewItem, verdict: str, at: datetime) -> None:
        session.add(ReviewAttempt(
            item_id=item.id, form="original", asked={}, response={"choice": 0}, verdict=verdict,
            missing=["x"] if verdict == "partial" else [], confidence="medium", grader="t",
            interval_before=3.0, interval_after=1.0, due_before=at, due_after=at, created_at=at,
        ))

    with session_scope(lib.settings) as session:
        item = ReviewItem(block_id=lib.blocks["b05"], cleared_at=T0, first_clean=True, interval_days=3.0,
                          due_at=T0, last_verdict="passed", last_answered_at=T0)
        session.add(item)
        session.flush()
        review(session, item, "failed", reviewed_at)
        review(session, item, "failed", reviewed_at + timedelta(days=1))
    got = standings(lib)["softmax"]
    assert (got["reviews"], got["reviews_passed"], got["first_tries"], got["questions_answered"]) == (2, 0, 3, 1)
    assert got["first_attempt_pass_rate"] == pytest.approx(1 / 3) and got["status"] == "weak"
    assert got["last_answered_at"] == "2026-03-06T09:00:00+00:00"
    assert got["last_studied_at"] == "2026-03-06T09:00:00+00:00"
    with session_scope(lib.settings) as session:
        item = session.scalars(select(ReviewItem)).one()
        review(session, item, "passed", reviewed_at + timedelta(days=2))
        review(session, item, "partial", reviewed_at + timedelta(days=3))
    got = standings(lib)["softmax"]
    assert (got["reviews"], got["reviews_passed"], got["reviews_partial"]) == (4, 1, 1)
    assert got["first_attempt_pass_rate"] == pytest.approx(2.5 / 5) and got["status"] == "developing"
    with session_scope(lib.settings) as session:  # the reason says so
        (softmax,) = [s for s in suggestions(session, lib.settings, now=T0 + timedelta(days=40))
                      if s["concept"]["slug"] == "softmax"]
    assert "and 1 of 4 reviews (1 partly) (50%)" in softmax["reason"]


def test_status_rule_with_reviews_and_half_passes() -> None:
    assert status_of(1, 1, 1) == "developing"
    assert status_of(1, 3, 3) == "developing"  # one question, however often reviewed
    assert status_of(2, 4.5, 5) == "strong"  # 90%
    assert status_of(2, 1.5, 4) == "weak"  # 37.5%
    with pytest.raises(ValueError, match="impossible evidence"):
        status_of(2, 1, 1)  # fewer first tries than questions
