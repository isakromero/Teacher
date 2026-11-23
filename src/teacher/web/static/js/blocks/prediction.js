// `prediction` gate: { prompt, options }; revealed { correct_index, resolution }. Response:
// { choice: <option index> }. A guess, not a test: the reader picks an option and commits
// before reading the explanation that follows; any choice clears it (the server records the
// guess). Afterwards the block says the prediction is recorded and to read on, and a collapsed
// "Check your prediction" disclosure holds the resolution and whether the guess matched.
// Nothing here is coloured as right or wrong, and the reader is never asked how sure they are.

import { h } from "../dom.js";
import { renderMarkdown } from "../markdown.js";
import { renderGate } from "./gate.js";
import { predictionOutcome } from "./gate_model.js";

function optionText(text) {
  return renderMarkdown(String(text), { inline: true });
}

function disclosure(outcome) {
  return h(
    "details",
    { class: "prediction-check" },
    h("summary", {}, "Check your prediction"),
    h(
      "div",
      { class: "prediction-check-body" },
      h(
        "p",
        {},
        outcome.matched
          ? ["Your guess matches what actually happens: ", h("strong", {}, optionText(outcome.actual)), "."]
          : ["What actually happens is closer to ", h("strong", {}, optionText(outcome.actual)), " than to your guess."]
      ),
      outcome.resolution ? renderMarkdown(outcome.resolution) : null
    )
  );
}

const spec = {
  label: "Make a prediction",
  neutral: true,
  className: "gate-prediction",
  submitLabel: "Commit my guess",
  answerHeading: "Your prediction",
  stateLabels: { open: "Not guessed yet", passed: "Prediction recorded", revealed: "Prediction recorded" },

  prompt(block, id) {
    const el = renderMarkdown(block.content.prompt);
    el.id = id;
    el.classList.add("gate-prompt");
    return el;
  },

  createInput(block, promptId) {
    const name = `prediction-${block.key}`;
    const radios = [];
    const rows = block.content.options.map((option, index) => {
      const id = `${name}-${index}`;
      const radio = h("input", { type: "radio", name, id, value: String(index) });
      radios.push(radio);
      return h("div", { class: "option" }, radio, h("label", { for: id }, optionText(option)));
    });
    const fieldset = h(
      "fieldset",
      { class: "options", "aria-labelledby": promptId, "aria-describedby": `${name}-hint` },
      h("p", { class: "prediction-hint muted", id: `${name}-hint` }, "There is no wrong answer here: make your best guess, then read on to see how it plays out."),
      rows
    );
    return {
      node: fieldset,
      read() {
        const checked = radios.find((r) => r.checked);
        if (!checked) throw new Error("Pick the option you expect first.");
        return { choice: Number(checked.value) };
      },
      fill(response) {
        const choice = response && response.choice;
        if (Number.isInteger(choice) && radios[choice]) radios[choice].checked = true;
      },
      setDisabled(disabled) {
        fieldset.disabled = disabled;
      },
      focus() {
        (radios.find((r) => r.checked) || radios[0])?.focus();
      },
    };
  },

  // The resolution belongs in the disclosure next to the guess (see `answer`), not in a box
  // of its own: nothing else is shown once the prediction is resolved.
  revealed() {
    return null;
  },

  answer(response, revealed, block) {
    const options = block.content.options;
    const chosen = response ? response.choice : null;
    const outcome = predictionOutcome(options, revealed, response);
    return h(
      "div",
      { class: "prediction-answer" },
      Number.isInteger(chosen) && options[chosen] !== undefined
        ? h("p", { class: "prediction-guess" }, optionText(options[chosen]))
        : h("p", { class: "muted" }, "(your guess could not be shown)"),
      outcome ? disclosure(outcome) : null
    );
  },
};

export function renderPrediction(block, ctx) {
  return renderGate(block, ctx, spec);
}
