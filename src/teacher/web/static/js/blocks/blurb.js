// `blurb` gate: { prompt, min_words }; revealed { model_answer, rubric }.
// Rubric items are strings or { criterion, required } objects. Response: { text }.
// A partly right answer (verdict "partial") is shown by gate.js with the required criteria it
// missed, so the next attempt is targeted; the model answer stays hidden until cleared.

import { h } from "../dom.js";
import { renderMarkdown } from "../markdown.js";
import { renderGate } from "./gate.js";

/** Whitespace-separated words, matching the server's `len(text.split())`. */
export function countWords(text) {
  return text.split(/\s+/).filter(Boolean).length;
}

function rubricItem(item) {
  if (item && typeof item === "object") {
    return h(
      "li",
      {},
      renderMarkdown(String(item.criterion ?? ""), { inline: true }),
      item.required ? h("span", { class: "tag" }, "required") : null
    );
  }
  return h("li", {}, renderMarkdown(String(item), { inline: true }));
}

const spec = {
  label: "In your own words",
  asksConfidence: true,

  prompt(block, id) {
    const el = renderMarkdown(block.content.prompt);
    el.id = id;
    el.classList.add("gate-prompt");
    return el;
  },

  createInput(block, promptId) {
    const minWords = block.content.min_words ?? 1;
    const textareaId = `blurb-${block.key}-text`;
    const countId = `blurb-${block.key}-count`;
    const textarea = h("textarea", {
      id: textareaId,
      rows: "7",
      "aria-labelledby": promptId,
      "aria-describedby": countId,
    });
    const count = h("p", { class: "word-count", id: countId });
    const update = () => {
      const words = countWords(textarea.value);
      count.textContent = `${words} / ${minWords} words`;
      count.classList.toggle("is-enough", words >= minWords);
    };
    textarea.addEventListener("input", update);
    update();
    return {
      node: h("div", { class: "blurb-input" }, textarea, count),
      read() {
        const words = countWords(textarea.value);
        if (words < minWords) {
          throw new Error(`Write at least ${minWords} words (you have ${words}).`);
        }
        return { text: textarea.value };
      },
      fill(response) {
        if (response && typeof response.text === "string") {
          textarea.value = response.text;
          update();
        }
      },
      setDisabled(disabled) {
        textarea.readOnly = disabled;
      },
      focus() {
        textarea.focus();
      },
    };
  },

  revealed(revealed) {
    const rubric = Array.isArray(revealed.rubric) ? revealed.rubric : [];
    return h(
      "div",
      {},
      revealed.model_answer
        ? [h("p", { class: "gate-subhead" }, "Model answer"), renderMarkdown(revealed.model_answer)]
        : null,
      rubric.length
        ? [
            h("p", { class: "gate-subhead" }, "What a good answer covers"),
            h("ul", { class: "rubric" }, rubric.map(rubricItem)),
          ]
        : null
    );
  },

  answer(response) {
    const text = response && typeof response.text === "string" ? response.text : "";
    return h("div", { class: "answer-text" }, text);
  },
};

export function renderBlurb(block, ctx) {
  return renderGate(block, ctx, spec);
}
