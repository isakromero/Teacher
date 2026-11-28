// Run by tests/web/test_explorations_api.py (`node explore_test.mjs`): the Explore pages'
// DOM-free logic (static/js/explore_model.js): the request, the limits and progress lines, the
// cost (web searches unpriced), a list item, moving steps (refused when a prerequisite would not
// stay earlier), a concept (known with standing, new, failed search), a candidate, the whole
// view, the enrichment (its preview, progress and summary, what it did per candidate and per
// concept), and replies that are not shaped as expected.

import assert from "node:assert/strict";

const STATIC = new URL("../../../src/teacher/web/static/", import.meta.url);
const m = await import(new URL("js/explore_model.js", STATIC));

// -- the request, limits and progress ------------------------------------------------------------

assert.deepEqual(m.exploreRequest("  Diffie-Hellman  ", "intro"), { topic: "Diffie-Hellman", level: "intro" });
assert.throws(() => m.exploreRequest(" ", "intro"), /Enter a topic/);
assert.throws(() => m.exploreRequest("x".repeat(301), "intro"), /too long/);

const limits = { max_concepts: 12, searches_per_concept: 2, candidates_per_concept: 5, price_estimate: null };
assert.match(m.limitsText(limits), /^At most 12 concepts\. .* at most 2 searches .* at most 5 candidate sources\. There is no reliable price estimate/);
assert.throws(() => m.limitsText(null), /limits are missing/);

assert.equal(m.progressText({ status: "running", stage: null, progress: { done: 0, total: 0 } }), "Starting...");
assert.equal(m.progressText({ status: "running", stage: "outline", progress: { done: 0, total: 0 } }), "Outlining the concepts...");
assert.equal(m.progressText({ status: "running", stage: "discovery", progress: { done: 2, total: 5 } }), "Finding sources: 2 of 5 concepts done...");
assert.equal(m.progressText({ status: "failed", stage: "brief", progress: { done: 0, total: 0 } }), "Failed while writing the brief.");
assert.equal(m.progressText({ status: "completed", stage: "discovery", progress: { done: 1, total: 1 } }), "Completed.");

// -- the cost ------------------------------------------------------------------------------------

const cost = (extra = {}) => ({ total_usd: 0.1234, calls: 4, unknown_price_calls: 0, unknown_price_models: [],
  estimate: false, estimate_calls: 0, parts: [], web_search_requests: 3, web_search_fees_priced: false, ...extra });
assert.equal(m.costText(cost()), "$0.12 for 4 model calls, and 3 web searches, billed separately and not priced here.");
assert.equal(m.costText(cost({ calls: 0, total_usd: 0, web_search_requests: 0 })), "No model call has been made yet.");
assert.equal(
  m.costText(cost({ calls: 1, total_usd: 0.002, unknown_price_calls: 1, unknown_price_models: ["claude-x"], web_search_requests: 0, estimate: true })),
  "At most $0.0020 for 1 model call, plus 1 with no known price (claude-x)."
);
assert.throws(() => m.costText({}), /no cost information/);

// -- the list ------------------------------------------------------------------------------------

const item = { id: 7, prompt: "DH", level: "intro", level_label: "Introductory", status: "running", stage: "discovery",
  progress: { done: 1, total: 3 }, error: null, concepts: 4, candidates: 1 };
let row = m.summaryView(item);
assert.equal(row.href, "/explore/7");
assert.equal(row.counts, "4 concepts, 1 candidate source");
assert.equal(row.progress, "Finding sources: 1 of 3 concepts done...");
row = m.summaryView({ ...item, status: "failed", error: "The exploration failed: boom", concepts: 0, candidates: 0 });
assert.equal(row.error, "The exploration failed: boom");
assert.equal(row.progress, null);
assert.equal(row.counts, "");
assert.throws(() => m.summaryView({}), /no id or topic/);

// -- moving steps --------------------------------------------------------------------------------

assert.deepEqual(m.moveStep(3, 1, -1), [2, 1, 3]);
assert.deepEqual(m.moveStep(3, 1, 1), [1, 3, 2]);
assert.equal(m.moveStep(3, 0, -1), null);
assert.equal(m.moveStep(3, 2, 1), null);

const c = (key, name, prerequisites = [], extra = {}) => ({
  id: key.length, key, name, drafted_name: name, renamed: false, aliases: [], summary: `${name}.`, position: 1, step: 1,
  prerequisites: prerequisites.map((k) => ({ key: k, name: k.toUpperCase(), dropped: false })), search_query: "q",
  status: "planned", match: { method: "new", reason: null, shortlist: [], concept_missing: false }, coverage: "new",
  known: null, discovery: { status: "done", error: null, log: [] }, candidates: [], ...extra,
});
const steps = [
  { number: 1, title: "One", concepts: [c("a", "A")] },
  { number: 2, title: "Two", concepts: [c("bb", "B", ["a"])] },
  { number: 3, title: "Three", concepts: [c("ccc", "C")] },
];
assert.equal(m.moveProblem(steps, [2, 1, 3]), "“B” builds on “A”, which must stay in an earlier step.");
assert.equal(m.moveProblem(steps, [1, 3, 2]), null);
const dropped = [steps[0], { ...steps[1], concepts: [c("bb", "B", ["a"], { status: "dropped" })] }, steps[2]];
assert.equal(m.moveProblem(dropped, [2, 1, 3]), null); // a dropped concept is not checked

// -- a candidate ---------------------------------------------------------------------------------

const candidate = (extra = {}) => ({
  id: 3, rank: 1, attempt: 2, fits: true, overall: 4.2, url: "https://arxiv.org/pdf/2101.00001",
  landing_url: "https://arxiv.org/abs/2101.00001", title: "A survey", authors: ["A. One", "B. Two", "C. Three", "D. Four"],
  year: 2021, arxiv_id: "2101.00001", doi: null, summary: null,
  found_by: [{ provider: "web", rank: 1, reason: "A clear survey." }],
  verdict: "readable", verdict_reason: "readable: 5400 words", readable: true,
  probe: { kind: "paper", words: 5400, words_estimated: false },
  triage: { topic_fit: 5, level_match: 3, teaching: 4, treatment: "teaches", reason: "Builds it up.", overall: 4.2 },
  library: [], decision: "approved", decided_at: null, knowledge_source: null, concepts: ["a", "bb"], ...extra,
});
let cv = m.candidateView(candidate(), "a", { a: "A", bb: "B" });
assert.equal(cv.details, "PDF · 5,400 words · A. One, B. Two, C. Three et al. · 2021");
assert.equal(cv.score, "4.2 / 5");
assert.equal(cv.judgement, "Teaches it: Builds it up. (topic 5/5, level 3/5, teaching 4/5)");
assert.deepEqual(cv.verdict, { label: "Readable", tone: "ok", reason: "readable: 5400 words" });
assert.deepEqual(cv.alsoFor, ["B"]);
assert.equal(cv.approved, true);
assert.equal(cv.followUp, true);
assert.deepEqual(cv.foundBy, ["Web search: A clear survey."]);
cv = m.candidateView(candidate({ verdict: "paywalled", verdict_reason: "needs a login", triage: null, decision: "rejected" }), "a");
assert.equal(cv.verdict.label, "Needs a login");
assert.equal(cv.score, null);
assert.equal(cv.rejected, true);
assert.equal(cv.approved, false);
assert.throws(() => m.candidateView({ id: 1 }), /no id, address or title/);
assert.deepEqual(m.decisionFor(true), { decision: "approved" });
assert.deepEqual(m.decisionFor(false), { decision: "proposed" });

// -- a concept -----------------------------------------------------------------------------------

const known = c("a", "Groups", [], {
  status: "known", drafted_name: "Group", renamed: true, coverage: "thin",
  match: { method: "exact", reason: "exact name match: 'Group'", shortlist: [], concept_missing: false },
  known: {
    concept: { id: 1, slug: "group", name: "Group" },
    standing: { status: "developing", questions_answered: 3, first_attempt_passed: 2, first_attempt_pass_rate: 0.66, last_studied_at: null },
    pages: [{ slug: "groups-page", title: "Groups", kind: "study_page", role: "main", status: "ready" }],
    sources: [{ slug: "notes", title: "Notes", role: "uses" }],
  },
  discovery: { status: "done", error: null, log: [
    { attempt: 1, query: "Group", candidates: 2, fitting: 0, error: null },
    { attempt: 2, query: "group theory notes", candidates: 1, fitting: 1, error: null },
    { left_out: 1, note: "1 more candidate(s) were found and ranked below the 5 kept." },
  ] },
  candidates: [candidate()],
});
let view = m.conceptView(known, { a: "Groups", bb: "B" });
assert.equal(view.statusLabel, "Known");
assert.equal(view.renamedFrom, "Group");
assert.equal(view.match, "Matched by name to “Group”.");
assert.equal(view.standing, "Your standing: developing (2 of 3 questions right the first time).");
assert.equal(view.coverage, "In your concept graph, but no source in your library introduces it.");
assert.deepEqual(view.known.pages, [{ slug: "groups-page", title: "Groups", kind: "study_page" }]);
assert.equal(view.discovery, "Searched for “Group”: 2 candidates, 0 fitting. Follow-up search for “group theory notes”: 1 candidate, 1 fitting.");
assert.deepEqual(view.leftOut, ["1 more candidate(s) were found and ranked below the 5 kept."]);
assert.equal(view.candidates.length, 1);

const failed = c("bb", "B", ["a"], { discovery: { status: "failed", error: "the search failed: HTTP 503", log: [
  { attempt: 1, query: "B", error: "HTTP 503" }] } });
view = m.conceptView(failed);
assert.equal(view.error, "the search failed: HTTP 503");
assert.equal(view.discovery, "Searched for “B”: failed.");
assert.equal(view.statusLabel, "New");
assert.deepEqual(view.prerequisites, ["A"]);
const covered = c("x", "X", [], { status: "known", coverage: "covered", discovery: { status: "not_needed", error: null, log: [] },
  match: { method: "judged", reason: "Same idea.", shortlist: [], concept_missing: false },
  known: { concept: { id: 2, slug: "x", name: "Ex" }, standing: { status: "unstudied", questions_answered: 0, first_attempt_passed: 0 }, pages: [], sources: [] } });
view = m.conceptView(covered);
assert.equal(view.discovery, "No search: a source in your library introduces it.");
assert.equal(view.match, "Judged the same as “Ex”: Same idea.");
assert.equal(view.standing, "Your standing: not studied yet.");
assert.throws(() => m.conceptView({}), /no id or name/);

// -- the whole view ------------------------------------------------------------------------------

const reply = {
  id: 7, prompt: "DH", level: "intro", level_label: "Introductory", status: "completed", stage: "discovery",
  progress: { done: 2, total: 2 }, error: null, notes: ["A note."], brief: { summary: "S." }, questions: [],
  steps, approved: 1, cost: cost(), editable: true, limits,
};
const whole = m.detailView(reply);
assert.equal(whole.statusLabel, "Completed");
assert.equal(whole.running, false);
assert.deepEqual(whole.steps.map((s) => [s.canMoveUp, s.canMoveDown]), [[false, false], [false, true], [true, false]]);
assert.equal(whole.failures, null);
const withFailure = m.detailView({ ...reply, steps: [steps[0], { ...steps[1], concepts: [failed] }, steps[2]] });
assert.equal(withFailure.failures, "Source search failed for 1 concept: B. Their errors are shown with them.");
const running = m.detailView({ ...reply, status: "running", editable: false });
assert.equal(running.running, true);
assert.deepEqual(running.steps.map((s) => [s.canMoveUp, s.canMoveDown]), [[false, false], [false, false], [false, false]]);
assert.throws(() => m.detailView({ id: 1 }), /not an exploration/);

// -- the enrichment --------------------------------------------------------------------------------

const preview = {
  exploration_id: 7, digest: "d".repeat(32), can_start: true, blocked: null,
  sources: { total: 2, to_add: 1, to_link: 1, items: [
    { candidate_id: 3, title: "Discrete logs", url: "https://a.example/dlog", action: "add", held: null, extraction: "run", concepts: ["B"], last_error: "fetching it: HTTP 503" },
    { candidate_id: 4, title: "Held", url: "https://a.example/held", action: "link", held: { slug: "held", title: "Held notes", method: "url" }, extraction: "done_before", concepts: ["A"], last_error: null },
  ] },
  concepts: { total: 2, matched: 1, to_resolve: 1, items: [
    { id: 1, key: "a", name: "A", status: "known", action: "use", concept: { slug: "a", name: "Ay" } },
    { id: 2, key: "bb", name: "B", status: "planned", action: "resolve", concept: null },
  ] },
  links: { total: 2, to_add: 1, present: 0, to_skip: 1, items: [
    { from_key: "bb", from: "B", to_key: "a", to: "A", expected: "add", code: null, reason: null },
    { from_key: "bb", from: "B", to_key: "x", to: "X", expected: "skip", code: "cycle", reason: "it would make builds_on cyclic" },
  ] },
  dropped: ["C"], model_calls: "Models are called.",
};
const pv = m.previewView(preview);
assert.equal(pv.headline, "1 source will be added to your library and 1 linked; 2 concepts written to your concept graph (1 to look up or create); 1 prerequisite link to add, 0 already present, 1 not written.");
assert.deepEqual(pv.sources, [
  "Discrete logs: added to your library, its concepts extracted. The last attempt failed: fetching it: HTTP 503.",
  "Held: already in your library as “Held notes”, linked (its concepts were extracted before).",
]);
assert.deepEqual(pv.concepts, ["A: in your graph as “Ay”", "B: looked up again after the sources are added, created if still absent (unreviewed)"]);
assert.deepEqual(pv.links, ["“B” builds on “A”: will be added.", "“B” builds on “X”: will not be written (it would make builds_on cyclic)."]);
assert.equal(pv.dropped, "Left out (dropped): C.");
assert.equal(pv.canStart, true);
assert.equal(pv.empty, false);
assert.equal(m.previewView({ ...preview, can_start: false, blocked: "Running." }).blocked, "Running.");
assert.throws(() => m.previewView({}), /preview is not shaped/);

const enrichment = (extra = {}) => ({
  id: 5, status: "running", stage: "ingest", progress: { done: 1, total: 3 }, error: null, stages: [],
  planned: { sources: 3, concepts: 2, links: 1 }, summary: null, created_at: null, finished_at: null, cost: cost({ web_search_requests: 0 }), ...extra,
});
assert.equal(m.enrichmentProgressText(enrichment()), "Adding the sources to your library: 1 of 3 sources done...");
assert.equal(m.enrichmentProgressText(enrichment({ stage: "graph" })), "Writing the outline to your concept graph...");
assert.equal(m.enrichmentProgressText(enrichment({ status: "failed", stage: "graph", error: "x" })), "The enrichment failed while writing the outline to your concept graph.");
const summary = {
  sources: { added: 1, held: 1, failed: 1, items: [
    { title: "A text", outcome: "added", error: null }, { title: "Broken", outcome: "failed", error: "fetching it: HTTP 404" }] },
  concepts: { matched: 1, found: 2, created: 1, items: [] },
  relations: { added: 2, present: 1, skipped: 1, items: [{ from: "B", to: "X", outcome: "skipped", reason: "would cycle" }] },
  grounding: { grounded: 2, ungrounded: 1, items: [{ name: "B", grounded: true }, { name: "C", grounded: false }] },
};
const ev = m.enrichmentView(enrichment({ status: "completed", summary }));
assert.equal(ev.progress, "The enrichment is done.");
assert.deepEqual(ev.lines, [
  "Sources: 1 added, 1 already in your library (linked), 1 failed.",
  "Concepts: 3 matched (2 found after the sources were added), 1 created.",
  "Prerequisite links: 2 added, 1 already present, 1 skipped.",
  "Skipped: “B” builds on “X”: would cycle",
  "Grounding: 2 grounded, 1 still ungrounded.",
  "Still ungrounded: C.",
]);
assert.equal(ev.failures, "1 source could not be added and stays ticked to be tried again: Broken: fetching it: HTTP 404");
assert.equal(ev.cost, "$0.12 for 4 model calls.");
assert.equal(m.enrichmentView(enrichment()).running, true);
assert.throws(() => m.enrichmentView({}), /enrichment is not shaped/);

// What it did per candidate and per concept.
cv = m.candidateView(candidate({ decision: "ingested", knowledge_source: { slug: "s", title: "S" },
  ingest: { outcome: "added", extraction: "ran", extracted: { links: 3, created: 1, matched: 2 } } }), "a");
assert.equal(cv.ingestText, "Added to your library. Its concepts were extracted: 3 concepts, 1 new.");
assert.deepEqual(cv.source, { slug: "s", title: "S" });
cv = m.candidateView(candidate({ decision: "ingested", ingest: { outcome: "held", method: "url", extraction: "done_before" } }), "a");
assert.equal(cv.ingestText, "Already in your library (found by address): linked, not added again. Its concepts had been extracted before.");
cv = m.candidateView(candidate({ decision: "approved", ingest_error: "fetching it: HTTP 404", ingest: { outcome: "failed" } }), "a");
assert.equal(cv.ingestError, "fetching it: HTTP 404");
assert.equal(cv.ingestText, null);

const enriched = c("bb", "B", ["a"], {
  status: "grounded", graph: { outcome: "found", reason: "exact name match: 'B'" },
  grounding: [{ slug: "s", title: "S", role: "introduces", grounds: true }, { slug: "t", title: "T", role: "uses", grounds: false }],
  links: [{ key: "a", name: "A", outcome: "added", code: null, reason: null }, { key: "x", name: "X", outcome: "skipped", code: "cycle", reason: "would cycle" }],
});
view = m.conceptView(enriched);
assert.equal(view.graph, "Found in your concept graph after the sources were added: exact name match: 'B'.");
assert.equal(view.grounding, "Grounded: a source in your library introduces it.");
assert.deepEqual(view.groundedBy, [{ slug: "s", title: "S" }]);
assert.deepEqual(view.alsoLinked, [{ slug: "t", title: "T", role: "uses" }]);
assert.deepEqual(view.links, ["A: added to your graph", "X: not written (would cycle)"]);
const ungrounded = c("x", "X", [], { status: "planned", graph: { outcome: "created", reason: "nothing close" },
  candidates: [candidate({ decision: "proposed" })], grounding: [], links: [] });
view = m.conceptView(ungrounded);
assert.equal(view.grounding, "Not grounded yet: no source in your library introduces it. Tick its proposed source and enrich again.");
assert.equal(view.graph, "Added to your concept graph as a new, unreviewed concept: nothing close.");
assert.equal(m.conceptView(c("y", "Y")).grounding, null); // never enriched

const enrichingReply = m.detailView({ ...reply, editable: false, enriching: true, can_enrich: false, enrichment: enrichment() });
assert.equal(enrichingReply.poll, true);
assert.equal(enrichingReply.canEnrich, false);
assert.equal(enrichingReply.enrichment.progress, "Adding the sources to your library: 1 of 3 sources done...");
assert.equal(m.detailView({ ...reply, can_enrich: true }).poll, false);

console.log("explore model: ok");
