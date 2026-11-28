"""teacher.discovery.providers: the web search provider (request shape and reply parsing with a
fake Claude client) and the arXiv provider (Atom parsing and HTTP failures with a MockTransport).
No test reaches the network."""

from __future__ import annotations

from typing import Any

import anthropic
import httpx2
import pytest

from teacher.discovery import DiscoveryQuery, ProviderError
from teacher.discovery.providers import (
    ARXIV_API,
    MAX_SEARCHES,
    SUBMIT_TOOL,
    WEB_SEARCH_TOOL_TYPE,
    WEB_SYSTEM_PROMPT,
    ArxivProvider,
    WebSearchProvider,
    arxiv_search_query,
    parse_arxiv_feed,
)
from tests.discovery_fakes import paused_reply, web_reply

# Every web search call is recorded in the cost ledger through the current meter.
pytestmark = pytest.mark.usefixtures("cost_meter")

QUERY = DiscoveryQuery("Diffie-Hellman key exchange", level="intro", aliases=("DH",))
SOURCES = [
    {"url": "https://example.org/dh", "title": "DH explained", "reason": "A gentle tutorial."},
    {"url": "https://arxiv.org/abs/2101.00001", "title": "A DH survey", "reason": "Covers it all."},
]


# -- the web provider ---------------------------------------------------------------------------


def test_web_request_caps_searches_and_uses_a_strict_submit_tool(fake_anthropic: Any) -> None:
    fake = fake_anthropic(web_reply(SOURCES))
    WebSearchProvider(fake, model="claude-sonnet-5-5").search(QUERY)
    (call,) = fake.calls
    assert call["model"] == "claude-sonnet-5-5"
    search, submit = call["tools"]
    assert search == {"type": WEB_SEARCH_TOOL_TYPE, "name": "web_search", "max_uses": MAX_SEARCHES}
    assert submit["name"] == SUBMIT_TOOL and submit["strict"] is True
    assert submit["input_schema"]["additionalProperties"] is False
    assert call["tool_choice"] == {"type": "auto"}  # the generation models reject forced tool use
    assert call["system"] == WEB_SYSTEM_PROMPT
    assert "thinking" not in call  # adaptive by default
    prompt = call["messages"][0]["content"]
    assert "Diffie-Hellman key exchange" in prompt and "DH" in prompt and "intro" in prompt


def test_web_reply_becomes_ranked_hits_with_their_cost(fake_anthropic: Any) -> None:
    out = WebSearchProvider(fake_anthropic(web_reply(SOURCES, searches=3))).search(QUERY)
    assert [(h.rank, h.url, h.title, h.reason, h.provider) for h in out.hits] == [
        (1, "https://example.org/dh", "DH explained", "A gentle tutorial.", "web"),
        (2, "https://arxiv.org/abs/2101.00001", "A DH survey", "Covers it all.", "web"),
    ]
    (call,) = out.calls
    assert (call.step, call.model, call.web_search_requests) == ("web_search", "claude-sonnet-5-5", 3)
    assert call.cost_usd == pytest.approx((2000 * 2.00 + 300 * 10.00) / 1_000_000)
    assert out.warnings == ()


def test_every_web_search_call_is_in_the_ledger_with_its_searches(
    fake_anthropic: Any, cost_meter: Any
) -> None:
    fake = fake_anthropic(paused_reply(searches=3), web_reply(SOURCES, searches=2))
    WebSearchProvider(fake).search(QUERY)
    assert [(e.purpose, e.step, e.model, e.web_search_requests) for e in cost_meter.entries] == [
        ("source_discovery", "web_search", "claude-sonnet-5-5", 3),
        ("source_discovery", "web_search", "claude-sonnet-5-5", 2),
    ]
    assert cost_meter.entries[0].cost_usd == pytest.approx((2000 * 2.00 + 300 * 10.00) / 1_000_000)


def test_without_a_meter_no_web_search_is_sent(fake_anthropic: Any) -> None:
    from teacher.costs import UnmeteredCallError, current_meter, metering

    fake = fake_anthropic(web_reply(SOURCES))
    assert current_meter() is not None
    with metering(None), pytest.raises(UnmeteredCallError):  # type: ignore[arg-type]
        WebSearchProvider(fake).search(QUERY)
    assert fake.calls == []


def test_a_paused_turn_is_sent_back_and_each_call_counts(fake_anthropic: Any) -> None:
    fake = fake_anthropic(paused_reply(), web_reply(SOURCES))
    out = WebSearchProvider(fake).search(QUERY)
    assert len(out.hits) == 2
    assert len(out.calls) == 2
    second = fake.calls[1]["messages"]
    assert [m["role"] for m in second] == ["user", "assistant"]  # no "continue" message added
    # The continuation may only use the searches left (5 - 3 used).
    assert [c["tools"][0]["max_uses"] for c in fake.calls] == [MAX_SEARCHES, MAX_SEARCHES - 3]


def test_a_turn_that_stays_paused_fails_with_its_calls(fake_anthropic: Any) -> None:
    fake = fake_anthropic(*(paused_reply(searches=1) for _ in range(3)))
    with pytest.raises(ProviderError, match="still paused") as info:
        WebSearchProvider(fake).search(QUERY)
    assert len(info.value.calls) == 3


def test_a_turn_paused_with_no_searches_left_fails(fake_anthropic: Any) -> None:
    fake = fake_anthropic(paused_reply(searches=MAX_SEARCHES))
    with pytest.raises(ProviderError, match=f"using all {MAX_SEARCHES} searches") as info:
        WebSearchProvider(fake).search(QUERY)
    assert len(fake.calls) == 1 and len(info.value.calls) == 1


@pytest.mark.parametrize(
    ("reply", "message"),
    [
        (web_reply(SOURCES, submit=None, stop_reason="end_turn"), "without calling submit_sources"),
        (web_reply(SOURCES, extra_tool="fetch_page"), "unknown tool"),
        (web_reply(submit={"sources": [{"url": "https://a.org"}]}), "not in the expected format"),
        (web_reply(submit={"sources": [{"url": "javascript:alert(1)", "title": "t", "reason": "r"}]}),
         "not an http"),
        (web_reply(submit={"sources": [{"url": "https://a.org", "title": " ", "reason": "r"}]}),
         "has no title"),
        (web_reply(submit={"sources": [{"url": f"https://a.org/{n}", "title": "t", "reason": "r"}
                                       for n in range(11)]}), "at most 10"),
        (web_reply(SOURCES, stop_reason="max_tokens"), "cut off"),
        (web_reply(SOURCES, stop_reason="refusal", stop_details={"type": "refusal", "category": "cyber",
                                                                  "explanation": None}), "declined"),
    ],
)  # fmt: skip
def test_an_unusable_web_reply_fails_the_provider_loudly(
    fake_anthropic: Any, reply: Any, message: str
) -> None:
    with pytest.raises(ProviderError, match=message) as info:
        WebSearchProvider(fake_anthropic(reply)).search(QUERY)
    assert len(info.value.calls) == 1  # the call was made and its cost is kept


def test_failed_searches_are_warnings_and_fatal_only_without_sources(fake_anthropic: Any) -> None:
    out = WebSearchProvider(
        fake_anthropic(web_reply(SOURCES, search_error="max_uses_exceeded"))
    ).search(QUERY)
    assert out.warnings == ("a web search failed on the server (max_uses_exceeded)",)
    assert len(out.hits) == 2
    with pytest.raises(ProviderError, match=r"web searches failed \(unavailable\)"):
        WebSearchProvider(
            fake_anthropic(web_reply([], search_error="unavailable"))
        ).search(QUERY)


def test_an_empty_submission_is_a_result_not_an_error(fake_anthropic: Any) -> None:
    assert WebSearchProvider(fake_anthropic(web_reply([]))).search(QUERY).hits == ()


def test_an_api_error_fails_the_provider(fake_anthropic: Any) -> None:
    fake = fake_anthropic(fake_anthropic.status_error(anthropic.RateLimitError, 429))
    with pytest.raises(ProviderError, match="rate limit"):
        WebSearchProvider(fake).search(QUERY)


def test_without_credentials_the_web_provider_says_so() -> None:
    with pytest.raises(ProviderError, match="web search is unavailable: .*credentials"):
        WebSearchProvider().search(QUERY)


# -- arXiv --------------------------------------------------------------------------------------

FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <title>ArXiv Query</title>
  <entry>
    <id>http://arxiv.org/abs/1706.03762v7</id>
    <published>2017-06-12T17:57:34Z</published>
    <title>Attention Is All
      You Need</title>
    <summary>  The dominant sequence transduction models are based on recurrent networks. </summary>
    <author><name>Ashish Vaswani</name></author>
    <author><name>Noam Shazeer</name></author>
    <arxiv:doi>10.48550/arXiv.1706.03762</arxiv:doi>
    <link title="pdf" href="http://arxiv.org/pdf/1706.03762v7" rel="related"/>
  </entry>
  <entry>
    <id>http://arxiv.org/abs/hep-th/9711200v1</id>
    <published>1997-11-27T00:00:00Z</published>
    <title>The Large N Limit</title>
    <summary>An old one.</summary>
    <author><name>Juan Maldacena</name></author>
  </entry>
</feed>
"""

ERROR_FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry><id>http://arxiv.org/api/errors#incorrect_id_format</id><title>Error</title>
  <summary>incorrect id format</summary></entry>
</feed>"""


def test_arxiv_feed_is_parsed_into_hits() -> None:
    first, second = parse_arxiv_feed(FEED, label="all:x")
    assert first.url == "https://arxiv.org/abs/1706.03762"
    assert (first.arxiv_id, first.year, first.title) == ("1706.03762", 2017, "Attention Is All You Need")
    assert first.authors == ("Ashish Vaswani", "Noam Shazeer")
    assert first.doi == "10.48550/arxiv.1706.03762"
    assert first.summary == "The dominant sequence transduction models are based on recurrent networks."
    assert first.reason == "arXiv search result 1 of 2 for all:x"
    assert (second.arxiv_id, second.rank, second.doi) == ("hep-th/9711200", 2, None)
    assert parse_arxiv_feed(b'<feed xmlns="http://www.w3.org/2005/Atom"/>') == []


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (b"<html>not a feed", "not valid XML"),
        (b"<rss/>", "not an Atom feed"),
        (ERROR_FEED, "arXiv rejected the query: incorrect id format"),
        (b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>http://arxiv.org/abs/1706.03762</id>'
         b"</entry></feed>", "has no title"),
        (b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>http://example.org/x</id>'
         b"<title>T</title></entry></feed>", "not an arXiv address"),
    ],
)  # fmt: skip
def test_a_bad_arxiv_feed_is_an_error(body: bytes, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_arxiv_feed(body)


def test_the_arxiv_query_ors_the_topic_and_its_aliases() -> None:
    assert arxiv_search_query(QUERY) == 'all:"Diffie-Hellman key exchange" OR all:"DH"'
    long = DiscoveryQuery("elliptic curve discrete logarithm problem hardness")
    assert arxiv_search_query(long) == (
        "(all:elliptic AND all:curve AND all:discrete AND all:logarithm AND all:problem AND all:hardness)"
    )
    with pytest.raises(ValueError, match="no words"):
        arxiv_search_query(DiscoveryQuery("!!!"))


def _arxiv(handler: Any, **kw: Any) -> ArxivProvider:
    return ArxivProvider(httpx2.Client(transport=httpx2.MockTransport(handler)), min_interval=0, **kw)


def test_arxiv_provider_requests_the_api_and_returns_its_hits() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, content=FEED, headers={"content-type": "application/atom+xml"})

    out = _arxiv(handler, max_results=1).search(QUERY)
    (request,) = seen
    assert str(request.url).startswith(ARXIV_API)
    assert request.url.params["search_query"] == 'all:"Diffie-Hellman key exchange" OR all:"DH"'
    assert request.url.params["max_results"] == "1"
    assert request.url.params["sortBy"] == "relevance"
    assert [h.arxiv_id for h in out.hits] == ["1706.03762"]  # at most max_results
    assert out.calls == ()


@pytest.mark.parametrize(
    ("handler", "message"),
    [
        (lambda r: httpx2.Response(503, content=b"busy"), "HTTP 503"),
        (lambda r: httpx2.Response(200, content=ERROR_FEED), "rejected the query"),
        (lambda r: (_ for _ in ()).throw(httpx2.ConnectError("refused", request=r)), "could not be reached"),
        (lambda r: (_ for _ in ()).throw(httpx2.ReadTimeout("slow", request=r)), "did not answer"),
    ],
)
def test_arxiv_failures_are_provider_errors(handler: Any, message: str) -> None:
    with pytest.raises(ProviderError, match=message):
        _arxiv(handler).search(QUERY)


def test_arxiv_requests_are_spaced_out() -> None:
    now = [100.0]
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    provider = ArxivProvider(
        httpx2.Client(transport=httpx2.MockTransport(lambda r: httpx2.Response(200, content=FEED))),
        min_interval=3.0, sleep=sleep, clock=lambda: now[0],
    )  # fmt: skip
    provider.search(QUERY)
    now[0] += 1.0
    provider.search(QUERY)
    assert slept and slept[-1] == pytest.approx(2.0)
