// One exploration's roadmap (#roadmap-page): GET /api/explorations/{id}/roadmap, drawn as the
// overall progress, the one next action and the steps in order; polled every POLL_MS while a
// page of it is being generated or the exploration is being enriched. Per concept: its standing, grounding, primary source (another of
// its grounding sources can be chosen: PATCH .../roadmap/concepts/{id} {primary_source}), its
// study page with its progress, or "Generate page" (POST .../roadmap/concepts/{id}/page, the
// same generation as the source page's button, with the model chosen here), and "I know this"
// (PATCH ... {marked_known}). Each change is one click; the page is drawn again from the reply.
// Wording comes from roadmap_model.js.

import { api, describeError, seg } from "./api.js";
import { byId, h, setChildren, setError } from "./dom.js";
import { chosenModel, mountModelChoice } from "./model_choice.js";
import { POLL_MS, knownBody, roadmapView, sourceBody } from "./roadmap_model.js";

const page = byId("roadmap-page");
const id = Number(page.dataset.exploration);
const base = `/api/explorations/${id}/roadmap`;
const meta = byId("roadmap-meta");
const errorEl = byId("roadmap-error");
const enrichingEl = byId("roadmap-enriching");
const summary = byId("roadmap-summary");
const progressEl = byId("roadmap-progress");
const progressFill = byId("roadmap-progress-fill");
const nextEl = byId("roadmap-next");
const actionError = byId("roadmap-action-error");
const outline = byId("roadmap-outline");
const rulesEl = byId("roadmap-rules");
const stepsEl = byId("roadmap-steps");
const droppedEl = byId("roadmap-dropped");
const loading = byId("roadmap-loading");

let timer = null;
let busy = false;

mountModelChoice("roadmap-model", { errorEl: actionError });

async function act(method, path, body, what) {
  if (busy) return;
  busy = true;
  setError(actionError, null);
  try {
    const reply = await api[method](`${base}${path}`, body);
    render(reply.roadmap || reply);
  } catch (err) {
    setError(actionError, `${what}: ${describeError(err)}`);
  } finally {
    busy = false;
  }
}

// One click starts one generation; on a failure the button comes back (the error is shown).
async function generate(conceptId, button) {
  if (busy) return;
  const label = button.textContent;
  button.disabled = true;
  button.textContent = "Starting...";
  await act("post", `/concepts/${seg(conceptId)}/page`, chosenModel("roadmap-model"), "The study page could not be started");
  if (button.isConnected) {
    button.disabled = false;
    button.textContent = label;
  }
}

function renderNext(next) {
  let control = null;
  if (next.generateFor !== null) {
    const button = h("button", { type: "button", class: "button primary", onClick: () => generate(next.generateFor, button) }, next.label);
    control = button;
  } else if (next.href && next.label) {
    control = h("a", { class: "button primary", href: next.href }, next.label);
  }
  setChildren(nextEl, h("p", { class: next.done ? "roadmap-next-text is-done" : "roadmap-next-text" }, next.text), control ? h("p", {}, control) : null);
}

function sourcePicker(concept) {
  if (!concept.choices.length) return null;
  const select = h(
    "select",
    { "aria-label": `Source to study ${concept.name} from` },
    concept.choices.map((s) => h("option", { value: s.slug, selected: s.chosen || (!concept.chosen && s.recommended) }, `${s.title}${s.recommended ? " (recommended)" : ""}`))
  );
  return h(
    "div",
    { class: "roadmap-source-picker small" },
    h("label", {}, "Study it from ", select),
    " ",
    h("button", { type: "button", class: "button quiet compact",
      onClick: () => act("patch", `/concepts/${seg(concept.id)}`, sourceBody(select.value), "The source was not changed") }, "Use this source"),
    concept.chosen
      ? h("button", { type: "button", class: "button quiet compact",
          onClick: () => act("patch", `/concepts/${seg(concept.id)}`, sourceBody(null), "The source was not changed") }, "Back to the recommended one")
      : null
  );
}

function conceptItem(concept) {
  const known = h("input", {
    type: "checkbox",
    checked: concept.markedKnown,
    onChange: (event) => act("patch", `/concepts/${seg(concept.id)}`, knownBody(event.target.checked), "Not saved"),
  });
  let pageSlot;
  if (concept.page) {
    pageSlot = h("p", { class: "small" }, "Study page: ",
      h("a", { href: concept.page.href }, concept.page.title), " ",
      h("span", { class: `tag tag-verdict tone-${concept.page.tone}` }, concept.page.label));
  } else if (concept.canGenerate) {
    const button = h("button", { type: "button", class: "button compact", onClick: () => generate(concept.id, button) }, "Generate page");
    pageSlot = h("p", { class: "small" }, `No study page of “${concept.primarySource.title}” yet. `, button);
  } else if (concept.ungrounded) {
    pageSlot = h("p", { class: "small notice-warn" }, concept.ungrounded.text, " ", h("a", { href: concept.ungrounded.href }, "Go to the review page"), ".");
  } else {
    pageSlot = null;
  }
  return h(
    "li",
    { id: concept.anchor, class: `card explore-concept roadmap-concept${concept.done ? " is-done" : ""}`, dataset: { concept: concept.key } },
    h("div", { class: "explore-concept-head" },
      h("h4", {}, concept.name, concept.done ? h("span", { class: "tag tag-ok" }, "Done") : null),
      h("label", { class: "roadmap-known small" }, known, " I know this")),
    h("p", { class: "small" }, concept.summary),
    concept.prerequisites ? h("p", { class: "muted small" }, concept.prerequisites) : null,
    concept.doneText ? h("p", { class: "small" }, concept.doneText) : null,
    h("p", { class: "small" }, concept.standing, concept.graphHref ? [" ", h("a", { href: concept.graphHref }, "In your graph")] : null),
    concept.review ? h("p", { class: "small notice-warn" }, concept.review.text, " ", h("a", { href: concept.review.href }, "Review it")) : null,
    concept.primarySource
      ? h("p", { class: "small" }, concept.primarySource.chosen ? "Your chosen source: " : "Recommended source: ",
          h("a", { href: `/sources/${seg(concept.primarySource.slug)}` }, concept.primarySource.title))
      : null,
    concept.choiceProblem ? h("p", { class: "error small", role: "alert" }, concept.choiceProblem) : null,
    sourcePicker(concept),
    pageSlot,
    concept.failedPage
      ? h("p", { class: "error small" }, concept.failedPage.text, " ", h("a", { href: concept.failedPage.href }, concept.failedPage.title), ".")
      : null,
    concept.shared ? h("p", { class: "muted small" }, concept.shared) : null
  );
}

function render(reply) {
  let view;
  try {
    view = roadmapView(reply);
  } catch (err) {
    setError(errorEl, `The roadmap could not be shown: ${err.message}`);
    return;
  }
  setError(errorEl, null);
  loading.hidden = true;
  meta.textContent = view.meta;
  enrichingEl.textContent = view.enriching || "";
  enrichingEl.hidden = !view.enriching;
  summary.hidden = false;
  progressEl.textContent = view.progress;
  progressFill.style.width = `${Math.round(view.fraction * 100)}%`;
  renderNext(view.next);
  outline.hidden = false;
  setChildren(rulesEl, view.rules.map((r) => h("li", {}, r)));
  setChildren(
    stepsEl,
    view.steps.map((step) =>
      h("section", { class: `explore-step roadmap-step is-${step.state}`, dataset: { step: String(step.number) }, "aria-current": step.current ? "step" : null },
        h("div", { class: "explore-step-head" },
          h("h3", {}, `Step ${step.number}: ${step.title}`),
          h("span", { class: "small" }, h("span", { class: `tag tag-verdict tone-${step.tone}` }, step.label), " ", step.count)),
        h("ul", { class: "explore-concepts" }, step.concepts.map(conceptItem))))
  );
  droppedEl.textContent = view.dropped || "";
  droppedEl.hidden = !view.dropped;
  clearTimeout(timer);
  if (view.poll) timer = setTimeout(load, POLL_MS);
}

async function load() {
  try {
    render(await api.get(base));
  } catch (err) {
    loading.hidden = true;
    setError(errorEl, `The roadmap could not be loaded: ${describeError(err)}`);
  }
}

load();
