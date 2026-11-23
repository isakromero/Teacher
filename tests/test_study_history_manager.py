"""StudyHistoryManager: session assignment, the event log and its rules, heartbeats and attention
spans, the session ids of new activity rows, and the read model (sessions, timeline, totals,
export and its Markdown)."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from teacher.config import Settings
from teacher.db.models import (
    STUDY_EVENT_KINDS,
    Artifact,
    ArtifactBlock,
    AttentionSpan,
    KnowledgeSource,
    StudyEvent,
    StudySession,
)
from teacher.glossary import GlossaryEntry
from teacher.history_export import describe_event, duration, history_markdown
from teacher.lab_generator import stub_lab
from teacher.labs import LabFile, write_lab
from teacher.managers import (
    ArtifactBlockManager,
    ArtifactManager,
    BlockResponseManager,
    ChatManager,
    ConceptManager,
    GlossaryManager,
    KnowledgeSourceManager,
    LabManager,
    NotFoundError,
    StudyHistoryManager,
)
from teacher.managers.chat import ChatBlockDraft
from teacher.managers.study_history import (
    CLIENT_EVENT_KINDS,
    EVENT_SPECS,
    SESSION_IDLE_GAP,
    SPAN_CONTINUE_GAP,
    reader_answer,
)

T0 = datetime(2026, 3, 1, 9, 0, 0)
MC = {"question": "Which?", "options": ["a", "b"], "private": {"correct_index": 1, "explanation": "b."}}


def at(minutes: float = 0, seconds: float = 0) -> datetime:
    return T0 + timedelta(minutes=minutes, seconds=seconds)


@pytest.fixture
def history(session: Session, settings: Settings) -> StudyHistoryManager:
    return StudyHistoryManager(session, settings)


class Clock:
    """The time the history manager sees as now (`teacher.managers.study_history.utcnow`)."""

    def __init__(self) -> None:
        self.now = T0

    def set(self, when: datetime) -> None:
        self.now = when

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    clock = Clock()
    monkeypatch.setattr("teacher.managers.study_history.utcnow", clock)
    return clock


@pytest.fixture
def source(session: Session, settings: Settings, tmp_path: Path) -> KnowledgeSource:
    path = tmp_path / "paper.txt"
    path.write_text("A paper about attention.\n", encoding="utf-8")
    return KnowledgeSourceManager(session, settings).add(
        path, slug="paper", kind="paper", title="The Paper"
    )


@pytest.fixture
def page(session: Session, settings: Settings, source: KnowledgeSource) -> Artifact:
    artifacts = ArtifactManager(session, settings)
    artifact = artifacts.create(
        slug="study-paper", kind="study_page", title="Study: The Paper", generator="test",
        sources=[source],
    )
    blocks = ArtifactBlockManager(session, settings)
    blocks.add(artifact, type="prose", content={"markdown": "Attention."})
    blocks.add(artifact, type="multiple_choice", content=MC, is_gate=True)
    artifacts.mark_ready(artifact)
    return artifact


@pytest.fixture
def lab(session: Session, settings: Settings, page: Artifact, source: KnowledgeSource) -> Artifact:
    artifacts = ArtifactManager(session, settings)
    artifact = artifacts.create(
        slug="lab-paper", kind="lab", title="Lab: The Paper", generator="stub-lab-v1",
        params={"study_page": page.slug}, sources=[source],
    )
    write_lab(artifacts.dir(artifact), stub_lab(page.slug))
    artifacts.mark_ready(artifact)
    return artifact


def block(page: Artifact, key: str) -> ArtifactBlock:
    return next(b for b in page.blocks if b.key == key)


# -- sessions ---------------------------------------------------------------------------------


def test_activity_joins_the_open_session_or_starts_one(history: StudyHistoryManager) -> None:
    first = history.activity_session(at(0))
    assert first.backfilled is False and first.started_at == first.last_activity_at == at(0)
    assert history.activity_session(at(10)) is first
    assert history.activity_session(at(40)) is first  # 30 minutes after the last activity
    assert first.last_activity_at == at(40)
    assert history.activity_session(at(40) + SESSION_IDLE_GAP) is first  # exactly the gap
    later = history.activity_session(at(100, seconds=1))  # more than the gap after 09:70
    assert later is not first and later.started_at == at(100, seconds=1)
    assert SESSION_IDLE_GAP == timedelta(minutes=30)


def test_live_activity_never_joins_a_backfilled_session(
    history: StudyHistoryManager, session: Session
) -> None:
    old = StudySession(started_at=at(0), last_activity_at=at(5), backfilled=True)
    session.add(old)
    session.flush()
    live = history.activity_session(at(6))
    assert live is not old and live.backfilled is False
    assert old.last_activity_at == at(5)


def test_get_session(history: StudyHistoryManager) -> None:
    created = history.activity_session(at(0))
    assert history.get_session(created.id) is created
    with pytest.raises(NotFoundError, match="no study session with id 999"):
        history.get_session(999)


# -- events -----------------------------------------------------------------------------------


def test_every_kind_has_a_spec_and_page_kinds_are_known() -> None:
    assert set(EVENT_SPECS) == set(STUDY_EVENT_KINDS)
    assert set(CLIENT_EVENT_KINDS) < set(STUDY_EVENT_KINDS)


def test_log_stores_the_event_in_the_open_session(
    history: StudyHistoryManager, page: Artifact
) -> None:
    event = history.log("study_page_opened", artifact=page, at=at(1))
    assert event.kind == "study_page_opened" and event.occurred_at == at(1)
    assert event.artifact_id == page.id and event.payload == {}
    assert event.lab_id is None and event.block_id is None
    again = history.log(
        "gate_cleared", artifact=page, block=block(page, "b02"), at=at(2),
        payload={"state": "passed", "attempt": 1, "unlocked": []},
    )
    assert again.session_id == event.session_id and again.block_id == block(page, "b02").id


def test_a_lab_reference_also_names_its_study_page(
    history: StudyHistoryManager, page: Artifact, lab: Artifact, session: Session, settings: Settings
) -> None:
    event = history.log("lab_opened", lab=lab)
    assert event.lab_id == lab.id and event.artifact_id == page.id
    with pytest.raises(ValueError, match="needs exactly one of artifact, lab"):
        history.log("chat_thread_started", artifact=page, lab=lab, payload={"thread_id": 1})
    with pytest.raises(ValueError, match="is a lab; pass it as the lab"):
        history.log("study_page_opened", artifact=lab)
    with pytest.raises(ValueError, match="is a study_page, not a lab"):
        history.log("lab_opened", lab=page)
    ArtifactManager(session, settings).delete(page)
    session.flush()
    orphan = history.log("lab_exported", lab=lab)
    assert orphan.artifact_id is None and orphan.lab_id == lab.id


@pytest.mark.parametrize(
    ("kind", "refs", "payload", "message"),
    [
        ("no_such_kind", {}, {}, "unknown study event kind 'no_such_kind'"),
        ("study_page_opened", {}, {}, "study_page_opened: needs artifact"),
        ("library_opened", {"page": True}, {}, "library_opened: takes no artifact"),
        ("chat_thread_started", {}, {"thread_id": 1}, "needs exactly one of artifact, lab"),
        ("gate_cleared", {"page": True, "block": True}, {"state": "passed"},
         r"invalid payload \(missing attempt, unlocked\)"),
        ("study_page_opened", {"page": True}, {"extra": 1}, r"invalid payload \(unexpected extra\)"),
        ("glossary_term_viewed", {"page": True}, {"term": float("nan")}, "not JSON-serialisable"),
    ],
)
def test_log_refuses_what_a_kind_does_not_take(
    history: StudyHistoryManager, page: Artifact, kind: str, refs: dict, payload: dict, message: str,
    session: Session,
) -> None:
    kwargs = {}
    if refs.get("page"):
        kwargs["artifact"] = page
    if refs.get("block"):
        kwargs["block"] = block(page, "b02")
    with pytest.raises(ValueError, match=message):
        history.log(kind, payload=payload, **kwargs)
    assert session.scalar(select(StudyEvent.id)) is None


def test_a_block_must_belong_to_the_artifact(
    history: StudyHistoryManager, page: Artifact, session: Session, settings: Settings
) -> None:
    other = ArtifactManager(session, settings).create(
        slug="other", kind="study_page", title="Other", generator="test"
    )
    with pytest.raises(ValueError, match="is not a block of the artifact given"):
        history.log(
            "gate_cleared", artifact=other, block=block(page, "b02"),
            payload={"state": "passed", "attempt": 1, "unlocked": []},
        )


def test_log_client_takes_only_page_kinds_and_checks_the_data(
    history: StudyHistoryManager, page: Artifact, lab: Artifact, session: Session,
    settings: Settings,
) -> None:
    with pytest.raises(ValueError, match="gate_cleared events are logged by the server"):
        history.log_client("gate_cleared", artifact=page)
    with pytest.raises(ValueError, match="unknown study event kind 'nope': a page may post"):
        history.log_client("nope")

    GlossaryManager(session, settings).replace(
        page, [GlossaryEntry(term="Attention", kind="term", definition="Weighting.")]
    )
    event = history.log_client("glossary_term_viewed", artifact=page, payload={"term": "Attention"})
    assert event.payload == {"term": "Attention"}
    with pytest.raises(NotFoundError, match="has no glossary term 'Softmax'"):
        history.log_client("glossary_term_viewed", artifact=page, payload={"term": "Softmax"})
    with pytest.raises(ValueError, match="payload.term must be a non-empty string"):
        history.log_client("glossary_term_viewed", artifact=page, payload={"term": 3})

    assert history.log_client("lab_hint_viewed", lab=lab, payload={"step": 1}).payload == {"step": 1}
    with pytest.raises(ValueError, match="step 0 of lab 'lab-paper' has no hint"):
        history.log_client("lab_hint_viewed", lab=lab, payload={"step": 0})
    with pytest.raises(NotFoundError, match="has no step 9"):
        history.log_client("lab_hint_viewed", lab=lab, payload={"step": 9})
    with pytest.raises(ValueError, match="must be a step index"):
        history.log_client("lab_hint_viewed", lab=lab, payload={"step": True})
    with pytest.raises(ValueError, match="is a study_page, not a lab"):  # before reading lab.json
        history.log_client("lab_hint_viewed", lab=page, payload={"step": 1})
    with pytest.raises(ValueError, match=r"invalid payload \(missing term\)"):
        history.log_client("glossary_term_viewed", artifact=page)

    concept = ConceptManager(session, settings).create("Attention", "Weighting.", "paper")
    assert history.log_client("concept_inspected", concept=concept).concept_id == concept.id
    with pytest.raises(ValueError, match="concept_inspected: needs concept"):
        history.log_client("concept_inspected")


def test_events_outlive_what_they_were_about(
    history: StudyHistoryManager, page: Artifact, session: Session, settings: Settings
) -> None:
    event = history.log("glossary_opened", artifact=page)
    ArtifactManager(session, settings).delete(page)
    session.flush()
    session.expire_all()
    assert session.get(StudyEvent, event.id).artifact_id is None


# -- heartbeats -------------------------------------------------------------------------------


def test_heartbeats_build_spans(history: StudyHistoryManager, page: Artifact) -> None:
    b1, b2 = block(page, "b01"), block(page, "b02")
    first = history.heartbeat("study_page", artifact=page, block=b1, at=at(0))
    assert (first.active_seconds, first.heartbeats) == (0.0, 1)
    assert first.started_at == first.last_seen_at == at(0)

    same = history.heartbeat("study_page", artifact=page, block=b1, at=at(0, 15))
    assert same is first and first.active_seconds == 15 and first.heartbeats == 2

    # the block in view changed: the time until now was on the block in view before (b01); a
    # new span for b02 starts now, with nothing credited yet
    moved = history.heartbeat("study_page", artifact=page, block=b2, at=at(0, 30))
    assert moved is not first and moved.block_id == b2.id
    assert (first.active_seconds, first.last_seen_at, first.heartbeats) == (30, at(0, 30), 2)
    assert (moved.started_at, moved.active_seconds, moved.heartbeats) == (at(0, 30), 0, 1)
    assert history.heartbeat("study_page", artifact=page, block=b2, at=at(0, 45)) is moved
    assert moved.active_seconds == 15

    # away for longer than SPAN_CONTINUE_GAP: a new span, nothing credited for the gap
    back = history.heartbeat(
        "study_page", artifact=page, block=b2, at=at(0, 45) + SPAN_CONTINUE_GAP + timedelta(seconds=1)
    )
    assert back is not moved and back.active_seconds == 0
    assert moved.last_seen_at == at(0, 45)
    assert first.session_id == back.session_id


def test_a_change_of_block_splits_the_interval_where_it_happened(
    history: StudyHistoryManager, page: Artifact
) -> None:
    """The page sends a heartbeat once the block in view has changed (attention.js), between
    its regular ones: the time before it goes to the block read before, the rest to the new."""
    b1, b2 = block(page, "b01"), block(page, "b02")
    history.heartbeat("study_page", artifact=page, block=b1, at=at(0))
    history.heartbeat("study_page", artifact=page, block=b2, at=at(0, 6))  # the change, settled
    history.heartbeat("study_page", artifact=page, block=b2, at=at(0, 15))  # the regular tick
    history.heartbeat("study_page", artifact=page, block=b2, at=at(0, 17))  # the tab hidden
    seconds = {
        span.block_id: span.active_seconds
        for span in history.session.scalars(select(AttentionSpan))
    }
    assert seconds == {b1.id: 6, b2.id: 11}


def test_a_resumed_heartbeat_credits_nothing_for_the_pause(
    history: StudyHistoryManager, page: Artifact
) -> None:
    """The page says the reader is back (the tab was hidden, or no input for a while): the
    time since the previous heartbeat is not credited, however short."""
    b1 = block(page, "b01")
    first = history.heartbeat("study_page", artifact=page, block=b1, at=at(0))
    history.heartbeat("study_page", artifact=page, block=b1, at=at(0, 10))  # the tab hidden
    back = history.heartbeat("study_page", artifact=page, block=b1, resumed=True, at=at(0, 40))
    assert back is not first and (back.started_at, back.active_seconds) == (at(0, 40), 0)
    assert (first.active_seconds, first.last_seen_at) == (10, at(0, 10))
    assert history.heartbeat("study_page", artifact=page, block=b1, at=at(0, 55)) is back
    assert back.active_seconds == 15
    with pytest.raises(ValueError, match="resumed must be a bool"):
        history.heartbeat("study_page", artifact=page, resumed="yes")  # type: ignore[arg-type]


def test_heartbeat_subjects(
    history: StudyHistoryManager, page: Artifact, lab: Artifact, source: KnowledgeSource,
    session: Session, settings: Settings,
) -> None:
    lab_span = history.heartbeat("lab", lab=lab, at=at(0))
    assert (lab_span.surface, lab_span.lab_id, lab_span.artifact_id) == ("lab", lab.id, page.id)
    source_span = history.heartbeat("source", source=source, at=at(0, 10))
    assert source_span.source_id == source.id and source_span.active_seconds == 0
    assert lab_span.active_seconds == 10  # the time until the source was opened was on the lab
    graph = history.heartbeat("graph", at=at(0, 20))
    assert (graph.artifact_id, graph.lab_id, graph.source_id, graph.block_id) == (None,) * 4
    for surface, kwargs, message in [
        ("reading", {}, "unknown attention surface 'reading'"),
        ("study_page", {}, "a study_page heartbeat needs artifact"),
        ("graph", {"artifact": page}, "a graph heartbeat takes no artifact"),
        ("lab", {"lab": lab, "block": block(page, "b01")}, "a lab heartbeat takes no block"),
        ("study_page", {"artifact": lab}, "is a lab; pass it as the lab"),
    ]:
        with pytest.raises(ValueError, match=message):
            history.heartbeat(surface, **kwargs)


def test_active_time_never_exceeds_wall_time(history: StudyHistoryManager, page: Artifact) -> None:
    """Two windows taking turns: every interval is credited once, to one of them."""
    other = block(page, "b02")
    for i in range(10):
        history.heartbeat("study_page", artifact=page, block=other if i % 2 else None, at=at(0, 7 * i))
    spans = history.session.scalars(select(AttentionSpan)).all()
    assert sum(s.active_seconds for s in spans) == 63
    assert all(s.last_seen_at >= s.started_at for s in spans)


# -- activity rows get the session ---------------------------------------------------------------


def test_new_activity_rows_join_the_open_session(
    history: StudyHistoryManager, page: Artifact, lab: Artifact, session: Session, settings: Settings
) -> None:
    current = history.activity_session()
    answer = BlockResponseManager(session, settings).record(
        block(page, "b02"), response={"choice": 1}, passed=True, grader="mc"
    )
    chat = ChatManager(session, settings)
    thread = chat.create_thread(page)
    message = chat.add_message(thread, role="assistant", text="Try this.", api_content=[{"type": "text", "text": "x"}])
    (exercise,) = chat.add_blocks(message, [ChatBlockDraft("multiple_choice", True, MC, 0)])
    chat_answer = chat.record_response(exercise, response={"choice": 0}, passed=False, grader="mc")
    spec = LabManager(session, settings).spec(lab)
    run = LabManager(session, settings).record_run(
        lab, spec, kind="run", step=None, files=[LabFile(path=spec.entry, content="print(1)\n")],
        passed=True, output="1\n", error=None,
    )
    assert {answer.session_id, message.session_id, chat_answer.session_id, run.session_id} == {
        current.id
    }


# -- the read model ------------------------------------------------------------------------------


def _two_sittings(
    history: StudyHistoryManager, page: Artifact, lab: Artifact, session: Session,
    settings: Settings, clock: Clock,
) -> tuple[StudySession, StudySession]:
    """Morning: the page (open, two heartbeats, a failed and a passed answer, a chat). Evening:
    the lab (open, heartbeats, a check). Rows get the clock's time as `created_at` (their
    column default is the real time)."""
    history.log("study_page_opened", artifact=page, at=at(0))
    history.heartbeat("study_page", artifact=page, block=block(page, "b01"), at=at(0, 5))
    history.heartbeat("study_page", artifact=page, block=block(page, "b01"), at=at(0, 20))
    responses = BlockResponseManager(session, settings)
    for minutes, passed in ((1, False), (2, True)):
        clock.set(at(minutes))
        row = responses.record(
            block(page, "b02"), response={"choice": int(passed)}, passed=passed, grader="mc",
            feedback="Yes." if passed else "Look again.",
        )
        row.created_at = at(minutes)
    chat = ChatManager(session, settings)
    thread = chat.create_thread(page)
    for minutes, role, text_ in ((3, "user", "Why softmax?"), (3.5, "assistant", "To normalise.")):
        clock.set(at(minutes))
        message = chat.add_message(
            thread, role=role, text=text_, api_content=[{"type": "text", "text": text_}]
        )
        message.created_at = at(minutes)
    evening = at(600)
    history.log("lab_opened", lab=lab, at=evening)
    history.heartbeat("lab", lab=lab, at=evening)
    history.heartbeat("lab", lab=lab, at=evening + timedelta(seconds=30))
    clock.set(evening + timedelta(seconds=40))
    spec = LabManager(session, settings).spec(lab)
    run = LabManager(session, settings).record_run(
        lab, spec, kind="check", step=2, files=[LabFile(path=spec.entry, content="x = 1\n")],
        passed=False, output="", error="AssertionError",
    )
    run.created_at = evening + timedelta(seconds=40)
    session.flush()
    morning, evening_session = session.scalars(select(StudySession).order_by(StudySession.id)).all()
    return morning, evening_session


def test_sessions_with_totals_newest_first(
    history: StudyHistoryManager, page: Artifact, lab: Artifact, session: Session,
    settings: Settings, clock: Clock,
) -> None:
    morning, evening = _two_sittings(history, page, lab, session, settings, clock)
    listed = history.list_sessions()
    assert listed.total == 2
    assert [s.session.id for s in listed.sessions] == [evening.id, morning.id]
    m = listed.sessions[1]
    assert (m.active_seconds, m.events, m.answers, m.answers_passed) == (15.0, 1, 2, 1)
    assert (m.user_messages, m.tutor_messages, m.lab_runs, m.lab_checks) == (1, 1, 0, 0)
    assert [a.slug for a in m.artifacts] == [page.slug]
    e = listed.sessions[0]
    assert (e.active_seconds, e.lab_checks, e.lab_checks_passed) == (30.0, 1, 0)
    assert {a.slug for a in e.artifacts} == {page.slug, lab.slug}  # a lab event names its page
    record = history.session_record(m)
    assert record["started_at"] == "2026-03-01T09:00:00+00:00" and record["backfilled"] is False

    assert [s.session.id for s in history.list_sessions(limit=1, offset=1).sessions] == [morning.id]
    for kwargs, message in [({"limit": 0}, "limit must be"), ({"offset": -1}, "offset must be"),
                            ({"since": at(5), "until": at(5)}, "until must be later")]:
        with pytest.raises(ValueError, match=message):
            history.list_sessions(**kwargs)


def test_session_filters(
    history: StudyHistoryManager, page: Artifact, lab: Artifact, session: Session,
    settings: Settings, source: KnowledgeSource, clock: Clock,
) -> None:
    morning, evening = _two_sittings(history, page, lab, session, settings, clock)
    other = ArtifactManager(session, settings).create(
        slug="other", kind="study_page", title="Other", generator="test"
    )
    history.log("study_page_opened", artifact=other, at=at(2000))

    def ids(**kwargs) -> list[int]:
        return [s.session.id for s in history.list_sessions(**kwargs).sessions]

    assert ids(artifact=page) == [evening.id, morning.id]  # the evening was on its lab
    assert ids(artifact=lab) == [evening.id]
    assert ids(artifact=other) != [] and morning.id not in ids(artifact=other)
    assert ids(source=source) == [evening.id, morning.id]
    assert ids(since=at(30), until=at(700)) == [evening.id]
    assert ids(until=at(1)) == [morning.id]  # overlapping, not contained
    assert morning.id in ids(since=at(3)) and morning.id not in ids(since=at(4))  # ended 09:03:30
    assert ids(artifact=page, until=at(100)) == [morning.id]


def test_timeline_merges_everything_in_time_order(
    history: StudyHistoryManager, page: Artifact, lab: Artifact, session: Session,
    settings: Settings, clock: Clock,
) -> None:
    morning, evening = _two_sittings(history, page, lab, session, settings, clock)
    timeline = history.timeline(morning)
    assert [(e["type"], e["at"][11:19]) for e in timeline] == [
        ("event", "09:00:00"),
        ("span", "09:00:05"),
        ("answer", "09:01:00"),
        ("answer", "09:02:00"),
        ("chat_message", "09:03:00"),
        ("chat_message", "09:03:30"),
    ]
    first_answer = timeline[2]
    assert first_answer["question"] == "Which?" and first_answer["feedback"] == "Look again."
    assert first_answer["block"] == "b02" and first_answer["passed"] is False
    assert first_answer["block_label"] == "Which?"
    assert first_answer["response"] == {"choice": 0}  # the raw response stays
    assert first_answer["answer"] == {"form": "choice", "choice": 0, "text": "a", "note": None}
    assert "details" not in first_answer
    assert "correct_index" not in str(first_answer) and "b." not in str(first_answer["answer"])
    assert timeline[1]["active_seconds"] == 15.0 and timeline[1]["block"] == "b01"
    assert timeline[1]["block_label"] == "Attention."
    assert timeline[0]["block"] is None and timeline[0]["block_label"] is None
    assert timeline[4]["text"] == "Why softmax?" and timeline[4]["role"] == "user"
    lab_timeline = history.timeline(evening)
    run = lab_timeline[-1]
    assert run["type"] == "lab_run" and run["error"] == "AssertionError" and run["step"] == 2
    assert run["lab"]["slug"] == lab.slug and run["artifact"]["slug"] == page.slug
    assert "files" not in run and run["file_paths"] == [stub_lab(page.slug).entry]
    assert history.timeline(evening, with_files=True)[-1]["files"][0]["content"] == "x = 1\n"


def test_rows_without_a_session_are_listed_apart(
    history: StudyHistoryManager, page: Artifact, session: Session
) -> None:
    session.execute(text(
        "INSERT INTO block_responses (block_id, attempt, response, passed, grader) "
        f"VALUES ({block(page, 'b02').id}, 1, '{{}}', 1, 'raw')"
    ))
    assert [e["grader"] for e in history.timeline(None)] == ["raw"]
    (unassigned,) = history.export()["unassigned"]
    assert unassigned["type"] == "answer" and unassigned["response"] == {}
    assert unassigned["answer"] == {  # a shape the history does not know: raw, and labelled
        "form": "raw", "choice": None, "text": "{}",
        "note": 'a multiple-choice answer should be {"choice": <option index>}',
    }
    markdown = history_markdown(history.export())
    assert "  - Answer, as stored (not in a form this export knows: a multiple-choice" in markdown
    assert "    {}" in markdown


@pytest.mark.parametrize(
    ("block_type", "content", "response", "expected"),
    [
        ("multiple_choice", MC, {"choice": 1}, {"form": "choice", "choice": 1, "text": "b"}),
        ("blurb", {"prompt": "Why?", "private": {"model_answer": "Because.", "rubric": []}},
         {"text": "It scales."}, {"form": "text", "choice": None, "text": "It scales."}),
        ("multiple_choice", MC, {"choice": 5}, {"form": "raw", "text": '{"choice": 5}',
         "note": "option 5 is not one of the question's 2 options"}),
        ("multiple_choice", MC, {"choice": True}, {"form": "raw",
         "note": "the choice is not an option index"}),
        ("multiple_choice", MC, "b", {"form": "raw", "text": '"b"'}),
        ("blurb", {"prompt": "Why?"}, {"text": 3}, {"form": "raw", "text": '{"text": 3}'}),
        ("prose", {"markdown": "x"}, {"x": 1}, {"form": "raw",
         "note": "a prose block takes no answers this history knows how to show"}),
        ("prediction", {"prompt": "Up?", "options": ["up", "down"],
                        "private": {"correct_index": 1, "resolution": "Because."}},
         {"choice": 0}, {"form": "choice", "choice": 0, "text": "up"}),
        ("prediction", {"prompt": "Up?", "options": ["up", "down"]}, {"text": "up"},
         {"form": "raw", "note": 'a prediction answer should be {"choice": <option index>}'}),
    ],
)
def test_reader_answer(block_type: str, content: dict, response: object, expected: dict) -> None:
    got = reader_answer(block_type, content, response)
    assert {k: got[k] for k in expected} == expected
    assert set(got) == {"form", "choice", "text", "note"}
    assert "Because" not in str(got)  # nothing private, ever


def test_artifact_totals(
    history: StudyHistoryManager, page: Artifact, lab: Artifact, session: Session,
    settings: Settings, clock: Clock,
) -> None:
    _two_sittings(history, page, lab, session, settings, clock)
    totals = history.artifact_totals(page)
    assert totals["artifact"] == {"slug": page.slug, "title": page.title, "kind": "study_page"}
    assert (totals["active_seconds"], totals["lab_seconds"]) == (15.0, 30.0)
    assert totals["block_seconds"] == [{"block": "b01", "label": "Attention.", "seconds": 15.0}]
    assert (totals["answers"], totals["answers_passed"], totals["pass_rate"]) == (2, 1, 0.5)
    assert totals["gates_attempted"] == 1 and totals["user_messages"] == 1
    assert (totals["lab_checks"], totals["lab_checks_passed"]) == (1, 0)
    assert totals["first_activity_at"] == "2026-03-01T09:00:00+00:00"
    lab_totals = history.artifact_totals(lab)
    assert lab_totals["active_seconds"] == lab_totals["lab_seconds"] == 30.0
    assert lab_totals["answers"] == 0 and lab_totals["pass_rate"] is None


def test_export_and_markdown(
    history: StudyHistoryManager, page: Artifact, lab: Artifact, session: Session,
    settings: Settings, clock: Clock,
) -> None:
    _two_sittings(history, page, lab, session, settings, clock)
    data = history.export()
    assert data["format"] == "teacher-study-history" and data["version"] == 4
    assert data["session_idle_gap_minutes"] == 30
    assert [len(s["timeline"]) for s in data["sessions"]] == [6, 3]  # oldest first
    assert {a["artifact"]["slug"] for a in data["artifacts"]} == {page.slug, lab.slug}
    markdown = history_markdown(data)
    for expected in (
        "# Study history",
        "| Study: The Paper (`study-paper`) | study_page | 15 s | 30 s | 2 | 2 | 1 | 50% |",
        "Opened the study page *Study: The Paper*.",
        "Answered checkpoint b02 “Which?” (multiple choice) on *Study: The Paper*, attempt 1: "
        "**not passed**.",
        "    > Which?",
        "  - Answer (option 1):\n    > a\n",
        "  - Answer (option 2):\n    > b\n",
        "    > Look again.",
        "**You**, conversation",
        "  > Why softmax?",
        "  > To normalise.",
        "Check of step 3 in *Lab: The Paper*: **failed**.",
        "AssertionError",
        "- *Study: The Paper*: 15 s (b01 “Attention.”: 15 s)",
    ):
        assert expected in markdown, expected


def test_every_kind_reads_as_a_sentence() -> None:
    refs = {"artifact": {"slug": "p", "title": "P", "kind": "study_page"},
            "lab": {"slug": "l", "title": "L", "kind": "lab"},
            "source": {"slug": "s", "title": "S"}, "concept": {"slug": "c", "name": "C"},
            "block": "b01", "block_label": "Intro"}
    sample = {"state": "passed", "attempt": 1, "unlocked": ["b02"], "term": "T", "thread_id": 1,
              "files": 1, "chars": 3, "step": 0, "first_time": True, "query": "q", "results": 2,
              "semantic": "ok", "regenerate": False, "model": None, "via": "upload",
              "fields": ["title"], "slug": "x", "title": "X", "kind": "lab", "sources": ["s"],
              "supersedes": "old"}
    for kind, spec in EVENT_SPECS.items():
        payload = {key: sample[key] for key in spec.payload}
        sentence = describe_event({"kind": kind, **refs, "payload": payload})
        assert sentence.endswith(".") and "{" not in sentence, kind
    gate = {"kind": "gate_cleared", **refs, "payload": {"state": "passed", "attempt": 1,
                                                        "unlocked": []}}
    assert describe_event(gate).startswith("Cleared checkpoint b01 “Intro” on *P*")
    assert "a deleted checkpoint" in describe_event({**gate, "block": None, "block_label": None})
    assert (duration(5), duration(125), duration(3900)) == ("5 s", "2 min", "1 h 5 min")


def test_predictions_are_listed_but_never_counted_as_answers(
    history: StudyHistoryManager, page: Artifact, session: Session, settings: Settings,
    clock: Clock,
) -> None:
    blocks = ArtifactBlockManager(session, settings)
    prediction = blocks.add(
        page, type="prediction", is_gate=True,
        content={"prompt": "Up or down?", "options": ["up", "down"],
                 "private": {"correct_index": 1, "resolution": "SECRET-RESOLUTION"}},
    )
    responses = BlockResponseManager(session, settings)
    clock.set(at(1))
    responses.record(prediction, response={"choice": 0}, passed=True, grader="prediction-v1",
                     feedback="Recorded.", details={"correct": False})
    clock.set(at(2))
    responses.record(block(page, "b02"), response={"choice": 0}, passed=False, grader="judge",
                     verdict="partial", missing=("names the cost",), confidence="high",
                     feedback="Half there.")
    session.flush()

    (summary,) = history.list_sessions().sessions
    assert (summary.answers, summary.answers_passed) == (1, 0)  # the prediction is left out
    totals = history.artifact_totals(page)
    assert (totals["answers"], totals["answers_passed"], totals["gates_attempted"]) == (1, 0, 1)
    period = history.period_totals(at(0))
    assert (period["answers"], period["questions"], period["first_attempts"]) == (1, 1, 1)
    assert period["first_attempt_pass_rate"] == 0.0

    guess, answer = [e for e in history.timeline(summary.session) if e["type"] == "answer"]
    assert guess["is_question"] is False and guess["block_type"] == "prediction"
    assert guess["answer"] == {"form": "choice", "choice": 0, "text": "up", "note": None}
    assert answer["is_question"] is True
    assert (answer["verdict"], answer["missing"], answer["confidence"]) == (
        "partial", ["names the cost"], "high"
    )
    markdown = history_markdown(history.export())
    assert "Made a prediction at checkpoint" in markdown and "not graded" in markdown
    assert "  - Guess (option 1):\n    > up\n" in markdown
    assert "SECRET-RESOLUTION" not in markdown
    assert "attempt 1: **partly right**." in markdown
    assert "  - How sure: Certain" in markdown
    assert "  - Still missing:\n    - names the cost" in markdown
