// Run by tests/web/test_review_page.py (`node review_test.mjs`): the review page's DOM-free
// logic (static/js/review_model.js): the queue's address, the header line, the empty states,
// due times in words, the schedule line after a review, form labels and the bank panel rows.

import assert from "node:assert/strict";

const STATIC = new URL("../../../src/teacher/web/static/", import.meta.url);
const m = await import(new URL("js/review_model.js", STATIC));

// -- the queue's address and the cap ----------------------------------------------------------

assert.equal(
  m.queueUrl({ tz: "Europe/Stockholm", cap: 20, limit: 1 }),
  "/api/review/queue?tz=Europe%2FStockholm&cap=20&limit=1"
);
assert.equal(
  m.queueUrl({ tz: "UTC", cap: 30, limit: 0, concept: "soft max" }),
  "/api/review/queue?tz=UTC&cap=30&limit=0&concept=soft+max"
);
assert.throws(() => m.queueUrl({ tz: "" }), /time zone/);
assert.equal(m.extendCap(20), 30);
assert.equal(m.extendCap(495), 500);

// -- due times in words -----------------------------------------------------------------------

const now = "2026-11-22T12:00:00+00:00";
assert.equal(m.dueIn("2026-11-22T11:00:00+00:00", now), "now");
assert.equal(m.dueIn("2026-11-22T12:40:00+00:00", now), "in 40 minutes");
assert.equal(m.dueIn("2026-11-22T12:00:20+00:00", now), "in 1 minute");
assert.equal(m.dueIn("2026-11-22T17:00:00+00:00", now), "in 5 hours");
assert.equal(m.dueIn("2026-11-23T13:00:00+00:00", now), "tomorrow");
assert.equal(m.dueIn("2026-11-25T12:00:00+00:00", now), "in 3 days");
assert.equal(m.dueIn("2027-01-03T12:00:00+00:00", now), "in 6 weeks");
assert.throws(() => m.dueIn("soon", now), /Not a time/);

// -- the header and the empty states -----------------------------------------------------------

const queue = (extra = {}) => ({
  now, cap: 20, done_today: 0, remaining_today: 20, due: 0, items_total: 0,
  next_due_at: null, concept: null, items: [], ...extra,
});
assert.equal(m.queueSummary(queue()), "Nothing due now · 0 of today's 20 reviews done");
assert.equal(
  m.queueSummary(queue({ due: 1, items_total: 4, done_today: 3 })),
  "1 question due now · 3 of today's 20 reviews done · 4 questions in the queue"
);

assert.equal(m.emptyState(queue({ due: 1, items_total: 1, items: [{ id: 1 }] })), null);
assert.equal(m.emptyState(queue()).kind, "empty");
assert.match(m.emptyState(queue()).text, /answered it on a study page/);
assert.match(
  m.emptyState(queue({ concept: { slug: "softmax", name: "softmax" } })).text,
  /No question about softmax/
);
const waiting = m.emptyState(queue({ items_total: 2, next_due_at: "2026-11-25T12:00:00+00:00" }));
assert.equal(waiting.kind, "waiting");
assert.match(waiting.text, /^Nothing is due now\. The next question is due in 3 days \(/);
assert.equal(waiting.canExtend, false);
const capped = m.emptyState(queue({ items_total: 30, due: 12, done_today: 20, remaining_today: 0 }));
assert.deepEqual([capped.kind, capped.canExtend], ["capped", true]);
assert.match(capped.text, /today's 20 reviews\. 12 more questions are due/);
assert.equal(m.emptyState(queue({ items_total: 3, due: 1, cap: 500, done_today: 500, remaining_today: 0 })).canExtend, false);
// items but no next due time and nothing due: the server contradicts itself
assert.throws(() => m.emptyState(queue({ items_total: 2 })), /none comes next/);

// -- after a review ---------------------------------------------------------------------------

assert.equal(m.intervalText(1), "1 day");
assert.equal(m.intervalText(2.2), "2.2 days");
assert.equal(m.intervalText(6.6000001), "6.6 days");
assert.equal(m.intervalText(31.944), "5 weeks");
assert.throws(() => m.intervalText(0), /Not an interval/);
assert.equal(
  m.scheduleText({ verdict: "passed", interval_days: 6.6 }),
  "Right. This question comes back in 6.6 days, a longer gap than last time."
);
assert.match(m.scheduleText({ verdict: "partial", interval_days: 3 }), /^Partly right\. .* in 3 days, the same gap/);
assert.match(m.scheduleText({ verdict: "failed", interval_days: 1 }), /^Not this time\. .* in 1 day,/);
assert.throws(() => m.scheduleText({ verdict: "maybe", interval_days: 1 }), /known verdict/);

// -- labels -------------------------------------------------------------------------------------

assert.equal(m.formLabel({ form: "variant", variant: 2, variants_available: 2 }), "A fresh form of a question from the page (variant 2)");
assert.equal(m.formLabel({ form: "original", variant: null, variants_available: 2 }), "The question as it is on the page");
assert.match(m.formLabel({ form: "original", variant: null, variants_available: 0 }), /has no variants/);
assert.throws(() => m.formLabel({ form: "copy" }), /Unknown form/);
assert.match(m.itemNote({ confident_error: true, reviews: 3 }), /certain last time/);
assert.match(m.itemNote({ confident_error: false, reviews: 0 }), /First review/);
assert.equal(m.itemNote({ confident_error: false, reviews: 2 }), null);

// -- the bank panel -----------------------------------------------------------------------------

const page = (status, extra = {}) => ({
  artifact: { slug: `p-${status}`, title: `Page ${status}` }, status, questions: 2, variants: 4,
  questions_with_variants: 2, reason: `because ${status}`, can_build: true, cannot_build_reason: null, ...extra,
});
const rows = m.bankRows({
  pages: [
    page("built"), page("not_built", { variants: 0, questions_with_variants: 0 }), page("failed"),
    page("building"), page("built", { questions: 3, artifact: { slug: "p-partial", title: "Partial" } }),
  ],
});
assert.deepEqual(
  rows.map((r) => [r.slug, r.needsAttention, r.canBuild, r.buttonLabel, r.label]),
  [
    ["p-built", false, true, "Rebuild variants", "Variants ready"],
    ["p-not_built", true, true, "Build variants", "Variants missing: never built"],
    ["p-failed", true, true, "Rebuild variants", "Variants missing: the build failed"],
    ["p-building", false, false, "Rebuild variants", "Building variants"],
    ["p-partial", true, true, "Rebuild variants", "Some variants missing"],
  ]
);
assert.equal(rows[1].text, "because not_built");
const offline = m.bankRows({ pages: [page("not_built", { can_build: false, cannot_build_reason: "offline" })] });
assert.deepEqual([offline[0].canBuild, offline[0].cannotBuildReason], [false, "offline"]);
assert.throws(() => m.bankRows({ pages: [page("lost")] }), /Unknown review bank status/);

// -- a review suggestion's links (static/js/standing_model.js) -----------------------------------

const standing = await import(new URL("js/standing_model.js", STATIC));
const links = standing.actionLinks({
  kind: "review_queue",
  text: "Review it in the review queue: 2 questions are due now (3 in all).",
  pages: [{ slug: "study-x", title: "Study X", role: "main", questions: 3, blocks: 5, superseded: false }],
  sources: [],
  review: { items: 3, due: 2, next_due_at: null, url: "/review?concept=softmax" },
});
assert.deepEqual(links.map((l) => [l.href, l.text, l.note]), [
  ["/review?concept=softmax", "Review queue", "2 due now"],
  ["/artifacts/study-x", "Study X", "3 questions"],
]);

console.log("review ok");
