// Run by tests/web/test_gate_blocks.py (`node gate_test.mjs`): the gate blocks' pure helpers
// (js/blocks/gate_model.js): confidence levels, how a graded attempt is shown, each option's
// explanation after clearing, and how a prediction turned out.

import assert from "node:assert/strict";

const STATIC = new URL("../../../src/teacher/web/static/", import.meta.url);
const M = await import(new URL("js/blocks/gate_model.js", STATIC));

// -- confidence -------------------------------------------------------------------------------

assert.deepEqual(
  M.CONFIDENCE_LEVELS.map((c) => [c.value, c.label]),
  [["low", "Guessing"], ["medium", "Fairly sure"], ["high", "Certain"]]
);
assert.ok(Object.isFrozen(M.CONFIDENCE_LEVELS) && Object.isFrozen(M.CONFIDENCE_LEVELS[0]));

// -- outcomes ---------------------------------------------------------------------------------

{
  const passed = M.outcomeView({ verdict: "passed", missing: [], confidence: "high" });
  assert.deepEqual(passed, { tone: "passed", heading: "Correct", missing: [], note: null });

  const failed = M.outcomeView({ verdict: "failed", missing: [], confidence: "medium" });
  assert.deepEqual(failed, { tone: "failed", heading: "Not yet", missing: [], note: null });

  // A confident error says it comes first in review; a guess or no rating does not.
  assert.equal(M.outcomeView({ verdict: "failed", missing: [], confidence: "high" }).note, M.CONFIDENT_ERROR_NOTE);
  assert.match(M.CONFIDENT_ERROR_NOTE, /certain/i);
  assert.match(M.CONFIDENT_ERROR_NOTE, /first when you review/);
  assert.equal(M.outcomeView({ verdict: "failed", missing: [], confidence: "low" }).note, null);
  assert.equal(M.outcomeView({ verdict: "failed", missing: [], confidence: null }).note, null);

  const partial = M.outcomeView({ verdict: "partial", missing: ["names the cost"], confidence: "high" });
  assert.equal(partial.tone, "partial");
  assert.equal(partial.heading, "Partly right");
  assert.deepEqual(partial.missing, ["names the cost"]);
  assert.equal(partial.note, M.CONFIDENT_ERROR_NOTE);

  assert.equal(M.outcomeView({ verdict: "partial", missing: ["x"] }, { historic: true }).heading, "Your last attempt: partly right");
  assert.equal(M.outcomeView({ verdict: "passed", missing: [] }, { historic: true }).heading, "Your last attempt: correct");

  // A prediction is neutral: never right, wrong or confident.
  const guess = M.outcomeView({ verdict: "passed", missing: [], confidence: "high" }, { neutral: true });
  assert.deepEqual(guess, { tone: "neutral", heading: "Prediction recorded", missing: [], note: null });
  assert.equal(M.outcomeView({ verdict: "passed" }, { neutral: true, historic: true }).heading, "Your prediction is recorded");

  // Fail loudly on what the server should never send.
  assert.throws(() => M.outcomeView({ passed: true }), /without a known verdict/);
  assert.throws(() => M.outcomeView({ verdict: "maybe" }), /without a known verdict/);
  assert.throws(() => M.outcomeView(null), /without a known verdict/);
  assert.throws(() => M.outcomeView({ verdict: "partial", missing: [] }), /without saying what is missing/);
}

// -- option explanations ----------------------------------------------------------------------

{
  const options = ["alpha", "beta", "gamma"];
  const revealed = { correct_index: 1, explanation: "Beta.", option_explanations: ["A is off.", "B fits.", "C is off."] };
  assert.deepEqual(M.optionReview(options, revealed, 2), [
    { index: 0, text: "alpha", explanation: "A is off.", correct: false, chosen: false },
    { index: 1, text: "beta", explanation: "B fits.", correct: true, chosen: false },
    { index: 2, text: "gamma", explanation: "C is off.", correct: false, chosen: true },
  ]);
  // A page from before per-option feedback: no explanations, still a review.
  const old = M.optionReview(options, { correct_index: 0, explanation: "Alpha." });
  assert.deepEqual(old.map((o) => [o.explanation, o.correct]), [[null, true], [null, false], [null, false]]);
  assert.throws(() => M.optionReview(options, { correct_index: 0, option_explanations: ["a", "b"] }), /2 option explanations for 3 options/);
}

// -- predictions ------------------------------------------------------------------------------

{
  const options = ["up", "down"];
  const revealed = { correct_index: 1, resolution: "It goes **down**." };
  assert.equal(M.predictionOutcome(options, null, { choice: 0 }), null); // not resolved yet
  assert.equal(M.predictionOutcome(options, revealed, null), null);
  assert.deepEqual(M.predictionOutcome(options, revealed, { choice: 0 }), {
    guess: "up", actual: "down", matched: false, resolution: "It goes **down**.",
  });
  assert.equal(M.predictionOutcome(options, revealed, { choice: 1 }).matched, true);
  assert.throws(() => M.predictionOutcome(options, { correct_index: 5, resolution: "x" }, { choice: 0 }), /does not match its options/);
  assert.throws(() => M.predictionOutcome(options, revealed, { choice: 9 }), /does not match its options/);
}

console.log("gate ok");
