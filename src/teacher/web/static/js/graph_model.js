// Pure logic of the concept graph page (no DOM, no d3), so node tests can import it:
// node captions, edge geometry (straight and curved parallel edges, labels kept upright,
// endpoints on the circles), merging what the API returns into what is drawn, the roadmap
// highlight, the inspector's relation groups, the "Find relations" / "Regenerate relations"
// result summary (and what a regenerate would replace), the search results' notes and the
// semantic-search notice, view (pan/zoom/fit) arithmetic, and which concepts get a readable
// name label at the current zoom, which concept a finger tap means, and when a touch drags.
//
// A graph here is `{nodes, edges}`: `nodes` are the concepts on the canvas (API node objects:
// {id, slug, name, origin, reviewed}); `edges` are every relation the page has been told about
// (API edge objects: {id, type, from_id, to_id, origin, rationale, reviewed, reviewed_at,
// redundant}). An edge is drawn when both its ends are on the canvas (`visibleEdges`), so hiding
// a concept and adding it back brings its relations back.

export const R = 25; // node radius, in graph units
export const ARROW_GAP = 3; // a builds_on edge stops this far outside the target circle
export const ZOOM_MIN = 0.15;
export const ZOOM_MAX = 4;
export const FIT_MARGIN = 80;
export const CAPTION_CHARS = 9;

export const ORIGINS = ["paper", "model", "user"];
export const RELATION_TYPES = ["builds_on", "different_version_of", "relates_to"];
export const DIRECTED_TYPES = new Set(["builds_on"]);
export const RELATION_LABELS = {
  builds_on: "builds on",
  different_version_of: "different version of",
  relates_to: "relates to",
};

// -- captions ---------------------------------------------------------------------------------

/** A node caption: the name on one line if it fits in CAPTION_CHARS, else two lines, the first
 * made of whole words (split after spaces, hyphens, slashes, dots, @) and the second clipped
 * with an ellipsis. A first word too long for a line is clipped on one line. */
export function caption(name, max = CAPTION_CHARS) {
  const text = String(name);
  const clip = (s) => (s.length > max ? s.slice(0, max - 1) + "…" : s);
  if (text.length <= max) return [text];
  const words = text.split(/(?<=[\s@./-])/);
  let first = "";
  for (const w of words) {
    if ((first + w).trimEnd().length > max) break;
    first += w;
  }
  if (!first.trim()) return [clip(text)];
  const rest = text.slice(first.length).trim();
  return rest ? [first.trim(), clip(rest)] : [first.trim()];
}

// -- edge geometry ----------------------------------------------------------------------------

/** For edges between the same pair of nodes (either direction), which slot each takes:
 * id -> {i, n}. The stored graph allows one relation per pair, but the geometry handles more. */
export function pairIndex(edges) {
  const groups = new Map();
  for (const e of edges) {
    const key = [e.from_id, e.to_id].sort((a, b) => a - b).join("-");
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(e);
  }
  const out = new Map();
  for (const list of groups.values()) list.forEach((e, i) => out.set(e.id, { i, n: list.length }));
  return out;
}

export const BEND = 34; // sideways offset between parallel edges

/**
 * Path and label placement for an edge from `s` to `t` (points {x, y}). `slot` comes from
 * `pairIndex`; parallel edges curve (a quadratic Bézier through an offset control point) on a
 * side that does not depend on direction. Both ends lie on the circles (radius `r`), heading
 * towards the control point; with `arrow` the end stops ARROW_GAP further out, leaving room for
 * the arrowhead. The label sits at the curve's midpoint, rotated along the chord and flipped so
 * it never reads upside down. Returns {d, x1, y1, x2, y2, lx, ly, angle}.
 */
export function edgeGeometry(s, t, { slot = null, arrow = false, fromId = 0, toId = 1, r = R } = {}) {
  const dx = t.x - s.x;
  const dy = t.y - s.y;
  const len = Math.hypot(dx, dy) || 1;
  const ux = dx / len;
  const uy = dy / len;
  const sign = fromId < toId ? 1 : -1;
  const bend = slot && slot.n > 1 ? (slot.i - (slot.n - 1) / 2) * BEND * sign : 0;
  const mx = (s.x + t.x) / 2 - uy * bend;
  const my = (s.y + t.y) / 2 + ux * bend;
  const a1 = Math.atan2(my - s.y, mx - s.x);
  const a2 = Math.atan2(my - t.y, mx - t.x);
  const end = arrow ? r + ARROW_GAP : r;
  const x1 = s.x + Math.cos(a1) * r;
  const y1 = s.y + Math.sin(a1) * r;
  const x2 = t.x + Math.cos(a2) * end;
  const y2 = t.y + Math.sin(a2) * end;
  const f = (v) => Math.round(v * 100) / 100;
  const d = bend
    ? `M${f(x1)},${f(y1)} Q${f(mx)},${f(my)} ${f(x2)},${f(y2)}`
    : `M${f(x1)},${f(y1)} L${f(x2)},${f(y2)}`;
  const lx = bend ? (x1 + 2 * mx + x2) / 4 : (x1 + x2) / 2;
  const ly = bend ? (y1 + 2 * my + y2) / 4 : (y1 + y2) / 2;
  let angle = (Math.atan2(y2 - y1, x2 - x1) * 180) / Math.PI;
  if (angle > 90 || angle < -90) angle += 180; // keep labels upright
  if (angle > 180) angle -= 360;
  return { d, x1, y1, x2, y2, lx, ly, angle };
}

// -- graph merging ----------------------------------------------------------------------------

/**
 * `graph` with `incoming` ({nodes?, edges?}) merged in, as a new object: nodes and edges are
 * keyed by id, so nothing is duplicated; an incoming node or edge replaces the data of the one
 * with its id (keeping its place in the order), and new ones are appended. With `anchor`, new
 * nodes get `anchor` set to that node id (the view places them next to it). Throws on an item
 * without an integer id, so malformed data is never silently drawn.
 */
export function mergeGraph(graph, incoming, { anchor = null } = {}) {
  const nodes = new Map(graph.nodes.map((n) => [n.id, n]));
  const edges = new Map(graph.edges.map((e) => [e.id, e]));
  for (const n of incoming.nodes || []) {
    requireId(n, "node");
    const existing = nodes.get(n.id);
    if (existing) nodes.set(n.id, { ...existing, ...n });
    else nodes.set(n.id, anchor !== null && anchor !== n.id ? { ...n, anchor } : { ...n });
  }
  for (const e of incoming.edges || []) {
    requireId(e, "edge");
    if (!Number.isInteger(e.from_id) || !Number.isInteger(e.to_id)) {
      throw new Error(`edge ${e.id} has no integer from_id/to_id`);
    }
    edges.set(e.id, edges.has(e.id) ? { ...edges.get(e.id), ...e } : { ...e });
  }
  return { nodes: [...nodes.values()], edges: [...edges.values()] };
}

function requireId(item, what) {
  if (!item || !Number.isInteger(item.id)) {
    throw new Error(`a ${what} without an integer id: ${JSON.stringify(item)}`);
  }
}

/** `graph` without node `id` on the canvas (its relations stay known, just not drawn). */
export function hideNode(graph, id) {
  return { nodes: graph.nodes.filter((n) => n.id !== id), edges: graph.edges };
}

/** The edges whose both ends are on the canvas, in the graph's edge order. */
export function visibleEdges(graph) {
  const ids = new Set(graph.nodes.map((n) => n.id));
  return graph.edges.filter((e) => ids.has(e.from_id) && ids.has(e.to_id));
}

/** Number of visible edges at each node id (0 for unconnected nodes). */
export function degrees(graph) {
  const degree = new Map(graph.nodes.map((n) => [n.id, 0]));
  for (const e of visibleEdges(graph)) {
    degree.set(e.from_id, degree.get(e.from_id) + 1);
    degree.set(e.to_id, degree.get(e.to_id) + 1);
  }
  return degree;
}

// -- roadmap highlight ------------------------------------------------------------------------

/**
 * The highlight for a roadmap (API `roadmap`: nodes in study order, the target last): `lit` is
 * the set of their ids, `hot` the ids of the builds_on edges (of `edges`) with both ends lit,
 * `order` the ids in study order, `target` the last id.
 */
export function roadmapHighlight(roadmap, edges) {
  if (!Array.isArray(roadmap) || roadmap.length === 0) {
    throw new Error("a roadmap always ends with its target; got an empty one");
  }
  const order = roadmap.map((n) => n.id);
  const lit = new Set(order);
  const hot = new Set(
    edges
      .filter((e) => e.type === "builds_on" && lit.has(e.from_id) && lit.has(e.to_id))
      .map((e) => e.id)
  );
  return { target: order[order.length - 1], order, lit, hot };
}

// -- inspector helpers ------------------------------------------------------------------------

export const RELATION_GROUPS = [
  { key: "builds_on:out", label: "Builds on" },
  { key: "builds_on:in", label: "Built on by" },
  { key: "different_version_of:symmetric", label: "Different version of" },
  { key: "relates_to:symmetric", label: "Related to" },
];

/** A concept's relations (API `relations`) grouped for the inspector, in RELATION_GROUPS order,
 * empty groups left out: [{key, label, items}]. Throws on a type/direction it does not know. */
export function relationGroups(relations) {
  const groups = new Map(RELATION_GROUPS.map((g) => [g.key, { ...g, items: [] }]));
  for (const r of relations) {
    const group = groups.get(`${r.type}:${r.direction}`);
    if (!group) throw new Error(`unknown relation ${r.type} (${r.direction}) on relation ${r.id}`);
    group.items.push(r);
  }
  return [...groups.values()].filter((g) => g.items.length);
}

/** Counts of `key` over `list`, as a Map in the order of `order` (zero counts kept), then any
 * other values in first-seen order. */
export function countBy(list, key, order = []) {
  const counts = new Map(order.map((k) => [k, 0]));
  for (const item of list) counts.set(item[key], (counts.get(item[key]) ?? 0) + 1);
  return counts;
}

// -- "Find relations" results ----------------------------------------------------------------
// The result of POST /api/graph/concepts/{slug}/relate (see teacher/web/routers/concepts.py).

export const SUGGESTION_SLOTS = [
  { slot: "builds_on", label: "Prerequisites" },
  { slot: "built_on_by", label: "Builds on it" },
  { slot: "different_version_of", label: "Other versions" },
  { slot: "relates_to", label: "Related" },
];
const DIRECTIONS = new Set(["out", "in", "symmetric"]);

/** "A builds on B" for a relation seen from `focus` ({name}) to `other` ({name}): `direction`
 * "out" (focus builds on other), "in" (other builds on focus) or "symmetric". */
export function relationPhrase(focus, other, type, direction) {
  if (!RELATION_LABELS[type]) throw new Error(`unknown relation type ${type}`);
  if (!DIRECTIONS.has(direction)) throw new Error(`unknown relation direction ${direction}`);
  if (type === "builds_on" && direction === "symmetric") throw new Error("builds_on has a direction");
  if (type !== "builds_on" && direction !== "symmetric") throw new Error(`${type} has no direction`);
  const [a, b] = direction === "in" ? [other, focus] : [focus, other];
  return `${a.name} ${RELATION_LABELS[type]} ${b.name}`;
}

// Why a regenerate run removed an old relation (API `removed[].reason`), and why it kept one
// (API `kept[].because`).
export const REMOVED_LABELS = {
  none: "judged unrelated",
  refused: "the new verdict was refused",
  implied: "a builds-on path already joins them",
};
export const KEPT_LABELS = {
  reviewed: "reviewed",
  "origin paper": "from a paper",
  "origin user": "added by a person",
};

/**
 * What a regenerate run would do to a concept's relations (API concept details `relations`):
 * `replaceable` = made by a model and never reviewed (re-decided and replaced), `kept` = the
 * rest (reviewed, or from a paper or a person). Throws on an unknown origin.
 */
export function regenerateScope(relations) {
  if (!Array.isArray(relations)) throw new Error("concept details have no relations list");
  let replaceable = 0;
  let kept = 0;
  for (const r of relations) {
    if (!ORIGINS.includes(r.origin)) throw new Error(`unknown relation origin ${r.origin} on relation ${r.id}`);
    if (r.origin === "model" && !r.reviewed_at) replaceable += 1;
    else kept += 1;
  }
  return { replaceable, kept };
}

/**
 * The inspector's summary of a relate result: {regenerate, model, added: [{edge, other, type,
 * phrase, rationale, replaces: null | {phrase, changed}}], none, refused: [{other, phrase,
 * reason}], removed: [{other, phrase, reason, label, detail}], kept: [{other, phrase, because}],
 * suggestions: [{slot, label, items}] (slots with suggestions only, in SUGGESTION_SLOTS order),
 * judged, dropped, judgeCalled, counts}. Throws on anything it does not know (a slot, a type, a
 * direction, a removal reason, a reason to keep, an added relation without its edge), so a
 * result is never shown half-understood.
 */
export function relateSummary(result) {
  if (
    !result ||
    !result.concept ||
    !Array.isArray(result.added) ||
    !Array.isArray(result.removed) ||
    !Array.isArray(result.kept) ||
    !Array.isArray(result.deleted_edge_ids)
  ) {
    throw new Error("not a relate result");
  }
  const focus = result.concept;
  const added = result.added.map((a) => {
    if (!a.edge || !Number.isInteger(a.edge.id)) throw new Error(`added relation to ${a.other?.slug} has no edge`);
    const old = a.replaces;
    return {
      edge: a.edge,
      other: a.other,
      type: a.type,
      phrase: relationPhrase(focus, a.other, a.type, a.direction),
      rationale: a.rationale,
      replaces: old
        ? {
            phrase: relationPhrase(focus, a.other, old.type, old.direction),
            changed: Boolean(old.type_changed || old.direction_changed),
          }
        : null,
    };
  });
  const removed = result.removed.map((r) => {
    if (!REMOVED_LABELS[r.reason]) throw new Error(`unknown removal reason ${r.reason}`);
    return {
      other: r.other,
      phrase: relationPhrase(focus, r.other, r.type, r.direction),
      reason: r.reason,
      label: REMOVED_LABELS[r.reason],
      detail: r.detail,
    };
  });
  const kept = result.kept.map((k) => {
    if (!Array.isArray(k.because) || !k.because.length) throw new Error(`kept relation ${k.relation_id} says not why`);
    const because = k.because.map((why) => {
      if (!KEPT_LABELS[why]) throw new Error(`unknown reason to keep a relation: ${why}`);
      return KEPT_LABELS[why];
    });
    return { other: k.other, phrase: relationPhrase(focus, k.other, k.type, k.direction), because: because.join(", ") };
  });
  const refused = result.refused.map((r) => ({
    other: r.other,
    phrase: relationPhrase(focus, r.other, r.type, r.direction),
    reason: r.reason,
  }));
  const known = new Set(SUGGESTION_SLOTS.map((s) => s.slot));
  for (const u of result.unmatched) {
    if (!known.has(u.slot)) throw new Error(`unknown suggestion slot ${u.slot}`);
  }
  const suggestions = SUGGESTION_SLOTS.map(({ slot, label }) => ({
    slot,
    label,
    items: result.unmatched.filter((u) => u.slot === slot),
  })).filter((g) => g.items.length);
  const dropped = Object.values(result.counts.dropped).reduce((sum, n) => sum + n, 0);
  return {
    regenerate: Boolean(result.regenerate),
    model: result.model,
    added,
    none: result.judged_none.length,
    refused,
    removed,
    kept,
    suggestions,
    judged: result.counts.judged,
    dropped,
    judgeCalled: result.judge_called,
    counts: result.counts,
  };
}

/** `graph` with a relate result applied: the relations the run deleted (`deleted_edge_ids`: a
 * regenerate run's removed and replaced ones) dropped, then its concepts and new relations
 * merged in; concepts that were not on the canvas are placed next to `anchor` (the concept the
 * run was for). */
export function mergeRelateResult(graph, result, anchor) {
  if (!Array.isArray(result.deleted_edge_ids)) throw new Error("not a relate result: no deleted_edge_ids");
  const gone = new Set(result.deleted_edge_ids);
  const rest = { nodes: graph.nodes, edges: graph.edges.filter((e) => !gone.has(e.id)) };
  return mergeGraph(rest, { nodes: result.nodes, edges: result.edges }, { anchor });
}

/** The slugs whose cached concept details a relate result makes stale: the concept, every
 * concept a new relation reaches, and every concept a removed relation reached. */
export function relateTouched(result) {
  return [
    ...new Set([
      result.concept.slug,
      ...result.added.map((a) => a.other.slug),
      ...result.removed.map((r) => r.other.slug),
    ]),
  ];
}

/** The one-line status after a run for concept `name`. */
export function relateMessage(result, name) {
  const s = relateSummary(result);
  const tail = `${s.none} judged unrelated, ${s.refused.length} refused.`;
  if (!s.regenerate) {
    return `${name}: ${s.added.length} relation${s.added.length === 1 ? "" : "s"} added, ${tail}`;
  }
  const replacing = s.added.filter((a) => a.replaces).length;
  return (
    `${name}: relations regenerated: ${s.added.length} added (${replacing} replacing an old one), ` +
    `${s.removed.length} removed, ${s.kept.length} kept, ${tail}`
  );
}

// -- search results --------------------------------------------------------------------------
// The result of GET /api/graph/search (see teacher/web/routers/concepts.py).

export const MATCH_LABELS = {
  exact: "exact match",
  word: "matches",
  prefix: "starts with your search",
  substring: "contains your search",
  fuzzy: "close match",
};
export const SEMANTIC_STATES = ["ok", "unavailable", "stale", "failed", "skipped"];

/**
 * How a search result was found, as notes in order ({kind, text}; kind is the match kind or
 * "semantic"): the lexical match unless it is simply the concept's own name matching exactly,
 * then "similar meaning 0.62" when it was found by meaning. Throws on an unknown match kind or
 * field, or a result that says neither how it matched nor its similarity.
 */
export function searchNotes(result) {
  const notes = [];
  const lexical = result.match_kind !== null && result.match_kind !== undefined;
  const semantic = result.similarity !== null && result.similarity !== undefined;
  if (!lexical && !semantic) throw new Error(`Search result “${result.slug}” says neither how it matched nor its similarity.`);
  if (lexical) {
    const label = MATCH_LABELS[result.match_kind];
    if (!label) throw new Error(`Unknown match kind “${result.match_kind}” in the search results.`);
    if (!(result.match_kind === "exact" && result.match_field === "name")) {
      let where;
      if (result.match_field === "name") where = "in the name";
      else if (result.match_field === "alias") where = `alias “${result.matched}”`;
      else if (result.match_field === "summary") where = "in the summary";
      else throw new Error(`Unknown match field “${result.match_field}” in the search results.`);
      notes.push({ kind: result.match_kind, text: `${label}: ${where}` });
    }
  }
  if (semantic) {
    if (typeof result.similarity !== "number" || !Number.isFinite(result.similarity)) {
      throw new Error(`Search result “${result.slug}” has similarity ${result.similarity}.`);
    }
    notes.push({ kind: "semantic", text: `similar meaning ${result.similarity.toFixed(2)}` });
  }
  return notes;
}

function sentence(text) {
  const t = String(text).trim();
  return /[.!?]$/.test(t) ? t : `${t}.`;
}

/**
 * The notice shown with every search's results: {degraded, text}. `semantic` is the response's
 * status object. ok: "Searched names and meaning."; any other status says that only names were
 * searched and why (and, for stale embeddings, the command that fixes it). Throws on an unknown
 * status or a degraded one without its reason.
 */
export function semanticNotice(semantic) {
  if (!semantic || !SEMANTIC_STATES.includes(semantic.status)) {
    throw new Error(`Unknown semantic search status “${semantic && semantic.status}”.`);
  }
  if (semantic.status === "ok") return { degraded: false, text: "Searched names and meaning." };
  if (!semantic.reason) throw new Error(`The semantic search status “${semantic.status}” has no reason.`);
  const lead =
    semantic.status === "failed"
      ? "Searching by meaning failed, so only names, aliases and summaries were searched"
      : "Only names, aliases and summaries were searched, not meaning";
  let text = `${lead}: ${sentence(semantic.reason)}`;
  if (semantic.status === "stale") {
    if (!semantic.fix) throw new Error("The stale semantic search status names no fix.");
    text += ` To fix it, run “${semantic.fix}”.`;
  }
  return { degraded: true, text };
}

// -- view arithmetic --------------------------------------------------------------------------
// A view is {x, y, k}: the graph origin is drawn at (width/2 + x, height/2 + y), scaled by k.

/** The view that fits all `points` ({x, y}) in a `size` ({w, h}) box with FIT_MARGIN to spare,
 * never zooming in past 1; null when there is nothing to fit or no room to fit it in. */
export function fitView(points, size, { r = R, margin = FIT_MARGIN } = {}) {
  if (!points.length || size.w < 1 || size.h < 1) return null;
  const xs = points.map((p) => p.x);
  const ys = points.map((p) => p.y);
  const [x0, x1, y0, y1] = [Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)];
  const room = (v) => Math.max(v - margin, 1);
  const k = clampZoom(Math.min(1, room(size.w) / (x1 - x0 + 2 * r), room(size.h) / (y1 - y0 + 2 * r)));
  return { k, x: -((x0 + x1) / 2) * k, y: -((y0 + y1) / 2) * k };
}

export function clampZoom(k) {
  return Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, k));
}

/** `view` zoomed by `factor` about the point (px, py), given relative to the box centre. */
export function zoomView(view, factor, px = 0, py = 0) {
  const k = clampZoom(view.k * factor);
  const f = k / view.k;
  return { k, x: px - (px - view.x) * f, y: py - (py - view.y) * f };
}

/** The view that keeps the zoom and puts graph point `p` in the middle. */
export function centreView(view, p) {
  return { k: view.k, x: -p.x * view.k, y: -p.y * view.k };
}

/** A point given relative to the box centre, in graph coordinates. */
export function toGraph(view, px, py) {
  return { x: (px - view.x) / view.k, y: (py - view.y) / view.k };
}

// -- labels and taps at any zoom ---------------------------------------------------------------
// Captions inside the circles are CAPTION_PX graph units tall, so they shrink with the zoom: with
// ~90 concepts fitted on a phone they are 3px tall. Below a readable size they are hidden and
// each concept may instead get a name label under its circle drawn at LABEL_PX on screen
// whatever the zoom; `pickLabels` decides which, so that labels never overlap.

export const CAPTION_PX = 9; // the in-circle caption's font size, in graph units
export const LABEL_PX = 13; // a name label's font size, in screen pixels
export const LABEL_CHARS = 22; // a name label longer than this is clipped with an ellipsis
export const LABEL_AREA_PX = 12000; // at most one name label per this many square pixels of view
export const MIN_LABELS = 6;

/** Whether in-circle captions are big enough to read at zoom `k`: `minPx` on screen. */
export function captionsReadable(k, minPx) {
  return CAPTION_PX * k >= minPx;
}

/** A name label's text: `name`, clipped to `max` characters with an ellipsis. */
export function labelText(name, max = LABEL_CHARS) {
  const text = String(name);
  return text.length > max ? text.slice(0, max - 1).trimEnd() + "…" : text;
}

/**
 * The order in which concepts are offered a name label: the selected one, then its
 * neighbours, then the roadmap's concepts (`lit`), then the best connected; ties by name.
 * `nodes`: [{id, name, degree}]. Returns the ids.
 */
export function labelOrder(nodes, { selectedId = null, neighbours = new Set(), lit = new Set() } = {}) {
  const tier = (n) => (n.id === selectedId ? 0 : neighbours.has(n.id) ? 1 : lit.has(n.id) ? 2 : 3);
  return [...nodes]
    .sort((a, b) => tier(a) - tier(b) || b.degree - a.degree || String(a.name).localeCompare(String(b.name)))
    .map((n) => n.id);
}

/** The screen box of a name label `side` ("below" or "above") a circle of screen radius
 * `radius` centred at (x, y). */
export function labelBox(x, y, text, { radius, fontPx = LABEL_PX, side = "below" } = {}) {
  const w = text.length * fontPx * 0.56 + 6;
  const h = fontPx * 1.3;
  const top = side === "above" ? y - radius - 2 - h : y + radius + 2;
  return { x0: x - w / 2, x1: x + w / 2, y0: top, y1: top + h };
}

export const LABEL_SIDES = ["below", "above"];

/** `box` moved sideways (by `dx`, also returned) to lie between 0 and `width`, if it is narrower. */
function shiftInside(box, width) {
  const dx = box.x0 < 0 ? -box.x0 : box.x1 > width ? width - box.x1 : 0;
  return { x0: box.x0 + dx, x1: box.x1 + dx, y0: box.y0, y1: box.y1, dx };
}

const overlaps = (a, b) => a.x0 < b.x1 && b.x0 < a.x1 && a.y0 < b.y1 && b.y0 < a.y1;

/**
 * Which concepts get a name label, and on which side of the circle: `items` [{id, x, y, text}]
 * in priority order (`labelOrder`), with (x, y) the circle's centre on screen; `width` x
 * `height` the visible part of the view. Greedy: an item is labelled when its centre is in view,
 * its circle is clear of the `blocked` boxes, and below its circle or else above it, its label (moved
 * sideways by `dx` to stay inside the view's left and right edges) fits in the view and
 * overlaps neither an earlier label nor the circle of an earlier labelled concept; up to `max`
 * labels. `blocked` boxes (the buttons and legend over the view) count as earlier labels. `keep`
 * (the selected concept) is labelled whenever it is in view, below it. Returns a
 * Map of id -> {side: "below" | "above", dx}.
 */
export function pickLabels(items, { width, height, radius, fontPx = LABEL_PX, max = Infinity, keep = null, blocked = [] }) {
  const chosen = new Map();
  const boxes = [...blocked];
  const inside = (b) => b.x0 >= 0 && b.x1 <= width && b.y0 >= 0 && b.y1 <= height;
  for (const item of items) {
    if (chosen.size >= max && item.id !== keep) continue;
    if (item.x < 0 || item.x > width || item.y < 0 || item.y > height) continue;
    const circle = { x0: item.x - radius, x1: item.x + radius, y0: item.y - radius, y1: item.y + radius };
    let place = null;
    let label = null;
    if (item.id === keep) {
      label = shiftInside(labelBox(item.x, item.y, item.text, { radius, fontPx }), width);
      place = { side: "below", dx: label.dx };
    } else {
      if (blocked.some((b) => overlaps(b, circle))) continue;
      for (const side of LABEL_SIDES) {
        const box = shiftInside(labelBox(item.x, item.y, item.text, { radius, fontPx, side }), width);
        if (inside(box) && !boxes.some((b) => overlaps(b, box))) {
          place = { side, dx: box.dx };
          label = box;
          break;
        }
      }
      if (!place) continue;
    }
    chosen.set(item.id, place);
    boxes.push(label, circle);
  }
  return chosen;
}

/** How many name labels a `width` x `height` view holds before it turns into a wall of text. */
export function labelBudget(width, height) {
  return Math.max(MIN_LABELS, Math.round((width * height) / LABEL_AREA_PX));
}

/**
 * The concept a tap at graph point `p` means: the nearest of `points` ([{id, x, y}], graph
 * units) whose circle (radius `r`) is within `slop` graph units of the tap; null when none is.
 * Lets a finger hit a circle a few pixels across.
 */
export function nearestNode(points, p, { r = R, slop = 0 } = {}) {
  let best = null;
  let bestD = Infinity;
  for (const q of points) {
    const d = Math.hypot(q.x - p.x, q.y - p.y);
    if (d <= r + slop && d < bestD) {
      best = q.id;
      bestD = d;
    }
  }
  return best;
}

/** Whether a press that lasted `heldMs` before it moved drags a concept (pinning it) rather
 * than panning the view: always with a mouse or pen, and with a finger only after a long press,
 * so that a swipe that starts on a concept pans like one that starts on the background. */
export function pressDrags(pointerType, heldMs, longPressMs) {
  return pointerType !== "touch" || heldMs >= longPressMs;
}
