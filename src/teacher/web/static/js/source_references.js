// The source page's "References" section (#source-references-section): the state of the
// source's reference extraction with an extract / try-again button, the references (a matched
// one links to the library source; an unmatched one with a known address offers "Add to
// library", which opens /add with the address filled in), and the library sources citing this
// one. While an extraction runs the section polls GET /api/sources/{slug}/references. The
// wording is decided by source_references_model.js.

import { api, describeError, seg } from "./api.js";
import { byId, h, setChildren, setError } from "./dom.js";
import {
  POLL_MS,
  citedByView,
  extractionView,
  matchedCount,
  referenceView,
} from "./source_references_model.js";

const section = byId("source-references-section");
const slug = byId("source-page").dataset.slug;
const base = `/api/sources/${seg(slug)}/references`;
const statusEl = byId("source-references-status");
const button = byId("source-references-extract");
const errorEl = byId("source-references-error");
const listEl = byId("source-references-list");
const citedByEl = byId("source-cited-by");

let timer = null;

function referenceItem(ref) {
  const view = referenceView(ref);
  return h(
    "li",
    { class: "reference-item", value: String(view.position) },
    h("span", { class: "reference-title" }, view.heading),
    view.details ? h("span", { class: "reference-meta muted small" }, view.details) : null,
    view.raw ? h("span", { class: "reference-raw muted small" }, view.raw) : null,
    view.match
      ? h(
          "span",
          { class: "reference-actions small" },
          h("span", { class: "tag tag-ok" }, "In your library"),
          " ",
          h("a", { href: view.match.href }, view.match.title),
          h("span", { class: "muted" }, ` (matched ${view.match.how})`)
        )
      : null,
    view.add
      ? h(
          "span",
          { class: "reference-actions small" },
          h("a", { href: view.add.href, class: "button compact" }, "Add to library"),
          h("span", { class: "muted" }, ` from ${view.add.how}`)
        )
      : null
  );
}

function render(data) {
  const view = extractionView(data.extraction, matchedCount(data.references));
  statusEl.textContent = view.text;
  statusEl.classList.toggle("is-busy", view.busy);
  statusEl.classList.toggle("error-inline", view.tone === "error");
  section.setAttribute("aria-busy", view.busy ? "true" : "false");
  button.hidden = !view.button;
  button.disabled = false;
  if (view.button) button.textContent = view.button;

  setChildren(
    listEl,
    data.references.length ? h("ol", { class: "reference-list" }, data.references.map(referenceItem)) : null
  );

  const cited = citedByView(data.cited_by);
  setChildren(
    citedByEl,
    cited.empty
      ? h("p", { class: "muted" }, cited.empty)
      : h(
          "ul",
          { class: "concept-list" },
          cited.items.map((item) =>
            h("li", {}, h("a", { href: item.href }, item.title), item.note ? h("span", { class: "muted small" }, ` (${item.note})`) : null)
          )
        )
  );

  clearTimeout(timer);
  timer = view.busy ? setTimeout(load, POLL_MS) : null;
}

function showLoadError(err) {
  statusEl.textContent = "";
  statusEl.classList.remove("is-busy");
  section.setAttribute("aria-busy", "false");
  setError(errorEl, `Could not load the references: ${describeError(err)}`);
}

async function load() {
  let data;
  try {
    data = await api.get(base);
  } catch (err) {
    showLoadError(err);
    return;
  }
  setError(errorEl, null);
  try {
    render(data);
  } catch (err) {
    showLoadError(err);
  }
}

button.addEventListener("click", async () => {
  setError(errorEl, null);
  button.disabled = true;
  let data;
  try {
    data = await api.post(`${base}/extract`);
  } catch (err) {
    button.disabled = false;
    setError(errorEl, `Could not start extracting the references: ${describeError(err)}`);
    return;
  }
  try {
    render(data);
  } catch (err) {
    showLoadError(err);
  }
});

load();
