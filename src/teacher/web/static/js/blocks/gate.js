// Shared behaviour of gate blocks (multiple_choice, blurb, prediction): the card, the
// confidence control, submitting an answer, feedback (passed, partly right with what is
// missing, not yet; neutral for a prediction), per-criterion grading details, the revealed
// private content, the read-only answer of a passed gate and the error shown when a submit
// fails.
//
// THE CALLER'S CONTEXT (`ctx`, the second argument of every block renderer). The gate never
// talks to the server itself, so any page can show these same question renderers and decide
// where answers go (the study page, the tutor chat, a review page):
//   ctx.askConfidence  boolean, REQUIRED (renderGate throws without it). true: a question
//                      (spec.asksConfidence: multiple_choice, blurb) shows the three-choice
//                      "How sure are you?" control (Guessing / Fairly sure / Certain) and will
//                      not submit until one is picked. false: no control (the tutor chat).
//                      A prediction never asks, whatever this says.
//   ctx.submit(block, response, meta) -> Promise<result>
//                      Called on submit. `response` is what the type's input read (`{choice}`
//                      or `{text}`); `meta` is `{confidence: "low"|"medium"|"high"}` when the
//                      control was shown, else `{}`. The study page posts
//                      `{response, ...meta}` to POST /api/artifacts/{slug}/blocks/{key}/responses.
//                      Resolve to an object shaped like that endpoint's SubmitOut: `state`
//                      ("open" | "passed" | "revealed", required), `attempt`, `verdict`
//                      ("passed" | "partial" | "failed", required), `feedback`, `missing`
//                      (strings), `confidence`, `details`, `revealed` (the private part once
//                      cleared, else null) and optionally `unlocked`. Reject with an ApiError
//                      (api.js) to show the reader why it failed; the answer stays filled in.
//   ctx.loadResponses(block) -> Promise<ResponseOut[]>
//                      Called once when `block.attempts > 0`, to show the last attempt
//                      (`response`, `verdict`, `feedback`, `missing`, `confidence`, `details`).
//   ctx.onSubmitted(block, result)  called after each successful submit (unlocking, progress).
//   ctx.baseUrl        for relative links in Markdown (prose blocks).
// ORDER: the gate shows `block.content.options` in the order given, and every index it sends
// or reads (`response.choice`, `revealed.correct_index`, `revealed.option_explanations`, a
// loaded response's `choice`) refers to that order. A caller that shows the options in another
// order (a review page) passes a `block` whose `content.options` are already reordered and
// maps indices both ways inside its own `submit` and `loadResponses`.
// `block` is one entry of GET /api/artifacts/{slug}/blocks: `key`, `type`, `content` (public),
// `state`, `attempts`, `revealed`.
//
// A gate type supplies a `spec`:
//   label                      short heading for the card
//   prompt(block, id)          -> Node   the question / prompt (given an element id to use)
//   createInput(block, promptId) -> { node, read(), fill(response), setDisabled(bool), focus() }
//                              read() returns the response dict or throws Error(message)
//   revealed(revealed, block)  -> Node | null   explanation / model answer / rubric
//   answer(response, revealed, block) -> Node   the reader's answer, read-only
// and optionally:
//   asksConfidence  true for a question (the confidence control, when ctx.askConfidence)
//   neutral         true for a prediction: neutral feedback, never right or wrong
//   className       an extra class for the card
//   stateLabels     labels per gate state, over the defaults
//   submitLabel     the submit button's text (default "Submit answer")
//   answerHeading   heading over the read-only answer (default "Your answer")

import { ApiError, describeError } from "../api.js";
import { h, plural, setError, setChildren } from "../dom.js";
import { renderMarkdown } from "../markdown.js";
import { CONFIDENCE_LEVELS, outcomeView } from "./gate_model.js";

const STATE_LABELS = {
  open: "Not answered yet",
  passed: "Passed",
  revealed: "Answer revealed",
};

/** The "How sure are you?" control: three radio buttons, none picked at first. */
function createConfidence(block) {
  const name = `confidence-${block.key}`;
  const legendId = `${name}-legend`;
  const radios = CONFIDENCE_LEVELS.map(({ value }) =>
    h("input", { type: "radio", name, id: `${name}-${value}`, value })
  );
  const fieldset = h(
    "fieldset",
    { class: "confidence", "aria-labelledby": legendId },
    h("legend", { class: "confidence-legend", id: legendId }, "How sure are you?"),
    h(
      "div",
      { class: "confidence-choices" },
      CONFIDENCE_LEVELS.map(({ value, label }, i) =>
        h("label", { class: "confidence-choice", for: `${name}-${value}` }, radios[i], label)
      )
    )
  );
  return {
    node: fieldset,
    read() {
      const checked = radios.find((r) => r.checked);
      if (!checked) throw new Error("Say how sure you are first: Guessing, Fairly sure or Certain.");
      return checked.value;
    },
    clear() {
      for (const r of radios) r.checked = false;
    },
    setDisabled(disabled) {
      fieldset.disabled = disabled;
    },
    focus() {
      radios[0].focus();
    },
  };
}

/** Strings in the revealed part (one level of lists too), to skip feedback that repeats one. */
function revealedTexts(revealed) {
  return Object.values(revealed || {}).flatMap((v) => (Array.isArray(v) ? v : [v]));
}

function submitErrorMessage(err) {
  if (!(err instanceof ApiError)) return describeError(err);
  const keep = "Your answer is still here, so you can try again.";
  switch (err.status) {
    case 0:
      return `${err.message} ${keep}`;
    case 503:
      // The API's detail already says grading is unavailable and why.
      return `${err.message} (HTTP 503). ${keep}`;
    case 409:
      return `${err.message} (HTTP 409). The page may be out of date: reload it to see the current state.`;
    case 422:
      return `Your answer was not accepted: ${err.message} (HTTP 422).`;
    default:
      return `Submitting failed: ${err.message} (HTTP ${err.status}). ${keep}`;
  }
}

function renderDetails(details) {
  const criteria = details && Array.isArray(details.criteria) ? details.criteria : [];
  if (!criteria.length) return null;
  return h(
    "div",
    { class: "criteria" },
    h("p", { class: "gate-subhead" }, "How your answer was graded"),
    h("p", { class: "muted" }, `${criteria.filter((c) => c.met).length} of ${criteria.length} criteria met`),
    h(
      "ul",
      { class: "criteria-list" },
      criteria.map((c) =>
        h(
          "li",
          { class: `criterion ${c.met ? "is-met" : "is-unmet"}` },
          h("span", { class: "criterion-verdict" }, c.met ? "Met" : "Not met"),
          h(
            "span",
            { class: "criterion-text" },
            renderMarkdown(String(c.criterion ?? ""), { inline: true }),
            c.required ? h("span", { class: "tag" }, "required") : null
          ),
          c.justification ? h("p", { class: "criterion-why" }, String(c.justification)) : null
        )
      )
    )
  );
}

export function renderGate(block, ctx, spec) {
  if (typeof ctx.askConfidence !== "boolean") {
    throw new Error("The page showing this checkpoint did not say whether to ask how sure the reader is (ctx.askConfidence).");
  }
  const ids = {
    label: `gate-${block.key}-label`,
    prompt: `gate-${block.key}-prompt`,
  };
  const input = spec.createInput(block, ids.prompt);
  const confidence = spec.asksConfidence && ctx.askConfidence ? createConfidence(block) : null;
  const stateLabels = { ...STATE_LABELS, ...(spec.stateLabels || {}) };
  const submitLabel = spec.submitLabel || "Submit answer";

  const stateBadge = h("span", { class: "gate-state" });
  const attemptsText = h("span", { class: "gate-attempts muted" });
  const submit = h("button", { type: "submit", class: "button primary" }, submitLabel);
  const form = h(
    "form",
    { class: "gate-form", novalidate: true },
    input.node,
    confidence ? confidence.node : null,
    h("div", { class: "gate-actions" }, submit, attemptsText)
  );
  const notice = h(
    "p",
    { class: "gate-notice", hidden: true },
    "Attempt limit reached: the answer is shown below. You can continue reading, or try again."
  );
  const answerBox = h("div", { class: "gate-answer", hidden: true });
  const errorBox = h("div", { class: "error gate-error", role: "alert", hidden: true });
  const feedbackBox = h("div", { class: "gate-feedback", tabindex: "-1", "aria-live": "polite", hidden: true });
  const detailsBox = h("div", { class: "gate-details", hidden: true });
  const revealedBox = h("div", { class: "gate-revealed", hidden: true });

  const card = h(
    "section",
    { class: spec.className ? `gate ${spec.className}` : "gate", "aria-labelledby": ids.label },
    h("header", { class: "gate-header" }, h("p", { class: "gate-label", id: ids.label }, spec.label), stateBadge),
    spec.prompt(block, ids.prompt),
    notice,
    answerBox,
    form,
    errorBox,
    feedbackBox,
    detailsBox,
    revealedBox
  );

  let current = { state: block.state || "open", attempts: block.attempts || 0, revealed: block.revealed };
  let busy = false;

  function paint() {
    card.dataset.state = current.state;
    stateBadge.textContent = stateLabels[current.state] || current.state;
    stateBadge.className = `gate-state gate-state-${current.state}`;
    attemptsText.textContent = current.attempts ? `${plural(current.attempts, "attempt")} so far` : "";
    notice.hidden = current.state !== "revealed";
    form.hidden = current.state === "passed";
    const revealedNode = current.revealed ? spec.revealed(current.revealed, block) : null;
    if (revealedNode) setChildren(revealedBox, revealedNode);
    revealedBox.hidden = !revealedNode;
  }

  function showFeedback(result, historic) {
    const view = outcomeView(result, { neutral: Boolean(spec.neutral), historic });
    const feedback = result.feedback;
    const duplicate = feedback && revealedTexts(current.revealed).includes(feedback);
    feedbackBox.className = `gate-feedback is-${view.tone}`;
    setChildren(
      feedbackBox,
      h("p", { class: "gate-subhead" }, view.heading),
      feedback && !duplicate ? renderMarkdown(feedback) : null,
      view.missing.length
        ? h(
            "div",
            { class: "gate-missing" },
            h("p", { class: "gate-missing-lead" }, "Still missing from your answer:"),
            h("ul", {}, view.missing.map((item) => h("li", {}, renderMarkdown(item, { inline: true }))))
          )
        : null,
      view.note ? h("p", { class: "gate-confident-note" }, view.note) : null
    );
    feedbackBox.hidden = false;
  }

  function showDetails(details) {
    const node = renderDetails(details);
    setChildren(detailsBox, ...(node ? [node] : []));
    detailsBox.hidden = !node;
  }

  function showAnswer(response) {
    setChildren(answerBox,
      h("p", { class: "gate-subhead" }, spec.answerHeading || "Your answer"),
      spec.answer(response, current.revealed, block)
    );
    answerBox.hidden = false;
  }

  function setBusy(value) {
    busy = value;
    submit.disabled = value;
    submit.textContent = value ? "Checking..." : submitLabel;
    input.setDisabled(value);
    if (confidence) confidence.setDisabled(value);
    card.setAttribute("aria-busy", value ? "true" : "false");
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (busy) return;
    let response;
    try {
      response = input.read();
    } catch (err) {
      setError(errorBox, err.message);
      input.focus();
      return;
    }
    const meta = {};
    if (confidence) {
      try {
        meta.confidence = confidence.read();
      } catch (err) {
        setError(errorBox, err.message);
        confidence.focus();
        return;
      }
    }
    setError(errorBox, null);
    setBusy(true);
    let result;
    try {
      result = await ctx.submit(block, response, meta);
    } catch (err) {
      setError(errorBox, submitErrorMessage(err));
      return;
    } finally {
      setBusy(false);
    }
    if (!result || typeof result.state !== "string") {
      setError(errorBox, "Unexpected response from the server: no gate state. Reload the page.");
      return;
    }
    current = {
      state: result.state,
      attempts: result.attempt ?? current.attempts + 1,
      revealed: result.revealed ?? current.revealed,
    };
    paint();
    try {
      showFeedback(result, false);
    } catch (err) {
      // The attempt is recorded either way: say what is wrong, then carry on unlocking.
      setError(errorBox, `Your answer was recorded, but its result cannot be shown: ${err.message} Reload the page.`);
    }
    showDetails(result.details);
    if (current.state === "passed") showAnswer(response);
    else if (confidence) confidence.clear(); // each attempt gets its own rating
    ctx.onSubmitted(block, result);
    if (form.hidden && !(result.unlocked && result.unlocked.length)) {
      feedbackBox.focus({ preventScroll: true });
    }
  });

  paint();

  if (current.attempts > 0) {
    ctx
      .loadResponses(block)
      .then((responses) => {
        const last = Array.isArray(responses) ? responses[responses.length - 1] : null;
        if (!last) return;
        if (current.state === "passed") {
          showAnswer(last.response);
        } else {
          input.fill(last.response);
        }
        showFeedback(last, true);
        showDetails(last.details);
      })
      .catch((err) => {
        setError(errorBox, `Could not load your earlier answers: ${describeError(err)}`);
      });
  }

  return card;
}
