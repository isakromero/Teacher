// Run by tests/web/test_history_page.py (`node history_test.mjs`): the study history's heartbeat
// logic (activity.js) and the history page's pure helpers (history_model.js).

import assert from "node:assert/strict";

const STATIC = new URL("../../../src/teacher/web/static/", import.meta.url);
const { HEARTBEAT_MS, IDLE_MS, SETTLE_MS, createHeartbeat } = await import(new URL("js/activity.js", STATIC));
const model = await import(new URL("js/history_model.js", STATIC));

const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

// -- the heartbeat ------------------------------------------------------------------------------

async function heartbeatRules() {
  let now = 0;
  const sent = [];
  const finals = [];
  const errors = [];
  let block = "b01";
  const hb = createHeartbeat({
    subject: () => ({ surface: "study_page", slug: "p", block }),
    send: async (body) => sent.push({ at: now, ...body }),
    sendFinal: async (body) => finals.push({ at: now, ...body }),
    now: () => now,
    visible: true,
    onError: (err) => errors.push(err),
  });
  assert.equal(hb.start(), true, "opening the page sends one at once");
  await settle();
  now += HEARTBEAT_MS;
  assert.equal(hb.tick(), true, "active and visible: a tick sends");
  await settle();
  block = "b02";
  now += HEARTBEAT_MS;
  hb.activity(); // the last send was HEARTBEAT_MS ago: not more, so nothing yet
  assert.equal(sent.length, 2);
  assert.equal(hb.tick(), true);
  await settle();
  assert.equal(sent[2].block, "b02", "the subject is read when sending");
  assert.equal(sent[2].resumed, undefined, "a heartbeat of continued reading is not resumed");

  // idle: no input for longer than IDLE_MS stops the ticks
  now += IDLE_MS + 1;
  assert.equal(hb.tick(), false, "idle: nothing is sent");
  now += HEARTBEAT_MS;
  assert.equal(hb.tick(), false);
  assert.equal(hb.activity(), true, "coming back sends at once");
  await settle();
  assert.equal(sent.length, 4);
  assert.equal(sent[3].resumed, true, "after the pause: the time away is not credited");

  // hidden: the last one goes out through sendFinal; then nothing, even with activity; shown
  // again: at once, resumed (the time hidden is not credited)
  now += 5_000;
  assert.equal(hb.visibility(false), true, "hiding the tab sends a last heartbeat");
  await settle();
  assert.deepEqual(finals, [{ at: now, surface: "study_page", slug: "p", block: "b02" }]);
  now += HEARTBEAT_MS;
  assert.equal(hb.tick(), false);
  assert.equal(hb.activity(), false);
  assert.equal(hb.visibility(false), false, "already hidden: nothing more");
  now += 2_000;
  assert.equal(hb.visibility(true), true, "shown again: at once");
  await settle();
  assert.equal(sent.length, 5);
  assert.equal(sent[4].resumed, true);
  now += HEARTBEAT_MS;
  assert.equal(hb.tick(), true);
  await settle();
  assert.equal(sent[5].resumed, undefined);

  // closing the page: one last heartbeat (pagehide after visibilitychange sends no second one)
  now += 3_000;
  assert.equal(hb.leave(), true);
  assert.equal(hb.visibility(false), false);
  assert.equal(hb.leave(), false);
  await settle();
  assert.equal(finals.length, 2);
  assert.deepEqual(errors, []);
}

async function heartbeatWhenTheBlockInViewChanges() {
  let now = 0;
  let block = "b01";
  let release = null;
  const sent = [];
  const hb = createHeartbeat({
    subject: () => ({ surface: "study_page", slug: "p", block }),
    send: (body) => {
      sent.push({ at: now, ...body });
      return release === null ? Promise.resolve() : new Promise((resolve) => (release = resolve));
    },
    sendFinal: async () => assert.fail("not hidden"),
    now: () => now,
    onError: (err) => assert.fail(err),
  });
  hb.start();
  await settle();
  now += SETTLE_MS;
  assert.equal(hb.settled(), false, "scrolled within the same block: nothing");
  block = "b02";
  assert.equal(hb.settled(), true, "a new block in view: at once, without waiting for the tick");
  await settle();
  assert.deepEqual(sent.map((b) => b.block), ["b01", "b02"]);
  assert.equal(hb.settled(), false, "already sent");

  // a change while a heartbeat is in flight is sent once it has answered
  release = () => {};
  now += HEARTBEAT_MS;
  assert.equal(hb.tick(), true);
  await settle();
  block = "b03";
  assert.equal(hb.settled(), false, "one in flight");
  const pending = release;
  release = null;
  pending();
  await settle();
  await settle();
  assert.deepEqual(sent.map((b) => b.block), ["b01", "b02", "b02", "b03"]);

  // idle: a change on screen without the reader (a block unlocking, a resize) sends nothing
  now += IDLE_MS + 1;
  block = "b04";
  assert.equal(hb.settled(), false);
  assert.equal(sent.length, 4);
}

async function heartbeatStopsOnTheFirstError() {
  let now = 0;
  let calls = 0;
  const errors = [];
  const hb = createHeartbeat({
    subject: () => ({ surface: "graph" }),
    send: async () => {
      calls += 1;
      throw new Error("HTTP 422");
    },
    sendFinal: async () => assert.fail("nothing is sent once stopped"),
    now: () => now,
    onError: (err) => errors.push(err.message),
  });
  hb.start();
  await settle();
  await settle();
  assert.equal(hb.stopped, true);
  now += HEARTBEAT_MS;
  assert.equal(hb.tick(), false);
  assert.equal(hb.activity(), false);
  assert.equal(hb.settled(), false);
  assert.equal(hb.visibility(false), false);
  assert.equal(calls, 1);
  assert.deepEqual(errors, ["HTTP 422"], "the error is reported once");
}

async function oneHeartbeatInFlight() {
  let now = 0;
  let release;
  let calls = 0;
  const hb = createHeartbeat({
    subject: () => ({ surface: "graph" }),
    send: () => {
      calls += 1;
      return new Promise((resolve) => (release = resolve));
    },
    now: () => now,
    onError: () => assert.fail("no error expected"),
  });
  hb.start();
  await settle();
  now += HEARTBEAT_MS;
  assert.equal(hb.tick(), false, "the previous one has not answered yet");
  release();
  await settle();
  assert.equal(hb.tick(), true);
  await settle();
  assert.equal(calls, 2);
}

// -- the helpers ------------------------------------------------------------------------------

function helpers() {
  const { formatDuration, blockInView, timeOnPages, listItems, sessionTotals, dayRange } = model;
  assert.equal(formatDuration(0), "0 s");
  assert.equal(formatDuration(59.4), "59 s");
  assert.equal(formatDuration(125), "2 min");
  assert.equal(formatDuration(3600), "1 h");
  assert.equal(formatDuration(3900), "1 h 5 min");

  const blocks = [
    { key: "b01", top: -500, bottom: -20 },
    { key: "b02", top: -10, bottom: 300 },
    { key: "b03", top: 320, bottom: 900 },
  ];
  assert.equal(blockInView(blocks, 1000), "b03", "crossing the line at 35%");
  assert.equal(blockInView(blocks, 800), "b02");
  assert.equal(blockInView([{ key: "b01", top: 500, bottom: 600 }], 1000), "b01", "nearest visible");
  assert.equal(blockInView([{ key: "b01", top: -900, bottom: -100 }], 1000), null, "none on screen");
  assert.equal(blockInView([], 1000), null);

  const page = { slug: "p", title: "Page", kind: "study_page" };
  const lab = { slug: "l", title: "Lab", kind: "lab" };
  const labels = { b01: "Introduction", b02: "Why softmax?" };
  const span = (surface, seconds, block = null) => ({
    type: "span", surface, artifact: page, lab: surface === "lab" ? lab : null, source: null, block,
    block_label: block ? labels[block] : null, active_seconds: seconds,
  });
  const pages = timeOnPages([span("study_page", 10, "b01"), span("lab", 50), span("study_page", 5, "b02"), span("study_page", 5, "b01")]);
  assert.deepEqual(
    pages.map((p) => [p.name, p.seconds, p.blocks]),
    [
      ["Lab", 50, []],
      ["Page", 20, [{ block: "b01", label: "Introduction", seconds: 15 }, { block: "b02", label: "Why softmax?", seconds: 5 }]],
    ]
  );
  const { blockName, answerView } = model;
  assert.equal(blockName("b02", "Why softmax?"), "b02 “Why softmax?”");
  assert.equal(blockName("b02", null), "b02", "no label: the key alone");
  assert.equal(blockName(null, null), "a deleted block");
  assert.deepEqual(answerView({ form: "choice", choice: 1, text: "Normalises", note: null }), {
    form: "choice", lead: "Your answer, option 2:", text: "Normalises",
  });
  assert.equal(answerView({ form: "text", choice: null, text: "It scales.", note: null }).text, "It scales.");
  const raw = answerView({ form: "raw", choice: null, text: "{}", note: "not a choice" });
  assert.equal(raw.text, "{}");
  assert.match(raw.lead, /as stored.*not a choice/);
  assert.throws(() => answerView({ form: "drawing" }), /does not know: drawing/);

  const save = { type: "event", kind: "lab_workspace_saved", lab, artifact: page, source: null, concept: null, block: null };
  const items = listItems([
    span("lab", 1),
    { ...save, payload: { files: 1, chars: 3 } },
    { ...save, payload: { files: 1, chars: 9 } },
    { type: "lab_run", kind: "run" },
    { ...save, payload: { files: 1, chars: 4 } },
  ]);
  assert.deepEqual(items.map((i) => [i.entry.type, i.count]), [["event", 2], ["lab_run", 1], ["event", 1]]);

  assert.deepEqual(
    sessionTotals({ answers: 2, chat_answers: 1, answers_passed: 1, chat_answers_passed: 1, user_messages: 1, lab_runs: 0, lab_checks: 0, lab_checks_passed: 0 }),
    ["3 answers (2 passed)", "1 message to the tutor"]
  );
  const range = dayRange("2026-03-01", "2026-03-01");
  assert.equal(new Date(range.until) - new Date(range.since), 24 * 3600 * 1000);
  assert.deepEqual(dayRange("", ""), { since: null, until: null });
}

await heartbeatRules();
await heartbeatWhenTheBlockInViewChanges();
await heartbeatStopsOnTheFirstError();
await oneHeartbeatInFlight();
helpers();
console.log("history ok");
