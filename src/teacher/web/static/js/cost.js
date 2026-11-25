// What the cost ledger (teacher.costs) says a page, a lab or a tutor conversation cost, shown
// quietly: one line under the page's meta line with the breakdown in a <details>, one line under
// the tutor's header, and a small figure under each tutor reply.
//
// The API's cost objects (teacher.web.common.CostOut): `total_usd` sums the calls with a known
// price; `unknown_price_calls` (models in `unknown_price_models`) are not in it and are always
// named ("plus 2 calls with no known price: model"), never counted as zero; `estimate` means
// the total includes upper-bound estimates (older calls kept without their token breakdown),
// shown as "at most". An amount under one cent is "<$0.01", anything else has two decimals.
//
// The elements are made here, next to the page's own (`#artifact-meta`, `.tutor-header`), so
// the templates need no markup for them.

import { api, describeError } from "./api.js";
import { h, setChildren } from "./dom.js";

/** "$0.84"; "<$0.01" for a positive amount under one cent; "$0.00" for nothing. */
export function formatUsd(usd) {
  if (typeof usd !== "number" || !Number.isFinite(usd) || usd < 0) {
    throw new TypeError(`not an amount in dollars: ${usd}`);
  }
  if (usd === 0) return "$0.00";
  if (usd < 0.01) return "<$0.01";
  return `$${usd.toFixed(2)}`;
}

/** The amount of a cost object or part: "$0.84", or "at most $0.84" for an estimate. */
export function amountText(cost) {
  return `${cost.estimate ? "at most " : ""}${formatUsd(cost.total_usd)}`;
}

/** "plus 2 calls with no known price: claude-x", or "" when every call has a price. */
export function unknownText(cost) {
  const n = cost.unknown_price_calls;
  if (!n) return "";
  const models = (cost.unknown_price_models || []).join(", ");
  return `plus ${n} ${n === 1 ? "call" : "calls"} with no known price${models ? `: ${models}` : ""}`;
}

/** A cost object's amount with its caveats: "$0.84", "at most $1.94 (estimate)",
 * "$0.12, plus 1 call with no known price: m". */
export function costText(cost) {
  const parts = [amountText(cost) + (cost.estimate ? " (estimate)" : "")];
  const unknown = unknownText(cost);
  if (unknown) parts.push(unknown);
  return parts.join(", ");
}

function partsText(parts) {
  return parts.map((part) => `${part.label} ${amountText(part)}`).join(", ");
}

/** The one-line summary of an artifact's cost (ArtifactCostOut). */
export function artifactSummary(cost) {
  if (!cost.calls) return "Cost: no model calls recorded for this page.";
  const gen = cost.generation;
  const since = cost.since_generation;
  let text = `Cost to generate: ${gen.calls ? costText(gen) : "no calls recorded"}`;
  if (since.calls) {
    text += ` · since then: ${costText(since)}`;
    if (since.parts.length) text += ` (${partsText(since.parts)})`;
  }
  return text;
}

function breakdown(title, cost) {
  return h(
    "p",
    {},
    h("strong", {}, `${title}: `),
    cost.parts
      .map((part) => {
        const calls = `${part.calls} ${part.calls === 1 ? "call" : "calls"}`;
        const unknown = part.unknown_price_calls ? `, ${part.unknown_price_calls} unpriced` : "";
        return `${part.label} ${amountText(part)} (${calls}${unknown})`;
      })
      .join(" · ")
  );
}

/** The cost line under `metaEl` (the page's or lab's meta line), made on first use and kept
 * (with its open state) when the artifact is shown again. */
export function renderArtifactCost(metaEl, artifact) {
  const cost = artifact.cost;
  if (!cost) throw new TypeError("the artifact has no cost object");
  let el = document.getElementById("artifact-cost");
  if (!el) {
    el = h("details", { id: "artifact-cost", class: "cost-details" });
    metaEl.after(el);
  }
  const body = [];
  if (cost.generation.calls) body.push(breakdown("Generation, by stage", cost.generation));
  if (cost.since_generation.calls) body.push(breakdown("Since then", cost.since_generation));
  if (cost.estimate) {
    body.push(
      h(
        "p",
        {},
        cost.estimate_calls === 1
          ? "1 older call was recorded without the breakdown of its input tokens, so its cost "
          : `${cost.estimate_calls} older calls were recorded without the breakdown of their `
            + "input tokens, so their cost ",
        "is an estimate: an upper bound."
      )
    );
  }
  const unknown = unknownText(cost);
  if (unknown) {
    body.push(h("p", {}, `${unknown[0].toUpperCase()}${unknown.slice(1)}; they are not in these totals.`));
  }
  if (!cost.calls) body.push(h("p", {}, "Nothing this page cost has been recorded yet."));
  setChildren(
    el,
    h("summary", {}, artifactSummary(cost)),
    h("div", { class: "cost-breakdown" }, body)
  );
}

// -- the tutor -------------------------------------------------------------------------------

/** The conversation's cost line under the tutor's header (`thread` a ThreadOut, or null to
 * hide it, e.g. for a new conversation). */
export function renderThreadCost(panel, thread) {
  let el = panel.querySelector(".tutor-cost");
  if (!thread) {
    if (el) el.hidden = true;
    return;
  }
  if (!el) {
    el = h("p", { class: "tutor-cost" });
    const header = panel.querySelector(".tutor-header");
    if (!header) throw new Error("the tutor panel has no header to put its cost under");
    header.after(el);
  }
  const cost = thread.cost;
  el.textContent = cost.calls ? `This conversation: ${costText(cost)}` : "";
  el.hidden = !cost.calls;
}

/** Fetch thread `threadId`'s summary (GET .../chat/threads lists them with their costs, without
 * the messages) and show its cost; a failure is shown in the cost line, never thrown (the
 * caller does not wait for this). */
export async function refreshThreadCost(panel, chatBase, threadId) {
  try {
    const threads = await api.get(`${chatBase}/threads`);
    if (!Array.isArray(threads)) throw new TypeError("Unexpected response: the threads are not a list.");
    renderThreadCost(panel, threads.find((t) => t.id === threadId) || null);
  } catch (err) {
    renderThreadCost(panel, { cost: { calls: 0 } });
    const el = panel.querySelector(".tutor-cost");
    el.textContent = `Could not load this conversation's cost: ${describeError(err)}`;
    el.hidden = false;
  }
}

/** The small figure under a tutor reply (`msg.cost`), or null when the ledger has none. */
export function replyCost(msg) {
  const cost = msg && msg.cost;
  if (!cost || !cost.calls) return null;
  const calls = `${cost.calls} model ${cost.calls === 1 ? "call" : "calls"}`;
  return h("p", { class: "tutor-msg-cost", title: `What this reply cost (${calls})` }, costText(cost));
}
