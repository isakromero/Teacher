"""`teacher costs backfill` (`teacher.cost_backfill`): past calls from stage files, run totals,
graded answers, tutor replies, reference extractions and command-line records; exact rows vs
upper-bound estimates; idempotency; nothing the live ledger covers."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from teacher.cli import main
from teacher.config import Settings
from teacher.cost_backfill import backfill
from teacher.costs import artifact_cost, thread_cost
from teacher.db.engine import session_scope
from teacher.db.models import (
    ApiCall,
    ArtifactBlock,
    BlockResponse,
    ChatMessage,
    ChatThread,
    GenerationRun,
    SourceReferenceExtraction,
)
from teacher.managers import ArtifactManager, KnowledgeSourceManager

M = 1_000_000
T0 = datetime(2026, 11, 20, 12, 0, 0)


def usage(fresh: int, write: int, read: int, out: int) -> dict[str, int]:
    return {"input_tokens": fresh, "cache_creation_input_tokens": write,
            "cache_read_input_tokens": read, "output_tokens": out}


def call(message_id: str, model: str, u: dict[str, int]) -> dict[str, Any]:
    return {"model": model, "message_id": message_id, "stop_reason": "end_turn", "usage": u}


def write_json(path: Path, data: Any) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


@pytest.fixture
def history(settings: Settings, tmp_path: Path) -> dict[str, int]:
    """A page with the records of a past generation, answers and a tutor conversation."""
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    ids: dict[str, int] = {}
    with session_scope(settings) as session:
        source = KnowledgeSourceManager(session, settings).add(
            pdf, slug="paper", kind="paper", title="Paper"
        )
        artifacts = ArtifactManager(session, settings)
        page = artifacts.create(slug="page", kind="study_page", title="P",
                                generator="claude-study-v1", sources=[(source, "primary")])
        gen = settings.data_root / "artifacts" / "page" / "generation"
        files = {
            "analyse": write_json(gen / "analyse.json", {
                **call("msg_a", "claude-sonnet-5-5", usage(500, 40_000, 0, 5_000)),
                "output": {}}),
            "question_check": write_json(gen / "question_check.json", {
                "judge_model": "claude-haiku-5-5",
                "open_book": [call("msg_q1", "claude-sonnet-5-5", usage(200, 2_000, 0, 90)),
                              call("msg_q2", "claude-sonnet-5-5", usage(200, 1_000, 2_000, 80))],
                "judged": [{"graded": True}, {"graded": True}],
                "closed_book": call("msg_q3", "claude-sonnet-5-5", usage(1_800, 0, 0, 120)),
            }),
            # A glossary file rewritten by a later rebuild: it does not describe this run.
            "glossary": write_json(gen / "glossary.json", {
                **call("msg_g2", "claude-sonnet-5-5", usage(14_000, 0, 40_000, 9_000))}),
        }
        runs = [
            ("analyse", "succeeded", 40_500, 5_000, files["analyse"]),
            # the judge's two calls: 2 x (1_000 in, 200 out) beyond the file's calls
            ("question_check", "succeeded", 2_200 + 3_200 + 1_800 + 2_000, 290 + 400,
             files["question_check"]),
            ("glossary", "succeeded", 54_000, 7_000, files["glossary"]),
            ("write", "failed", 50_000, 9_000, None),  # tokens kept, no file: an estimate
            ("verify", "failed", None, None, None),
            ("review_bank", "succeeded", 6_000, 10_000, None),  # no cache breakpoint: exact
        ]
        for n, (stage, status, tokens_in, tokens_out, path) in enumerate(runs):
            run = GenerationRun(
                artifact_id=page.id, stage=stage, model="claude-sonnet-5-5", status=status,
                input_tokens=tokens_in, output_tokens=tokens_out,
                output_file=path.relative_to(settings.data_root).as_posix() if path else None,
                started_at=T0 + timedelta(minutes=n), finished_at=T0 + timedelta(minutes=n),
                error="boom" if status == "failed" else None,
            )  # fmt: skip
            session.add(run)
            session.flush()
            ids[stage] = run.id
        block = ArtifactBlock(artifact_id=page.id, position=1, key="b01", type="blurb",
                              is_gate=True, content_file="blocks/b01.json", content_sha256="x")
        session.add(block)
        session.flush()
        session.add(BlockResponse(
            block_id=block.id, attempt=1, response={"text": "t"}, passed=True, grader="llm",
            details={"model": "claude-haiku-5-5", "input_tokens": 1_868, "output_tokens": 335},
            created_at=T0 + timedelta(hours=1),
        ))  # fmt: skip
        session.add(BlockResponse(block_id=block.id, attempt=2, response={"choice": 1},
                                  passed=True, grader="mc", details=None))
        thread = ChatThread(artifact_id=page.id)
        session.add(thread)
        session.flush()
        reply = ChatMessage(thread_id=thread.id, position=2, role="assistant", text="hi",
                            api_content=[], model="claude-opus-5-5", input_tokens=239_208,
                            output_tokens=1_228, created_at=T0 + timedelta(hours=2))
        session.add(reply)
        session.add(SourceReferenceExtraction(
            source_id=source.id, status="succeeded", run_number=1, trigger="cli",
            extractor="claude-references-v1", model="claude-haiku-5-5",
            input_tokens=81_639, output_tokens=9_825, started_at=T0, finished_at=T0,
            succeeded_at=T0, found_in="reference_list", reference_count=3,
        ))  # fmt: skip
        session.flush()
        ids.update(page=page.id, source=source.id, thread=thread.id, reply=reply.id)
    write_json(settings.derived_dir / "_block_concepts" / "page.json", {
        "calls": [call("msg_t", "claude-sonnet-5-5", usage(18_000, 0, 0, 4_000))]})
    write_json(settings.derived_dir / "_concept_relations" / "attention.json", {
        "calls": [call("msg_r", "claude-unknown-9", usage(3_000, 0, 0, 500))]})
    return ids


def rows(settings: Settings) -> list[ApiCall]:
    with session_scope(settings) as session:
        return list(session.scalars(select(ApiCall).order_by(ApiCall.id)))


def test_backfill_adds_every_recoverable_call_once(
    settings: Settings, history: dict[str, int]
) -> None:
    report = backfill(settings)
    found = rows(settings)
    by_key = {r.backfill_key: r for r in found}
    run = history
    # analyse: exact from its stage file, writes at 5 minutes.
    analyse = by_key[f"generation_run:{run['analyse']}:1"]
    assert (analyse.purpose, analyse.step, analyse.response_id, analyse.is_estimate) == (
        "generation", "analyse", "msg_a", False
    )
    assert (analyse.input_tokens, analyse.cache_write_tokens, analyse.cache_write_1h_tokens,
            analyse.output_tokens) == (500, 40_000, 0, 5_000)
    assert analyse.cost_usd == pytest.approx((500 * 2 + 40_000 * 2.5 + 5_000 * 10) / M)
    # question check: the three calls of its file, then the judge's calls from the remainder.
    check = [r for r in found if r.generation_run_id == run["question_check"]]
    assert [(r.step, r.model, r.input_tokens, r.output_tokens, r.is_estimate) for r in check] == [
        ("question_check", "claude-sonnet-5-5", 200, 90, False),
        ("question_check", "claude-sonnet-5-5", 200, 80, False),
        ("question_check", "claude-sonnet-5-5", 1_800, 120, False),
        ("judge", "claude-haiku-5-5", 2_000, 400, False),
    ]
    assert check[-1].cost_usd == pytest.approx((2_000 * 0.10 + 400 * 0.50) / M)
    # glossary: the file is another run's, so the run's totals: an upper bound at 5 minutes.
    glossary = by_key[f"generation_run:{run['glossary']}:1"]
    assert (glossary.response_id, glossary.input_tokens, glossary.cache_write_tokens,
            glossary.is_estimate) == (None, 54_000, 0, True)
    assert glossary.cost_usd == pytest.approx((54_000 * 2.5 + 7_000 * 10) / M)
    write = by_key[f"generation_run:{run['write']}:1"]
    assert write.is_estimate and write.purpose == "generation"
    assert not any(r.generation_run_id == run["verify"] for r in found)  # no tokens: no row
    bank = by_key[f"generation_run:{run['review_bank']}:1"]
    assert (bank.purpose, bank.is_estimate) == ("review_bank", False)
    assert bank.cost_usd == pytest.approx((6_000 * 2 + 10_000 * 10) / M)
    # a graded answer (the multiple-choice one cost nothing)
    graded = [r for r in found if r.purpose == "grading"]
    assert [(r.step, r.input_tokens, r.artifact_id, r.is_estimate) for r in graded] == [
        ("judge", 1_868, run["page"], False)
    ]
    # the tutor reply: an upper bound at the 1-hour write rate, linked to thread and message
    tutor = by_key[f"chat_message:{run['reply']}"]
    assert (tutor.chat_thread_id, tutor.chat_message_id, tutor.artifact_id, tutor.is_estimate) == (
        run["thread"], run["reply"], run["page"], True
    )
    assert tutor.cost_usd == pytest.approx((239_208 * 8 + 1_228 * 20) / M)
    refs = by_key[f"reference_extraction:{run['source']}:1"]
    assert (refs.knowledge_source_id, refs.is_estimate) == (run["source"], False)
    tags = by_key["file:derived/_block_concepts/page.json:1"]
    assert (tags.purpose, tags.artifact_id, tags.response_id) == ("block_tagging", run["page"],
                                                                  "msg_t")
    relate = by_key["file:derived/_concept_relations/attention.json:1"]
    assert relate.cost_usd is None and relate.priced_on is None and not relate.is_estimate

    assert report.rows == len(found) == 13
    assert (report.exact, report.estimate, report.unknown_price) == (9, 3, 1)
    assert report.unknown_models == {"claude-unknown-9"}
    assert report.total_usd == pytest.approx(sum(r.cost_usd or 0 for r in found))

    # Idempotent: a second run adds nothing.
    again = backfill(settings)
    assert again.rows == 0 and again.skipped == 13
    assert len(rows(settings)) == 13

    with session_scope(settings) as session:
        cost = artifact_cost(session, run["page"])
        conversation = thread_cost(session, run["thread"])
    assert cost.generation.calls == 7 and cost.generation.estimate_calls == 2
    assert [p.key for p in cost.since_generation.parts] == [
        "tutor", "review_bank", "block_tagging", "grading"
    ]
    assert conversation.estimate and conversation.calls == 1


def test_dry_run_adds_nothing(settings: Settings, history: dict[str, int]) -> None:
    report = backfill(settings, dry_run=True)
    assert report.rows == 13 and report.dry_run
    assert rows(settings) == []
    assert report.lines()[0].startswith("Would add 13 ledger row(s): 9 exact, 3 estimate")


def test_nothing_the_live_ledger_covers_is_added(
    settings: Settings, history: dict[str, int]
) -> None:
    """A run or reply live rows point at is skipped, and so is anything created after the first
    live row (every call since was recorded as it was made)."""
    with session_scope(settings) as session:
        session.add(ApiCall(
            created_at=T0 + timedelta(minutes=90), purpose="generation", step="analyse",
            provider="anthropic", model="claude-sonnet-5-5", input_tokens=1,
            cache_write_tokens=0, cache_write_1h_tokens=0, cache_read_tokens=0, output_tokens=1,
            cost_usd=0.0, priced_on="2026-11-24", generation_run_id=history["analyse"],
        ))  # fmt: skip
    backfill(settings)
    added = [r for r in rows(settings) if r.backfill_key is not None]
    keys = {r.backfill_key for r in added}
    assert not any(k.startswith(f"generation_run:{history['analyse']}:") for k in keys)
    assert f"generation_run:{history['glossary']}:1" in keys  # before the first live row
    # The graded answer (T0 + 1 h) is before it too; the tutor reply (T0 + 2 h) is not.
    assert any(k.startswith("block_response:") for k in keys)
    assert f"chat_message:{history['reply']}" not in keys
    # The command-line records were written just now, after the live row.
    assert not any(k.startswith("file:") for k in keys)


def test_cli_backfill(
    settings: Settings, history: dict[str, int], settings_env: Any, capsys
) -> None:
    settings_env(settings)
    assert main(["costs", "backfill", "--dry-run"]) == 0
    assert "Would add 13 ledger row(s)" in capsys.readouterr().out
    assert rows(settings) == []
    assert main(["costs", "backfill"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("Added 13 ledger row(s): 9 exact, 3 estimate (upper bound), 1 with "
                          "no known price (claude-unknown-9)")
    assert "  tutor replies: 1" in out
    assert main(["costs", "backfill"]) == 0
    assert capsys.readouterr().out.startswith("Added 0 ledger row(s)")
