// The add page's "Find sources" box (add_discover.js): its DOM-free logic, tested under node by
// tests/web/js/discover_test.mjs. It turns the reply of POST /api/sources/discover
// (teacher.discovery.DiscoveryResult.to_dict()) into what the page shows. A reply that is not
// shaped as expected throws: the page shows that error instead of a partial list.

export const PROVIDER_LABELS = { web: "Web search", arxiv: "arXiv" };

// The probe's verdicts (teacher.discovery.probe and teacher.url_sources' reasons): label, tone.
export const VERDICTS = {
  readable: ["Readable", "ok"],
  too_thin: ["Too little text", "warn"],
  no_text: ["No text in the PDF", "warn"],
  unreadable_pdf: ["PDF cannot be opened", "error"],
  paywalled: ["Needs a login", "error"],
  timeout: ["Did not answer", "error"],
  fetch_failed: ["Could not be fetched", "error"],
  http_error: ["Server error", "error"],
  too_large: ["Too large", "error"],
  unsupported_type: ["Unsupported file", "error"],
  blocked: ["Private address", "error"],
  invalid_url: ["Not a web address", "error"],
  empty: ["Empty document", "error"],
  undecodable: ["Unreadable text", "error"],
  unreadable: ["Cannot be read", "error"],
};

const TREATMENTS = { teaches: "Teaches it", mentions: "Only mentions it", off_topic: "Off topic" };

/** The request body for a topic and level; throws an Error saying what is missing. */
export function discoverRequest(topic, level) {
  const text = String(topic ?? "").trim();
  if (!text) throw new Error("Enter a topic to find sources for.");
  if (text.length > 300) throw new Error("The topic is too long: at most 300 characters.");
  return { topic: text, level };
}

export function providerLabel(name) {
  return PROVIDER_LABELS[name] || name;
}

/** One line per provider: { text, ok }. A failed provider says so with its error. */
export function providerLines(providers) {
  if (!Array.isArray(providers) || providers.length === 0) {
    throw new Error("The reply lists no providers.");
  }
  return providers.map((p) => {
    const label = providerLabel(p.name);
    if (p.ok) {
      const found = p.hits === 1 ? "1 result" : `${p.hits} results`;
      const warnings = (p.warnings || []).length ? ` (${p.warnings.join("; ")})` : "";
      return { ok: true, text: `${label}: ${found}${warnings}` };
    }
    return { ok: false, text: `${label} failed: ${p.error || "no reason given"}` };
  });
}

export function verdictView(probe) {
  if (!probe || typeof probe.verdict !== "string") throw new Error("A candidate has no probe verdict.");
  const [label, tone] = VERDICTS[probe.verdict] || [probe.verdict, "error"];
  return { label, tone, reason: probe.reason || "" };
}

function formatUsd(value) {
  return value < 0.01 ? `$${value.toFixed(4)}` : `$${value.toFixed(2)}`;
}

/** What the search cost, with every caveat said. */
export function usageText(usage) {
  if (!usage || typeof usage.total_usd !== "number" || !Array.isArray(usage.calls)) {
    throw new Error("The reply has no cost information.");
  }
  const calls = usage.calls.length;
  const parts = [`This search cost ${usage.upper_bound ? "at most " : ""}${formatUsd(usage.total_usd)} for ${calls === 1 ? "1 model call" : `${calls} model calls`}`];
  if (usage.unpriced_calls) parts.push(`plus ${usage.unpriced_calls} with no known price (${usage.unpriced_models.join(", ")})`);
  if (usage.web_search_requests) {
    parts.push(`and ${usage.web_search_requests === 1 ? "1 web search" : `${usage.web_search_requests} web searches`}, which are billed separately and not priced here`);
  }
  return `${parts.join(", ")}.`;
}

/** One candidate as the list shows it. */
export function candidateView(c) {
  if (!c || typeof c.url !== "string" || typeof c.title !== "string") {
    throw new Error("A candidate has no address or title.");
  }
  const verdict = verdictView(c.probe);
  const t = c.triage;
  const details = [];
  if (c.probe.kind === "paper") details.push("PDF");
  else if (c.probe.kind === "web_page") details.push("web page");
  if (typeof c.probe.words === "number") {
    details.push(`${c.probe.words_estimated ? "about " : ""}${c.probe.words.toLocaleString("en-US")} words`);
  }
  if ((c.authors || []).length) details.push(c.authors.slice(0, 3).join(", ") + (c.authors.length > 3 ? " et al." : ""));
  if (c.year) details.push(String(c.year));
  return {
    id: c.id,
    rank: c.rank,
    title: c.title,
    url: c.url,
    landing: c.landing_url ? `Full text of ${c.landing_url}` : null,
    details: details.join(" · "),
    score: t ? `${t.overall.toFixed(1)} / 5` : null,
    judgement: t
      ? `${TREATMENTS[t.treatment] || t.treatment}: ${t.reason} (topic ${t.topic_fit}/5, level ${t.level_match}/5, teaching ${t.teaching}/5)`
      : null,
    verdict,
    foundBy: (c.found_by || []).map((f) => `${providerLabel(f.provider)}: ${f.reason}`),
    library: (c.library || []).map((m) => ({ slug: m.slug, title: m.title })),
  };
}

/** The whole reply: { summary, providers, items, notKept, triageNote, cost, empty }. */
export function resultView(result) {
  if (!result || !Array.isArray(result.candidates) || !result.query) {
    throw new Error("The reply is not a list of sources.");
  }
  const items = result.candidates.map(candidateView);
  const providers = providerLines(result.providers);
  const n = items.length;
  const readable = result.candidates.filter((c) => c.probe.readable).length;
  const summary = n
    ? `${n === 1 ? "1 source" : `${n} sources`} for “${result.query.topic}”, ${readable} readable, best first.`
    : `No sources were found for “${result.query.topic}”.`;
  const notKept = (result.not_kept || []).length
    ? `${result.not_kept.length} more ${result.not_kept.length === 1 ? "result was" : "results were"} found but not checked: ${result.not_kept.map((x) => x.title).join("; ")}.`
    : null;
  return {
    summary,
    providers,
    items,
    notKept,
    triageNote: result.triage && result.triage.note ? result.triage.note : null,
    cost: usageText(result.usage),
    empty: n === 0,
  };
}
