// The home page's search box (GET /api/search): results appear under the box as the reader
// types (debounced; a newer query aborts the request of the older one) and take the place of
// the rest of the page while a query is in the box. "/" focuses the box from anywhere on the
// page, the arrow keys move between the box and the results, Escape clears the box. The query
// is kept in the address (/?q=...), so a search can be linked to and survives a reload.
// Result text comes as segments ({text, mark}) and is drawn as text nodes and <mark> elements,
// never as HTML.

import { ApiError, detailText } from "./api.js";
import { byId, h, setChildren, setError } from "./dom.js";
import {
  DEBOUNCE_MS,
  checkSegments,
  homeAddress,
  kindLabel,
  matchLabel,
  metaText,
  moveIndex,
  queryState,
  resultsStatus,
  searchUrl,
  studyText,
} from "./search_model.js";

const form = byId("home-search");
const input = byId("home-search-input");
const resultsEl = byId("home-results");
const statusEl = byId("home-results-status");
const errorEl = byId("home-results-error");
const listEl = byId("home-results-list");
const dashboard = byId("home-dashboard");

let timer = null;
let controller = null; // the request in flight
let shownQuery = null; // the query whose results are on the page

// -- the request --------------------------------------------------------------------------------

async function fetchResults(text, signal) {
  let response;
  try {
    response = await fetch(searchUrl(text), { headers: { Accept: "application/json" }, signal });
  } catch (err) {
    if (err.name === "AbortError") throw err;
    throw new ApiError(`Could not reach the server (${err.message}). Check that it is still running, then try again.`);
  }
  const body = await response.text();
  let data = null;
  try {
    data = body ? JSON.parse(body) : null;
  } catch {
    data = null;
  }
  if (!response.ok) {
    throw new ApiError(detailText(data, body ? body.slice(0, 300) : response.statusText || "request failed"), { status: response.status });
  }
  if (!data || !Array.isArray(data.results)) {
    throw new ApiError("Unexpected response from the search: no list of results.", { status: response.status });
  }
  return data;
}

// -- drawing ------------------------------------------------------------------------------------

function marked(segments) {
  return checkSegments(segments).map((s) => (s.mark ? h("mark", {}, s.text) : s.text));
}

function resultItem(r) {
  const meta = metaText(r);
  return h(
    "li",
    {},
    h(
      "a",
      { class: "result", href: r.match.href || r.url, dataset: { kind: r.kind } },
      h(
        "span",
        { class: "result-head" },
        h("span", { class: `badge badge-kind kind-${r.kind}` }, kindLabel(r.kind)),
        r.superseded ? h("span", { class: "badge" }, "Older version") : null,
        h("span", { class: `result-study study-${r.study.state}` }, studyText(r))
      ),
      h("span", { class: "result-title" }, marked(r.title_segments)),
      meta ? h("span", { class: "result-meta" }, meta) : null,
      h("span", { class: "result-where" }, matchLabel(r.match)),
      r.snippet.length ? h("span", { class: "result-snippet" }, marked(r.snippet)) : null
    )
  );
}

function showDashboard(show) {
  dashboard.hidden = !show;
  resultsEl.hidden = show;
}

function clearResults() {
  shownQuery = null;
  listEl.replaceChildren();
  statusEl.textContent = "";
  setError(errorEl, null);
}

// -- searching ----------------------------------------------------------------------------------

async function run(text) {
  clearTimeout(timer);
  timer = null;
  if (controller) controller.abort();
  controller = null;
  window.history.replaceState(null, "", homeAddress(text));
  const state = queryState(text);
  if (!state.ready) {
    clearResults();
    if (state.reason) {
      // Not searched yet: say what is missing, and keep the rest of the page.
      resultsEl.hidden = false;
      dashboard.hidden = false;
      statusEl.textContent = state.reason;
    } else {
      showDashboard(true);
    }
    return;
  }
  showDashboard(false);
  const mine = new AbortController();
  controller = mine;
  listEl.setAttribute("aria-busy", "true");
  statusEl.textContent = "Searching...";
  setError(errorEl, null);
  let data;
  try {
    data = await fetchResults(text, mine.signal);
  } catch (err) {
    if (err.name === "AbortError") return; // a newer query took over
    if (controller !== mine) return;
    controller = null;
    listEl.setAttribute("aria-busy", "false");
    listEl.replaceChildren();
    shownQuery = null;
    statusEl.textContent = "";
    setError(errorEl, `The search failed: ${err instanceof ApiError && err.status ? `${err.message} (HTTP ${err.status})` : err.message}`);
    return;
  }
  if (controller !== mine) return;
  controller = null;
  listEl.setAttribute("aria-busy", "false");
  let items;
  try {
    items = data.results.map(resultItem);
  } catch (err) {
    listEl.replaceChildren();
    statusEl.textContent = "";
    setError(errorEl, `The search results could not be shown: ${err.message}`);
    return;
  }
  setChildren(listEl, items);
  statusEl.textContent = resultsStatus(data);
  shownQuery = text;
}

function schedule() {
  clearTimeout(timer);
  const text = input.value;
  timer = setTimeout(() => run(text), DEBOUNCE_MS);
}

function clearBox() {
  input.value = "";
  run("");
  input.focus();
}

// -- keyboard -----------------------------------------------------------------------------------

const links = () => [...listEl.querySelectorAll("a.result")];

function focusResult(index) {
  const all = links();
  if (index < 0 || !all.length) {
    input.focus();
    return;
  }
  all[index].focus();
}

input.addEventListener("input", schedule);
input.addEventListener("search", () => {
  // The clear button of a search field (and Enter in some browsers).
  if (!input.value) run("");
});
input.addEventListener("keydown", (event) => {
  if (event.key === "ArrowDown" && links().length) {
    event.preventDefault();
    focusResult(0);
  } else if (event.key === "Escape" && input.value) {
    event.preventDefault();
    clearBox();
  }
});

form.addEventListener("submit", (event) => {
  event.preventDefault();
  if (shownQuery !== null && shownQuery === input.value && links().length) {
    links()[0].click(); // Enter on results already shown opens the best one
    return;
  }
  run(input.value);
});

listEl.addEventListener("keydown", (event) => {
  const all = links();
  const current = all.indexOf(document.activeElement);
  if (current === -1) return;
  if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    event.preventDefault();
    focusResult(moveIndex(current, event.key === "ArrowDown" ? 1 : -1, all.length));
  } else if (event.key === "Home" || event.key === "End") {
    event.preventDefault();
    focusResult(event.key === "Home" ? 0 : all.length - 1);
  } else if (event.key === "Escape") {
    event.preventDefault();
    clearBox();
  }
});

function typingIn(el) {
  if (!el || el === document.body) return false;
  return el.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(el.tagName);
}

document.addEventListener("keydown", (event) => {
  if (event.key !== "/" || event.ctrlKey || event.metaKey || event.altKey) return;
  if (typingIn(event.target)) return;
  event.preventDefault();
  input.focus();
  input.select();
});

// -- start ----------------------------------------------------------------------------------------

// /?q=...: the server put the query in the box.
if (input.value) run(input.value);
