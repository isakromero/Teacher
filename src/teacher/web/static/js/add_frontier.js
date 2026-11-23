// The add page's "Cited by your library" list (#add-frontier): the works the library cites but
// does not hold (GET /api/references/frontier), most cited first. "Use this address" fills the
// web address field through the `teacher:prefill-url` event that add.js listens for; the reader
// then presses Fetch. The wording and the empty states come from source_references_model.js.

import { api, describeError } from "./api.js";
import { byId, h, setChildren } from "./dom.js";
import { frontierView } from "./source_references_model.js";

const listEl = byId("add-frontier-list");

function prefill(url) {
  document.dispatchEvent(new CustomEvent("teacher:prefill-url", { detail: { url } }));
}

function item(view) {
  return h(
    "li",
    { class: "frontier-item" },
    h("span", { class: "reference-title" }, view.title),
    view.details ? h("span", { class: "reference-meta muted small" }, view.details) : null,
    h("span", { class: "reference-meta muted small" }, view.cited),
    h(
      "span",
      { class: "reference-actions small" },
      h("button", { type: "button", class: "button compact", onClick: () => prefill(view.url) }, "Use this address"),
      h("span", { class: "muted" }, ` ${view.how}: ${view.url}`)
    )
  );
}

async function load() {
  let frontier;
  try {
    frontier = await api.get("/api/references/frontier?limit=50");
  } catch (err) {
    setChildren(listEl, h("p", { class: "error", role: "alert" }, `Could not load what your library cites: ${describeError(err)}`));
    return;
  }
  let view;
  try {
    view = frontierView(frontier);
  } catch (err) {
    setChildren(listEl, h("p", { class: "error", role: "alert" }, `Could not show what your library cites: ${err.message}`));
    return;
  }
  if (view.empty) {
    setChildren(listEl, h("p", { class: "empty-state" }, view.empty));
    return;
  }
  setChildren(
    listEl,
    h("ol", { class: "reference-list frontier-list" }, view.items.map(item)),
    view.note ? h("p", { class: "muted small" }, view.note) : null
  );
}

load();
