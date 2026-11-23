// The force-directed SVG drawing of the concept graph (ported from the context-graph explorer's
// GraphView): a d3-force simulation, fit-to-view that follows the layout until the reader pans,
// zooms or drags a node (the Fit button turns following back on), ctrl/cmd + wheel and pinch zoom, background drag to pan, node drag to pin, click to
// select a node or relation, double-click to expand, and the roadmap highlight ("tracing": all
// but the lit concepts and hot relations dimmed). `setStanding(map)` marks each concept with
// the reader's standing (a dot at the node's lower right; standing.css).
//
// Elements are created once per node and edge when the graph changes (`setGraph`); simulation
// ticks only move them (transforms and path data), they never rebuild the SVG.
//
// `createGraphView({svg, canvas, buttons: {zoomIn, zoomOut, fit}, onSelect, onExpand,
// onPinChange})`;
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
  centreView,
  degrees,
  edgeGeometry,
  fitView,
  pairIndex,
  toGraph,
  visibleEdges,
  zoomView,
} from "./graph_model.js";
import { STATUSES, statusLabel } from "./standing_model.js";

const DOUBLE_CLICK_MS = 400;
const DRAG_THRESHOLD_PX = 3;
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

export function createGraphView({ svg, canvas, buttons, onSelect, onExpand, onPinChange }) {
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

  const edgeLayer = s("g", { class: "graph-edges" });
  const nodeLayer = s("g", { class: "graph-nodes" });
  const viewport = s("g", { class: "graph-viewport" }, edgeLayer, nodeLayer);
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
      view = centreView(view, simNodes.get(follow));
    } else if (!touched) {
      const fitted = fitView([...simNodes.values()], size);
      if (fitted) view = fitted;
    }
    applyView();
  }

  function applyView() {
    viewport.setAttribute(
      "transform",
      `translate(${size.w / 2 + view.x},${size.h / 2 + view.y}) scale(${view.k})`
    );
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
  }

  function nodeElement(n) {
    const text = s("text", { class: "node-caption", "text-anchor": "middle" });
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
    return { g, text, title, name: null };
  }

  function updateNode(el, n) {
    if (el.name !== n.name) {
      const lines = caption(n.name);
      el.text.replaceChildren(
        ...lines.map((line, i) =>
          s("tspan", { x: 0, dy: i === 0 ? (lines.length === 1 ? 4 : -2) : 12 }, line)
        )
      );
      el.name = n.name;
    }
    const status = standing.get(n.id);
    el.title.textContent =
      `${n.name}\n${n.origin} concept, ${n.reviewed ? "reviewed" : "unreviewed"}\n` +
      (status ? `Your standing: ${statusLabel(status)}\n` : "") +
      "Click to inspect, double-click to expand, drag to pin.";
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

  /** Put node `id` in the middle (and keep it there while the layout settles). */
  function centreOn(id) {
    if (!simNodes.has(id)) throw new Error(`concept #${id} is not on the canvas`);
    touched = true;
    follow = id;
    track();
  }

  function select(selection) {
    selected = selection;
    applyClasses();
  }

  function setHighlight(next) {
    highlight = next;
    applyClasses();
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

  function pinchState() {
    const [a, b] = [...pointers.values()];
    return { dist: Math.hypot(a.x - b.x, a.y - b.y) || 1, mid: local((a.x + b.x) / 2, (a.y + b.y) / 2) };
  }

  svg.addEventListener("pointerdown", (e) => {
    if (e.pointerType === "mouse" && e.button !== 0) return;
    svg.setPointerCapture(e.pointerId);
    pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (pointers.size === 2) {
      if (gesture?.target?.kind === "node" && !reduced) sim.alphaTarget(0);
      gesture = { kind: "pinch", ...pinchState() };
      return;
    }
    if (pointers.size > 2) return;
    gesture = { kind: "press", target: targetOf(e.target), x: e.clientX, y: e.clientY, moved: false };
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
    if (!g.moved && Math.hypot(dx, dy) < DRAG_THRESHOLD_PX) return;
    g.moved = true;
    if (g.target.kind === "node") {
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

  new ResizeObserver(([entry]) => {
    size = { w: entry.contentRect.width, h: entry.contentRect.height };
    track();
  }).observe(canvas);

  /** The reader's standing per concept id (a Map of status, standing_model.js STATUSES): a
   * mark on the node (none while unstudied) and a line in its tooltip. */
  function setStanding(next) {
    standing = new Map(next);
    for (const [id, el] of nodeEls) {
      el.name = null; // rebuild the caption and tooltip
      updateNode(el, simNodes.get(id).data);
    }
    applyClasses();
  }

  return {
    setGraph,
    select,
    setHighlight,
    setStanding,
    fit,
    centreOn,
    isPinned,
    unpin,
    has: (id) => simNodes.has(id),
  };
}

function round(v) {
  return Math.round(v * 100) / 100;
}
