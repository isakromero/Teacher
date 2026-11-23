// `multiple_choice` gate: { question, options }; revealed { correct_index, explanation,
// option_explanations? }. Response: { choice: <option index> }. A wrong answer's feedback (from
// the server) says why the chosen option is wrong; once cleared, each option's explanation is
// shown where the page has them (pages generated before per-option feedback have none).

import { h } from "../dom.js";
import { renderMarkdown } from "../markdown.js";
import { renderGate } from "./gate.js";
import { optionReview } from "./gate_model.js";

function optionText(text) {
  return renderMarkdown(String(text), { inline: true });
}

const spec = {
  label: "Check your understanding",
  asksConfidence: true,

  prompt(block, id) {
    const el = renderMarkdown(block.content.question);
    el.id = id;
    el.classList.add("gate-prompt");
    return el;
  },

  createInput(block, promptId) {
    const name = `choice-${block.key}`;
    const radios = [];
    const rows = block.content.options.map((option, index) => {
      const id = `${name}-${index}`;
      const radio = h("input", { type: "radio", name, id, value: String(index) });
      radios.push(radio);
      return h("div", { class: "option" }, radio, h("label", { for: id }, optionText(option)));
    });
    const fieldset = h("fieldset", { class: "options", "aria-labelledby": promptId }, rows);
    return {
      node: fieldset,
      read() {
        const checked = radios.find((r) => r.checked);
        if (!checked) throw new Error("Choose an option first.");
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

  revealed(revealed, block) {
    const options = block.content.options;
    const correct = revealed.correct_index;
    const review = optionReview(options, revealed);
    const explained = review.some((o) => o.explanation !== null);
    return h(
      "div",
      {},
      h("p", { class: "gate-subhead" }, "Answer"),
      Number.isInteger(correct) && options[correct] !== undefined
        ? h("p", {}, "The correct answer is: ", h("strong", {}, optionText(options[correct])))
        : null,
      revealed.explanation ? renderMarkdown(revealed.explanation) : null,
      explained
        ? [
            h("p", { class: "gate-subhead" }, "Why each option is right or wrong"),
            h(
              "ul",
              { class: "option-explanations" },
              review.map((o) =>
                h(
                  "li",
                  { class: o.correct ? "is-correct" : null },
                  h("span", { class: "option-explanation-option" }, optionText(o.text)),
                  o.correct ? h("span", { class: "tag tag-ok" }, "correct") : null,
                  renderMarkdown(o.explanation)
                )
              )
            ),
          ]
        : null
    );
  },

  answer(response, revealed, block) {
    const chosen = response ? response.choice : null;
    const correct = revealed ? revealed.correct_index : null;
    return h(
      "ul",
      { class: "option-review" },
      block.content.options.map((option, index) =>
        h(
          "li",
          { class: index === chosen ? "is-chosen" : null },
          optionText(option),
          index === chosen ? h("span", { class: "tag" }, "your answer") : null,
          index === correct ? h("span", { class: "tag tag-ok" }, "correct") : null
        )
      )
    );
  },
};

export function renderMultipleChoice(block, ctx) {
  return renderGate(block, ctx, spec);
}
