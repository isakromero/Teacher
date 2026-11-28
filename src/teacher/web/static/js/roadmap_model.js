// The roadmap's DOM-free logic (roadmap.js: one exploration's roadmap; explore.js and home.js: a
// roadmap's progress line), tested under node by tests/web/js/roadmap_test.mjs. It turns the
// replies of GET /api/explorations/{id}/roadmap (teacher.exploration.roadmap.roadmap_view) and a
// list item's `roadmap` (roadmap_summary) into what the pages show, and builds the request bodies
// of the reader's choices. A reply that is not shaped as expected throws: the page shows that
// error instead of a partial view.

export const POLL_MS = 2000;

// A step's state: label and tone.
export const STEP_STATES = {
  done: ["Done", "ok"],
  current: ["Current step", "warn"],
  upcoming: ["Upcoming", "muted"],
};

// Why a concept is done (teacher.exploration.roadmap.DONE_REASONS).
export const DONE_BY_TEXT = {
  marked_known: "you marked it “I know this”",
  strong: "your standing on it is strong",
  page_complete: "its study page is complete",
};

const STANDING_TEXT = {
  strong: "strong",
  developing: "developing",
  weak: "weak",
  unstudied: "not studied yet",
};

// The next action's button or link (teacher.exploration.roadmap.NEXT_ACTIONS).
const ACTION_LABELS = {
  open_page: "Open the page",
  continue_page: "Continue the page",
  review: "Review now",
  choose_source: "Choose a source",
  generate_page: "Generate page",
  enrich: "Find sources",
  done: null,
};

function plural(n, one, many = `${one}s`) {
  return `${n} ${n === 1 ? one : many}`;
}

function names(list) {
  if (list.length <= 1) return list.join("");
  return `${list.slice(0, -1).join(", ")} and ${list[list.length - 1]}`;
}

/** The anchor of a concept on the roadmap page. */
export function conceptAnchor(key) {
  return `roadmap-concept-${key}`;
}

/** The overall progress in words, and as a fraction for the bar. */
export function progressText(p) {
  if (!p || typeof p.concepts_done !== "number" || typeof p.concepts_total !== "number") {
    throw new Error("The roadmap has no progress.");
  }
  if (p.complete) return `Complete: all ${plural(p.concepts_total, "concept")} done.`;
  return `${p.concepts_done} of ${plural(p.concepts_total, "concept")} done · ${p.steps_done} of ${plural(p.steps_total, "step")} done`;
}

export function progressFraction(p) {
  return p.concepts_total ? p.concepts_done / p.concepts_total : 1;
}

/** The reader's standing on the concept, in words. */
export function standingText(standing) {
  if (!standing) return "Not in your concept graph yet, so there is no standing.";
  let text = `Your standing: ${STANDING_TEXT[standing.status] || standing.status}`;
  if (standing.questions_answered) {
    text += ` (${standing.first_attempt_passed} of ${plural(standing.questions_answered, "question")} right the first time)`;
  }
  return `${text}.`;
}

/** A study page as the roadmap shows it: where it is and how far the reader is. */
export function pageView(page) {
  if (!page || typeof page.slug !== "string" || typeof page.url !== "string") throw new Error("A study page has no slug or address.");
  let label;
  let tone;
  if (page.status === "generating") [label, tone] = ["Being generated: open it to watch its progress", "warn"];
  else if (page.complete) [label, tone] = ["Complete", "ok"];
  else if (page.started) [label, tone] = [`${page.gates_cleared} of ${plural(page.gates_total, "gate")} cleared`, "warn"];
  else [label, tone] = ["Not started", "muted"];
  return { slug: page.slug, title: page.title, href: page.url, label, tone, complete: Boolean(page.complete), generating: page.status === "generating" };
}

/** One concept of the roadmap. */
export function conceptView(c, reviewUrl) {
  if (!c || typeof c.id !== "number" || typeof c.name !== "string" || !Array.isArray(c.sources)) {
    throw new Error("A concept of the roadmap has no id, name or sources.");
  }
  const doneBy = c.done_by.map((r) => DONE_BY_TEXT[r] || r);
  const ungrounded = c.state === "ungrounded";
  return {
    id: c.id,
    key: c.key,
    anchor: conceptAnchor(c.key),
    name: c.name,
    summary: c.summary,
    prerequisites: c.prerequisites.length ? `Builds on: ${c.prerequisites.map((p) => p.name).join(", ")}.` : null,
    done: Boolean(c.done),
    doneText: c.done ? `Done: ${names(doneBy)}.` : null,
    standing: standingText(c.standing),
    graphHref: c.concept ? `/graph?concept=${encodeURIComponent(c.concept.slug)}` : null,
    grounded: Boolean(c.grounded),
    primarySource: c.primary_source
      ? { slug: c.primary_source.slug, title: c.primary_source.title, chosen: Boolean(c.primary_source.chosen) }
      : null,
    // The choice is offered when there is one to make, or when the chosen source no longer grounds it.
    choices: c.sources.length > 1 || c.choice_problem ? c.sources.map((s) => ({ slug: s.slug, title: s.title, recommended: s.recommended, chosen: s.chosen })) : [],
    chosen: Boolean(c.primary_source && c.primary_source.chosen),
    choiceProblem: c.choice_problem ? `No source to study it from: ${c.choice_problem}` : null,
    page: c.page ? pageView(c.page) : null,
    failedPage: c.failed_page
      ? { href: c.failed_page.url, title: c.failed_page.title, text: "The last attempt to generate its page failed:" }
      : null,
    shared: c.shared_with.length
      ? `${c.page ? "This page" : "A page of this source"} is shared with ${names(c.shared_with)}: one page for all of them.`
      : null,
    canGenerate: Boolean(c.can_generate),
    ungrounded: ungrounded
      ? {
          text: c.concept
            ? "No source in your library introduces it, so there is no page to generate yet. Find and tick sources for it on the review page, then enrich."
            : "It is not in your concept graph yet and no source introduces it. Find and tick sources for it on the review page, then enrich.",
          href: `${reviewUrl}#concept-${c.key}`,
        }
      : null,
    markedKnown: Boolean(c.marked_known),
    review: c.review && c.review.due && c.standing && c.standing.status === "weak"
      ? { text: `${plural(c.review.due, "question")} due in the review queue.`, href: c.review.url }
      : null,
  };
}

/** The one next action: its words, and the link or the button that does it. */
export function nextActionView(a) {
  if (!a || typeof a.kind !== "string" || typeof a.text !== "string") throw new Error("The roadmap has no next action.");
  if (!(a.kind in ACTION_LABELS)) throw new Error(`Unknown next action ${a.kind}.`);
  return {
    kind: a.kind,
    text: a.text,
    label: ACTION_LABELS[a.kind],
    href: a.kind === "choose_source" ? `#${conceptAnchor(a.concept.key)}` : a.url,
    generateFor: a.kind === "generate_page" ? a.concept.id : null,
    done: a.kind === "done",
  };
}

/** The whole roadmap as the page shows it. */
export function roadmapView(data) {
  if (!data || !data.exploration || !Array.isArray(data.steps) || !data.progress || !data.next_action) {
    throw new Error("The reply is not a roadmap.");
  }
  const steps = data.steps.map((s) => {
    const [label, tone] = STEP_STATES[s.state] || [s.state, "muted"];
    return {
      number: s.number,
      title: s.title,
      state: s.state,
      label,
      tone,
      count: `${s.done} of ${plural(s.total, "concept")} done`,
      current: s.state === "current",
      concepts: s.concepts.map((c) => conceptView(c, data.review_url)),
    };
  });
  const generating = steps.some((s) => s.concepts.some((c) => c.page && c.page.generating));
  return {
    id: data.exploration.id,
    title: data.exploration.prompt,
    meta: `${data.exploration.level_label || data.exploration.level} · ${data.progress.complete ? "Complete" : data.current_step ? `Step ${data.current_step} of ${data.progress.steps_total}` : ""}`,
    progress: progressText(data.progress),
    fraction: progressFraction(data.progress),
    next: nextActionView(data.next_action),
    steps,
    rules: [data.rules.concept_done, data.rules.step_done, data.rules.current_step,
      `The next action is the first of these that applies: ${data.rules.next_action.map((r, i) => `(${i + 1}) ${r}`).join(" ")}`],
    dropped: data.dropped.length ? `Dropped from the plan (not counted): ${data.dropped.map((d) => d.name).join(", ")}.` : null,
    enriching: data.exploration.enriching
      ? "An enrichment of this exploration is running: which concepts are grounded may change when it ends."
      : null,
    poll: generating || Boolean(data.exploration.enriching),
  };
}

/** A roadmap's line in the explorations list and on the home page (a list item's `roadmap`). */
export function roadmapSummaryView(summary) {
  if (!summary) return null;
  if (typeof summary.url !== "string" || !summary.progress || !summary.next_action) {
    throw new Error("A roadmap summary is not shaped as expected.");
  }
  const p = summary.progress;
  return {
    href: summary.url,
    complete: Boolean(p.complete),
    progress: progressText(p),
    step: summary.current_step ? `Step ${summary.current_step}: ${summary.current_step_title}` : null,
    next: summary.next_action.kind === "done" ? null : summary.next_action.text,
  };
}

/** The PATCH body for the "I know this" box. */
export function knownBody(checked) {
  return { marked_known: Boolean(checked) };
}

/** The PATCH body for choosing a source (null: the recommended one). */
export function sourceBody(slug) {
  return { primary_source: slug || null };
}
