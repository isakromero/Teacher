// The Explore page (#explore-page): the form posts a topic and a level to POST
// /api/explorations and opens the new exploration's page, where it runs in the background; the
// list shows the past explorations (GET /api/explorations), a completed one with its roadmap's
// progress and next step. Wording from explore_model.js and roadmap_model.js.

import { api, describeError } from "./api.js";
import { byId, h, setChildren, setError } from "./dom.js";
import { exploreRequest, summaryView } from "./explore_model.js";
import { roadmapSummaryView } from "./roadmap_model.js";

const form = byId("explore-form");
const topicInput = byId("explore-topic");
const levelSelect = byId("explore-level");
const submitButton = byId("explore-submit");
const statusText = byId("explore-status");
const errorEl = byId("explore-error");
const listEl = byId("explore-list");

function setBusy(busy) {
  submitButton.disabled = busy;
  topicInput.disabled = busy;
  levelSelect.disabled = busy;
  form.setAttribute("aria-busy", busy ? "true" : "false");
  statusText.textContent = busy ? "Starting..." : "";
}

function row(view) {
  return h(
    "li",
    { class: "explore-row", dataset: { exploration: String(view.id) } },
    h(
      "div",
      { class: "explore-row-head" },
      h("a", { href: view.href, class: "explore-row-title" }, view.title),
      " ",
      h("span", { class: `badge badge-${view.status === "completed" ? "succeeded" : view.status}` }, view.statusLabel)
    ),
    h("span", { class: "muted small" }, [view.level, view.counts].filter(Boolean).join(" · ")),
    view.progress ? h("span", { class: "muted small" }, view.progress) : null,
    view.error ? h("span", { class: "error-text small" }, view.error) : null,
    view.roadmap
      ? h("span", { class: "small" },
          h("a", { href: view.roadmap.href }, "Roadmap"), `: ${view.roadmap.progress}`,
          view.roadmap.step ? ` · ${view.roadmap.step}` : "",
          view.roadmap.next ? h("span", { class: "muted roadmap-line" }, `Next: ${view.roadmap.next}`) : null)
      : null
  );
}

async function loadList() {
  listEl.setAttribute("aria-busy", "true");
  let items;
  try {
    items = await api.get("/api/explorations");
  } catch (err) {
    setChildren(listEl, h("p", { class: "error", role: "alert" }, `The explorations could not be loaded: ${describeError(err)}`));
    listEl.setAttribute("aria-busy", "false");
    return;
  }
  let views;
  try {
    views = items.map((item) => ({ ...summaryView(item), roadmap: roadmapSummaryView(item.roadmap) }));
  } catch (err) {
    setChildren(listEl, h("p", { class: "error", role: "alert" }, `The explorations could not be shown: ${err.message}`));
    listEl.setAttribute("aria-busy", "false");
    return;
  }
  setChildren(
    listEl,
    views.length ? h("ul", { class: "explore-list" }, views.map(row)) : h("p", { class: "muted" }, "No explorations yet.")
  );
  listEl.setAttribute("aria-busy", "false");
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  setError(errorEl, null);
  let body;
  try {
    body = exploreRequest(topicInput.value, levelSelect.value);
  } catch (err) {
    setError(errorEl, err.message);
    return;
  }
  setBusy(true);
  let created;
  try {
    created = await api.post("/api/explorations", body);
  } catch (err) {
    setBusy(false);
    setError(errorEl, `The exploration could not be started: ${describeError(err)}`);
    return;
  }
  window.location.assign(`/explore/${created.id}`);
});

loadList();
