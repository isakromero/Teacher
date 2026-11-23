// Run by tests/web/test_standing_api.py (`node standing_test.mjs`): the pure helpers behind the
// standing table, the "Suggested next" list and the graph's standing marks (standing_model.js).

import assert from "node:assert/strict";

const STATIC = new URL("../../../src/teacher/web/static/", import.meta.url);
const m = await import(new URL("js/standing_model.js", STATIC));

const standing = (slug, status, extra = {}) => ({
  concept: { id: slug.length, slug, name: slug },
  status,
  seen: false,
  questions: 0,
  questions_answered: 0,
  first_attempt_passed: 0,
  first_attempt_pass_rate: null,
  attempts: 0,
  blocks: 0,
  incidental_blocks: 0,
  last_studied_at: null,
  ...extra,
});

// labels: unknown values are errors, never guesses
assert.equal(m.statusLabel("weak"), "Weak");
assert.throws(() => m.statusLabel("great"), /unknown standing status great/);
assert.equal(m.kindLabel("fill_gap"), "Fill the gap");
assert.throws(() => m.kindLabel("later"), /unknown suggestion kind/);
assert.equal(m.rateText(null), "-");
assert.equal(m.rateText(2 / 3), "67%");

// the words for the evidence
assert.equal(m.passedText(standing("a", "unstudied")), "No question in the library tests it yet");
assert.equal(m.passedText(standing("a", "unstudied", { questions: 2 })), "No answer yet (2 questions in the library)");
assert.equal(
  m.passedText(standing("a", "weak", { questions: 3, questions_answered: 2, first_attempt_passed: 0, first_attempt_pass_rate: 0 })),
  "0 of 2 passed at the first attempt (0%)"
);
assert.match(
  m.thresholdsText({ weak_below: 0.5, strong_from: 0.8, strong_min_questions: 2 }),
  /weak below 50% .* strong from 80% with at least 2 questions answered/
);

// the table: covered or seen concepts only (unless all), weak first, then most recent
const rows = m.standingRows([
  standing("strong-one", "strong", { blocks: 1, last_studied_at: "2026-01-02T00:00:00+00:00" }),
  standing("never", "unstudied"),
  standing("weak-old", "weak", { blocks: 1, last_studied_at: "2026-01-01T00:00:00+00:00" }),
  standing("weak-new", "weak", { blocks: 1, last_studied_at: "2026-01-03T00:00:00+00:00" }),
  standing("read", "unstudied", { seen: true }),
]);
assert.deepEqual(rows.map((r) => r.concept.slug), ["weak-new", "weak-old", "strong-one", "read"]);
assert.equal(m.standingRows([standing("never", "unstudied")], { all: true }).length, 1);

// the graph's marks and legend counts
const byId = m.statusById([standing("ab", "weak"), standing("abc", "strong")]);
assert.equal(byId.get(2), "weak");
assert.equal(byId.get(3), "strong");
assert.throws(() => m.statusById([standing("x", "meh")]), /unknown standing status/);
assert.deepEqual(m.statusCounts([standing("a", "weak"), standing("b", "weak")]), [
  ["unstudied", 0],
  ["weak", 2],
  ["developing", 0],
  ["strong", 0],
]);

// what a suggestion offers
assert.deepEqual(
  m.actionLinks({
    kind: "study_page",
    pages: [
      { slug: "p 1", title: "P", role: "main", questions: 2, superseded: false },
      { slug: "p0", title: "Old", role: "incidental", questions: 0, superseded: true },
    ],
  }),
  [
    { href: "/artifacts/p%201", text: "P", note: "2 questions" },
    { href: "/artifacts/p0", text: "Old", note: "mentions it, older version" },
  ]
);
assert.deepEqual(m.actionLinks({ kind: "source", sources: [{ slug: "s", title: "S", state: "no_study_page" }] }), [
  { href: "/sources/s", text: "S", note: "no study page yet" },
]);
assert.deepEqual(m.actionLinks({ kind: "none", pages: [], sources: [] }), []);
assert.throws(() => m.actionLinks({ kind: "later" }), /unknown suggestion action/);
assert.throws(() => m.actionLinks({ kind: "source", sources: [{ slug: "s", title: "S", state: "odd" }] }), /unknown source state/);

const gap = {
  kind: "fill_gap",
  evidence: {
    status: "unstudied",
    questions: 1,
    questions_answered: 0,
    first_attempt_passed: 0,
    first_attempt_pass_rate: null,
    attempts: 0,
    supports: [{ name: "attention", first_attempt_pass_rate: 0 }],
  },
};
assert.deepEqual(m.evidenceParts(gap), ["Not studied yet: no answer yet (1 question in the library)", "needed for attention (0% first time)"]);
assert.equal(
  m.questionText({ block: "b02", block_type: "multiple_choice", role: "main", attempts: 2, first_attempt_passed: false, eventually_passed: true }),
  "b02 (multiple choice, tests it): 2 attempts, passed after a retry"
);
assert.equal(
  m.questionText({ block: "b04", block_type: "blurb", role: "incidental", attempts: 0, first_attempt_passed: null, eventually_passed: false }),
  "b04 (written answer, mentions it): not answered yet"
);

console.log("standing ok");
