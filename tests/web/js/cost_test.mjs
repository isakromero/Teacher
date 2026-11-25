// Run by tests/web/test_costs_api.py (`node cost_test.mjs`): how cost.js words and formats
// the cost ledger's totals (amounts, estimates, calls with no known price, the page's line).

import assert from "node:assert/strict";

const STATIC = new URL("../../../src/teacher/web/static/", import.meta.url);
const { amountText, artifactSummary, costText, formatUsd, unknownText } = await import(
  new URL("js/cost.js", STATIC)
);

const total = (usd, extra = {}) => ({
  total_usd: usd, calls: 1, unknown_price_calls: 0, unknown_price_models: [], estimate: false,
  estimate_calls: 0, parts: [], ...extra,
});

// -- amounts ---------------------------------------------------------------------------------
assert.equal(formatUsd(0), "$0.00");
assert.equal(formatUsd(0.0049), "<$0.01");
assert.equal(formatUsd(0.01), "$0.01");
assert.equal(formatUsd(0.835), "$0.83"); // toFixed: 0.835 is 0.83499... in binary
assert.equal(formatUsd(12.3456), "$12.35");
assert.throws(() => formatUsd(null), TypeError);
assert.throws(() => formatUsd(-1), TypeError);

// -- caveats ---------------------------------------------------------------------------------
assert.equal(amountText(total(1.94, { estimate: true })), "at most $1.94");
assert.equal(costText(total(1.94, { estimate: true })), "at most $1.94 (estimate)");
assert.equal(unknownText(total(0)), "");
const unknown = total(0.12, { unknown_price_calls: 2, unknown_price_models: ["claude-x", "y"] });
assert.equal(unknownText(unknown), "plus 2 calls with no known price: claude-x, y");
assert.equal(costText(unknown), "$0.12, plus 2 calls with no known price: claude-x, y");
assert.equal(
  unknownText(total(0, { unknown_price_calls: 1, unknown_price_models: ["m"] })),
  "plus 1 call with no known price: m"
);

// -- the page's line -------------------------------------------------------------------------
const part = (key, label, usd, extra = {}) => ({
  key, label, total_usd: usd, calls: 1, unknown_price_calls: 0, estimate: false, ...extra,
});
const page = {
  ...total(0.96, { calls: 3 }),
  generation: total(0.84, { parts: [part("write", "writing", 0.84)] }),
  since_generation: total(0.12, {
    calls: 2, parts: [part("tutor", "tutor", 0.09), part("grading", "grading", 0.03)],
  }),
};
assert.equal(
  artifactSummary(page),
  "Cost to generate: $0.84 · since then: $0.12 (tutor $0.09, grading $0.03)"
);
const estimated = {
  ...page,
  since_generation: total(5.91, {
    calls: 2, estimate: true, estimate_calls: 1,
    parts: [part("tutor", "tutor", 5.85, { estimate: true }), part("grading", "grading", 0.06)],
  }),
};
assert.equal(
  artifactSummary(estimated),
  "Cost to generate: $0.84 · since then: at most $5.91 (estimate) (tutor at most $5.85, grading $0.06)"
);
const untouched = { ...page, since_generation: total(0, { calls: 0 }) };
assert.equal(artifactSummary(untouched), "Cost to generate: $0.84");
assert.equal(
  artifactSummary({ ...total(0, { calls: 0 }), generation: total(0, { calls: 0 }), since_generation: total(0, { calls: 0 }) }),
  "Cost: no model calls recorded for this page."
);

console.log("cost ok");
