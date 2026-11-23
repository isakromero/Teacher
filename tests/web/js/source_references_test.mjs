// Run by tests/web/test_references_api.py (`node source_references_test.mjs`): the citation
// views' DOM-free logic (static/js/source_references_model.js): the extraction states and their
// buttons, one reference, "cited by", the add page's frontier list and its empty states, and the
// address an /add?url=... link carries.

import assert from "node:assert/strict";

const STATIC = new URL("../../../src/teacher/web/static/", import.meta.url);
const m = await import(new URL("js/source_references_model.js", STATIC));

const extraction = (extra = {}) => ({
  status: "never_run", available: true, unavailable_reason: null, run_number: null,
  trigger: null, extractor: null, model: null, started_at: null, finished_at: null, error: null,
  input_tokens: null, output_tokens: null, succeeded_at: null, found_in: null,
  reference_count: null, ...extra,
});

// -- the extraction states ----------------------------------------------------------------------

let view = m.extractionView(extraction());
assert.equal(view.button, "Extract references");
assert.equal(view.busy, false);
assert.match(view.text, /have not been extracted yet/);
assert.doesNotMatch(view.text, /no references|0 references/i); // never run is not "none"

view = m.extractionView(extraction({ available: false, unavailable_reason: "No extractor here." }));
assert.equal(view.button, null);
assert.match(view.text, /not been extracted yet\. No extractor here\./);

view = m.extractionView(extraction({ status: "running", started_at: "2026-11-22T10:00:00+00:00" }));
assert.deepEqual([view.tone, view.busy, view.button], ["busy", true, null]);
assert.match(view.text, /Extracting the references \(started 2026-11-22\)/);

const done = { status: "done", finished_at: "2026-11-22T10:01:00+00:00", succeeded_at: "2026-11-22T10:01:00+00:00" };
view = m.extractionView(extraction({ ...done, found_in: "reference_list", reference_count: 12 }), 3);
assert.deepEqual([view.tone, view.button], ["ok", "Extract again"]);
assert.equal(view.text, "12 references from its reference list, extracted 2026-11-22; 3 in your library.");

view = m.extractionView(extraction({ ...done, found_in: "inline_citations", reference_count: 1 }), 0);
assert.equal(view.text, "1 work cited in its text (it has no reference list), extracted 2026-11-22; 0 in your library.");

view = m.extractionView(extraction({ ...done, found_in: "none", reference_count: 0 }));
assert.equal(view.text, "No cited works were found in this source (extracted 2026-11-22).");

view = m.extractionView(extraction({ status: "failed", finished_at: "2026-11-23T09:00:00+00:00", error: "References could not be extracted: boom" }));
assert.deepEqual([view.tone, view.button], ["error", "Try again"]);
assert.equal(view.text, "The last extraction failed on 2026-11-23: References could not be extracted: boom No references are stored for this source.");

view = m.extractionView(extraction({ status: "failed", finished_at: "2026-11-23T09:00:00+00:00", error: "boom", succeeded_at: "2026-11-22T10:01:00+00:00", found_in: "reference_list", reference_count: 2 }), 1);
assert.match(view.text, /failed on 2026-11-23: boom Showing the earlier result: 2 references from its reference list, extracted 2026-11-22; 1 in your library\./);

view = m.extractionView(extraction({ status: "interrupted", started_at: "2026-11-22T10:00:00+00:00" }));
assert.deepEqual([view.tone, view.button], ["error", "Try again"]);
assert.match(view.text, /never finished/);

assert.throws(() => m.extractionView(extraction({ status: "maybe" })), /Unknown reference extraction status/);
assert.throws(() => m.extractionView(null), /Unknown/);
assert.throws(() => m.extractionView(extraction({ status: "done" })), /no reference count/);
assert.throws(() => m.isoDay("soon"), /Not a timestamp/);

// -- one reference ----------------------------------------------------------------------------

const ref = (extra = {}) => ({
  position: 1, raw_text: "Vaswani et al. Attention is all you need. 2017.", title: null,
  authors: [], year: null, venue: null, doi: null, arxiv_id: null, url: null, match: null,
  add_url: null, add_url_kind: null, ...extra,
});

let r = m.referenceView(ref());
assert.deepEqual([r.heading, r.details, r.raw, r.match, r.add], ["Vaswani et al. Attention is all you need. 2017.", "", null, null, null]);

r = m.referenceView(ref({ title: "Attention Is All You Need", authors: ["A", "B", "C", "D"], year: 2017, venue: "NeurIPS", add_url: "https://arxiv.org/pdf/1706.03762", add_url_kind: "arxiv" }));
assert.equal(r.heading, "Attention Is All You Need");
assert.equal(r.details, "A, B, C et al. · 2017 · NeurIPS");
assert.equal(r.raw, "Vaswani et al. Attention is all you need. 2017.");
assert.deepEqual(r.add, { href: "/add?url=https%3A%2F%2Farxiv.org%2Fpdf%2F1706.03762", how: "the arXiv PDF" });

r = m.referenceView(ref({ match: { slug: "attention is", title: "Attention", method: "title" } }));
assert.deepEqual(r.match, { href: "/sources/attention%20is", title: "Attention", how: "by title" });
assert.equal(r.add, null);
assert.throws(() => m.referenceView(ref({ match: { slug: "a", title: "A", method: "vibes" } })), /Unknown match method/);
assert.throws(() => m.referenceView(ref({ add_url: "https://x.org", add_url_kind: "ftp" })), /Unknown kind/);
assert.equal(m.matchedCount([ref(), ref({ match: { slug: "a", title: "A", method: "doi" } })]), 1);

// -- cited by -----------------------------------------------------------------------------------

assert.match(m.citedByView([]).empty, /No source in your library cites this one/);
assert.deepEqual(m.citedByView([{ slug: "s", title: "S", positions: [2, 5] }]), {
  empty: null,
  items: [{ href: "/sources/s", title: "S", note: "2 of its references" }],
});

// -- the add page's frontier ------------------------------------------------------------------

const coverage = (extra = {}) => ({ sources: 3, never_run: 0, running: 0, interrupted: 0, done: 3, failed: 0, ...extra });
const item = (extra = {}) => ({
  key: "arxiv:1706.03762", title: "Attention Is All You Need", raw_text: "Vaswani", authors: ["Ashish Vaswani"],
  year: 2017, venue: null, doi: null, arxiv_id: "1706.03762", url: null,
  add_url: "https://arxiv.org/pdf/1706.03762", add_url_kind: "arxiv", citing_count: 2,
  reference_count: 2, cited_by: [{ slug: "a", title: "A" }, { slug: "b", title: "B" }], ...extra,
});

assert.match(m.frontierView({ items: [], total: 0, coverage: coverage({ sources: 0, done: 0 }) }).empty, /library is empty/);
assert.match(m.frontierView({ items: [], total: 0, coverage: coverage({ done: 0, never_run: 3 }) }).empty, /have not been extracted yet.*Incomplete: 3 sources not extracted yet\./);
assert.match(m.frontierView({ items: [], total: 0, coverage: coverage() }).empty, /Everything your library cites is already in it/);
assert.match(m.frontierView({ items: [], total: 0, coverage: coverage({ done: 2, failed: 1 }) }).empty, /Incomplete: 1 whose last extraction failed\./);

let f = m.frontierView({ items: [item(), item({ key: "reference:9", add_url: null, add_url_kind: null, citing_count: 1 })], total: 2, coverage: coverage() });
assert.equal(f.empty, null);
assert.deepEqual(f.items, [{
  title: "Attention Is All You Need", details: "Ashish Vaswani · 2017", cited: "Cited by 2 sources: A; B",
  how: "the arXiv PDF", url: "https://arxiv.org/pdf/1706.03762",
}]);
assert.equal(f.note, "1 cited work with no known address is left out.");

f = m.frontierView({ items: [item({ add_url: null, add_url_kind: null })], total: 1, coverage: coverage() });
assert.match(f.empty, /cites 1 work it does not hold, but none of them has an address/);

f = m.frontierView({ items: Array.from({ length: 10 }, (_, i) => item({ key: `k${i}` })), total: 60, coverage: coverage({ never_run: 1 }) });
assert.equal(f.items.length, m.FRONTIER_SHOWN);
assert.equal(f.note, "Showing the most cited of 60 works. Incomplete: 1 source not extracted yet.");
assert.throws(() => m.frontierView({ items: [] }), /incomplete/);

// -- /add?url= ----------------------------------------------------------------------------------

assert.equal(m.prefilledUrl(""), null);
assert.equal(m.prefilledUrl("?mode=file"), null);
assert.deepEqual(m.prefilledUrl("?url=https%3A%2F%2Farxiv.org%2Fpdf%2F1706.03762"), { url: "https://arxiv.org/pdf/1706.03762" });
assert.match(m.prefilledUrl("?url=javascript%3Aalert(1)").error, /not a web address/);
assert.match(m.prefilledUrl("?url=").error, /\(nothing\)/);
assert.equal(m.addPageUrl("https://e.org/a b"), "/add?url=https%3A%2F%2Fe.org%2Fa+b");
assert.throws(() => m.addPageUrl(""), /address is needed/);
assert.equal(m.authorsText(["A", "B"]), "A, B");

console.log("source references ok");
