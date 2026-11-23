// The concept graph page (/graph): loads the graph (or, with ?concept=<slug>, one concept and
// its neighbours), draws it with graph_view.js, and fills the inspector (graph_inspector.js).
// It reads from /api/graph (see teacher/web/routers/concepts.py); the one write is the
// inspector's "Find relations" / "Regenerate relations" (POST /api/graph/concepts/{slug}/relate,
// with the model chosen next to them), whose result is applied to the canvas without a reload:
// deleted relations disappear, new ones (and the concepts they reach) appear.
//
// State: `graph` = {nodes on the canvas, every relation the page has been told about}
// (graph_model.js); `selection` = {kind: "node" | "edge", id} or null; `roadmap` = the roadmap
// highlight ({nodes in study order, highlight}) or null; `relating` = concept id -> "find" |
// "regenerate" while that run is in progress; `relateResults` = concept id -> its last run's
// result; `confirming` = the concept whose "Regenerate relations" confirmation is open;
// `relateModels` / `relateModel` = the models offered for relating and the one chosen.
// Concept details are fetched when a concept is selected and cached per slug for the page's
// lifetime (a relate run drops the cache of every concept whose relations it changed).
//
// The reader's standing (GET /api/history/concepts) marks every node (graph_view.js
// setStanding, with legend entries); the selected concept's standing and the questions that test
// it (GET /api/history/concepts/{slug}) are shown in the inspector, fetched with its details.

import { logEvent } from "./activity.js";
import { api, describeError, seg } from "./api.js";
import { byId, h, plural, setChildren, setError } from "./dom.js";
import {
  conceptPanel,
  originChip,
  overviewPanel,
  relationPanel,
  roadmapPanel,
} from "./graph_inspector.js";
import {
  ORIGINS,
  hideNode,
  mergeGraph,
  mergeRelateResult,
  relateMessage,
  relateTouched,
  roadmapHighlight,
  searchNotes,
  semanticNotice,
  visibleEdges,
} from "./graph_model.js";
import { createGraphView } from "./graph_view.js";
import { standingSection } from "./standing.js";
import { STATUSES, statusById, statusLabel } from "./standing_model.js";
import { RELATE_MODELS_URL, loadModelChoices, modelSelect } from "./model_choice.js";

const page = byId("graph-page");
const focusSlug = page.dataset.concept || null;
const loadingEl = byId("graph-loading");
const emptyEl = byId("graph-empty");
const viewEl = byId("graph-view");
const inspector = byId("graph-inspector");
const errorEl = byId("graph-error");
const statusEl = byId("graph-status");
const noteEl = byId("graph-note");
const originLegend = byId("graph-origin-legend");
const searchForm = byId("graph-search");
const searchInput = byId("graph-search-input");
const searchStatus = byId("graph-search-status");
const searchNotice = byId("graph-search-notice");
const searchResults = byId("graph-search-results");

let graph = { nodes: [], edges: [] };
let selection = null;
let roadmap = null;
let renderToken = 0; // the latest inspector render; older async renders are dropped
const relating = new Map(); // concept id -> "find" | "regenerate" while that run is in progress
const relateResults = new Map(); // concept id -> the last relate result
const details = new Map(); // slug -> Promise of concept details
const standingDetails = new Map(); // slug -> Promise of the concept's standing and questions
let standing = new Map(); // concept id -> standing status, once loaded
let confirming = null; // concept id whose "Regenerate relations" confirmation is open
let relateModels = { state: "loading", choices: null, error: null };
let relateModel = null; // the model chosen for relating (null until the choices are loaded)

const view = createGraphView({
  svg: byId("graph-svg"),
  canvas: byId("graph-canvas"),
  buttons: { zoomIn: byId("graph-zoom-in"), zoomOut: byId("graph-zoom-out"), fit: byId("graph-fit") },
  onSelect: (next) => {
    const inspected = next?.kind === "node" && !(selection?.kind === "node" && selection.id === next.id);
    selection = next;
    if (inspected) logEvent("concept_inspected", { concept: nodeById(next.id)?.slug ?? null });
    confirming = null;
    renderInspector();
  },
  onExpand: (id) => run(() => expand(id)),
  onPinChange: (id) => {
    if (selection?.kind === "node" && selection.id === id) renderInspector();
  },
});

// -- helpers ---------------------------------------------------------------------------------

const nodeById = (id) => graph.nodes.find((n) => n.id === id) || null;

function conceptUrl(slug, part = "") {
  return `/api/graph/concepts/${seg(slug)}${part}`;
}

function conceptDetails(slug) {
  if (!details.has(slug)) {
    const pending = api.get(conceptUrl(slug));
    pending.catch(() => details.delete(slug)); // a failed fetch is retried next time
    details.set(slug, pending);
  }
  return details.get(slug);
}

/** Run an action; its error is shown above the canvas (and replaces any earlier one). */
async function run(action) {
  setError(errorEl, null);
  try {
    await action();
  } catch (err) {
    setError(errorEl, describeError(err));
  }
}

function say(message) {
  statusEl.textContent = message;
}

/** Redraw after `graph` changed; keeps the roadmap's hot edges in step with the relations and
 * drops a selection that is no longer drawn. */
function redraw(options) {
  if (selection?.kind === "node" && !nodeById(selection.id)) selection = null;
  if (selection?.kind === "edge" && !visibleEdges(graph).some((e) => e.id === selection.id)) {
    selection = null;
  }
  view.setGraph(graph, options);
  if (roadmap) {
    roadmap.highlight = roadmapHighlight(roadmap.nodes, graph.edges);
    view.setHighlight(roadmap.highlight);
  }
  renderLegend();
}

function renderLegend() {
  const present = new Set(graph.nodes.map((n) => n.origin));
  const statuses = new Set(graph.nodes.map((n) => standing.get(n.id)));
  setChildren(
    originLegend,
    ORIGINS.filter((o) => present.has(o)).map((o) => h("li", {}, h("i", { class: `origin-dot origin-${o}` }), originChip(o))),
    STATUSES.filter((st) => st !== "unstudied" && statuses.has(st)).map((st) =>
      h("li", {}, h("i", { class: `standing-dot standing-${st}` }), `You: ${statusLabel(st).toLowerCase()}`)
    )
  );
}

/** The reader's standing on every concept, drawn on the nodes; a failure is said above the
 * canvas (the graph itself still works). */
async function loadStanding() {
  try {
    const data = await api.get("/api/history/concepts");
    standing = statusById(data.concepts);
  } catch (err) {
    setError(errorEl, `Could not load your standing on the concepts: ${describeError(err)}`);
    return;
  }
  view.setStanding(standing);
  renderLegend();
}

function conceptStanding(slug) {
  if (!standingDetails.has(slug)) {
    const pending = api.get(`/api/history/concepts/${seg(slug)}`);
    pending.catch(() => standingDetails.delete(slug)); // a failed fetch is retried next time
    standingDetails.set(slug, pending);
  }
  return standingDetails.get(slug);
}

/** The inspector's standing section for `slug`, or why it could not be loaded. */
async function standingPart(slug) {
  try {
    return standingSection(await conceptStanding(slug));
  } catch (err) {
    return h("p", { class: "error small", role: "alert" }, `Could not load your standing on this concept: ${describeError(err)}`);
  }
}

/** Concept `slug` on the canvas (fetching it with its relations if it is not there yet), so its
 * relations to concepts already drawn are drawn too. Returns its node. */
async function ensureOnCanvas(slug) {
  const known = graph.nodes.find((n) => n.slug === slug);
  if (known) return known;
  const data = await api.get(conceptUrl(slug, "/neighbourhood"));
  graph = mergeGraph(graph, { nodes: [data.concept], edges: [...data.edges, ...data.neighbour_edges] });
  redraw();
  say(`Added ${data.concept.name} to the canvas.`);
  return data.concept;
}

/** Select concept `node` (adding it to the canvas if needed) and centre the view on it. */
async function focusConcept(node) {
  const shown = await ensureOnCanvas(node.slug);
  selection = { kind: "node", id: shown.id };
  view.select(selection);
  logEvent("concept_inspected", { concept: shown.slug });
  view.centreOn(shown.id);
  await renderInspector({ focus: true });
}

// -- actions ---------------------------------------------------------------------------------

async function expand(id) {
  const node = nodeById(id);
  if (!node) throw new Error(`concept #${id} is not on the canvas`);
  const data = await api.get(conceptUrl(node.slug, "/neighbourhood"));
  const before = new Set(graph.nodes.map((n) => n.id));
  graph = mergeGraph(
    graph,
    { nodes: [data.concept, ...data.nodes], edges: [...data.edges, ...data.neighbour_edges] },
    { anchor: id }
  );
  redraw({ anchor: id });
  const added = data.nodes.filter((n) => !before.has(n.id)).length;
  say(
    data.nodes.length === 0
      ? `${node.name} has no related concepts.`
      : added
        ? `Added ${plural(added, "related concept")} of ${node.name}.`
        : `All concepts related to ${node.name} are already shown.`
  );
}

function hide(id) {
  const node = nodeById(id);
  graph = hideNode(graph, id);
  redraw();
  view.select(selection);
  say(`Hid ${node ? node.name : "the concept"}. Search for it to show it again.`);
  renderInspector();
}

async function showRoadmap(id) {
  const node = nodeById(id);
  const data = await api.get(conceptUrl(node.slug, "/roadmap"));
  graph = mergeGraph(graph, { nodes: data.roadmap, edges: data.edges }, { anchor: id });
  roadmap = { nodes: data.roadmap, highlight: null };
  redraw({ anchor: id });
  say(`Showing the roadmap to ${node.name}.`);
  await renderInspector();
}

/** The request body for a relate run: `regenerate`, and the chosen model when the list of
 * models loaded (otherwise the server's default, which the inspector says). */
function relateBody(regenerate) {
  return relateModels.state === "ready" && relateModel ? { regenerate, model: relateModel } : { regenerate };
}

/** "Find relations" (or, with `regenerate`, "Regenerate relations") for concept `id`: one POST
 * (the server asks Claude and writes what it accepts), then the result is applied to the canvas:
 * deleted relations go, new relations and the concepts they reach come. */
async function relate(id, { regenerate = false } = {}) {
  const node = nodeById(id);
  if (!node) throw new Error(`concept #${id} is not on the canvas`);
  if (relating.has(id)) return;
  relating.set(id, regenerate ? "regenerate" : "find");
  say(regenerate ? `Regenerating the relations of ${node.name}...` : `Finding relations for ${node.name}...`);
  renderInspector();
  try {
    const result = await api.post(conceptUrl(node.slug, "/relate"), relateBody(regenerate));
    for (const slug of relateTouched(result)) details.delete(slug);
    // A selected relation the run deleted is gone (its id may come back for a new relation).
    if (selection?.kind === "edge" && result.deleted_edge_ids.includes(selection.id)) selection = null;
    graph = mergeRelateResult(graph, result, id);
    relateResults.set(id, result);
    redraw({ anchor: id });
    say(relateMessage(result, node.name));
  } catch (err) {
    say("");
    throw err;
  } finally {
    relating.delete(id);
    if (selection?.kind === "node" && selection.id === id) renderInspector();
  }
}

function openRegenerate(id) {
  confirming = id;
  renderInspector({ focusKey: "regenerate-confirm" });
}

function cancelRegenerate() {
  confirming = null;
  renderInspector({ focusKey: "regenerate" });
}

function confirmRegenerate(id) {
  confirming = null;
  run(() => relate(id, { regenerate: true }));
}

/** The model select for relating, or why there is none. */
function modelControl() {
  if (relateModels.state === "loading") {
    return h("p", { class: "muted small", role: "status" }, "Loading the list of models...");
  }
  if (relateModels.state === "failed") {
    return h(
      "p",
      { class: "error small", role: "alert" },
      `Could not load the list of models: ${relateModels.error}. ` +
        "Find and Regenerate use the server's default model."
    );
  }
  return modelSelect(relateModels.choices, {
    id: "relate-model",
    label: "Model for finding relations",
    value: relateModel,
    onChange: (value) => {
      relateModel = value;
    },
  });
}

function loadRelateModels() {
  loadModelChoices(RELATE_MODELS_URL).then(
    (choices) => {
      relateModels = { state: "ready", choices, error: null };
      relateModel = relateModel ?? choices.default;
      if (selection?.kind === "node") renderInspector();
    },
    (err) => {
      relateModels = { state: "failed", choices: null, error: describeError(err) };
      if (selection?.kind === "node") renderInspector();
    }
  );
}

/** Select relation `edge` (an API edge on the canvas) and show it in the inspector. */
function selectRelation(edge) {
  selection = { kind: "edge", id: edge.id };
  view.select(selection);
  renderInspector({ focus: true });
}

function hideRoadmap() {
  roadmap = null;
  view.setHighlight(null);
  say("Roadmap turned off.");
  renderInspector();
}

// -- inspector -------------------------------------------------------------------------------

/** Render the inspector for the selection. Focus: with `focusKey`, the element marked
 * `data-focus="<focusKey>"` (the confirmation's button, or the button that opened it);
 * otherwise the title when `focus` is asked for or the inspector had the focus. */
async function renderInspector({ focus = false, focusKey = null } = {}) {
  const token = ++renderToken;
  const keepFocus = focus || focusKey !== null || inspector.contains(document.activeElement);
  const top = roadmap
    ? roadmapPanel(roadmap.nodes, {
        selectedId: selection?.kind === "node" ? selection.id : null,
        onSelectConcept: (n) => run(() => focusConcept(n)),
        onClose: hideRoadmap,
      })
    : null;
  let body;
  if (!selection) {
    body = overviewPanel(graph.nodes, visibleEdges(graph));
  } else if (selection.kind === "edge") {
    const edge = graph.edges.find((e) => e.id === selection.id);
    body = relationPanel(edge, nodeById(edge.from_id), nodeById(edge.to_id), {
      onSelectConcept: (n) => run(() => focusConcept(n)),
    });
  } else {
    const node = nodeById(selection.id);
    setChildren(inspector, top, h("p", { class: "muted small", role: "status" }, `Loading ${node.name}...`));
    let concept;
    const standingPending = standingPart(node.slug);
    try {
      concept = await conceptDetails(node.slug);
    } catch (err) {
      if (token !== renderToken) return;
      setChildren(
        inspector,
        top,
        h("p", { class: "error", role: "alert" }, `Could not load ${node.name}: ${describeError(err)}`)
      );
      return;
    }
    const standingEl = await standingPending;
    if (token !== renderToken) return;
    const id = node.id;
    body = conceptPanel(concept, {
      standingSection: standingEl,
      pinned: view.isPinned(id),
      roadmapOn: Boolean(roadmap && roadmap.highlight && roadmap.highlight.target === id),
      onExpand: () => run(() => expand(id)),
      onHide: () => hide(id),
      onUnpin: () => view.unpin(id),
      onRoadmap: () =>
        roadmap && roadmap.highlight && roadmap.highlight.target === id ? hideRoadmap() : run(() => showRoadmap(id)),
      relating: relating.get(id) || null,
      relateResult: relateResults.get(id) || null,
      confirmingRegenerate: confirming === id,
      modelControl: modelControl(),
      onRelate: () => run(() => relate(id)),
      onRegenerate: () => (confirming === id ? cancelRegenerate() : openRegenerate(id)),
      onConfirmRegenerate: () => confirmRegenerate(id),
      onCancelRegenerate: cancelRegenerate,
      onSelectConcept: (n) => run(() => focusConcept(n)),
      onSelectRelation: selectRelation,
    });
  }
  setChildren(inspector, top, body);
  if (keepFocus) {
    const target =
      (focusKey && inspector.querySelector(`[data-focus="${focusKey}"]`)) ||
      inspector.querySelector(".inspector-title") ||
      inspector;
    target.focus();
  }
}

// -- search ----------------------------------------------------------------------------------

// How a result was found (graph_model.searchNotes): the lexical match, unless it is simply the
// concept's own name matching exactly, and "similar meaning 0.62" when found by meaning.
function matchNotes(result) {
  return searchNotes(result).map((note) =>
    h("span", { class: `graph-result-match match-${note.kind}` }, note.text)
  );
}

/** Show whether this search also searched meaning; a names-only search says why, every time. */
function showSearchNotice(semantic) {
  const notice = semanticNotice(semantic);
  searchNotice.className = notice.degraded ? "notice notice-warn graph-search-notice" : "muted graph-search-notice";
  searchNotice.setAttribute("role", notice.degraded ? "alert" : "status");
  searchNotice.textContent = notice.text;
  searchNotice.hidden = false;
}

function hideSearchNotice() {
  searchNotice.hidden = true;
  searchNotice.textContent = "";
}

let searchToken = 0;

function closeResults() {
  searchResults.hidden = true;
  searchResults.replaceChildren();
}

searchForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const q = searchInput.value;
  const token = ++searchToken;
  setError(errorEl, null);
  closeResults();
  hideSearchNotice();
  searchStatus.textContent = "Searching...";
  searchStatus.classList.add("is-busy");
  let data;
  try {
    data = await api.get(`/api/graph/search?q=${encodeURIComponent(q)}`);
  } catch (err) {
    if (token !== searchToken) return;
    searchStatus.classList.remove("is-busy");
    searchStatus.textContent = "";
    setError(errorEl, `Search failed: ${describeError(err)}`);
    return;
  }
  if (token !== searchToken) return;
  searchStatus.classList.remove("is-busy");
  let items;
  try {
    showSearchNotice(data.semantic);
    items = data.results.map((r) =>
      h(
        "li",
        {},
        h(
          "button",
          {
            type: "button",
            class: "graph-result",
            onClick: () => {
              closeResults();
              searchStatus.textContent = "";
              hideSearchNotice();
              run(() => focusConcept(r));
            },
          },
          h("span", { class: "graph-result-name" }, r.name, " ", originChip(r.origin)),
          matchNotes(r),
          h("span", { class: "graph-result-summary" }, r.summary)
        )
      )
    );
  } catch (err) {
    searchStatus.textContent = "";
    hideSearchNotice();
    setError(errorEl, `Could not show the search results: ${describeError(err)}`);
    return;
  }
  if (!items.length) {
    searchStatus.textContent = `No concepts match “${data.query}”.`;
    return;
  }
  searchStatus.textContent = `${plural(items.length, "concept")} found. Choose one to show it.`;
  setChildren(searchResults, items);
  searchResults.hidden = false;
});

searchForm.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !searchResults.hidden) {
    closeResults();
    searchStatus.textContent = "";
    hideSearchNotice();
    searchInput.focus();
  }
});

// -- start -----------------------------------------------------------------------------------

async function start() {
  loadRelateModels();
  if (focusSlug) {
    const data = await api.get(conceptUrl(focusSlug, "/neighbourhood"));
    graph = mergeGraph(graph, {
      nodes: [data.concept, ...data.nodes],
      edges: [...data.edges, ...data.neighbour_edges],
    });
    setChildren(
      noteEl,
      `Opened on ${data.concept.name} (${plural(data.nodes.length, "related concept")}). `,
      h("a", { href: "/graph" }, "Show the whole graph")
    );
    noteEl.hidden = false;
    selection = { kind: "node", id: data.concept.id };
  } else {
    const data = await api.get("/api/graph");
    if (data.total_nodes === 0) {
      loadingEl.hidden = true;
      emptyEl.hidden = false;
      searchForm.hidden = true;
      return;
    }
    graph = mergeGraph(graph, data);
    if (data.truncated) {
      noteEl.textContent =
        `Showing the ${plural(data.nodes.length, "most connected concept")} of ${data.total_nodes} ` +
        `(${data.edges.length} of ${plural(data.total_edges, "relation")}). ` +
        "Search, or expand a concept, to show others.";
      noteEl.hidden = false;
    }
  }
  loadingEl.hidden = true;
  viewEl.hidden = false; // shown before drawing, so the view knows its size
  redraw();
  view.select(selection);
  loadStanding();
  await renderInspector();
}

start().catch((err) => {
  loadingEl.hidden = true;
  setError(errorEl, `Could not load the concept graph: ${describeError(err)}`);
});
