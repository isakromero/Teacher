// Run by tests/web/test_pages.py (`node home_test.mjs`): the pure helpers behind the home page
// (home_model.js), its search (search_model.js) and the library page's filter (library_model.js).

import assert from "node:assert/strict";

const STATIC = new URL("../../../src/teacher/web/static/", import.meta.url);
const home = await import(new URL("js/home_model.js", STATIC));
const search = await import(new URL("js/search_model.js", STATIC));
const library = await import(new URL("js/library_model.js", STATIC));

// -- home: when, progress, the summary, sessions ---------------------------------------------------

const now = new Date(2026, 10, 22, 15, 30); // a Sunday, local time
const local = (d, h = 9, m = 5) => new Date(2026, 10, d, h, m).toISOString();
assert.equal(home.relativeDay(local(22), now), "Today");
assert.equal(home.relativeDay(local(21, 23, 59), now), "Yesterday");
assert.equal(home.relativeDay(local(17), now), new Date(2026, 10, 17).toLocaleDateString(undefined, { weekday: "long" }));
assert.equal(home.relativeDay(local(1), now), new Date(2026, 10, 1).toLocaleDateString(undefined, { day: "numeric", month: "short" }));
assert.match(home.relativeDay(new Date(2025, 0, 3).toISOString(), now), /2025/);
assert.throws(() => home.relativeDay("not a date", now), /not a date/);
assert.match(home.whenText(local(22, 9, 5), now), /^Today at /);

const page = (progress, extra = {}) => ({ kind: "study_page", status: "ready", superseded_by: null, title: "Optics", progress, ...extra });
assert.equal(home.progressText(page({ gates_total: 7, gates_cleared: 5, complete: false })), "5 of 7 questions cleared");
assert.equal(home.progressText(page({ gates_total: 1, gates_cleared: 0, complete: false })), "0 of 1 question cleared");
assert.equal(home.progressText(page({ gates_total: 3, gates_cleared: 2 }, { kind: "lab" })), "2 of 3 checked steps passed");
assert.equal(home.progressText(page({ gates_total: 0, gates_cleared: 0 })), null);
assert.equal(home.progressText(page({ gates_total: 4, gates_cleared: 0 }, { status: "failed" })), null);
assert.equal(home.progressShare(page({ gates_total: 4, gates_cleared: 1 })), 0.25);
assert.equal(home.artifactState(page({ gates_total: 2, gates_cleared: 2, complete: true })), "Finished");
assert.equal(home.artifactState(page({ gates_total: 2, gates_cleared: 1, complete: false })), null);
assert.equal(home.artifactState(page(null, { status: "failed" })), "Generation failed");
assert.throws(() => home.artifactState(page(null, { status: "lost" })), /unknown artifact status lost/);

const view = home.continueView(
  { artifact: page({ gates_total: 7, gates_cleared: 5, complete: false }, { superseded_by: "newer" }), url: "/artifacts/optics", last_studied_at: local(22, 9, 5), active_seconds: 720 },
  now
);
assert.equal(view.href, "/artifacts/optics");
assert.match(view.when, /^Studied today at /);
assert.equal(view.time, "12 min on it");
assert.equal(view.progress, "5 of 7 questions cleared");
assert.equal(view.kind, "Study page");
assert.equal(view.older, true);
assert.equal(home.continueView({ artifact: page(null), url: "/x", last_studied_at: local(22), active_seconds: 0 }, now).time, null);

const period = (extra = {}) => ({ since: "", active_seconds: 0, sessions: 0, answers: 0, questions: 0, first_attempts: 0, first_attempts_passed: 0, first_attempt_pass_rate: null, ...extra });
const tiles = home.summaryTiles({
  today: period({ active_seconds: 600, sessions: 1 }),
  week: period({ active_seconds: 3900, sessions: 4, questions: 6, first_attempts: 5, first_attempts_passed: 4, first_attempt_pass_rate: 0.8 }),
});
assert.deepEqual(
  tiles.map((t) => [t.label, t.value, t.note]),
  [
    ["This week", "1 h 5 min", "10 min today"],
    ["Sessions", "4", "this week, 1 today"],
    ["Questions", "6", "answered this week"],
    ["First time", "80%", "4 of 5 passed at once"],
  ]
);
const quiet = home.summaryTiles({ today: period(), week: period() });
assert.equal(quiet[0].note, "Nothing today");
assert.equal(quiet[3].value, "-");
assert.equal(quiet[3].note, "no first attempts this week");

const session = { id: 7, started_at: local(22, 9, 5), backfilled: false, active_seconds: 95, artifacts: [{ title: "Optics" }], sources: [{ title: "A paper" }] };
assert.deepEqual(
  { ...home.sessionView(session, now), when: null },
  { href: "/history#session-7", when: null, length: "2 min", what: "Optics, A paper" }
);
assert.equal(home.sessionView({ ...session, backfilled: true }, now).length, "From earlier activity");
assert.equal(home.sessionView({ ...session, active_seconds: 0, artifacts: [], sources: [] }, now).what, "No pages studied");

const links = (action) => (action.kind === "none" ? [] : [{ href: "/a", text: "A", note: "" }, { href: "/b", text: "B" }]);
assert.deepEqual(home.suggestionLink({ action: { kind: "study_page" } }, links), { href: "/a", text: "A", note: "" });
assert.equal(home.suggestionLink({ action: { kind: "none" } }, links), null);

// -- search: when to send, words for results, moving with the keyboard ---------------------------------

assert.deepEqual(search.queryState(""), { ready: false, reason: null });
assert.deepEqual(search.queryState("   "), { ready: false, reason: null });
assert.match(search.queryState("a").reason, /at least 2/);
assert.match(search.queryState("?!").reason, /letters or digits/);
assert.match(search.queryState("x".repeat(201)).reason, /too long/);
assert.deepEqual(search.queryState("é1"), { ready: true, reason: null });
assert.equal(search.searchUrl("a&b c"), "/api/search?q=a%26b%20c&limit=20");
assert.equal(search.homeAddress("  reward model "), "/?q=reward%20model");
assert.equal(search.homeAddress(""), "/");

assert.equal(search.kindLabel("lab"), "Lab");
assert.throws(() => search.kindLabel("video"), /unknown search result kind video/);

const match = (extra) => ({ field: "body", section: null, block: null, term: null, concept: null, locked: false, href: null, ...extra });
assert.equal(search.matchLabel(match({ field: "title" })), "In the title");
assert.equal(search.matchLabel(match({ field: "authors" })), "In the authors");
assert.equal(search.matchLabel(match({ field: "summary" })), "In the summary");
assert.equal(search.matchLabel(match({ field: "heading", section: "Attention" })), "In section “Attention”");
assert.equal(search.matchLabel(match({ section: "Attention" })), "In section “Attention”");
assert.equal(search.matchLabel(match({})), "In the text");
assert.equal(search.matchLabel(match({ term: "GRPO" })), "In the glossary: “GRPO”");
assert.equal(search.matchLabel(match({ field: "glossary", term: "GRPO" })), "In the glossary: “GRPO”");
assert.equal(search.matchLabel(match({ field: "concept", concept: "Reward model" })), "Linked concept “Reward model”");
assert.equal(search.matchLabel(match({ locked: true })), "In a part of the page you have not unlocked yet");
assert.throws(() => search.matchLabel(match({ field: "colour" })), /unknown search match field colour/);
for (const label of ["In the title", "In section “x”"]) assert.ok(!label.includes("—"), "no em dashes");

assert.equal(search.metaText({ authors: ["A", "B"], year: "2017" }), "A, B · 2017");
assert.equal(search.metaText({ authors: ["A", "B", "C", "D", "E"], year: null }), "A, B and 3 others");
assert.equal(search.metaText({ authors: [], year: null }), null);

const study = (kind, s, status = "ready") => search.studyText({ kind, status, study: s });
assert.equal(study("study_page", { state: "not_opened", active_seconds: 0, gates_total: 3, gates_cleared: 0 }), "Not opened yet");
assert.equal(study("study_page", { state: "in_progress", active_seconds: 720, gates_total: 7, gates_cleared: 3 }), "In progress · 12 min · 3 of 7 questions cleared");
assert.equal(study("study_page", { state: "in_progress", active_seconds: 0, gates_total: null }), "In progress");
assert.equal(study("study_page", { state: "complete", active_seconds: 3600 }), "Finished · 1 h");
assert.equal(study("study_page", { state: "not_ready", active_seconds: 0 }, "failed"), "Generation failed");
assert.equal(study("source", { state: "not_opened", active_seconds: 0, study_pages: 2 }), "Not opened yet · 2 study pages");
assert.equal(study("source", { state: "opened", active_seconds: 0, study_pages: 0 }), "Opened · no study page yet");
assert.throws(() => study("source", { state: "forgotten" }), /unknown study state forgotten/);

const segs = [{ text: "<b>", mark: false }, { text: "x", mark: true }];
assert.equal(search.checkSegments(segs), segs);
assert.throws(() => search.checkSegments([{ text: "<b>x</b>", html: true }]), /malformed segment/);
assert.throws(() => search.checkSegments("<b>x</b>"), /not a list/);

assert.equal(search.resultsStatus({ query: "x", total: 0, results: [] }), "No results for “x”.");
assert.equal(search.resultsStatus({ query: "x", total: 1, results: [{}] }), "1 result for “x”.");
assert.equal(search.resultsStatus({ query: "x", total: 34, results: new Array(20).fill({}) }), "Showing the best 20 of 34 results for “x”.");

assert.equal(search.moveIndex(-1, 1, 3), 0); // from the box to the first result
assert.equal(search.moveIndex(0, 1, 3), 1);
assert.equal(search.moveIndex(2, 1, 3), 2); // stays on the last
assert.equal(search.moveIndex(0, -1, 3), -1); // back to the box
assert.equal(search.moveIndex(-1, -1, 3), -1);
assert.equal(search.moveIndex(-1, 1, 0), -1);

// -- library: grouping and the filter ----------------------------------------------------------------

const sources = [
  { slug: "old", title: "Old paper", creators: ["Ada Lovelace"], added_at: "2026-01-01T00:00:00+00:00", identifiers: [] },
  { slug: "new", title: "Les courbes de Bézier", creators: [], originated_on: "1962", added_at: "2026-02-01T00:00:00+00:00", identifiers: [{ scheme: "arxiv", value: "1706.03762" }] },
];
const artifacts = [
  { slug: "p1", kind: "study_page", title: "Study: old", created_at: "2026-01-02T00:00:00+00:00", sources: [{ slug: "old" }] },
  { slug: "l1", kind: "lab", title: "Lab: old", created_at: "2026-01-03T00:00:00+00:00", sources: [{ slug: "old" }] },
  { slug: "p2", kind: "study_page", title: "Study: curves", created_at: "2026-02-02T00:00:00+00:00", sources: [{ slug: "new" }, { slug: "old" }] },
];
const groups = library.groupBySource(sources, artifacts);
assert.deepEqual(groups.map((g) => g.source.slug), ["new", "old"]); // newest first
assert.deepEqual(groups[1].artifacts.map((a) => a.slug), ["p2", "l1", "p1"]);
assert.equal(library.libraryCount(groups), "2 sources, 2 study pages and 1 lab");
assert.equal(library.libraryCount(groups.slice(0, 1)), "1 source and 1 study page");

assert.deepEqual(library.filterWords("  Bézier, CURVES "), ["bezier", "curves"]);
const keep = (text) => groups.filter((g) => library.groupMatches(g, library.filterWords(text))).map((g) => g.source.slug);
assert.deepEqual(keep(""), ["new", "old"]);
assert.deepEqual(keep("bezier"), ["new"]);
assert.deepEqual(keep("love"), ["old"]); // a prefix of an author
assert.deepEqual(keep("lab"), ["old"]); // a lab's title
assert.deepEqual(keep("1706"), ["new"]); // an identifier
assert.deepEqual(keep("curves old"), ["old"]); // every word, anywhere in the group
assert.deepEqual(keep("submarine"), []);

console.log("home ok");
