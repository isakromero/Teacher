// Pure helpers of the gate blocks (no DOM): the confidence levels, how a graded attempt is
// shown (verdict, what a partly right answer missed, the note on a confident error), each
// option's explanation once a multiple-choice question is cleared, and how a prediction turned
// out. Tested by tests/web/js/gate_test.mjs.

/** The reader's confidence before submitting: the API's values and the words the page shows. */
export const CONFIDENCE_LEVELS = Object.freeze([
  Object.freeze({ value: "low", label: "Guessing" }),
  Object.freeze({ value: "medium", label: "Fairly sure" }),
  Object.freeze({ value: "high", label: "Certain" }),
]);

export const CONFIDENT_ERROR_NOTE =
  "You said you were certain, so this question will come first when you review.";

const VERDICTS = new Set(["passed", "partial", "failed"]);

/**
 * How one graded attempt (a SubmitOut or ResponseOut) is shown: `{tone, heading, missing,
 * note}`. `tone` is "passed", "partial", "failed" or "neutral" (a prediction: never right or
 * wrong); `missing` the criteria a partly right answer missed (word for word, as the server
 * sent them); `note` the confident-error note or null. `historic` is for the last attempt
 * loaded with the page. A result without a known verdict is an error, not a guess.
 */
export function outcomeView(result, { neutral = false, historic = false } = {}) {
  if (!result || !VERDICTS.has(result.verdict)) {
    throw new Error(`The server sent an answer without a known verdict (${JSON.stringify(result && result.verdict)}).`);
  }
  if (neutral) {
    return { tone: "neutral", heading: historic ? "Your prediction is recorded" : "Prediction recorded", missing: [], note: null };
  }
  const missing = Array.isArray(result.missing) ? result.missing.map(String) : [];
  if (result.verdict === "partial" && !missing.length) {
    throw new Error("The server sent a partly right answer without saying what is missing.");
  }
  const headings = { passed: "Correct", partial: "Partly right", failed: "Not yet" };
  const heading = historic ? `Your last attempt: ${headings[result.verdict].toLowerCase()}` : headings[result.verdict];
  const note = result.verdict !== "passed" && result.confidence === "high" ? CONFIDENT_ERROR_NOTE : null;
  return { tone: result.verdict, heading, missing: result.verdict === "partial" ? missing : [], note };
}

/**
 * A cleared multiple-choice question's options for review: `[{index, text, explanation,
 * correct, chosen}]` in the order shown. `explanation` is the option's entry of
 * `revealed.option_explanations`, or null on a page generated before those existed.
 */
export function optionReview(options, revealed, chosen = null) {
  const explanations = revealed && Array.isArray(revealed.option_explanations) ? revealed.option_explanations : null;
  if (explanations && explanations.length !== options.length) {
    throw new Error(`The answer has ${explanations.length} option explanations for ${options.length} options.`);
  }
  const correct = revealed ? revealed.correct_index : null;
  return options.map((text, index) => ({
    index,
    text: String(text),
    explanation: explanations ? String(explanations[index]) : null,
    correct: index === correct,
    chosen: index === chosen,
  }));
}

/**
 * How a prediction turned out, for the "Check your prediction" disclosure: `{guess, actual,
 * matched, resolution}` (option texts; `actual` is the option that matches what holds).
 * Null before the prediction is resolved (no `revealed` yet) or without a recorded guess.
 */
export function predictionOutcome(options, revealed, response) {
  if (!revealed || !response || !Number.isInteger(response.choice)) return null;
  const choice = response.choice;
  const correct = revealed.correct_index;
  if (!Number.isInteger(correct) || options[correct] === undefined || options[choice] === undefined) {
    throw new Error("The prediction's answer does not match its options.");
  }
  return {
    guess: String(options[choice]),
    actual: String(options[correct]),
    matched: choice === correct,
    resolution: String(revealed.resolution ?? ""),
  };
}
