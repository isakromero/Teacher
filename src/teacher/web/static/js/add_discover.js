// The add page's "Find sources" box (#add-discover): a topic and a level are posted to
// POST /api/sources/discover, and the ranked candidates are listed with their verdicts and
// reasons (wording from discover_model.js). Nothing is added here: a candidate's Add button
// fills the web address field and fetches it through the `teacher:prefill-url` event that
// add.js listens for ({ url, fetch: true }); the reader checks the details and presses "Add to
// library" as for any address.

import { api, describeError, seg } from "./api.js";
import { byId, h, setChildren, setError } from "./dom.js";
import { discoverRequest, resultView } from "./discover_model.js";

const form = byId("discover-form");
const topicInput = byId("discover-topic");
const levelSelect = byId("discover-level");
const submitButton = byId("discover-submit");
const statusText = byId("discover-status");
const errorEl = byId("discover-error");
const resultsEl = byId("discover-results");

let searching = false;

function setBusy(busy) {
  searching = busy;
  submitButton.disabled = busy;
  topicInput.disabled = busy;
  levelSelect.disabled = busy;
  form.setAttribute("aria-busy", busy ? "true" : "false");
  statusText.textContent = busy ? "Searching the web and arXiv, then checking each result: this takes a minute or two..." : "";
  statusText.classList.toggle("is-busy", busy);
}

function addCandidate(url) {
  document.dispatchEvent(new CustomEvent("teacher:prefill-url", { detail: { url, fetch: true } }));
}

function item(view) {
  return h(
    "li",
    { class: "discover-item", dataset: { candidate: view.id } },
    h(
      "div",
      { class: "discover-head" },
      h("a", { class: "discover-title", href: view.url, target: "_blank", rel: "noopener noreferrer" }, view.title),
      view.score ? h("span", { class: "discover-score", title: "Overall score from triage" }, view.score) : null
    ),
    h("span", { class: "discover-meta muted small" }, view.url),
    view.landing ? h("span", { class: "discover-meta muted small" }, view.landing) : null,
    view.details ? h("span", { class: "discover-meta muted small" }, view.details) : null,
    h(
      "span",
      { class: "discover-verdict small" },
      h("span", { class: `tag tag-verdict tone-${view.verdict.tone}` }, view.verdict.label),
      " ",
      view.verdict.reason
    ),
    view.judgement ? h("p", { class: "discover-judgement small" }, view.judgement) : null,
    view.foundBy.length
      ? h("ul", { class: "discover-found small muted" }, view.foundBy.map((text) => h("li", {}, text)))
      : null,
    view.library.length
      ? h(
          "p",
          { class: "discover-library small" },
          "Already in your library: ",
          view.library.map((m, i) => [i ? ", " : "", h("a", { href: `/sources/${seg(m.slug)}` }, m.title)])
        )
      : null,
    h(
      "div",
      { class: "discover-actions" },
      h("button", { type: "button", class: "button compact", onClick: () => addCandidate(view.url) }, "Add"),
      h("span", { class: "muted small" }, " Fetches it above for you to check; nothing is stored yet.")
    )
  );
}

function render(result) {
  let view;
  try {
    view = resultView(result);
  } catch (err) {
    setChildren(resultsEl);
    setError(errorEl, `The sources could not be shown: ${err.message}`);
    return;
  }
  setChildren(
    resultsEl,
    h("p", { class: "discover-summary" }, view.summary),
    h(
      "ul",
      { class: "discover-providers small" },
      view.providers.map((p) => h("li", { class: p.ok ? "muted" : "error-text", role: p.ok ? null : "alert" }, p.text))
    ),
    view.triageNote ? h("p", { class: "muted small" }, view.triageNote) : null,
    view.empty ? null : h("ol", { class: "discover-list" }, view.items.map(item)),
    view.notKept ? h("p", { class: "muted small" }, view.notKept) : null,
    h("p", { class: "muted small discover-cost" }, view.cost)
  );
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (searching) return;
  setError(errorEl, null);
  let body;
  try {
    body = discoverRequest(topicInput.value, levelSelect.value);
  } catch (err) {
    setError(errorEl, err.message);
    return;
  }
  setChildren(resultsEl);
  setBusy(true);
  let result;
  try {
    result = await api.post("/api/sources/discover", body);
  } catch (err) {
    setBusy(false);
    setError(errorEl, `No sources could be found: ${describeError(err)}`);
    return;
  }
  setBusy(false);
  render(result);
});
