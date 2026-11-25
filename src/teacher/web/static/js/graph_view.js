// The force-directed SVG drawing of the concept graph (ported from the context-graph explorer's
// GraphView): a d3-force simulation, fit-to-view that follows the layout until the reader pans,
// zooms or drags a node (the Fit button turns following back on), ctrl/cmd + wheel and pinch zoom, background drag to pan, node drag to pin, click to
// select a node or relation, double-click to expand, and the roadmap highlight ("tracing": all
// but the lit concepts and hot relations dimmed). `setStanding(map)` marks each concept with
// the reader's standing (a dot at the node's lower right; standing.css).
//
// Readable at any zoom: while the in-circle captions are smaller than `captionMinPx` on screen
// they are hidden (the svg gets class "zoomed-out") and concepts get a name label under the
// circle at a fixed screen size instead, as many as fit without overlapping, the selected
// concept and its neighbours first, then the best connected (graph_model.js pickLabels). The
// svg's `--k` custom property is the zoom, so CSS can size things in screen pixels.
//
// Touch: a tap selects the nearest concept within a finger's reach (TOUCH_RADIUS_PX), not only
// one hit exactly; a swipe pans even when it starts on a concept, and a concept is dragged (and
// pinned) only after a long press (LONG_PRESS_MS, the node shows "is-held" when it can be
// dragged); two fingers pinch-zoom. `setInsets({bottom})` says how much of the canvas's bottom
// is covered (the phone layout's inspector sheet): fitting and centring use the part above it,
// and `reveal(id)` brings a concept hidden under it into view.
//
// Elements are created once per node and edge when the graph changes (`setGraph`); simulation
// ticks only move them (transforms and path data), they never rebuild the SVG.
//
// `createGraphView({svg, canvas, buttons: {zoomIn, zoomOut, fit}, onSelect, onExpand,
// onPinChange, captionMinPx, overlays})` (`overlays`: elements over the canvas that labels avoid);
// `onSelect(selection)` gets {kind: "node" | "edge", id} or null (a click), `onExpand(id)` a
// node id (a double-click), `onPinChange(id)` a node that was pinned (dragged) or unpinned.

import {
  forceCollide,
  forceLink,
  forceManyBody,
  forceSimulation,
  forceX,
  forceY,
} from "./d3_force.js";
import { prefersReducedMotion, svgEl as s } from "./dom.js";
import {
  DIRECTED_TYPES,
  R,
  RELATION_LABELS,
  caption,
  captionsReadable,
  centreView,
  clampZoom,
  degrees,
  edgeGeometry,
  fitView,
  labelBudget,
  labelOrder,
  labelText,
  nearestNode,
  pairIndex,
  pickLabels,
  pressDrags,
  toGraph,
  visibleEdges,
  zoomView,
} from "./graph_model.js";
import { STATUSES, statusLabel } from "./standing_model.js";

const DOUBLE_CLICK_MS = 400;
const DRAG_THRESHOLD_PX = 3;
const TOUCH_DRAG_THRESHOLD_PX = 8; // a finger wobbles more than a mouse: below this a touch is a tap
const TOUCH_RADIUS_PX = 22; // a tap this close to a concept's centre (screen px) selects it
const LONG_PRESS_MS = 350; // a finger held this long on a concept then drags it
const WHEEL_ZOOM_RATE = 0.004;
const BUTTON_ZOOM = 1.25;
const NEW_NODE_SPREAD = 60;
const MARKERS = ["reviewed", "unreviewed", "selected", "hot"];

function markerDefs() {
  return s(
    "defs",
    {},
    MARKERS.map((kind) =>
      s(
        "marker",
        {
          id: `graph-arrow-${kind}`,
          viewBox: "0 0 10 10",
          refX: 9,
          refY: 5,
          markerWidth: 7,
          markerHeight: 7,
          orient: "auto",
        },
        s("path", { d: "M0 0L10 5L0 10z", class: `arrowhead arrowhead-${kind}` })
      )
    )
  );
}

export function edgeLabel(edge) {
  const label = RELATION_LABELS[edge.type];
  if (!label) throw new Error(`unknown relation type ${edge.type} on relation ${edge.id}`);
  return edge.redundant ? `${label} (redundant)` : label;
}

export function createGraphView({ svg, canvas, buttons, overlays = [], onSelect, onExpand, onPinChange, captionMinPx = 9 }) {
  const reduced = prefersReducedMotion();
  const simNodes = new Map(); // id -> simulation node {id, data, x, y, fx, fy, degree}
  const nodeEls = new Map(); // id -> {g, text, title}
  const edgeEls = new Map(); // id -> {g, hit, line, inner, label, title, edge}
  let links = []; // [{edge, source, target}] for the drawn edges
  let pairs = new Map();
  let view = { x: 0, y: 0, k: 1 };
  let size = { w: 0, h: 0 };
  let touched = false; // the reader panned or zoomed: stop following the layout
  let follow = null; // node id kept in the middle while the layout settles
  let selected = null;
  let highlight = null;
  let standing = new Map(); // concept id -> the reader's standing status (setStanding)
  let insets = { bottom: 0 }; // screen px of the canvas covered by the inspector sheet
  let neighbours = new Set(); // ids of the selected concept's neighbours (label priority)
  let blocked = []; // screen boxes of the overlays on the canvas (measureOverlays)
  let labelled = new Map(); // id -> {side, dx} (pickLabels) of the concepts showing a name label

  const edgeLayer = s("g", { class: "graph-edges" });
  const nodeLayer = s("g", { class: "graph-nodes" });
  // Name labels above every circle, so a label is never hidden under a neighbouring concept.
  const labelLayer = s("g", { class: "graph-labels", "aria-hidden": "true" });
  const viewport = s("g", { class: "graph-viewport" }, edgeLayer, nodeLayer, labelLayer);
  svg.replaceChildren(markerDefs(), viewport);

  const sim = forceSimulation()
    .force("charge", forceManyBody().strength(-420).distanceMax(450))
    .force("link", forceLink().distance(110).strength(0.6))
    .force("collide", forceCollide(R + 12))
    // Unconnected nodes are pulled in harder so they don't drift off and shrink the fit.
    .force("x", forceX(0).strength((n) => (n.degree ? 0.05 : 0.25)))
    .force("y", forceY(0).strength((n) => (n.degree ? 0.05 : 0.25)))
    .on("tick", () => {
      track();
      render();
    })
    .stop();

  // -- layout and drawing ---------------------------------------------------------------------

  /** Run the layout: animated, or (reduced motion) to rest at once and drawn once. */
  function kick(alpha) {
    if (!reduced) {
      sim.alpha(alpha).restart();
      return;
    }
    sim.alpha(alpha);
    while (sim.alpha() > sim.alphaMin()) sim.tick();
    track();
    render();
  }

  /** Keep the followed node in the middle, or the whole graph in view, unless touched. */
  function track() {
    if (follow !== null && simNodes.has(follow)) {
      const centred = centreView(view, simNodes.get(follow));
      view = { ...centred, y: centred.y - insets.bottom / 2 };
    } else if (!touched) {
      const fitted = fitView([...simNodes.values()], { w: size.w, h: size.h - insets.bottom });
      if (fitted) view = { ...fitted, y: fitted.y - insets.bottom / 2 };
    }
    applyView();
  }

  function applyView() {
    viewport.setAttribute(
      "transform",
      `translate(${size.w / 2 + view.x},${size.h / 2 + view.y}) scale(${view.k})`
    );
    svg.style.setProperty("--k", String(round(view.k)));
    updateLabels();
  }

  /** In-circle captions while readable, else name labels for the concepts that fit. */
  function updateLabels() {
    const far = !captionsReadable(view.k, captionMinPx);
    svg.classList.toggle("zoomed-out", far);
    let next = new Map();
    if (far && size.w > 0) {
      const lit = highlight ? highlight.lit : new Set();
      const selectedId = selected?.kind === "node" ? selected.id : null;
      const order = labelOrder(
        [...simNodes.values()].map((n) => ({ id: n.id, name: n.data.name, degree: n.degree ?? 0 })),
        { selectedId, neighbours, lit }
      );
      const h = size.h - insets.bottom;
      next = pickLabels(
        order.map((id) => {
          const n = simNodes.get(id);
          return { id, x: size.w / 2 + view.x + (n.x ?? 0) * view.k, y: size.h / 2 + view.y + (n.y ?? 0) * view.k, text: shownText(id, selectedId) };
        }),
        { width: size.w, height: h, radius: R * view.k, max: labelBudget(size.w, h), keep: selectedId, blocked }
      );
    }
    for (const id of labelled.keys()) if (!next.has(id)) nodeEls.get(id)?.label.classList.remove("is-shown");
    for (const [id, { side, dx }] of next) {
      const el = nodeEls.get(id);
      const n = simNodes.get(id);
      el.label.setAttribute("transform", `translate(${round(n.x ?? 0)},${round(n.y ?? 0)})`);
      el.label.setAttribute("x", round(dx / view.k));
      el.label.setAttribute("y", side === "above" ? -R : R);
      el.label.setAttribute("dy", side === "above" ? "-0.45em" : "1em");
      const text = shownText(id, selected?.kind === "node" ? selected.id : null);
      if (el.label.textContent !== text) el.label.textContent = text;
      el.label.classList.add("is-shown");
    }
    labelled = next;
  }

  /** A name label's text: clipped (labelText), except the selected concept's, which is whole. */
  function shownText(id, selectedId) {
    return id === selectedId ? simNodes.get(id).data.name : nodeEls.get(id).labelText;
  }

  /** The selected concept's neighbours on the canvas. */
  function neighboursOf(id) {
    const out = new Set();
    for (const l of links) {
      if (l.edge.from_id === id) out.add(l.edge.to_id);
      else if (l.edge.to_id === id) out.add(l.edge.from_id);
    }
    return out;
  }

  function render() {
    for (const [id, el] of nodeEls) {
      const n = simNodes.get(id);
      el.g.setAttribute("transform", `translate(${round(n.x)},${round(n.y)})`);
    }
    for (const link of links) {
      const el = edgeEls.get(link.edge.id);
      const geo = edgeGeometry(link.source, link.target, {
        slot: pairs.get(link.edge.id),
        arrow: DIRECTED_TYPES.has(link.edge.type),
        fromId: link.edge.from_id,
        toId: link.edge.to_id,
      });
      el.hit.setAttribute("d", geo.d);
      el.line.setAttribute("d", geo.d);
      if (el.inner) el.inner.setAttribute("d", geo.d);
      el.label.setAttribute(
        "transform",
        `translate(${round(geo.lx)},${round(geo.ly)}) rotate(${round(geo.angle)})`
      );
    }
    updateLabels();
  }

  function nodeElement(n) {
    const text = s("text", { class: "node-caption", "text-anchor": "middle" });
    // The name label under the circle (in the label layer, moved with the node while shown):
    // CSS sizes it in screen pixels (font-size from --k), so its offset is in em.
    const label = s("text", { class: "node-label", "text-anchor": "middle", y: R, dy: "1em" });
    labelLayer.append(label);
    const title = s("title");
    const g = s(
      "g",
      { class: "node", "data-id": n.id },
      s("circle", { class: "halo", r: R + 7 }),
      s("circle", { class: "halo-lit", r: R + 4 }),
      s("circle", { class: "node-body", r: R }),
      s("circle", { class: "pin-mark", r: 4, cx: round(R * 0.72), cy: round(-R * 0.72) }),
      s("circle", { class: "standing-mark", r: 5, cx: round(R * 0.72), cy: round(R * 0.72) }),
      text,
      title
    );
    nodeLayer.append(g);
    return { g, text, label, title, name: null, labelText: "" };
  }

  function updateNode(el, n) {
    if (el.name !== n.name) {
      const lines = caption(n.name);
      el.text.replaceChildren(
        ...lines.map((line, i) =>
          s("tspan", { x: 0, dy: i === 0 ? (lines.length === 1 ? 4 : -2) : 12 }, line)
        )
      );
      el.labelText = labelText(n.name);
      el.label.textContent = el.labelText;
      el.name = n.name;
    }
    const status = standing.get(n.id);
    el.title.textContent =
      `${n.name}\n${n.origin} concept, ${n.reviewed ? "reviewed" : "unreviewed"}\n` +
      (status ? `Your standing: ${statusLabel(status)}\n` : "") +
      "Click or tap to inspect, double-click or double-tap to expand, drag (on a touch screen: press, hold, then drag) to pin.";
  }

  function edgeElement(e) {
    const line = s("path", { class: "rel-line" });
    const inner = e.type === "different_version_of" ? s("path", { class: "rel-inner" }) : null;
    const hit = s("path", { class: "rel-hit" });
    const label = s("text", { class: "rel-label", dy: -4 }, edgeLabel(e));
    const title = s("title");
    const g = s("g", { class: "rel", "data-id": e.id }, hit, line, inner, label, title);
    edgeLayer.append(g);
    return { g, hit, line, inner, label, title };
  }

  function nameOf(id) {
    const n = simNodes.get(id);
    return n ? n.data.name : `#${id}`;
  }

  /** Classes for selection, pins, review state and the highlight; arrowheads to match. */
  function applyClasses() {
    svg.classList.toggle("tracing", Boolean(highlight));
    for (const [id, el] of nodeEls) {
      const n = simNodes.get(id);
      const c = el.g.classList;
      c.toggle("is-selected", selected?.kind === "node" && selected.id === id);
      c.toggle("is-pinned", n.fx !== null && n.fx !== undefined);
      c.toggle("is-unreviewed", !n.data.reviewed);
      c.toggle("lit", Boolean(highlight && highlight.lit.has(id)));
      el.label.classList.toggle("is-selected", selected?.kind === "node" && selected.id === id);
      el.label.classList.toggle("lit", Boolean(highlight && highlight.lit.has(id)));
      for (const origin of ["paper", "model", "user"]) c.toggle(`origin-${origin}`, n.data.origin === origin);
      for (const status of STATUSES) c.toggle(`standing-${status}`, standing.get(id) === status);
    }
    for (const [id, el] of edgeEls) {
      const e = el.edge;
      const isSelected = selected?.kind === "edge" && selected.id === id;
      const hot = Boolean(highlight && highlight.hot.has(id));
      const c = el.g.classList;
      for (const type of Object.keys(RELATION_LABELS)) c.toggle(`t-${type}`, e.type === type);
      c.toggle("is-unreviewed", !e.reviewed);
      c.toggle("is-redundant", Boolean(e.redundant));
      c.toggle("is-selected", isSelected);
      c.toggle("hot", hot);
      if (DIRECTED_TYPES.has(e.type)) {
        const marker = isSelected ? "selected" : hot ? "hot" : e.reviewed ? "reviewed" : "unreviewed";
        el.line.setAttribute("marker-end", `url(#graph-arrow-${marker})`);
      }
    }
  }

  /** Bring the drawing in step with `graph` ({nodes, edges}, see graph_model.js). New nodes are
   * placed next to their `anchor` (or `anchor` here), else next to a drawn neighbour, else in
   * the middle of the view; on the first draw d3 lays them out itself. */
  function setGraph(graph, { anchor = null } = {}) {
    const ids = new Set(graph.nodes.map((n) => n.id));
    for (const [id, el] of nodeEls) {
      if (!ids.has(id)) {
        el.g.remove();
        el.label.remove();
        nodeEls.delete(id);
        simNodes.delete(id);
      }
    }
    if (selected?.kind === "node" && !ids.has(selected.id)) selected = null;
    if (follow !== null && !ids.has(follow)) follow = null;
    const firstDraw = simNodes.size === 0;
    const middle = toGraph(view, 0, 0);
    for (const n of graph.nodes) {
      let sn = simNodes.get(n.id);
      if (sn) {
        sn.data = n;
      } else {
        sn = { id: n.id, data: n };
        if (!firstDraw) {
          const near = simNodes.get(n.anchor ?? anchor) || drawnNeighbour(graph, n.id) || middle;
          const a = Math.random() * 2 * Math.PI;
          sn.x = near.x + Math.cos(a) * NEW_NODE_SPREAD;
          sn.y = near.y + Math.sin(a) * NEW_NODE_SPREAD;
        }
        simNodes.set(n.id, sn);
        nodeEls.set(n.id, nodeElement(n));
      }
      updateNode(nodeEls.get(n.id), n);
    }

    const drawn = visibleEdges(graph);
    const drawnIds = new Set(drawn.map((e) => e.id));
    for (const [id, el] of edgeEls) {
      if (!drawnIds.has(id)) {
        el.g.remove();
        edgeEls.delete(id);
      }
    }
    if (selected?.kind === "edge" && !drawnIds.has(selected.id)) selected = null;
    for (const e of drawn) {
      const known = edgeEls.get(e.id);
      if (known && !sameEdgeShape(known.edge, e)) {
        // An id the database reused for another relation (a regenerate deletes old relations
        // before adding new ones): its element was built for another type or other ends.
        known.g.remove();
        edgeEls.delete(e.id);
      }
      if (!edgeEls.has(e.id)) edgeEls.set(e.id, edgeElement(e));
      const el = edgeEls.get(e.id);
      el.edge = e;
      el.label.textContent = edgeLabel(e);
      el.title.textContent =
        `${nameOf(e.from_id)} ${RELATION_LABELS[e.type]} ${nameOf(e.to_id)}` +
        `${e.reviewed ? "" : " (unreviewed)"}${e.redundant ? " (redundant)" : ""}`;
    }
    links = drawn.map((e) => ({ edge: e, source: simNodes.get(e.from_id), target: simNodes.get(e.to_id) }));
    pairs = pairIndex(drawn);
    const degree = degrees(graph);
    for (const sn of simNodes.values()) sn.degree = degree.get(sn.id) ?? 0;
    neighbours = selected?.kind === "node" ? neighboursOf(selected.id) : new Set();

    sim.nodes([...simNodes.values()]);
    sim.force("link").links(links);
    applyClasses();
    kick(0.9);
  }

  function sameEdgeShape(a, b) {
    return a.type === b.type && a.from_id === b.from_id && a.to_id === b.to_id;
  }

  function drawnNeighbour(graph, id) {
    for (const e of graph.edges) {
      const other = e.from_id === id ? e.to_id : e.to_id === id ? e.from_id : null;
      if (other !== null && simNodes.has(other) && simNodes.get(other).x !== undefined) {
        return simNodes.get(other);
      }
    }
    return null;
  }

  // -- view changes ---------------------------------------------------------------------------

  function userMovedView() {
    touched = true;
    follow = null;
  }

  function zoomAt(factor, px = 0, py = 0) {
    view = zoomView(view, factor, px, py);
    applyView();
  }

  /** Fit everything in view, and follow the layout again until the reader pans or zooms. */
  function fit() {
    touched = false;
    follow = null;
    track();
  }

  /** Put node `id` in the middle (and keep it there while the layout settles), zoomed in to at
   * least `minZoom` if given. */
  function centreOn(id, { minZoom = null } = {}) {
    if (!simNodes.has(id)) throw new Error(`concept #${id} is not on the canvas`);
    if (minZoom !== null && view.k < minZoom) view = { ...view, k: clampZoom(minZoom) };
    touched = true;
    follow = id;
    track();
  }

  /** Centre on concept `id` if it is not well inside the uncovered part of the canvas. */
  function reveal(id) {
    const n = simNodes.get(id);
    if (!n) throw new Error(`concept #${id} is not on the canvas`);
    const margin = Math.min(48, size.h / 6);
    const x = size.w / 2 + view.x + n.x * view.k;
    const y = size.h / 2 + view.y + n.y * view.k;
    if (x < margin || x > size.w - margin || y < margin || y > size.h - insets.bottom - margin) centreOn(id);
  }

  /** How much of the canvas is covered at the bottom (screen px); refits if still following. */
  function setInsets(next) {
    const bottom = Math.max(0, Math.min(next.bottom, size.h * 0.8));
    if (bottom === insets.bottom) return;
    insets = { bottom };
    measureOverlays(); // they sit above the sheet
    track();
  }

  function select(selection) {
    selected = selection;
    neighbours = selected?.kind === "node" ? neighboursOf(selected.id) : new Set();
    applyClasses();
    updateLabels();
  }

  function setHighlight(next) {
    highlight = next;
    applyClasses();
    updateLabels();
  }

  function isPinned(id) {
    const n = simNodes.get(id);
    return Boolean(n && n.fx !== null && n.fx !== undefined);
  }

  function unpin(id) {
    const n = simNodes.get(id);
    if (!n) throw new Error(`concept #${id} is not on the canvas`);
    n.fx = null;
    n.fy = null;
    applyClasses();
    onPinChange(id);
    kick(0.3);
  }

  // -- pointer input --------------------------------------------------------------------------

  const pointers = new Map(); // pointerId -> {x, y} (client coordinates)
  let gesture = null;
  let lastClick = { id: null, t: 0 };
  let holdTimer = null; // a finger on a concept: marks it draggable after LONG_PRESS_MS

  function endHold() {
    clearTimeout(holdTimer);
    holdTimer = null;
    for (const el of nodeEls.values()) el.g.classList.remove("is-held");
  }

  function local(clientX, clientY) {
    const r = svg.getBoundingClientRect();
    return { x: clientX - r.left - r.width / 2, y: clientY - r.top - r.height / 2 };
  }

  function targetOf(el) {
    const node = el.closest(".node");
    if (node) return { kind: "node", id: Number(node.dataset.id) };
    const rel = el.closest(".rel");
    if (rel) return { kind: "edge", id: Number(rel.dataset.id) };
    return { kind: "background" };
  }

  /** What a press means: what it hit, or for a finger that missed every circle, the nearest
   * concept within TOUCH_RADIUS_PX of it. */
  function pressTarget(e) {
    const hit = targetOf(e.target);
    if (hit.kind === "node" || e.pointerType !== "touch") return hit;
    const p = local(e.clientX, e.clientY);
    const id = nearestNode([...simNodes.values()], toGraph(view, p.x, p.y), {
      slop: Math.max(0, TOUCH_RADIUS_PX / view.k - R),
    });
    return id === null ? hit : { kind: "node", id };
  }

  function pinchState() {
    const [a, b] = [...pointers.values()];
    return { dist: Math.hypot(a.x - b.x, a.y - b.y) || 1, mid: local((a.x + b.x) / 2, (a.y + b.y) / 2) };
  }

  svg.addEventListener("pointerdown", (e) => {
    if (e.pointerType === "mouse" && e.button !== 0) return;
    svg.setPointerCapture(e.pointerId);
    pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (pointers.size === 2) {
      endHold();
      if (gesture?.target?.kind === "node" && !reduced) sim.alphaTarget(0);
      gesture = { kind: "pinch", ...pinchState() };
      return;
    }
    if (pointers.size > 2) return;
    const target = pressTarget(e);
    gesture = {
      kind: "press",
      target,
      x: e.clientX,
      y: e.clientY,
      moved: false,
      drag: false,
      pointerType: e.pointerType,
      t0: performance.now(),
    };
    endHold();
    if (e.pointerType === "touch" && target.kind === "node") {
      holdTimer = setTimeout(() => nodeEls.get(target.id)?.g.classList.add("is-held"), LONG_PRESS_MS);
    }
  });

  svg.addEventListener("pointermove", (e) => {
    if (!pointers.has(e.pointerId)) return;
    pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
    const g = gesture;
    if (!g) return;
    if (g.kind === "pinch") {
      if (pointers.size < 2) return;
      const now = pinchState();
      userMovedView();
      view = { ...view, x: view.x + now.mid.x - g.mid.x, y: view.y + now.mid.y - g.mid.y };
      zoomAt(now.dist / g.dist, now.mid.x, now.mid.y);
      Object.assign(g, now);
      return;
    }
    const dx = e.clientX - g.x;
    const dy = e.clientY - g.y;
    const threshold = g.pointerType === "touch" ? TOUCH_DRAG_THRESHOLD_PX : DRAG_THRESHOLD_PX;
    if (!g.moved && Math.hypot(dx, dy) < threshold) return;
    if (!g.moved) {
      g.drag = g.target.kind === "node" && pressDrags(g.pointerType, performance.now() - g.t0, LONG_PRESS_MS);
      if (!g.drag) endHold();
    }
    g.moved = true;
    if (g.drag) {
      // Stop fitting to the layout, or the view would shift under the node being dragged.
      userMovedView();
      const n = simNodes.get(g.target.id);
      const p = local(e.clientX, e.clientY);
      const at = toGraph(view, p.x, p.y);
      n.fx = at.x;
      n.fy = at.y;
      if (reduced) {
        n.x = at.x;
        n.y = at.y;
        render();
      } else {
        sim.alphaTarget(0.25).restart();
      }
      if (!nodeEls.get(g.target.id).g.classList.contains("is-pinned")) {
        applyClasses();
        onPinChange(g.target.id);
      }
    } else {
      userMovedView();
      g.x = e.clientX;
      g.y = e.clientY;
      view = { ...view, x: view.x + dx, y: view.y + dy };
      applyView();
    }
  });

  function pointerEnd(e) {
    if (!pointers.delete(e.pointerId)) return;
    endHold();
    const g = gesture;
    if (!g) return;
    if (g.kind === "pinch") {
      if (pointers.size < 2) gesture = null; // a lifted pinch never selects
      return;
    }
    gesture = null;
    if (!reduced) sim.alphaTarget(0);
    if (g.moved || e.type === "pointercancel") return; // dragged nodes stay where they are dropped
    const target = g.target;
    select(target.kind === "background" ? null : target);
    onSelect(selected);
    // Pointer capture sends dblclick to the svg, so double-clicks on nodes are detected here.
    if (target.kind === "node") {
      const now = Date.now();
      if (lastClick.id === target.id && now - lastClick.t < DOUBLE_CLICK_MS) {
        lastClick = { id: null, t: 0 };
        onExpand(target.id);
      } else {
        lastClick = { id: target.id, t: now };
      }
    }
  }
  svg.addEventListener("pointerup", pointerEnd);
  svg.addEventListener("pointercancel", pointerEnd);

  // Wheel zoom only with ctrl/cmd (a trackpad pinch arrives as a ctrl + wheel event).
  svg.addEventListener(
    "wheel",
    (e) => {
      if (!e.ctrlKey && !e.metaKey) return;
      e.preventDefault();
      userMovedView();
      const p = local(e.clientX, e.clientY);
      zoomAt(Math.exp(-e.deltaY * WHEEL_ZOOM_RATE), p.x, p.y);
    },
    { passive: false }
  );

  buttons.zoomIn.addEventListener("click", () => {
    userMovedView();
    zoomAt(BUTTON_ZOOM);
  });
  buttons.zoomOut.addEventListener("click", () => {
    userMovedView();
    zoomAt(1 / BUTTON_ZOOM);
  });
  buttons.fit.addEventListener("click", fit);

  /** Where the overlays (zoom buttons, legend) sit over the svg, so labels avoid them. */
  function measureOverlays() {
    const r = svg.getBoundingClientRect();
    blocked = overlays
      .map((el) => el.getBoundingClientRect())
      .filter((b) => b.width > 0 && b.height > 0)
      .map((b) => ({ x0: b.left - r.left, x1: b.right - r.left, y0: b.top - r.top, y1: b.bottom - r.top }));
  }

  const resized = new ResizeObserver(() => {
    size = { w: canvas.clientWidth, h: canvas.clientHeight };
    measureOverlays();
    track();
  });
  resized.observe(canvas);
  for (const el of overlays) resized.observe(el);

  /** The reader's standing per concept id (a Map of status, standing_model.js STATUSES): a
   * mark on the node (none while unstudied) and a line in its tooltip. */
  function setStanding(next) {
    standing = new Map(next);
    for (const [id, el] of nodeEls) {
      el.name = null; // rebuild the caption and tooltip
      updateNode(el, simNodes.get(id).data);
    }
    applyClasses();
    updateLabels(); // updateNode put back the clipped text
  }

  return {
    setGraph,
    select,
    setHighlight,
    setStanding,
    fit,
    centreOn,
    hold: userMovedView, // stop following the layout, as if the reader had panned
    reveal,
    setInsets,
    isPinned,
    unpin,
    has: (id) => simNodes.has(id),
  };
}

function round(v) {
  return Math.round(v * 100) / 100;
}
