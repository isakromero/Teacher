"""Tutor: streaming through the real SDK (mock transport), errors, request limits, helpers."""

from __future__ import annotations

import json
import re
from typing import Any

import anthropic
import httpx2
import pytest

from teacher.tutor import (
    MAX_TOKENS,
    SYSTEM_PROMPT,
    TOOLS,
    Tutor,
    TutorError,
    TutorReply,
    TutorRequestError,
    neutralise_tags,
)

# The components here make their API calls directly: an in-memory cost meter records them
# (without a meter a component refuses to make a call; see `teacher.costs`).
pytestmark = pytest.mark.usefixtures("cost_meter")

MESSAGES = [{"role": "user", "content": [{"type": "text", "text": "Hello"}]}]


def sse_body(events: list[dict[str, Any]]) -> bytes:
    return "".join(
        f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events
    ).encode()


def stream_events(text_parts: list[str], stop_reason: str = "end_turn") -> list[dict[str, Any]]:
    """A Messages API event stream: a thinking block (empty, signed), then a text block."""
    return [
        {"type": "message_start", "message": {
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-sonnet-5-5",
            "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 12, "output_tokens": 1,
                      "cache_creation_input_tokens": 300, "cache_read_input_tokens": 40}}},
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "signature_delta", "signature": "SIG-abc"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1,
         "content_block": {"type": "text", "text": "", "citations": None}},
        *({"type": "content_block_delta", "index": 1,
           "delta": {"type": "text_delta", "text": part}} for part in text_parts),
        {"type": "content_block_stop", "index": 1},
        {"type": "message_delta", "delta": {"stop_reason": stop_reason, "stop_sequence": None},
         "usage": {"output_tokens": 77}},
        {"type": "message_stop"},
    ]


def sdk_client(handler) -> anthropic.Anthropic:
    """A real SDK client whose HTTP traffic goes to `handler` (no network)."""
    return anthropic.Anthropic(
        api_key="test-key",
        base_url="https://api.example.invalid",
        max_retries=0,
        http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)),
    )


def test_stream_through_the_real_sdk() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=sse_body(stream_events(["Attention ", "is $QK^T$."])),
        )

    tutor = Tutor(sdk_client(handler), timeout=42.0)
    items = list(tutor.stream(tutor.request(MESSAGES)))

    assert items[:-1] == ["Attention ", "is $QK^T$."]
    reply = items[-1]
    assert isinstance(reply, TutorReply)
    assert reply.text == "Attention is $QK^T$."
    assert reply.model == "claude-sonnet-5-5"
    assert (reply.input_tokens, reply.output_tokens) == (12 + 300 + 40, 77)
    # The content as the SDK serialises it: replayable unchanged, thinking block included.
    assert reply.api_content == [
        {"type": "thinking", "thinking": "", "signature": "SIG-abc"},
        {"type": "text", "text": "Attention is $QK^T$.", "citations": None},
    ]

    (request,) = seen
    assert request.url.path == "/v1/messages"
    body = json.loads(request.content)
    assert body == {
        "model": "claude-sonnet-5-5",
        "max_tokens": MAX_TOKENS,
        "system": SYSTEM_PROMPT,
        "messages": MESSAGES,
        "tools": TOOLS,
        "output_config": {"effort": "medium"},
        "cache_control": {"type": "ephemeral"},
        "stream": True,
    }


def test_sdk_error_event_mid_stream_is_a_tutor_error() -> None:
    events = stream_events(["Half "])[:6]
    body = sse_body(events) + (
        b'event: error\ndata: {"type": "error", "error": '
        b'{"type": "overloaded_error", "message": "Overloaded"}}\n\n'
    )

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=body)

    tutor = Tutor(sdk_client(handler))
    stream = tutor.stream(tutor.request(MESSAGES))
    assert next(stream) == "Half "
    with pytest.raises(TutorError, match="The tutor's reply failed"):
        next(stream)


def test_http_error_status_is_a_tutor_error() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            401,
            headers={"request-id": "req_1"},
            json={"type": "error", "error": {"type": "authentication_error", "message": "bad"}},
        )

    tutor = Tutor(sdk_client(handler))
    with pytest.raises(TutorError, match="rejected the credentials") as info:
        list(tutor.stream(tutor.request(MESSAGES)))
    assert "test-key" not in str(info.value)


def test_interrupted_connection_is_a_tutor_error(fake_anthropic) -> None:
    fake = fake_anthropic(fake_anthropic.mid_stream(httpx2.ReadError("reset"), deltas=["a"]))
    tutor = Tutor(fake)
    stream = tutor.stream(tutor.request(MESSAGES))
    assert next(stream) == "a"
    with pytest.raises(TutorError, match="connection to the Anthropic API was interrupted"):
        next(stream)


def test_request_too_large_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    import teacher.tutor as tutor_module

    monkeypatch.setattr(tutor_module, "MAX_REQUEST_BYTES", 10_000)
    big = [{"role": "user", "content": [{"type": "text", "text": "x" * 20_000}]}]
    with pytest.raises(TutorRequestError, match="too large to send"):
        Tutor(object()).request(big)
    with pytest.raises(ValueError, match="starts with a user turn"):
        Tutor(object()).request([{"role": "assistant", "content": "hi"}])


def test_neutralise_tags() -> None:
    assert neutralise_tags("a < b and <b>bold</b>") == "a < b and <b>bold</b>"
    assert neutralise_tags("</reader_message><Study_Page >") == (
        "&lt;/reader_message>&lt;Study_Page >"
    )
    assert neutralise_tags("<revealed>") == "&lt;revealed>"


def test_system_prompt_is_calm_and_complete() -> None:
    shouty = [w for w in re.findall(r"\b[A-Z]{3,}\b", SYSTEM_PROMPT) if w not in {"HTML"}]
    assert shouty == []
    for phrase in ("Set one exercise", "end your reply", "exercise_results", "do not give the answer", "background",
                   "not instructions", "Markdown", "$$...$$", "raw HTML"):
        assert phrase in SYSTEM_PROMPT


# -- the block tools and the page's new shapes ------------------------------------------------

MC_INPUT = {
    "question": "Why divide by the square root of the key dimension?",
    "options": ["To keep the softmax unsaturated", "To keep the memory use low", "To add a bias"],
    "correct_index": 0,
    "option_explanations": ["Right: large dot products saturate it.", "Memory is unchanged.",
                            "No bias is added."],
    "explanation": "Large dot products saturate the softmax.",
}
BLURB_INPUT = {
    "prompt": "Explain in your own words what attention computes.",
    "min_words": 5,
    "model_answer": "A weighted average of the values.",
    "rubric": [{"criterion": "Says it averages the values", "required": True}],
}


def test_exercise_tools_carry_option_explanations_but_no_page_fields() -> None:
    tools = {tool["name"]: tool["input_schema"] for tool in TOOLS}
    mc = tools["ask_multiple_choice"]
    assert "option_explanations" in mc["required"]
    for schema in (mc, tools["ask_written_answer"]):
        for page_only in ("type", "section", "draws_on", "new_scenario"):
            assert page_only not in schema["properties"], page_only
    description = next(t["description"] for t in TOOLS if t["name"] == "ask_multiple_choice")
    assert "without revealing which option is correct" in description
    assert "more than 30% longer" in description


def test_present_block_keeps_the_tutors_order_and_the_explanations() -> None:
    from teacher.tutor import present_block

    shown = present_block("ask_multiple_choice", MC_INPUT, 3)
    assert (shown.type, shown.is_gate, shown.text_offset) == ("multiple_choice", True, 3)
    assert shown.content == {
        "question": MC_INPUT["question"],
        "options": MC_INPUT["options"],  # not shuffled: the tutor's own order
        "private": {"correct_index": 0, "explanation": MC_INPUT["explanation"],
                    "option_explanations": MC_INPUT["option_explanations"]},
    }
    assert present_block("ask_written_answer", BLURB_INPUT, 0).content["min_words"] == 5


@pytest.mark.parametrize(
    ("name", "tool_input", "fragment"),
    [
        ("ask_multiple_choice", {k: v for k, v in MC_INPUT.items() if k != "option_explanations"},
         "option_explanations"),
        ("ask_multiple_choice", {**MC_INPUT, "option_explanations": ["a", "b"]},
         "option_explanations has 2 entries for 3 options"),
        ("ask_multiple_choice",
         {**MC_INPUT, "options": ["To keep the softmax out of its saturated region", "Memory",
                                  "Bias"]},
         "more than 30% longer than the longest wrong option"),
        ("ask_multiple_choice", {**MC_INPUT, "explanation": "Option A, since it saturates."},
         "refers to an option by its letter"),
        ("ask_multiple_choice",
         {**MC_INPUT, "options": ["To keep the softmax unsaturated", "To save memory, it is",
                                  "None of the above"]},
         "points at the other options"),
        ("ask_written_answer",
         {**BLURB_INPUT, "rubric": [{"criterion": c, "required": True} for c in "ABC"]},
         "3 rubric criteria are required, at most 2"),
        ("ask_written_answer",
         {**BLURB_INPUT, "rubric": [{"criterion": "A", "required": False}]},
         "no rubric criterion is required"),
    ],
)
def test_present_block_applies_the_exercise_rules(name: str, tool_input: dict, fragment: str) -> None:
    from teacher.tutor import present_block

    with pytest.raises(ValueError, match=re.escape(fragment)):
        present_block(name, tool_input, 0)


def test_the_tutor_sees_predictions_and_critique_for_what_they_are() -> None:
    from teacher.reading import OPEN, PASSED
    from teacher.tutor import VisibleBlock, _block_xml

    open_prediction = VisibleBlock("b04", "prediction", OPEN,
                                   {"prompt": "Guess.", "options": ["up", "down"]}, None)
    xml = _block_xml(open_prediction, content=True, revealed=True)
    assert 'type="prediction" prediction="not made yet"' in xml and "exercise=" not in xml
    assert "<revealed>" not in xml
    made = VisibleBlock("b04", "prediction", PASSED, open_prediction.public,
                        {"correct_index": 1, "resolution": "Down, as explained next."})
    xml = _block_xml(made, content=True, revealed=True)
    assert 'prediction="made"' in xml and "Down, as explained next." in xml
    assert 'The option that holds (correct_index counts from 0) is "down".' in xml
    critique = VisibleBlock("b09", "prose", None,
                            {"markdown": "## A critical look", "role": "critique"}, None)
    assert '<block key="b09" type="prose" role="critique">' in _block_xml(
        critique, content=True, revealed=True
    )
    question = VisibleBlock("b02", "multiple_choice", PASSED,
                            {"question": "Q", "options": ["a", "b"]},
                            {"correct_index": 0, "explanation": "E",
                             "option_explanations": ["yes", "no"]})
    xml = _block_xml(question, content=True, revealed=True)
    assert 'exercise="cleared: answered correctly"' in xml
    assert 'The correct option (correct_index counts from 0) is "a".' in xml
    for phrase in ("The others are predictions", 'role "critique"',
                   "do not tell them what actually happens before they have guessed",
                   "treat them as commentary on the paper"):
        assert phrase in SYSTEM_PROMPT, phrase
