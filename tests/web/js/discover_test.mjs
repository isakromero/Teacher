// Run by tests/web/test_discovery_api.py (`node discover_test.mjs`): the "Find sources" box's
// DOM-free logic (static/js/discover_model.js): the request, provider lines (a failed provider
// is shown, never dropped), verdicts, a candidate, the cost line, and replies that are not
// shaped as expected.

import assert from "node:assert/strict";

const STATIC = new URL("../../../src/teacher/web/static/", import.meta.url);
const m = await import(new URL("js/discover_model.js", STATIC));

// -- the request ---------------------------------------------------------------------------------

assert.deepEqual(m.discoverRequest("  Diffie-Hellman  ", "intro"), { topic: "Diffie-Hellman", level: "intro" });
assert.throws(() => m.discoverRequest("   ", "intro"), /Enter a topic/);
assert.throws(() => m.discoverRequest("x".repeat(301), "intro"), /too long/);

// -- providers -----------------------------------------------------------------------------------

const providers = [
  { name: "web", ok: true, hits: 6, error: null, warnings: ["a web search failed on the server (max_uses_exceeded)"], detail: "", seconds: 40 },
  { name: "arxiv", ok: false, hits: 0, error: "arXiv answered with HTTP 503", warnings: [], detail: "", seconds: 1 },
];
assert.deepEqual(m.providerLines(providers), [
  { ok: true, text: "Web search: 6 results (a web search failed on the server (max_uses_exceeded))" },
  { ok: false, text: "arXiv failed: arXiv answered with HTTP 503" },
]);
assert.deepEqual(m.providerLines([{ name: "openalex", ok: true, hits: 1, warnings: [] }]), [
  { ok: true, text: "openalex: 1 result" },
]);
assert.throws(() => m.providerLines([]), /no providers/);

// -- one candidate -------------------------------------------------------------------------------

const probe = (extra = {}) => ({
  verdict: "readable", readable: true, reason: "readable: 5400 words, a PDF of 12 page(s)",
  final_url: null, media_type: "application/pdf", kind: "paper", words: 5400, words_estimated: false,
  pages: 12, page_title: null, seconds: 1.2, ...extra,
});
const candidate = (extra = {}) => ({
  id: "c_0123456789ab", rank: 1, url: "https://arxiv.org/pdf/2101.00001",
  landing_url: "https://arxiv.org/abs/2101.00001", title: "A survey of key exchange",
  providers: ["web", "arxiv"],
  found_by: [{ provider: "web", rank: 1, reason: "A clear survey." }, { provider: "arxiv", rank: 2, reason: "arXiv search result 2 of 5" }],
  arxiv_id: "2101.00001", doi: null, authors: ["A. One", "B. Two", "C. Three", "D. Four"], year: 2021,
  summary: null, in_library: false, library: [], probe: probe(),
  triage: { topic_fit: 5, level_match: 3, teaching: 4, treatment: "teaches", reason: "Builds it up from groups.", overall: 4.2, model: "claude-haiku-5-5" },
  ...extra,
});

let view = m.candidateView(candidate());
assert.equal(view.title, "A survey of key exchange");
assert.equal(view.landing, "Full text of https://arxiv.org/abs/2101.00001");
assert.equal(view.details, "PDF · 5,400 words · A. One, B. Two, C. Three et al. · 2021");
assert.equal(view.score, "4.2 / 5");
assert.equal(view.judgement, "Teaches it: Builds it up from groups. (topic 5/5, level 3/5, teaching 4/5)");
assert.deepEqual(view.verdict, { label: "Readable", tone: "ok", reason: "readable: 5400 words, a PDF of 12 page(s)" });
assert.deepEqual(view.foundBy, ["Web search: A clear survey.", "arXiv: arXiv search result 2 of 5"]);
assert.deepEqual(view.library, []);

view = m.candidateView(candidate({
  probe: probe({ verdict: "paywalled", readable: false, reason: "needs a login or a subscription: HTTP 403", kind: null, words: null }),
  triage: null, landing_url: null, authors: [], year: null,
  library: [{ slug: "dh", title: "DH notes", method: "url" }],
}));
assert.deepEqual([view.verdict.label, view.verdict.tone], ["Needs a login", "error"]);
assert.equal(view.score, null);
assert.equal(view.judgement, null);
assert.equal(view.details, "");
assert.deepEqual(view.library, [{ slug: "dh", title: "DH notes" }]);
// A verdict the page does not know is shown as it is, as an error.
assert.deepEqual(m.verdictView({ verdict: "brand_new", reason: "why" }), { label: "brand_new", tone: "error", reason: "why" });
assert.throws(() => m.candidateView({ title: "no url" }), /no address or title/);
assert.throws(() => m.candidateView(candidate({ probe: null })), /no probe verdict/);

// -- the cost ------------------------------------------------------------------------------------

const usage = (extra = {}) => ({
  total_usd: 0.0412, calls: [{}, {}], unpriced_calls: 0, unpriced_models: [], upper_bound: false,
  web_search_requests: 3, search_fees_priced: false, ...extra,
});
assert.equal(
  m.usageText(usage()),
  "This search cost $0.04 for 2 model calls, and 3 web searches, which are billed separately and not priced here."
);
assert.equal(
  m.usageText(usage({ total_usd: 0.0021, calls: [{}], web_search_requests: 0, unpriced_calls: 1, unpriced_models: ["claude-x"] })),
  "This search cost $0.0021 for 1 model call, plus 1 with no known price (claude-x)."
);
assert.throws(() => m.usageText(null), /no cost information/);

// -- the whole reply -----------------------------------------------------------------------------

const reply = {
  query: { topic: "Diffie-Hellman", level: "intro", aliases: [], summary: null, concept_slug: null },
  candidates: [candidate(), candidate({ id: "c_2", rank: 2, probe: probe({ verdict: "too_thin", readable: false }), triage: null })],
  providers, hits: 9, distinct: 8,
  not_kept: [{ id: "c_9", url: "https://x.example", title: "Ninth", providers: ["web"] }],
  triage: { ran: true, model: "claude-haiku-5-5", scored: 1, note: null },
  usage: usage(), seconds: 50, page_cost_estimate: null,
};
const result = m.resultView(reply);
assert.equal(result.summary, "2 sources for “Diffie-Hellman”, 1 readable, best first.");
assert.equal(result.providers.length, 2); // the failed provider is listed too
assert.equal(result.items.length, 2);
assert.equal(result.notKept, "1 more result was found but not checked: Ninth.");
assert.equal(result.triageNote, null);
assert.equal(result.empty, false);

const empty = m.resultView({ ...reply, candidates: [], not_kept: [], triage: { ran: false, model: null, scored: 0, note: "Nothing was found to score." } });
assert.equal(empty.summary, "No sources were found for “Diffie-Hellman”.");
assert.equal(empty.empty, true);
assert.equal(empty.triageNote, "Nothing was found to score.");
assert.throws(() => m.resultView({ detail: "boom" }), /not a list of sources/);

console.log("discover_test.mjs: ok");
