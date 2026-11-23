// Run by tests/web/test_pages.py (`node graph_relate_test.mjs`): the pure logic behind the
// concept inspector's "Find relations" and "Regenerate relations" buttons: relation phrases, the
// result summary (added, none, refused, suggestions by slot; for a regenerate run what each
// added relation replaces, what was removed and kept), what a regenerate would replace, merging
// the result into the canvas (deleted relations dropped), which cached concept details it makes
// stale, and the status line.

import assert from "node:assert/strict";

const STATIC = new URL("../../../src/teacher/web/static/", import.meta.url);
const m = await import(new URL("js/graph_model.js", STATIC));

const node = (id, slug = `c${id}`) => ({ id, slug, name: slug.toUpperCase(), origin: "paper", reviewed: false });
const edge = (id, type, from_id, to_id) => ({ id, type, from_id, to_id, origin: "model", rationale: "Why.", reviewed: false, reviewed_at: null, redundant: false });

const focus = node(1, "attention");
const result = {
  concept: focus,
  model: "claude-test",
  regenerate: false,
  removed: [],
  kept: [],
  deleted_edge_ids: [],
  judge_called: true,
  added: [
    { edge: edge(10, "builds_on", 1, 2), type: "builds_on", direction: "out", other: node(2, "softmax"), rationale: "Uses it." },
    { edge: edge(11, "builds_on", 3, 1), type: "builds_on", direction: "in", other: node(3, "transformer"), rationale: "Builds on it." },
    { edge: edge(12, "relates_to", 1, 4), type: "relates_to", direction: "symmetric", other: node(4, "rnn"), rationale: "Contrast." },
  ],
  refused: [
    { other: node(5, "dot"), type: "builds_on", direction: "in", rationale: "Claimed.", reason: "would make builds_on cyclic: a -> b -> a", cycle: ["a", "b", "a"] },
  ],
  judged_none: [{ other: node(6), reason: "No." }, { other: node(7), reason: "No." }],
  unmatched: [
    { slot: "relates_to", name: "kernels", aliases: [], description: "D." },
    { slot: "builds_on", name: "linear algebra", aliases: [], description: "D." },
    { slot: "builds_on", name: "probability", aliases: [], description: "D." },
  ],
  counts: { judged: 6, embedding: 5, proposal: 3, dropped: { self: 0, related: 1, judged: 2, implied: 0, cap: 1 } },
  nodes: [focus, node(2, "softmax"), node(3, "transformer"), node(4, "rnn")],
  edges: [edge(10, "builds_on", 1, 2), edge(11, "builds_on", 3, 1), edge(12, "relates_to", 1, 4)],
};

// -- phrases ---------------------------------------------------------------------------------
{
  const other = { name: "Softmax" };
  const f = { name: "Attention" };
  assert.equal(m.relationPhrase(f, other, "builds_on", "out"), "Attention builds on Softmax");
  assert.equal(m.relationPhrase(f, other, "builds_on", "in"), "Softmax builds on Attention");
  assert.equal(m.relationPhrase(f, other, "different_version_of", "symmetric"), "Attention different version of Softmax");
  assert.throws(() => m.relationPhrase(f, other, "same_as", "out"), /unknown relation type/);
  assert.throws(() => m.relationPhrase(f, other, "builds_on", "up"), /unknown relation direction/);
  assert.throws(() => m.relationPhrase(f, other, "builds_on", "symmetric"), /has a direction/);
  assert.throws(() => m.relationPhrase(f, other, "relates_to", "out"), /has no direction/);
}

// -- the summary -----------------------------------------------------------------------------
{
  const s = m.relateSummary(result);
  assert.deepEqual(s.added.map((a) => [a.edge.id, a.phrase]), [
    [10, "ATTENTION builds on SOFTMAX"],
    [11, "TRANSFORMER builds on ATTENTION"],
    [12, "ATTENTION relates to RNN"],
  ]);
  assert.equal(s.none, 2);
  assert.deepEqual(s.refused.map((r) => [r.other.slug, r.phrase, r.reason]), [
    ["dot", "DOT builds on ATTENTION", "would make builds_on cyclic: a -> b -> a"],
  ]);
  // grouped by slot in the fixed order, empty slots left out
  assert.deepEqual(s.suggestions.map((g) => [g.slot, g.label, g.items.map((u) => u.name)]), [
    ["builds_on", "Prerequisites", ["linear algebra", "probability"]],
    ["relates_to", "Related", ["kernels"]],
  ]);
  assert.equal(s.judged, 6);
  assert.equal(s.dropped, 4);
  assert.equal(s.judgeCalled, true);
  assert.throws(() => m.relateSummary({ ...result, unmatched: [{ slot: "cousins", name: "x" }] }), /unknown suggestion slot/);
  assert.throws(() => m.relateSummary({ ...result, added: [{ ...result.added[0], edge: null }] }), /has no edge/);
  assert.throws(() => m.relateSummary({}), /not a relate result/);
  const { removed: _r, ...withoutRemoved } = result;
  assert.throws(() => m.relateSummary(withoutRemoved), /not a relate result/);
  assert.equal(s.regenerate, false);
  assert.deepEqual(s.added.map((a) => a.replaces), [null, null, null]);
  assert.deepEqual([s.removed, s.kept], [[], []]);
  const empty = m.relateSummary({ ...result, added: [], refused: [], judged_none: [], unmatched: [], judge_called: false });
  assert.deepEqual([empty.added, empty.refused, empty.suggestions, empty.none], [[], [], [], 0]);
}

// -- merging into the canvas -----------------------------------------------------------------
{
  const graph = { nodes: [focus, node(2, "softmax")], edges: [edge(9, "relates_to", 2, 8)] };
  const merged = m.mergeRelateResult(graph, result, 1);
  assert.deepEqual(merged.nodes.map((n) => n.id), [1, 2, 3, 4]);
  assert.equal(merged.nodes.find((n) => n.id === 2).anchor, undefined); // was already shown
  assert.equal(merged.nodes.find((n) => n.id === 3).anchor, 1); // placed next to the concept
  assert.deepEqual(merged.edges.map((e) => e.id), [9, 10, 11, 12]);
  assert.deepEqual(m.visibleEdges(merged).map((e) => e.id), [10, 11, 12]);
  assert.deepEqual(m.relateTouched(result), ["attention", "softmax", "transformer", "rnn"]);
  assert.deepEqual(m.relateTouched({ ...result, added: [] }), ["attention"]);
  assert.throws(() => m.mergeRelateResult(graph, { ...result, deleted_edge_ids: undefined }, 1), /no deleted_edge_ids/);
  assert.equal(m.relateMessage(result, "Attention"), "Attention: 3 relations added, 2 judged unrelated, 1 refused.");
}

// -- regenerate --------------------------------------------------------------------------------
const regenerated = {
  ...result,
  regenerate: true,
  added: [
    // the same relation as before (re-judged), a changed one (relates_to -> builds_on), a new one
    { ...result.added[0], replaces: { relation_id: 20, type: "builds_on", direction: "out", rationale: "Old.", type_changed: false, direction_changed: false } },
    { ...result.added[1], replaces: { relation_id: 21, type: "relates_to", direction: "symmetric", rationale: "Old.", type_changed: true, direction_changed: true } },
    { ...result.added[2], replaces: null },
  ],
  removed: [
    { relation_id: 22, type: "builds_on", direction: "in", other: node(8, "gru"), rationale: "Old.", reason: "none", detail: "Different jobs." },
    { relation_id: 23, type: "builds_on", direction: "out", other: node(9, "vectors"), rationale: "Old.", reason: "implied", detail: "builds_on path attention -> dot -> vectors" },
  ],
  kept: [
    { relation_id: 30, type: "relates_to", direction: "symmetric", other: node(10, "lstm"), rationale: "Kept.", origin: "paper", reviewed_at: "2026-01-01T00:00:00+00:00", because: ["reviewed", "origin paper"] },
  ],
  deleted_edge_ids: [20, 21, 22, 23],
  counts: { ...result.counts, existing: 4 },
};
{
  const s = m.relateSummary(regenerated);
  assert.equal(s.regenerate, true);
  assert.deepEqual(s.added.map((a) => a.replaces), [
    { phrase: "ATTENTION builds on SOFTMAX", changed: false },
    { phrase: "ATTENTION relates to TRANSFORMER", changed: true },
    null,
  ]);
  assert.deepEqual(s.removed.map((r) => [r.other.slug, r.phrase, r.reason, r.label, r.detail]), [
    ["gru", "GRU builds on ATTENTION", "none", "judged unrelated", "Different jobs."],
    ["vectors", "ATTENTION builds on VECTORS", "implied", "a builds-on path already joins them", "builds_on path attention -> dot -> vectors"],
  ]);
  assert.deepEqual(s.kept.map((k) => [k.phrase, k.because]), [["ATTENTION relates to LSTM", "reviewed, from a paper"]]);
  assert.throws(() => m.relateSummary({ ...regenerated, removed: [{ ...regenerated.removed[0], reason: "bored" }] }), /unknown removal reason/);
  assert.throws(() => m.relateSummary({ ...regenerated, kept: [{ ...regenerated.kept[0], because: ["liked"] }] }), /unknown reason to keep/);
  assert.throws(() => m.relateSummary({ ...regenerated, kept: [{ ...regenerated.kept[0], because: [] }] }), /says not why/);
  assert.equal(
    m.relateMessage(regenerated, "Attention"),
    "Attention: relations regenerated: 3 added (2 replacing an old one), 2 removed, 1 kept, 2 judged unrelated, 1 refused."
  );

  // On the canvas: the deleted relations disappear (removed and replaced), the new ones appear,
  // relations of other concepts stay.
  const graph = {
    nodes: [focus, node(2, "softmax"), node(3, "transformer"), node(8, "gru"), node(9, "vectors")],
    edges: [
      edge(20, "builds_on", 1, 2),
      edge(21, "relates_to", 1, 3),
      edge(22, "builds_on", 8, 1),
      edge(23, "builds_on", 1, 9),
      edge(24, "builds_on", 8, 9),
    ],
  };
  const merged = m.mergeRelateResult(graph, regenerated, 1);
  assert.deepEqual(merged.edges.map((e) => e.id), [24, 10, 11, 12]);
  assert.deepEqual(m.visibleEdges(merged).map((e) => e.id), [24, 10, 11, 12]);
  assert.deepEqual(merged.nodes.map((n) => n.id), [1, 2, 3, 8, 9, 4]);
  assert.equal(merged.nodes.find((n) => n.id === 4).anchor, 1);
  assert.deepEqual(m.visibleEdges(m.hideNode(merged, 4)).map((e) => e.id), [24, 10, 11]);
  // SQLite may give a new relation the id of one the run deleted: the old one still goes first.
  const reused = {
    ...regenerated,
    added: [{ ...regenerated.added[2], edge: edge(22, "relates_to", 1, 4) }],
    edges: [edge(22, "relates_to", 1, 4)],
    nodes: [focus, node(4, "rnn")],
  };
  const again = m.mergeRelateResult(graph, reused, 1);
  assert.deepEqual(again.edges.map((e) => [e.id, e.type, e.from_id, e.to_id]), [
    [24, "builds_on", 8, 9],
    [22, "relates_to", 1, 4],
  ]);
  // The cached details of every concept whose relations changed are dropped.
  assert.deepEqual(m.relateTouched(regenerated), ["attention", "softmax", "transformer", "rnn", "gru", "vectors"]);
}

// -- what a regenerate would replace -------------------------------------------------------------
{
  const rel = (id, origin, reviewed_at = null) => ({ id, origin, reviewed_at });
  assert.deepEqual(
    m.regenerateScope([
      rel(1, "model"),
      rel(2, "model", "2026-01-01T00:00:00+00:00"),
      rel(3, "paper"),
      rel(4, "user"),
      rel(5, "paper", "2026-01-01T00:00:00+00:00"),
      rel(6, "model"),
    ]),
    { replaceable: 2, kept: 4 }
  );
  assert.deepEqual(m.regenerateScope([]), { replaceable: 0, kept: 0 });
  assert.throws(() => m.regenerateScope([rel(1, "robot")]), /unknown relation origin/);
  assert.throws(() => m.regenerateScope(undefined), /no relations list/);
}

console.log("graph relate ok");
