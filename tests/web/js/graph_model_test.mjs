// Run by tests/web/test_pages.py (`node graph_model_test.mjs`): the concept graph page's pure
// logic: edge geometry, captions, the pair index, merging on expand, the roadmap highlight, the
// inspector's relation groups and the view arithmetic.

import assert from "node:assert/strict";

const STATIC = new URL("../../../src/teacher/web/static/", import.meta.url);
const m = await import(new URL("js/graph_model.js", STATIC));
const { R, ARROW_GAP } = m;

const close = (a, b, eps = 1e-6) => assert.ok(Math.abs(a - b) < eps, `${a} != ${b}`);
const dist = (x1, y1, p) => Math.hypot(x1 - p.x, y1 - p.y);

// -- edge geometry: straight ---------------------------------------------------------------------
{
  const s = { x: 0, y: 0 };
  const t = { x: 200, y: 0 };
  const g = m.edgeGeometry(s, t);
  assert.match(g.d, /^M[-\d.]+,[-\d.]+ L[-\d.]+,[-\d.]+$/);
  close(g.x1, R);
  close(g.y1, 0);
  close(g.x2, 200 - R);
  close(dist(g.x1, g.y1, s), R); // both ends on the circles
  close(dist(g.x2, g.y2, t), R);
  close(g.lx, 100);
  close(g.ly, 0);
  close(g.angle, 0);
  // with an arrow the end stops ARROW_GAP outside the target circle (room for the arrowhead)
  const a = m.edgeGeometry(s, t, { arrow: true });
  close(dist(a.x2, a.y2, t), R + ARROW_GAP);
  close(dist(a.x1, a.y1, s), R);
}
// labels stay upright: right-to-left and steep edges are flipped into [-90, 90]
{
  const left = m.edgeGeometry({ x: 200, y: 0 }, { x: 0, y: 0 });
  close(left.angle, 0);
  const upLeft = m.edgeGeometry({ x: 100, y: 100 }, { x: 0, y: 0 }); // atan2 = -135°
  close(upLeft.angle, 45);
  const downLeft = m.edgeGeometry({ x: 100, y: 0 }, { x: 0, y: 100 }); // atan2 = 135°
  close(downLeft.angle, -45);
  for (let deg = -180; deg <= 180; deg += 15) {
    const rad = (deg * Math.PI) / 180;
    const g = m.edgeGeometry({ x: 0, y: 0 }, { x: Math.cos(rad) * 150, y: Math.sin(rad) * 150 });
    assert.ok(g.angle >= -90 && g.angle <= 90, `angle ${g.angle} for ${deg}°`);
  }
  // diagonal: endpoints still on the circles
  const s = { x: 10, y: 20 };
  const t = { x: 130, y: 180 };
  const g = m.edgeGeometry(s, t, { arrow: true });
  close(dist(g.x1, g.y1, s), R);
  close(dist(g.x2, g.y2, t), R + ARROW_GAP);
}

// -- edge geometry: curved parallel edges ----------------------------------------------------
{
  const s = { x: 0, y: 0 };
  const t = { x: 200, y: 0 };
  const two = [
    { id: 1, from_id: 1, to_id: 2 },
    { id: 2, from_id: 2, to_id: 1 },
    { id: 3, from_id: 3, to_id: 4 },
  ];
  const pairs = m.pairIndex(two);
  assert.deepEqual(pairs.get(1), { i: 0, n: 2 });
  assert.deepEqual(pairs.get(2), { i: 1, n: 2 });
  assert.deepEqual(pairs.get(3), { i: 0, n: 1 });

  const single = m.edgeGeometry(s, t, { slot: pairs.get(3), fromId: 3, toId: 4 });
  assert.ok(single.d.includes(" L"), "a lone edge is straight");

  const g1 = m.edgeGeometry(s, t, { slot: pairs.get(1), fromId: 1, toId: 2 });
  const g2 = m.edgeGeometry(t, s, { slot: pairs.get(2), fromId: 2, toId: 1, arrow: true });
  for (const g of [g1, g2]) assert.match(g.d, /^M[-\d.]+,[-\d.]+ Q[-\d.]+,[-\d.]+ [-\d.]+,[-\d.]+$/);
  // control points: the midpoint offset sideways by BEND/2, on opposite sides for the two slots
  // (the side does not depend on the edge's direction, so they never overlap)
  const ctrl = (g) => g.d.match(/Q([-\d.]+),([-\d.]+)/).slice(1).map(Number);
  const [c1x, c1y] = ctrl(g1);
  const [c2x, c2y] = ctrl(g2);
  close(c1x, 100);
  close(c2x, 100);
  close(Math.abs(c1y), m.BEND / 2);
  // edge 2 runs the other way (t -> s) in the other slot: its curve is on the other side
  close(c1y, -c2y);
  assert.ok(c1y * c2y < 0);
  assert.ok(g1.ly * g2.ly < 0, "labels on opposite sides of the chord");
  // ends on the circles, heading to the control point
  close(dist(g1.x1, g1.y1, s), R);
  close(dist(g1.x2, g1.y2, t), R);
  close(dist(g2.x2, g2.y2, s), R + ARROW_GAP);
  // label at the curve midpoint, upright
  close(g1.lx, (g1.x1 + 2 * c1x + g1.x2) / 4);
  assert.ok(g1.angle >= -90 && g1.angle <= 90 && g2.angle >= -90 && g2.angle <= 90);
}
// coincident nodes do not produce NaN
{
  const g = m.edgeGeometry({ x: 5, y: 5 }, { x: 5, y: 5 });
  assert.ok(!g.d.includes("NaN"), g.d);
}

// -- captions --------------------------------------------------------------------------------
assert.deepEqual(m.caption("Attention"), ["Attention"]); // 9 chars: one line
assert.deepEqual(m.caption("Softmax"), ["Softmax"]);
assert.deepEqual(m.caption("Self attention"), ["Self", "attention"]);
assert.deepEqual(m.caption("Scaled dot-product attention"), ["Scaled", "dot-prod…"]);
assert.deepEqual(m.caption("Multi-head attention"), ["Multi-", "head att…"]);
assert.deepEqual(m.caption("Backpropagation"), ["Backprop…"]); // one long word: clipped, one line
assert.deepEqual(m.caption("KL divergence"), ["KL", "divergen…"]);
assert.deepEqual(m.caption("Layer norm"), ["Layer", "norm"]);
for (const name of ["Transformer architecture", "Q-learning", "a b c d e f g h i j k l"]) {
  const lines = m.caption(name);
  assert.ok(lines.length >= 1 && lines.length <= 2, name);
  for (const line of lines) assert.ok(line.length <= m.CAPTION_CHARS, `${name}: ${line}`);
}
assert.deepEqual(m.caption(42), ["42"]);

// -- merging on expand -----------------------------------------------------------------------
const node = (id, extra = {}) => ({ id, slug: `c${id}`, name: `C${id}`, origin: "paper", reviewed: false, ...extra });
const edge = (id, from_id, to_id, type = "builds_on", extra = {}) => ({
  id, type, from_id, to_id, origin: "model", rationale: "Why.", reviewed: false, reviewed_at: null, redundant: false, ...extra,
});
{
  let g = { nodes: [], edges: [] };
  g = m.mergeGraph(g, { nodes: [node(1), node(2)], edges: [edge(10, 1, 2)] });
  assert.deepEqual(g.nodes.map((n) => n.id), [1, 2]);
  assert.equal(g.nodes[0].anchor, undefined);
  // expanding 2: its neighbours 1 (already there) and 3, plus 3's relation to 4 (not drawn)
  const expanded = m.mergeGraph(
    g,
    { nodes: [node(2), node(1), node(3)], edges: [edge(10, 1, 2), edge(11, 3, 2, "relates_to"), edge(12, 3, 4)] },
    { anchor: 2 }
  );
  assert.deepEqual(expanded.nodes.map((n) => n.id), [1, 2, 3]); // no duplicates, order kept
  assert.deepEqual(expanded.edges.map((e) => e.id), [10, 11, 12]);
  assert.equal(expanded.nodes[2].anchor, 2); // new nodes are placed next to the expanded one
  assert.equal(expanded.nodes[0].anchor, undefined); // existing ones keep their place
  assert.equal(expanded.nodes[1].anchor, undefined); // and the anchor is never its own anchor
  assert.deepEqual(m.visibleEdges(expanded).map((e) => e.id), [10, 11]); // 12 waits for node 4
  assert.equal(g.nodes.length, 2, "the input graph is not changed");
  // merging the same data again changes nothing; new data for an id replaces the old
  const again = m.mergeGraph(expanded, { nodes: [node(3)], edges: [edge(11, 3, 2, "relates_to")] });
  assert.deepEqual(again.nodes.map((n) => n.id), [1, 2, 3]);
  assert.deepEqual(again.edges.map((e) => e.id), [10, 11, 12]);
  const renamed = m.mergeGraph(expanded, { nodes: [node(1, { name: "Renamed", reviewed: true })] });
  assert.equal(renamed.nodes[0].name, "Renamed");
  assert.equal(renamed.nodes[0].reviewed, true);
  // node 4 arrives: edge 12 is drawn without being sent again
  const withFour = m.mergeGraph(again, { nodes: [node(4)] });
  assert.deepEqual(m.visibleEdges(withFour).map((e) => e.id), [10, 11, 12]);
  // hiding keeps the relations known, so showing the concept again reconnects it
  const hidden = m.hideNode(withFour, 3);
  assert.deepEqual(hidden.nodes.map((n) => n.id), [1, 2, 4]);
  assert.deepEqual(m.visibleEdges(hidden).map((e) => e.id), [10]);
  assert.deepEqual(m.visibleEdges(m.mergeGraph(hidden, { nodes: [node(3)] })).map((e) => e.id), [10, 11, 12]);
  assert.deepEqual([...m.degrees(hidden)], [[1, 1], [2, 1], [4, 0]]);
  // malformed data fails loudly
  assert.throws(() => m.mergeGraph(g, { nodes: [{ slug: "x" }] }), /without an integer id/);
  assert.throws(() => m.mergeGraph(g, { edges: [{ id: 5, from_id: 1 }] }), /from_id\/to_id/);
}

// -- roadmap highlight -----------------------------------------------------------------------
{
  // t builds on a and b, a builds on c; t -> c is redundant; x relates to t; y builds on t
  const edges = [
    edge(1, 10, 11), // t -> a
    edge(2, 10, 12), // t -> b
    edge(3, 11, 13), // a -> c
    edge(4, 10, 13, "builds_on", { redundant: true }), // t -> c
    edge(5, 10, 14, "relates_to"), // t ~ x
    edge(6, 15, 10), // y -> t (y is not on t's roadmap)
    edge(7, 11, 12, "different_version_of"), // a = b: not a builds_on edge
  ];
  const roadmap = [node(12), node(13), node(11), node(10)]; // b, c, a, t in study order
  const hl = m.roadmapHighlight(roadmap, edges);
  assert.equal(hl.target, 10);
  assert.deepEqual(hl.order, [12, 13, 11, 10]);
  assert.deepEqual([...hl.lit].sort(), [10, 11, 12, 13]);
  assert.deepEqual([...hl.hot].sort(), [1, 2, 3, 4]);
  const alone = m.roadmapHighlight([node(13)], edges);
  assert.deepEqual([...alone.lit], [13]);
  assert.deepEqual([...alone.hot], []);
  assert.throws(() => m.roadmapHighlight([], edges), /empty/);
}

// -- relation groups and counts --------------------------------------------------------------
{
  const rel = (id, type, direction) => ({ id, type, direction, other: node(id) });
  const groups = m.relationGroups([
    rel(1, "relates_to", "symmetric"),
    rel(2, "builds_on", "in"),
    rel(3, "builds_on", "out"),
    rel(4, "builds_on", "out"),
    rel(5, "different_version_of", "symmetric"),
  ]);
  assert.deepEqual(groups.map((g) => [g.label, g.items.map((r) => r.id)]), [
    ["Builds on", [3, 4]],
    ["Built on by", [2]],
    ["Different version of", [5]],
    ["Related to", [1]],
  ]);
  assert.deepEqual(m.relationGroups([]), []);
  assert.throws(() => m.relationGroups([rel(9, "builds_on", "symmetric")]), /unknown relation/);
  const counts = m.countBy([{ o: "user" }, { o: "paper" }, { o: "user" }], "o", m.ORIGINS);
  assert.deepEqual([...counts], [["paper", 1], ["model", 0], ["user", 2]]);
}

// -- view arithmetic -------------------------------------------------------------------------
{
  assert.equal(m.fitView([], { w: 800, h: 600 }), null);
  assert.equal(m.fitView([{ x: 0, y: 0 }], { w: 0, h: 0 }), null);
  const small = m.fitView([{ x: -10, y: 0 }, { x: 10, y: 0 }], { w: 800, h: 600 });
  assert.deepEqual(small, { k: 1, x: -0, y: -0 }); // never zooms in past 1
  const wide = m.fitView([{ x: 0, y: 0 }, { x: 2000, y: 0 }], { w: 800, h: 600 });
  close(wide.k, (800 - m.FIT_MARGIN) / (2000 + 2 * R));
  close(wide.x, -1000 * wide.k); // centred
  const huge = m.fitView([{ x: 0, y: 0 }, { x: 1e6, y: 0 }], { w: 800, h: 600 });
  assert.equal(huge.k, m.ZOOM_MIN);
  // zooming about a point keeps that point still
  const v = { x: 30, y: -20, k: 1 };
  const z = m.zoomView(v, 2, 100, 50);
  const before = m.toGraph(v, 100, 50);
  const after = m.toGraph(z, 100, 50);
  close(before.x, after.x);
  close(before.y, after.y);
  assert.equal(m.zoomView(v, 1000).k, m.ZOOM_MAX);
  assert.equal(m.zoomView(v, 0.0001).k, m.ZOOM_MIN);
  const c = m.centreView({ x: 0, y: 0, k: 2 }, { x: 10, y: -5 });
  assert.deepEqual(m.toGraph(c, 0, 0), { x: 10, y: -5 });
}

console.log("graph ok");
