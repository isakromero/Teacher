// Run by tests/web/test_pages.py (`node sse_test.mjs`): the event-stream parser used by the
// tutor chat handles any chunking of the byte stream.

import assert from "node:assert/strict";

const STATIC = new URL("../../../src/teacher/web/static/", import.meta.url);
const { createSSEParser } = await import(new URL("js/sse.js", STATIC));

const encoder = new TextEncoder();
const bytes = (text) => encoder.encode(text);

/** Feed `chunks` (strings or byte arrays) and collect every event, including those at end(). */
function run(chunks) {
  const parser = createSSEParser();
  const events = [];
  for (const chunk of chunks) events.push(...parser.push(typeof chunk === "string" ? bytes(chunk) : chunk));
  const tail = parser.end();
  events.push(...tail.events);
  return { events: events.map(({ event, data }) => ({ event, data })), incomplete: tail.incomplete };
}

/** Every way of splitting `bytes` into two chunks gives the same events as one chunk. */
function allSplits(raw) {
  const whole = run([raw]);
  for (let i = 1; i < raw.length; i++) {
    assert.deepEqual(run([raw.slice(0, i), raw.slice(i)]), whole, `split at byte ${i}`);
  }
  // ...and so does one byte at a time.
  assert.deepEqual(run([...raw].map((b) => Uint8Array.of(b))), whole, "byte by byte");
  return whole;
}

const STREAM =
  'event: user_message\ndata: {"id": 1, "role": "user"}\n\n' +
  'event: delta\ndata: {"text": "Hel"}\n\n' +
  'event: delta\ndata: {"text": "lo"}\n\n' +
  'event: done\ndata: {"message": {"id": 2}}\n\n';

// Several events in one chunk, and the same events however the stream is chunked.
{
  const { events, incomplete } = allSplits(bytes(STREAM));
  assert.equal(incomplete, false);
  assert.deepEqual(
    events.map((e) => e.event),
    ["user_message", "delta", "delta", "done"]
  );
  assert.equal(JSON.parse(events[1].data).text + JSON.parse(events[2].data).text, "Hello");
}

// A multi-byte UTF-8 character split across chunks is decoded intact.
{
  const text = "Ünïcödé: π ≈ 3.14, 数学, emoji-free 𝔼[X]";
  const raw = bytes(`event: delta\ndata: ${JSON.stringify({ text })}\n\n`);
  const { events } = allSplits(raw);
  assert.equal(events.length, 1);
  assert.equal(JSON.parse(events[0].data).text, text);
}

// CRLF and lone CR line endings, including a \r\n pair split between chunks.
{
  const crlf = STREAM.replace(/\n/g, "\r\n");
  const expected = run([STREAM]).events;
  assert.deepEqual(allSplits(bytes(crlf)).events, expected);
  assert.deepEqual(allSplits(bytes(STREAM.replace(/\n/g, "\r"))).events, expected);
}

// Multi-line data joins with \n; comments are ignored; one space after the colon is removed.
{
  const { events } = run([": keep-alive\n", "event: delta\ndata: line one\ndata:line two\ndata:  indented\n\n"]);
  assert.deepEqual(events, [{ event: "delta", data: "line one\nline two\n indented" }]);
}

// Unknown event names and fields pass through without crashing; the caller ignores them.
{
  const { events } = run(["event: ping\nretry: 1000\nid: 7\nfoo: bar\ndata: {}\n\nevent: delta\ndata: {\"text\":\"x\"}\n\n"]);
  assert.deepEqual(
    events.map((e) => e.event),
    ["ping", "delta"]
  );
}

// No event name -> "message"; a blank line without data dispatches nothing.
{
  const { events } = run(["\n\nevent: delta\n\ndata: plain\n\n"]);
  assert.deepEqual(events, [{ event: "message", data: "plain" }]);
}

// An event without its terminating blank line is not dispatched, and end() reports it.
{
  const { events, incomplete } = run(['event: delta\ndata: {"text": "a"}\n\nevent: done\ndata: {"mess']);
  assert.equal(events.length, 1);
  assert.equal(incomplete, true);
  const clean = run(['event: delta\ndata: {"text": "a"}\n\n']);
  assert.equal(clean.incomplete, false);
}

// Strings can be pushed too (already decoded text).
{
  const parser = createSSEParser();
  assert.deepEqual(parser.push("event: done\ndata: {}\n"), []);
  assert.deepEqual(parser.push("\n"), [{ event: "done", data: "{}", id: "" }]);
}

console.log("sse ok");
