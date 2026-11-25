"""The cost ledger (`teacher.costs`): meters and scopes, the recording at each kind of call site
(fake clients with usage, cache fields, rejected replies, unknown models), robustness to a
rolled-back transaction, and the totals."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import anthropic
import httpx2
import pytest
from openai.types import CreateEmbeddingResponse
from sqlalchemy import select

from teacher.claude_generator import GlossaryBuilder, GlossaryUnavailableError
from teacher.config import Settings
from teacher.costs import (
    Meter,
    UnmeteredCallError,
    artifact_cost,
    cost_scope,
    current_meter,
    message_costs,
    metering,
    thread_cost,
)
from teacher.db.engine import session_scope
from teacher.db.models import ApiCall, Artifact, ChatMessage, ChatThread, GenerationRun
from teacher.embeddings import OpenAIEmbedder
from teacher.grading import GradingUnavailableError
from teacher.judge import LLMJudgeGrader
from teacher.metadata import MetadataExtractor
from teacher.review_bank import BankQuestion, ClaudeReviewBankBuilder, ReviewBankOutputError
from teacher.tutor import Tutor, TutorError

M = 1_000_000
BLURB = {
    "prompt": "Explain why the filter looks blue.",
    "min_words": 3,
    "private": {
        "model_answer": "It passes blue and absorbs the rest.",
        "rubric": [{"criterion": "Says it passes blue", "required": True}],
    },
}
ANSWER = {"text": "It lets blue light through."}


# -- meters and scopes ----------------------------------------------------------------------


def test_no_meter_means_no_call(fake_anthropic) -> None:
    fake = fake_anthropic(fake_anthropic.reply([(True, "Met.")]))
    assert current_meter() is None
    with pytest.raises(UnmeteredCallError, match="grading call to Claude was not sent"):
        LLMJudgeGrader(fake).grade(BLURB, ANSWER)
    assert fake.calls == []  # nothing was spent


def test_scopes_nest_and_the_outermost_purpose_wins(settings: Settings) -> None:
    with cost_scope(settings, purpose="generation", artifact_id=None) as outer:
        with cost_scope(settings, purpose="concept_extraction", knowledge_source_id=7) as inner:
            assert inner.purpose == "generation"
            assert inner.links.knowledge_source_id == 7
            assert current_meter() is inner
        assert current_meter() is outer
    assert current_meter() is None
    with cost_scope(settings, chat_thread_id=1):
        with pytest.raises(ValueError, match="cannot change chat_thread_id from 1 to 2"):
            with cost_scope(settings, chat_thread_id=2):
                pass
    with pytest.raises(ValueError, match="unknown cost purpose"):
        Meter.in_memory(purpose="fun")
    with pytest.raises(ValueError, match="unknown cost link"):
        Meter.in_memory(page_id=1)


def test_a_scope_for_another_database_does_not_extend_the_current_meter(
    settings: Settings,
) -> None:
    memory = Meter.in_memory(purpose="tutor")
    with metering(memory), cost_scope(settings, purpose="grading") as meter:
        assert meter is not memory and meter.records_to(settings)
        assert meter.purpose == "grading"


# -- recording at each kind of call site ----------------------------------------------------


def test_judge_records_its_call_with_the_cache_breakdown(cost_meter: Meter, fake_anthropic) -> None:
    reply = fake_anthropic.reply(
        [(True, "Met.")], input_tokens=1_000, output_tokens=200,
        cache_creation_input_tokens=300, cache_read_input_tokens=50,
    )
    reply.usage.cache_creation = anthropic.types.CacheCreation(
        ephemeral_5m_input_tokens=200, ephemeral_1h_input_tokens=100
    )
    LLMJudgeGrader(fake_anthropic(reply)).grade(BLURB, ANSWER)
    [entry] = cost_meter.entries
    assert (entry.purpose, entry.step, entry.provider, entry.model) == (
        "grading", "judge", "anthropic", "claude-haiku-5-5"
    )
    usage = entry.usage
    assert (usage.input_tokens, usage.cache_write_tokens, usage.cache_write_1h_tokens,
            usage.cache_read_tokens, usage.output_tokens) == (1_000, 300, 100, 50, 200)
    expected = (1_000 * 0.10 + 200 * 0.125 + 100 * 0.20 + 50 * 0.01 + 200 * 0.50) / M
    assert entry.cost_usd == pytest.approx(expected)
    assert entry.priced_on == "2026-11-24" and not entry.is_estimate
    assert entry.response_id == "msg_fake"


def test_a_rejected_reply_is_recorded_too(cost_meter: Meter, fake_anthropic) -> None:
    refusal = fake_anthropic.reply(text="", stop_reason="refusal", input_tokens=40,
                                   output_tokens=3)
    garbage = fake_anthropic.reply(text="not json", input_tokens=41, output_tokens=4)
    for reply in (refusal, garbage):
        with pytest.raises(GradingUnavailableError):
            LLMJudgeGrader(fake_anthropic(reply)).grade(BLURB, ANSWER)
    assert [(e.usage.input_tokens, e.usage.output_tokens) for e in cost_meter.entries] == [
        (40, 3), (41, 4)
    ]
    assert all(e.cost_usd and e.cost_usd > 0 for e in cost_meter.entries)


def test_an_unknown_model_is_recorded_without_a_cost(
    cost_meter: Meter, fake_anthropic, caplog: pytest.LogCaptureFixture
) -> None:
    reply = fake_anthropic.reply([(True, "Met.")], model="claude-unknown-9")
    with caplog.at_level(logging.WARNING, logger="teacher.costs"):
        LLMJudgeGrader(fake_anthropic(reply), model="claude-unknown-9").grade(BLURB, ANSWER)
    [entry] = cost_meter.entries
    assert entry.model == "claude-unknown-9" and entry.cost_usd is None
    assert entry.priced_on is None and entry.usage.input_tokens == 321
    assert "no known price for model 'claude-unknown-9'" in caplog.text


def test_stream_message_records_before_the_reply_is_checked(
    cost_meter: Meter, fake_anthropic, settings: Settings
) -> None:
    """The shared streamed call (generator, page checks, glossary, labs, concepts, tags,
    relating, references) records each reply, also one whose stop reason is then rejected."""
    cut = fake_anthropic.reply(text="{", stop_reason="max_tokens", model="claude-sonnet-5-5",
                               input_tokens=7, cache_creation_input_tokens=0,
                               cache_read_input_tokens=9_000, output_tokens=32_000)
    builder = GlossaryBuilder(fake_anthropic(cut), model="claude-sonnet-5-5")
    document = {"type": "document", "source": {"type": "text", "media_type": "text/plain",
                                               "data": "x"}}
    blocks = [("prose", {"markdown": "Attention.", "role": "explanation"})]
    with pytest.raises(GlossaryUnavailableError, match="cut off"):
        builder.interpret(builder.call(document, blocks), blocks)
    [entry] = cost_meter.entries
    assert (entry.purpose, entry.step) == ("glossary", "glossary")
    assert entry.usage.cache_read_tokens == 9_000
    assert entry.cost_usd == pytest.approx((7 * 2 + 9_000 * 0.10 + 32_000 * 10) / M)


def test_metadata_records_its_call(cost_meter: Meter, fake_anthropic, make_pdf, tmp_path: Path) -> None:
    path = tmp_path / "paper.pdf"
    path.write_bytes(make_pdf(2))
    MetadataExtractor(fake_anthropic(fake_anthropic.metadata_reply())).extract(
        path, "application/pdf"
    )
    [entry] = cost_meter.entries
    assert (entry.purpose, entry.step, entry.usage.input_tokens) == ("metadata", "metadata", 321)


def test_review_bank_records_a_rejected_reply(cost_meter: Meter, fake_anthropic) -> None:
    question = BankQuestion(1, 11, "b02", "blurb", {**BLURB, "min_words": 12})
    bad = fake_anthropic.reply(text="{}", model="claude-opus-5-5", input_tokens=900,
                               output_tokens=80)
    with pytest.raises(ReviewBankOutputError):
        ClaudeReviewBankBuilder(fake_anthropic(bad)).build([question], model="claude-opus-5-5")
    [entry] = cost_meter.entries
    assert (entry.purpose, entry.step, entry.model) == ("review_bank", "review_bank",
                                                        "claude-opus-5-5")
    assert entry.cost_usd == pytest.approx((900 * 4 + 80 * 20) / M)


def _tutor_events(stop_reason: str, *, tool: bool = False) -> bytes:
    usage = {"input_tokens": 12, "output_tokens": 1, "cache_creation_input_tokens": 300,
             "cache_read_input_tokens": 40,
             "cache_creation": {"ephemeral_5m_input_tokens": 100,
                                "ephemeral_1h_input_tokens": 200}}
    events: list[dict[str, Any]] = [
        {"type": "message_start", "message": {
            "id": f"msg_{stop_reason}", "type": "message", "role": "assistant",
            "model": "claude-opus-5-5", "content": [], "stop_reason": None,
            "stop_sequence": None, "usage": usage}},
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "text", "text": "", "citations": None}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "text_delta", "text": "A reply."}},
        {"type": "content_block_stop", "index": 0},
    ]
    if tool:
        events += [
            {"type": "content_block_start", "index": 1, "content_block": {
                "type": "tool_use", "id": "toolu_1", "name": "show_chart", "input": {}}},
            {"type": "content_block_delta", "index": 1,
             "delta": {"type": "input_json_delta", "partial_json": "{}"}},
            {"type": "content_block_stop", "index": 1},
        ]
    events += [
        {"type": "message_delta", "delta": {"stop_reason": stop_reason, "stop_sequence": None},
         "usage": {"output_tokens": 50}},
        {"type": "message_stop"},
    ]
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def _sdk(bodies: list[bytes]) -> anthropic.Anthropic:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, headers={"content-type": "text/event-stream"},
                               content=bodies.pop(0))

    return anthropic.Anthropic(
        api_key="test-key", base_url="https://api.example.invalid", max_retries=0,
        http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)),
    )


def test_tutor_records_every_round_through_its_meter() -> None:
    """A reply whose first round's tool call is rejected and whose second round ends the turn:
    two calls, both recorded through the meter passed in (no current meter needed)."""
    tutor = Tutor(_sdk([_tutor_events("tool_use", tool=True), _tutor_events("end_turn")]))
    meter = Meter.in_memory(purpose="tutor", chat_thread_id=3)
    request = tutor.request([{"role": "user", "content": [{"type": "text", "text": "Hi"}]}])
    items = list(tutor.stream(request, meter=meter))
    assert items[-1].text.endswith("A reply.")
    assert [e.step for e in meter.entries] == ["tutor reply", "tutor reply"]
    entry = meter.entries[0]
    assert (entry.usage.cache_write_tokens, entry.usage.cache_write_1h_tokens) == (300, 200)
    assert entry.links.chat_thread_id == 3
    expected = (12 * 4 + 100 * 5 + 200 * 8 + 40 * 0.20 + 50 * 20) / M
    assert entry.cost_usd == pytest.approx(expected) and not entry.is_estimate
    assert meter.recorded_ids == [1, 2]


def test_tutor_records_a_refused_round() -> None:
    tutor = Tutor(_sdk([_tutor_events("refusal")]))
    meter = Meter.in_memory()
    request = tutor.request([{"role": "user", "content": [{"type": "text", "text": "Hi"}]}])
    with pytest.raises(TutorError, match="declined"):
        list(tutor.stream(request, meter=meter))
    assert [(e.purpose, e.response_id) for e in meter.entries] == [("tutor", "msg_refusal")]


def test_tutor_without_a_meter_sends_nothing() -> None:
    sent: list[bytes] = []
    tutor = Tutor(_sdk(sent))
    request = tutor.request([{"role": "user", "content": [{"type": "text", "text": "Hi"}]}])
    with pytest.raises(UnmeteredCallError, match="tutor call"):
        list(tutor.stream(request))


def test_embeddings_record_each_batch(cost_meter: Meter, monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeOpenAI:
        def __init__(self) -> None:
            self.embeddings = self
            self.calls = 0

        def create(self, **kwargs: Any) -> CreateEmbeddingResponse:
            self.calls += 1
            return CreateEmbeddingResponse.model_validate({
                "object": "list", "model": "text-embedding-3-small",
                "data": [{"object": "embedding", "index": i, "embedding": [1.0, 0.5]}
                         for i in range(len(kwargs["input"]))],
                "usage": {"prompt_tokens": 1_000 * len(kwargs["input"]), "total_tokens": 0},
            })  # fmt: skip

    embedder = OpenAIEmbedder(client=FakeOpenAI(), batch_size=2)
    embedder.embed(["a", "b", "c"])
    assert [(e.provider, e.step, e.purpose, e.usage.input_tokens) for e in cost_meter.entries] == [
        ("openai", "embedding", "embedding", 2_000), ("openai", "embedding", "embedding", 1_000)
    ]
    assert cost_meter.entries[0].cost_usd == pytest.approx(2_000 * 0.02 / M)


# -- the database sink ----------------------------------------------------------------------


def test_a_row_survives_the_rollback_of_the_work_around_it(
    settings: Settings, fake_anthropic
) -> None:
    """The row is committed on a connection of its own as soon as the call returns, so it stays
    when the transaction around the call (which may read first and write after the call, as
    the routers do) rolls back. (A caller must not hold a write transaction during the call:
    the row would wait for it.)"""
    fake = fake_anthropic(fake_anthropic.reply([(True, "Met.")]))
    with pytest.raises(RuntimeError, match="after the call"):
        with session_scope(settings) as session, cost_scope(settings, purpose="grading"):
            assert session.scalar(select(Artifact)) is None  # a read before the call
            LLMJudgeGrader(fake).grade(BLURB, ANSWER)
            session.add(Artifact(slug="x", kind="study_page", title="X", dir_path="artifacts/x",
                                 generator="g"))
            session.flush()
            raise RuntimeError("the work failed after the call")
    with session_scope(settings) as session:
        assert session.scalar(select(Artifact).where(Artifact.slug == "x")) is None
        [row] = session.scalars(select(ApiCall)).all()
        assert (row.purpose, row.step, row.input_tokens, row.backfill_key) == (
            "grading", "judge", 321, None
        )


# -- totals ---------------------------------------------------------------------------------


def _row(**kwargs: Any) -> ApiCall:
    defaults = dict(purpose="generation", step="write", provider="anthropic",
                    model="claude-opus-5-5", input_tokens=1, cache_write_tokens=0,
                    cache_write_1h_tokens=0, cache_read_tokens=0, output_tokens=1,
                    cost_usd=0.5, priced_on="2026-11-24")
    return ApiCall(**{**defaults, **kwargs})


def test_artifact_and_thread_totals(settings: Settings) -> None:
    with session_scope(settings) as session:
        artifact = Artifact(slug="p", kind="study_page", title="P", dir_path="artifacts/p",
                            generator="g")
        session.add(artifact)
        session.flush()
        write = GenerationRun(artifact_id=artifact.id, stage="write", model="m",
                              status="succeeded")
        review = GenerationRun(artifact_id=artifact.id, stage="review", model="m",
                               status="succeeded")
        thread = ChatThread(artifact_id=artifact.id)
        session.add_all([write, review, thread])
        session.flush()
        reply = ChatMessage(thread_id=thread.id, position=1, role="assistant", text="t",
                            api_content=[])
        session.add(reply)
        session.flush()
        session.add_all([
            _row(artifact_id=artifact.id, generation_run_id=write.id, cost_usd=0.80),
            _row(artifact_id=artifact.id, generation_run_id=review.id, cost_usd=0.04),
            _row(artifact_id=artifact.id, purpose="tutor", step="tutor reply", cost_usd=0.09,
                 chat_thread_id=thread.id, chat_message_id=reply.id),
            _row(artifact_id=artifact.id, purpose="tutor", step="tutor reply", cost_usd=1.90,
                 is_estimate=True, chat_thread_id=thread.id),
            _row(artifact_id=artifact.id, purpose="grading", step="judge", cost_usd=0.03,
                 chat_thread_id=thread.id),
            _row(artifact_id=artifact.id, purpose="grading", step="judge", cost_usd=None,
                 priced_on=None, model="claude-unknown-9"),
            _row(purpose="embedding", step="embedding", provider="openai", cost_usd=0.01),
        ])  # fmt: skip
        session.flush()
        cost = artifact_cost(session, artifact.id)
        threads = thread_cost(session, thread.id)
        replies = message_costs(session, [reply.id])
    gen = cost.generation
    assert gen.total_usd == pytest.approx(0.84) and gen.calls == 2 and not gen.estimate
    assert [(p.key, p.label) for p in gen.parts] == [("write", "writing"), ("review", "review")]
    since = cost.since_generation
    assert since.total_usd == pytest.approx(2.02) and since.calls == 4
    assert since.estimate and since.estimate_calls == 1
    assert since.unknown_price_calls == 1 and since.unknown_price_models == ("claude-unknown-9",)
    assert [(p.key, p.calls, p.estimate_calls) for p in since.parts] == [
        ("tutor", 2, 1), ("grading", 2, 0)
    ]
    assert cost.total.total_usd == pytest.approx(2.86) and cost.total.calls == 6
    assert threads.total_usd == pytest.approx(2.02) and threads.calls == 3
    assert replies[reply.id].total_usd == pytest.approx(0.09)
    data = cost.to_dict()
    assert data["generation"]["parts"][0] == {
        "key": "write", "label": "writing", "total_usd": 0.8, "calls": 1,
        "unknown_price_calls": 0, "estimate": False,
    }
    assert data["since_generation"]["unknown_price_models"] == ["claude-unknown-9"]
