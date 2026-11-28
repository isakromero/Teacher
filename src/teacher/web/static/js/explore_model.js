// The Explore pages' DOM-free logic (explore.js: the form and the list; exploration.js: one
// exploration to review), tested under node by tests/web/js/explore_test.mjs. It turns the
// replies of /api/explorations (teacher.exploration.view) into what the pages show, and works
// out the edits' request bodies. A reply that is not shaped as expected throws: the page shows
// that error instead of a partial view. The enrichment (teacher.exploration.enrichment): what
// its preview says will happen (previewView), its progress and summary (enrichmentView), and
// per candidate and concept what it did (candidateView, conceptView).

import { VERDICTS } from "./discover_model.js";

export const POLL_MS = 2000;

export const STATUS_LABELS = { running: "Running", completed: "Completed", failed: "Failed" };

export const STAGE_LABELS = {
  brief: "Writing the brief",
  outline: "Outlining the concepts",
  graph_match: "Matching the concepts against your concept graph",
  discovery: "Finding sources",
};

// A concept's status: label and tone.
export const CONCEPT_STATUS = {
  planned: ["New", "warn"],
  known: ["Known", "ok"],
  grounded: ["Grounded", "ok"],
  dropped: ["Dropped", "muted"],
};

export const COVERAGE_TEXT = {
  new: "New to your concept graph.",
  thin: "In your concept graph, but no source in your library introduces it.",
  covered: "In your concept graph, and a source in your library introduces it.",
};

const STANDING_TEXT = {
  strong: "strong",
  developing: "developing",
  weak: "weak",
  unstudied: "not studied yet",
};

const TREATMENTS = { teaches: "Teaches it", mentions: "Only mentions it", off_topic: "Off topic" };

export const ENRICHMENT_STAGE_LABELS = {
  ingest: "Adding the sources to your library",
  graph: "Writing the outline to your concept graph",
  grounding: "Checking which concepts are grounded",
};

const GRAPH_OUTCOMES = {
  matched: "In your concept graph (matched before).",
  found: "Found in your concept graph after the sources were added",
  created: "Added to your concept graph as a new, unreviewed concept",
};

function plural(n, one, many = `${one}s`) {
  return `${n} ${n === 1 ? one : many}`;
}

/** The request body for a topic and level; throws an Error saying what is missing. */
export function exploreRequest(topic, level) {
  const text = String(topic ?? "").trim();
  if (!text) throw new Error("Enter a topic to explore.");
  if (text.length > 300) throw new Error("The topic is too long: at most 300 characters.");
  return { topic: text, level };
}

/** What a run is limited to, said before it starts (there is no price estimate). */
export function limitsText(limits) {
  if (!limits || typeof limits.max_concepts !== "number") throw new Error("The limits are missing.");
  return (
    `At most ${limits.max_concepts} concepts. For each concept that is new or thinly covered: ` +
    `at most ${limits.searches_per_concept} searches (one, and one follow-up if nothing readable ` +
    `fits) and at most ${limits.candidates_per_concept} candidate sources. There is no reliable ` +
    "price estimate: what the run spent is shown once it has run. Nothing is added to your " +
    "library or your concept graph."
  );
}

/** The progress line of a running exploration (or the stage it stopped in). */
export function progressText(x) {
  if (x.status === "completed") return "Completed.";
  const label = STAGE_LABELS[x.stage] || (x.stage ? x.stage : "Starting");
  const p = x.progress || { done: 0, total: 0 };
  const counted = x.stage === "discovery" && p.total
    ? `: ${p.done} of ${p.total} ${p.total === 1 ? "concept" : "concepts"} done`
    : "";
  if (x.status === "failed") return `Failed while ${label.charAt(0).toLowerCase()}${label.slice(1)}${counted}.`;
  return `${label}${counted}...`;
}

export function formatUsd(value) {
  return value < 0.01 ? `$${value.toFixed(4)}` : `$${value.toFixed(2)}`;
}

/** What the run cost, from the cost ledger, with every caveat said. */
export function costText(cost) {
  if (!cost || typeof cost.total_usd !== "number" || typeof cost.calls !== "number") {
    throw new Error("The exploration has no cost information.");
  }
  if (!cost.calls) return "No model call has been made yet.";
  const parts = [
    `${cost.estimate ? "At most " : ""}${formatUsd(cost.total_usd)} for ${cost.calls === 1 ? "1 model call" : `${cost.calls} model calls`}`,
  ];
  if (cost.unknown_price_calls) {
    parts.push(`plus ${cost.unknown_price_calls} with no known price (${cost.unknown_price_models.join(", ")})`);
  }
  if (cost.web_search_requests) {
    const n = cost.web_search_requests;
    parts.push(`and ${n === 1 ? "1 web search" : `${n} web searches`}, billed separately and not priced here`);
  }
  return `${parts.join(", ")}.`;
}

/** One past exploration in the list. */
export function summaryView(item) {
  if (!item || typeof item.id !== "number" || typeof item.prompt !== "string") {
    throw new Error("An exploration in the list has no id or topic.");
  }
  const counts = [];
  if (item.concepts) counts.push(item.concepts === 1 ? "1 concept" : `${item.concepts} concepts`);
  if (item.candidates) counts.push(item.candidates === 1 ? "1 candidate source" : `${item.candidates} candidate sources`);
  return {
    id: item.id,
    href: `/explore/${item.id}`,
    title: item.prompt,
    level: item.level_label || item.level,
    status: item.status,
    statusLabel: STATUS_LABELS[item.status] || item.status,
    progress: item.status === "running" ? progressText(item) : null,
    error: item.error || null,
    counts: counts.join(", "),
  };
}

/** A new step order with the step at `index` (0-based) moved by `delta` (-1 up, +1 down);
 * null when it cannot move. The order is what PUT /api/explorations/{id}/steps takes. */
export function moveStep(count, index, delta) {
  const target = index + delta;
  if (index < 0 || index >= count || target < 0 || target >= count) return null;
  const order = Array.from({ length: count }, (_, i) => i + 1);
  [order[index], order[target]] = [order[target], order[index]];
  return order;
}

/** Whether moving would break a prerequisite (a concept before or beside what it builds on):
 * the reason, or null. The server checks the same and refuses. */
export function moveProblem(steps, order) {
  const stepOf = {};
  const names = {};
  order.forEach((oldNumber, i) => {
    const step = steps[oldNumber - 1];
    for (const c of step.concepts) {
      if (c.status === "dropped") continue;
      stepOf[c.key] = i + 1;
      names[c.key] = c.name;
    }
  });
  for (const step of steps) {
    for (const c of step.concepts) {
      if (!(c.key in stepOf)) continue;
      for (const p of c.prerequisites) {
        if (p.key in stepOf && stepOf[p.key] > stepOf[c.key]) {
          return `“${c.name}” builds on “${names[p.key]}”, which must stay in an earlier step.`;
        }
      }
    }
  }
  return null;
}

/** One candidate source of a concept. */
export function candidateView(c, conceptKey, namesByKey = {}) {
  if (!c || typeof c.id !== "number" || typeof c.url !== "string" || typeof c.title !== "string") {
    throw new Error("A candidate source has no id, address or title.");
  }
  const [label, tone] = VERDICTS[c.verdict] || [c.verdict, "error"];
  const t = c.triage;
  const details = [];
  const probe = c.probe || {};
  if (probe.kind === "paper") details.push("PDF");
  else if (probe.kind === "web_page") details.push("web page");
  if (typeof probe.words === "number") {
    details.push(`${probe.words_estimated ? "about " : ""}${probe.words.toLocaleString("en-US")} words`);
  }
  if ((c.authors || []).length) details.push(c.authors.slice(0, 3).join(", ") + (c.authors.length > 3 ? " et al." : ""));
  if (c.year) details.push(String(c.year));
  const others = (c.concepts || []).filter((k) => k !== conceptKey).map((k) => namesByKey[k] || k);
  return {
    id: c.id,
    rank: c.rank,
    title: c.title,
    url: c.url,
    landing: c.landing_url ? `Full text of ${c.landing_url}` : null,
    details: details.join(" · "),
    verdict: { label, tone, reason: c.verdict_reason || "" },
    score: t ? `${t.overall.toFixed(1)} / 5` : null,
    judgement: t
      ? `${TREATMENTS[t.treatment] || t.treatment}: ${t.reason} (topic ${t.topic_fit}/5, level ${t.level_match}/5, teaching ${t.teaching}/5)`
      : null,
    fits: Boolean(c.fits),
    followUp: c.attempt === 2,
    foundBy: (c.found_by || []).map((f) => `${f.provider === "web" ? "Web search" : f.provider === "arxiv" ? "arXiv" : f.provider}: ${f.reason}`),
    library: (c.library || []).map((m) => ({ slug: m.slug, title: m.title })),
    alsoFor: others,
    decision: c.decision,
    approved: c.decision === "approved" || c.decision === "ingested",
    rejected: c.decision === "rejected",
    ingested: c.decision === "ingested",
    source: c.knowledge_source ? { slug: c.knowledge_source.slug, title: c.knowledge_source.title } : null,
    ingestText: ingestText(c),
    ingestError: c.decision === "ingested" ? null : c.ingest_error || null,
  };
}

/** What the last enrichment did with a candidate, in words (null before any). */
export function ingestText(c) {
  const d = c.ingest;
  if (!d || c.decision !== "ingested") return null;
  const held = d.outcome === "held";
  let text = held
    ? `Already in your library${d.method === "earlier attempt" ? " (stored by an earlier attempt)" : d.method ? ` (found by ${d.method === "url" ? "address" : d.method === "content" ? "its content" : d.method})` : ""}: linked, not added again.`
    : "Added to your library.";
  if (d.extraction === "ran") {
    const x = d.extracted;
    text += x ? ` Its concepts were extracted: ${plural(x.links, "concept")}, ${x.created} new.` : " Its concepts were extracted.";
  } else if (d.extraction === "done_before") text += " Its concepts had been extracted before.";
  if (d.slug_note) text += ` (${d.slug_note}.)`;
  return text;
}

/** One concept of the outline. */
export function conceptView(concept, namesByKey = {}) {
  if (!concept || typeof concept.id !== "number" || typeof concept.name !== "string") {
    throw new Error("A concept of the outline has no id or name.");
  }
  const [statusLabel, tone] = CONCEPT_STATUS[concept.status] || [concept.status, "muted"];
  const known = concept.known;
  let standing = null;
  if (known && known.standing) {
    const s = known.standing;
    standing = `Your standing: ${STANDING_TEXT[s.status] || s.status}`;
    if (s.questions_answered) {
      standing += ` (${s.first_attempt_passed} of ${s.questions_answered} ${s.questions_answered === 1 ? "question" : "questions"} right the first time)`;
    }
    standing += ".";
  }
  const match = concept.match;
  let matchText = null;
  if (match) {
    if (match.concept_missing) matchText = "It matched a concept that has since been deleted from your graph.";
    else if (match.method === "exact") matchText = `Matched by name to “${known ? known.concept.name : ""}”.`;
    else if (match.method === "judged") matchText = `Judged the same as “${known ? known.concept.name : ""}”: ${match.reason}`;
    else matchText = match.reason ? `New: ${match.reason}` : "New: no concept of your graph is the same.";
  }
  const d = concept.discovery || { status: "not_needed", error: null, log: [] };
  let discovery = null;
  if (d.status === "pending") discovery = "Waiting for its source search.";
  else if (d.status === "done" || d.status === "failed") {
    const searches = (d.log || []).filter((e) => typeof e.attempt === "number");
    discovery = searches.map((e) => {
      const head = e.attempt === 1 ? `Searched for “${e.query}”` : `Follow-up search for “${e.query}”`;
      if (e.error) return `${head}: failed.`;
      return `${head}: ${e.candidates} ${e.candidates === 1 ? "candidate" : "candidates"}, ${e.fitting} fitting.`;
    }).join(" ");
  } else if (concept.coverage === "covered") discovery = "No search: a source in your library introduces it.";
  const prerequisites = concept.prerequisites.map((p) => (p.dropped ? `${p.name} (dropped)` : p.name));
  const graph = concept.graph;
  let graphText = null;
  if (graph && graph.outcome === "matched") graphText = GRAPH_OUTCOMES.matched;
  else if (graph) graphText = `${GRAPH_OUTCOMES[graph.outcome] || graph.outcome}${graph.reason ? `: ${graph.reason}` : ""}.`;
  const sources = concept.grounding || [];
  const groundedBy = sources.filter((g) => g.grounds).map((g) => ({ slug: g.slug, title: g.title }));
  const alsoLinked = sources.filter((g) => !g.grounds).map((g) => ({ slug: g.slug, title: g.title, role: g.role }));
  let grounding = null;
  if (concept.status === "grounded") grounding = "Grounded: a source in your library introduces it.";
  else if (graph && concept.status !== "dropped") {
    const proposed = concept.candidates.filter((c) => c.decision === "proposed").length;
    grounding = "Not grounded yet: no source in your library introduces it."
      + (proposed === 1 ? " Tick its proposed source and enrich again."
        : proposed ? ` Tick one of its ${proposed} proposed sources and enrich again.` : "");
  }
  const LINK_TEXT = { added: "added to your graph", present: "already in your graph" };
  const links = (concept.links || []).map((l) =>
    l.outcome === "skipped" ? `${l.name}: not written (${l.reason})` : `${l.name}: ${LINK_TEXT[l.outcome] || l.outcome}`);
  return {
    id: concept.id,
    key: concept.key,
    name: concept.name,
    renamedFrom: concept.renamed ? concept.drafted_name : null,
    summary: concept.summary,
    aliases: concept.aliases || [],
    status: concept.status,
    statusLabel,
    tone,
    dropped: concept.status === "dropped",
    coverage: COVERAGE_TEXT[concept.coverage] || null,
    match: matchText,
    known: known
      ? {
          slug: known.concept.slug,
          name: known.concept.name,
          pages: known.pages.map((p) => ({ slug: p.slug, title: p.title, kind: p.kind })),
          sources: known.sources.map((s) => ({ slug: s.slug, title: s.title, role: s.role })),
        }
      : null,
    standing,
    prerequisites,
    discovery,
    error: d.status === "failed" ? d.error : null,
    leftOut: (d.log || []).filter((e) => e.left_out).map((e) => e.note),
    candidates: concept.candidates.map((c) => candidateView(c, concept.key, namesByKey)),
    graph: graphText,
    grounded: concept.status === "grounded",
    grounding,
    groundedBy,
    alsoLinked,
    links,
  };
}

/** The progress line of an enrichment (or the stage it stopped in). */
export function enrichmentProgressText(e) {
  if (e.status === "completed") return "The enrichment is done.";
  const label = ENRICHMENT_STAGE_LABELS[e.stage] || (e.stage ? e.stage : "Starting the enrichment");
  const p = e.progress || { done: 0, total: 0 };
  const counted = e.stage === "ingest" && p.total ? `: ${p.done} of ${plural(p.total, "source")} done` : "";
  if (e.status === "failed") return `The enrichment failed while ${label.charAt(0).toLowerCase()}${label.slice(1)}${counted}.`;
  return `${label}${counted}...`;
}

/** The latest enrichment as the review page shows it: progress, error and summary lines. */
export function enrichmentView(e) {
  if (!e || typeof e.id !== "number" || typeof e.status !== "string") throw new Error("The enrichment is not shaped as expected.");
  const s = e.summary;
  const lines = [];
  const failures = [];
  if (s) {
    const src = s.sources;
    lines.push(`Sources: ${src.added} added, ${src.held} already in your library (linked), ${src.failed} failed.`);
    for (const item of src.items) if (item.outcome === "failed") failures.push(`${item.title}: ${item.error}`);
    const c = s.concepts;
    lines.push(`Concepts: ${c.matched + c.found} matched (${c.found} found after the sources were added), ${c.created} created.`);
    const r = s.relations;
    lines.push(`Prerequisite links: ${r.added} added, ${r.present} already present, ${r.skipped} skipped.`);
    for (const item of r.items) if (item.outcome === "skipped") lines.push(`Skipped: “${item.from}” builds on “${item.to}”: ${item.reason}`);
    const g = s.grounding;
    lines.push(`Grounding: ${g.grounded} grounded, ${g.ungrounded} still ungrounded.`);
    const open = g.items.filter((i) => !i.grounded).map((i) => i.name);
    if (open.length) lines.push(`Still ungrounded: ${open.join(", ")}.`);
  }
  return {
    id: e.id,
    status: e.status,
    running: e.status === "running",
    progress: enrichmentProgressText(e),
    error: e.error || null,
    lines,
    failures: failures.length
      ? `${plural(failures.length, "source")} could not be added and ${failures.length === 1 ? "stays" : "stay"} ticked to be tried again: ${failures.join("; ")}`
      : null,
    cost: e.cost ? costText(e.cost) : null,
  };
}

const EXPECTED = { add: "will be added", present: "is already in your graph", skip: "will not be written" };

/** What the preview (GET .../enrichment/preview) says the enrichment will do. */
export function previewView(p) {
  if (!p || typeof p.digest !== "string" || !p.sources || !p.concepts || !p.links) {
    throw new Error("The preview is not shaped as expected.");
  }
  const sources = p.sources.items.map((s) => {
    let text = s.action === "link"
      ? `${s.title}: already in your library as “${s.held.title}”, linked`
      : `${s.title}: added to your library`;
    text += s.extraction === "run" ? ", its concepts extracted" : " (its concepts were extracted before)";
    if (s.last_error) text += `. The last attempt failed: ${s.last_error}`;
    return `${text}.`;
  });
  const concepts = p.concepts.items.map((c) =>
    c.action === "use" ? `${c.name}: in your graph as “${c.concept.name}”` : `${c.name}: looked up again after the sources are added, created if still absent (unreviewed)`);
  const links = p.links.items.map((l) => `“${l.from}” builds on “${l.to}”: ${EXPECTED[l.expected] || l.expected}${l.reason ? ` (${l.reason})` : ""}.`);
  return {
    digest: p.digest,
    canStart: Boolean(p.can_start),
    blocked: p.blocked || null,
    headline:
      `${plural(p.sources.to_add, "source")} will be added to your library and ${p.sources.to_link} linked; ` +
      `${plural(p.concepts.total, "concept")} written to your concept graph (${p.concepts.to_resolve} to look up or create); ` +
      `${plural(p.links.to_add, "prerequisite link")} to add, ${p.links.present} already present, ${p.links.to_skip} not written.`,
    sources,
    concepts,
    links,
    dropped: p.dropped.length ? `Left out (dropped): ${p.dropped.join(", ")}.` : null,
    note: p.model_calls,
    empty: p.sources.total === 0,
  };
}

/** The whole exploration as the review page shows it. */
export function detailView(data) {
  if (!data || typeof data.id !== "number" || !Array.isArray(data.steps) || !data.cost) {
    throw new Error("The reply is not an exploration.");
  }
  const namesByKey = {};
  for (const step of data.steps) for (const c of step.concepts) namesByKey[c.key] = c.name;
  const steps = data.steps.map((s, i) => ({
    number: s.number,
    title: s.title,
    canMoveUp: data.editable && moveStep(data.steps.length, i, -1) !== null && moveProblem(data.steps, moveStep(data.steps.length, i, -1)) === null,
    canMoveDown: data.editable && moveStep(data.steps.length, i, 1) !== null && moveProblem(data.steps, moveStep(data.steps.length, i, 1)) === null,
    concepts: s.concepts.map((c) => conceptView(c, namesByKey)),
  }));
  const failures = steps.flatMap((s) => s.concepts.filter((c) => c.error).map((c) => c.name));
  return {
    id: data.id,
    title: data.prompt,
    level: data.level_label || data.level,
    status: data.status,
    statusLabel: STATUS_LABELS[data.status] || data.status,
    running: data.status === "running",
    progress: progressText(data),
    error: data.error || null,
    notes: data.notes || [],
    brief: data.brief,
    questions: data.questions || [],
    steps,
    failures: failures.length
      ? `Source search failed for ${failures.length === 1 ? "1 concept" : `${failures.length} concepts`}: ${failures.join(", ")}. Their errors are shown with them.`
      : null,
    approved: data.approved,
    cost: costText(data.cost),
    editable: Boolean(data.editable),
    limits: data.limits ? limitsText(data.limits) : null,
    enriching: Boolean(data.enriching),
    canEnrich: Boolean(data.can_enrich),
    enrichment: data.enrichment ? enrichmentView(data.enrichment) : null,
    poll: data.status === "running" || Boolean(data.enriching),
  };
}

/** The PATCH body for a candidate's checkbox. */
export function decisionFor(checked) {
  return { decision: checked ? "approved" : "proposed" };
}
