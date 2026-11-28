// Run by tests/web/test_roadmap_api.py (`node roadmap_test.mjs`): the roadmap's DOM-free logic
// (static/js/roadmap_model.js): the progress line, the standing, a page's state, a concept in each
// state (done, with a page, to generate, ungrounded, a chosen source that no longer grounds it,
// weak with reviews due), the next action's link or button, the whole view (polling while a page
// is generated), the list's summary line, the request bodies, and replies that are not shaped as
// expected.

import assert from "node:assert/strict";

const STATIC = new URL("../../../src/teacher/web/static/", import.meta.url);
const m = await import(new URL("js/roadmap_model.js", STATIC));

// -- progress and standing ---------------------------------------------------------------------

const progress = { concepts_done: 2, concepts_total: 5, steps_done: 1, steps_total: 3, complete: false };
assert.equal(m.progressText(progress), "2 of 5 concepts done · 1 of 3 steps done");
assert.equal(m.progressFraction(progress), 0.4);
assert.equal(m.progressText({ ...progress, concepts_done: 5, steps_done: 3, complete: true }), "Complete: all 5 concepts done.");
assert.equal(m.progressFraction({ ...progress, concepts_total: 0, concepts_done: 0 }), 1);
assert.throws(() => m.progressText(null), /no progress/);

assert.equal(m.standingText(null), "Not in your concept graph yet, so there is no standing.");
assert.equal(m.standingText({ status: "unstudied", questions_answered: 0, first_attempt_passed: 0 }), "Your standing: not studied yet.");
assert.equal(
  m.standingText({ status: "weak", questions_answered: 2, first_attempt_passed: 0 }),
  "Your standing: weak (0 of 2 questions right the first time)."
);

// -- pages -------------------------------------------------------------------------------------

const page = (extra = {}) => ({ slug: "nt", title: "Study: Number theory", status: "ready", url: "/artifacts/nt",
  gates_total: 2, gates_cleared: 0, started: false, complete: false, ...extra });
assert.equal(m.pageView(page()).label, "Not started");
assert.equal(m.pageView(page({ gates_cleared: 1, started: true })).label, "1 of 2 gates cleared");
assert.deepEqual([m.pageView(page({ complete: true, gates_cleared: 2, started: true })).label, m.pageView(page({ complete: true })).tone], ["Complete", "ok"]);
const generating = m.pageView(page({ status: "generating" }));
assert.equal(generating.generating, true);
assert.match(generating.label, /^Being generated/);
assert.throws(() => m.pageView({}), /no slug or address/);

// -- concepts ----------------------------------------------------------------------------------

const source = (slug, extra = {}) => ({ slug, title: `Title of ${slug}`, recommended: false, chosen: false, ...extra });
const concept = (extra = {}) => ({
  id: 1, key: "ma", name: "Modular arithmetic", summary: "Clock arithmetic.", position: 1, step: 1,
  prerequisites: [], concept: { id: 9, slug: "modular-arithmetic", name: "Modular arithmetic" },
  standing: { status: "unstudied", questions_answered: 0, first_attempt_passed: 0, first_attempt_pass_rate: null },
  grounded: true, sources: [source("number-theory", { recommended: true })],
  recommended_source: { slug: "number-theory", title: "Title of number-theory" },
  primary_source: { slug: "number-theory", title: "Title of number-theory", chosen: false },
  choice_problem: null, page: null, failed_page: null, shared_with: [], marked_known: false,
  marked_known_at: null, done: false, done_by: [], state: "generate", can_generate: true, review: null,
  ...extra,
});

let c = m.conceptView(concept(), "/explore/4");
assert.equal(c.anchor, "roadmap-concept-ma");
assert.equal(c.canGenerate, true);
assert.deepEqual(c.choices, []); // one source: nothing to choose
assert.equal(c.graphHref, "/graph?concept=modular-arithmetic");
assert.equal(c.doneText, null);
assert.equal(c.ungrounded, null);

c = m.conceptView(concept({ done: true, done_by: ["marked_known", "page_complete"], marked_known: true, state: "done",
  page: page({ complete: true }), can_generate: false, shared_with: ["Group"] }), "/explore/4");
assert.equal(c.doneText, "Done: you marked it “I know this” and its study page is complete.");
assert.equal(c.markedKnown, true);
assert.equal(c.page.label, "Complete");
assert.equal(c.shared, "This page is shared with Group: one page for all of them.");

c = m.conceptView(concept({ shared_with: ["Group", "Ring"] }), "/explore/4");
assert.equal(c.shared, "A page of this source is shared with Group and Ring: one page for all of them.");

c = m.conceptView(concept({ sources: [source("a", { recommended: true }), source("b", { chosen: true })],
  primary_source: { slug: "b", title: "Title of b", chosen: true } }), "/explore/4");
assert.deepEqual(c.choices.map((s) => [s.slug, s.recommended, s.chosen]), [["a", true, false], ["b", false, true]]);
assert.equal(c.chosen, true);

c = m.conceptView(concept({ key: "dh", grounded: false, sources: [], primary_source: null, recommended_source: null,
  state: "ungrounded", can_generate: false }), "/explore/4");
assert.equal(c.ungrounded.href, "/explore/4#concept-dh");
assert.match(c.ungrounded.text, /^No source in your library introduces it/);
c = m.conceptView(concept({ key: "ec", concept: null, standing: null, grounded: false, sources: [], primary_source: null,
  state: "ungrounded", can_generate: false }), "/explore/4");
assert.match(c.ungrounded.text, /^It is not in your concept graph yet/);
assert.equal(c.graphHref, null);

c = m.conceptView(concept({ primary_source: null, sources: [source("a", { recommended: true })], state: "choose_source",
  can_generate: false, choice_problem: "the source you chose, “B”, no longer introduces it." }), "/explore/4");
assert.equal(c.choiceProblem, "No source to study it from: the source you chose, “B”, no longer introduces it.");
assert.equal(c.choices.length, 1); // offered even with one: the reader must choose again

c = m.conceptView(concept({ standing: { status: "weak", questions_answered: 2, first_attempt_passed: 0 },
  review: { items: 2, due: 2, next_due_at: null, url: "/review?concept=modular-arithmetic" } }), "/explore/4");
assert.deepEqual(c.review, { text: "2 questions due in the review queue.", href: "/review?concept=modular-arithmetic" });
c = m.conceptView(concept({ review: { items: 2, due: 0, next_due_at: null, url: "/review?concept=x" } }), "/explore/4");
assert.equal(c.review, null); // nothing due

c = m.conceptView(concept({ failed_page: { slug: "f", title: "Study: F", url: "/artifacts/f" } }), "/explore/4");
assert.equal(c.failedPage.href, "/artifacts/f");
assert.throws(() => m.conceptView({}, "/explore/4"), /no id, name or sources/);

// -- the next action ---------------------------------------------------------------------------

const action = (kind, extra = {}) => ({ kind, text: `Do ${kind}.`, step: 1, concept: { id: 3, key: "dl", name: "Discrete logarithm" },
  url: null, page: null, source: null, review: null, ...extra });
let a = m.nextActionView(action("generate_page"));
assert.deepEqual([a.label, a.generateFor, a.href], ["Generate page", 3, null]);
a = m.nextActionView(action("continue_page", { url: "/artifacts/nt" }));
assert.deepEqual([a.label, a.href, a.generateFor], ["Continue the page", "/artifacts/nt", null]);
assert.equal(m.nextActionView(action("open_page", { url: "/artifacts/nt" })).label, "Open the page");
assert.equal(m.nextActionView(action("review", { url: "/review?concept=dl" })).href, "/review?concept=dl");
assert.equal(m.nextActionView(action("enrich", { url: "/explore/4#concept-dl" })).label, "Find sources");
assert.equal(m.nextActionView(action("choose_source")).href, "#roadmap-concept-dl");
a = m.nextActionView(action("done", { concept: null, step: null }));
assert.deepEqual([a.done, a.label], [true, null]);
assert.throws(() => m.nextActionView(action("fly")), /Unknown next action fly/);
assert.throws(() => m.nextActionView(null), /no next action/);

// -- the whole roadmap -------------------------------------------------------------------------

const roadmap = (extra = {}) => ({
  exploration: { id: 4, prompt: "Diffie-Hellman", level: "intro", level_label: "Introductory", status: "completed", enriching: false },
  url: "/explore/4/roadmap",
  review_url: "/explore/4",
  rules: { concept_done: "Concept rule.", step_done: "Step rule.", current_step: "Current rule.", next_action: ["First.", "Second."] },
  steps: [
    { number: 1, title: "Foundations", state: "done", done: 1, total: 1, concepts: [concept({ done: true, done_by: ["strong"], state: "done" })] },
    { number: 2, title: "Next", state: "current", done: 0, total: 1, concepts: [concept({ id: 2, key: "dl", page: page({ status: "generating" }), can_generate: false, state: "page" })] },
  ],
  dropped: [{ key: "xx", name: "Extra" }],
  progress: { concepts_done: 1, concepts_total: 2, steps_done: 1, steps_total: 2, complete: false },
  current_step: 2,
  next_action: action("open_page", { url: "/artifacts/nt" }),
  ...extra,
});
let v = m.roadmapView(roadmap());
assert.equal(v.meta, "Introductory · Step 2 of 2");
assert.deepEqual(v.steps.map((s) => [s.label, s.tone, s.count, s.current]), [
  ["Done", "ok", "1 of 1 concept done", false],
  ["Current step", "warn", "0 of 1 concept done", true],
]);
assert.equal(v.poll, true); // a page is being generated
assert.equal(v.dropped, "Dropped from the plan (not counted): Extra.");
assert.equal(v.rules[3], "The next action is the first of these that applies: (1) First. (2) Second.");
v = m.roadmapView(roadmap({ steps: [roadmap().steps[0]], dropped: [], current_step: null,
  progress: { concepts_done: 1, concepts_total: 1, steps_done: 1, steps_total: 1, complete: true },
  next_action: action("done", { concept: null, step: null }) }));
assert.equal(v.meta, "Introductory · Complete");
assert.equal(v.poll, false);
assert.equal(v.enriching, null);
assert.equal(v.dropped, null);
assert.equal(v.next.done, true);
v = m.roadmapView(roadmap({ steps: [roadmap().steps[0]], exploration: { ...roadmap().exploration, enriching: true } }));
assert.equal(v.poll, true); // grounding may change when the enrichment ends
assert.match(v.enriching, /^An enrichment of this exploration is running/);
assert.throws(() => m.roadmapView({}), /not a roadmap/);

// -- the list's line and the request bodies ----------------------------------------------------

assert.equal(m.roadmapSummaryView(null), null);
const summary = { url: "/explore/4/roadmap", progress, current_step: 2, current_step_title: "Next", next_action: action("generate_page") };
assert.deepEqual(m.roadmapSummaryView(summary), {
  href: "/explore/4/roadmap", complete: false, progress: "2 of 5 concepts done · 1 of 3 steps done",
  step: "Step 2: Next", next: "Do generate_page.",
});
const finished = m.roadmapSummaryView({ ...summary, current_step: null, progress: { ...progress, complete: true },
  next_action: action("done") });
assert.deepEqual([finished.complete, finished.step, finished.next], [true, null, null]);
assert.throws(() => m.roadmapSummaryView({ url: 3 }), /not shaped as expected/);

assert.deepEqual(m.knownBody(true), { marked_known: true });
assert.deepEqual(m.knownBody(false), { marked_known: false });
assert.deepEqual(m.sourceBody("dlog-paper"), { primary_source: "dlog-paper" });
assert.deepEqual(m.sourceBody(null), { primary_source: null });

console.log("roadmap_model: ok");
