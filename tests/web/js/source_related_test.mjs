// Run by tests/web/test_source_graph_api.py (`node source_related_test.mjs`): the pure helpers
// behind the source page's "Related sources" section (source_related_model.js).

import assert from "node:assert/strict";

const STATIC = new URL("../../../src/teacher/web/static/", import.meta.url);
const m = await import(new URL("js/source_related_model.js", STATIC));

const src = (slug, title = slug.toUpperCase()) => ({ slug, title, kind: "paper" });
// No references extracted anywhere: what the relations say about citations then.
const NO_REFS = {
  references: { state: "never_run", stored: false, reference_count: null },
  cites: null, cited_by: [], references_not_extracted: [],
};

// words: unknown roles are errors, never guesses
assert.equal(m.roleWord("assumes"), "assumed");
assert.throws(() => m.roleWord("mentions"), /unknown concept role mentions/);
assert.equal(m.listText([]), "");
assert.equal(m.listText(["a"]), "a");
assert.equal(m.listText(["a", "b", "c"]), "a, b and c");
assert.equal(
  m.conceptsText([{ slug: "x", name: "X", role: "uses" }, { slug: "y", name: "Y", role: "assumes" }]),
  "X (used) and Y (assumed)"
);
assert.throws(() => m.conceptsText([]), /without concepts/);
assert.equal(
  m.sharedText([
    { slug: "x", name: "X", role: "introduces", other_role: "introduces" },
    { slug: "y", name: "Y", role: "uses", other_role: "introduces" },
  ]),
  "X (introduced in both) and Y (used here, introduced there)"
);
assert.equal(m.weightText(0.4249), "42% overlap");
assert.equal(m.weightText(0.001), "1% overlap"); // never "0%" for a real overlap
assert.throws(() => m.weightText(0), /invalid related weight/);
assert.throws(() => m.weightText(1.5), /invalid related weight/);
assert.equal(m.sourceHref("a b"), "/sources/a%20b");
assert.equal(m.pageHref("p"), "/artifacts/p");

// not analysed: its own state, never "no relations"
const notAnalysed = m.relatedView({
  ...NO_REFS,
  analysed: false, builds_on: null, built_on_by: null, related: null, readiness: null,
  not_analysed: [src("other", "Other")],
});
assert.equal(notAnalysed.state, "not_analysed");
assert.match(notAnalysed.message, /^Not analysed yet: generate a study page to extract its concepts/);
assert.equal(notAnalysed.notAnalysedNote, "1 other source is not analysed yet, so it was not compared: Other.");
assert.throws(
  () => m.relatedView({ ...NO_REFS, analysed: false, builds_on: [], built_on_by: null, related: null, readiness: null, not_analysed: [] }),
  /unanalysed source with builds_on/
);
assert.throws(() => m.relatedView({ not_analysed: [] }), /no analysed flag/);
assert.throws(() => m.relatedView({ ...NO_REFS, analysed: true, not_analysed: [], builds_on: [], built_on_by: [], related: [] }),
  /no readiness/);

// analysed but alone
const alone = m.relatedView({
  ...NO_REFS,
  analysed: true, builds_on: [], built_on_by: [], related: [],
  readiness: { assumed: 0, read_first: [] }, not_analysed: [src("p"), src("q")],
});
assert.equal(alone.state, "alone");
assert.equal(alone.message, "No other analysed source shares a concept with this one yet.");
assert.deepEqual(alone.groups, []);
assert.equal(alone.readinessNote, "It assumes no background concepts.");
assert.equal(alone.notAnalysedNote, "2 other sources are not analysed yet, so they were not compared: P and Q.");
const known = m.relatedView({
  ...NO_REFS,
  analysed: true, builds_on: [], built_on_by: [], related: [],
  readiness: { assumed: 3, read_first: [] }, not_analysed: [],
});
assert.equal(known.readinessNote, "You are in good standing on all 3 concepts it assumes.");
assert.equal(known.notAnalysedNote, null);

// related: groups, the concepts behind each item, Related not repeating what is listed above
const view = m.relatedView({
  ...NO_REFS,
  analysed: true,
  builds_on: [{ source: src("intro", "Intro"), concepts: [{ slug: "s", name: "softmax", role: "assumes" }] }],
  built_on_by: [],
  related: [
    { source: src("intro", "Intro"), weight: 0.43, shared: [{ slug: "s", name: "softmax", role: "assumes", other_role: "introduces" }] },
    { source: src("side", "Side"), weight: 0.2, shared: [{ slug: "t", name: "tensor", role: "uses", other_role: "uses" }] },
  ],
  readiness: {
    assumed: 2,
    read_first: [
      {
        concept: { slug: "s", name: "softmax" }, status: "weak",
        introduced_by: [{
          source: src("intro", "Intro"), has_ready_page: true,
          study_pages: [{ slug: "old", title: "Old", superseded: true }, { slug: "new", title: "New", superseded: false }],
        }],
      },
      { concept: { slug: "z", name: "zeta" }, status: "unstudied", introduced_by: [] },
    ],
  },
  not_analysed: [],
});
assert.equal(view.state, "related");
assert.equal(view.message, null);
assert.deepEqual(view.groups.map((g) => g.key), ["builds_on", "related"]);
assert.deepEqual(view.groups[0].items, [
  { slug: "intro", title: "Intro", href: "/sources/intro", text: "Through softmax (assumed)." },
]);
assert.deepEqual(view.groups[1].items.map((i) => [i.slug, i.text]), [
  ["side", "20% overlap: shares tensor (used in both)."],
]);
assert.equal(view.groups[1].omitted, 1);
const [softmax, zeta] = view.readFirst;
assert.equal(softmax.statusText, "Weak");
assert.equal(softmax.href, "/graph?concept=s");
assert.deepEqual(softmax.introducers, [
  { slug: "intro", title: "Intro", href: "/sources/intro", page: { slug: "new", title: "New", href: "/artifacts/new" } },
]);
assert.equal(softmax.note, null);
assert.equal(zeta.statusText, "Not studied yet");
assert.match(zeta.note, /Nothing in the library introduces it yet/);
assert.throws(
  () => m.readFirstItem({ concept: { slug: "a", name: "a" }, status: "great", introduced_by: [] }),
  /unknown standing status great/
);
assert.throws(
  () => m.readFirstItem({
    concept: { slug: "a", name: "a" }, status: "weak",
    introduced_by: [{ source: src("x"), has_ready_page: true, study_pages: [] }],
  }),
  /inconsistent study pages for x/
);
const noPage = m.readFirstItem({
  concept: { slug: "a", name: "a" }, status: "weak",
  introduced_by: [{ source: src("x"), has_ready_page: false, study_pages: [] }],
});
assert.equal(noPage.introducers[0].page, null);

// citations: unknown until references are stored, never an empty list in disguise
const refs = (state, stored, extra = {}) => ({
  references: { state, stored, reference_count: stored ? 3 : null },
  cites: stored ? [] : null, cited_by: [], references_not_extracted: [], ...extra,
});
for (const [state, pattern] of [
  ["never_run", /have not been extracted yet/],
  ["running", /are being extracted/],
  ["interrupted", /was interrupted/],
  ["failed", /extraction failed/],
]) {
  const c = m.citationsView(refs(state, false));
  assert.equal(c.cites.items, null, state);
  assert.match(c.cites.note, pattern);
}
const none = m.citationsView(refs("done", true));
assert.deepEqual(none.cites, { items: [], note: "It cites no other source in the library." });
assert.equal(none.citedBy.note, "No source in the library cites it.");
const some = m.citationsView(refs("failed", true, {
  cites: [{ source: src("a", "Alpha") }],
  cited_by: [{ source: src("b", "Beta") }],
  references_not_extracted: [src("c", "Gamma"), src("d", "Delta")],
}));
assert.deepEqual(some.cites.items, [{ slug: "a", title: "Alpha", href: "/sources/a" }]);
assert.match(some.cites.note, /latest extraction of its references failed; these are from an earlier one/);
assert.deepEqual(some.citedBy.items, [{ slug: "b", title: "Beta", href: "/sources/b" }]);
assert.equal(some.citedBy.note, "2 other sources have no extracted references, so it is not known whether they cite this source: Gamma and Delta.");
assert.throws(() => m.citationsView({ ...refs("failed", false), cites: [] }), /without stored references/);
assert.throws(() => m.citationsView(refs("done", false)), /done extraction without stored references/);
assert.throws(() => m.citationsView(refs("someday", false)), /unknown reference extraction state someday/);
assert.throws(() => m.citationsView({ ...refs("done", true), cites: null }), /no cites list/);
assert.throws(() => m.citationsView({ cites: null }), /no reference state/);
// citations come with every state, the not-analysed one included, and are not de-duplicated
const cited = m.relatedView({
  ...refs("done", true, { cites: [{ source: src("intro", "Intro") }] }),
  analysed: true,
  builds_on: [{ source: src("intro", "Intro"), concepts: [{ slug: "s", name: "softmax", role: "uses" }] }],
  built_on_by: [], related: [], readiness: { assumed: 0, read_first: [] }, not_analysed: [],
});
assert.deepEqual(cited.citations.cites.items.map((i) => i.slug), ["intro"]);
assert.deepEqual(cited.groups[0].items.map((i) => i.slug), ["intro"]);
assert.equal(notAnalysed.citations.cites.items, null);

console.log("source related ok");
