// Pure helpers for the reader's standing on each concept and the "Suggested next" list (no DOM,
// so node can test them). The data comes from GET /api/history/concepts, /concepts/{slug} and
// /suggestions (teacher/web/routers/history.py; the rules are in
// teacher/managers/concept_standing.py and teacher/suggestions.py).

export const STATUSES = ["unstudied", "weak", "developing", "strong"];

const STATUS_LABELS = {
  unstudied: "Not studied yet",
  weak: "Weak",
  developing: "Developing",
  strong: "Strong",
};

const KIND_LABELS = {
  fill_gap: "Fill the gap",
  review: "Review",
  ready_next: "Ready next",
};

/** The words for a status; an unknown one is an error, not a guess. */
export function statusLabel(status) {
  if (!STATUS_LABELS[status]) throw new Error(`unknown standing status ${status}`);
  return STATUS_LABELS[status];
}

/** The words for a suggestion kind; an unknown one is an error. */
export function kindLabel(kind) {
  if (!KIND_LABELS[kind]) throw new Error(`unknown suggestion kind ${kind}`);
  return KIND_LABELS[kind];
}

/** A rate (0..1, or null) as "67%" (or "-"). */
export function rateText(rate) {
  return rate === null || rate === undefined ? "-" : `${Math.round(rate * 100)}%`;
}

/** "2 of 3 passed at the first attempt (67%)", or why there is nothing to say. */
export function passedText(s) {
  if (!s.questions_answered) {
    return s.questions ? `No answer yet (${s.questions} ${s.questions === 1 ? "question" : "questions"} in the library)` : "No question in the library tests it yet";
  }
  const base = `${s.first_attempt_passed} of ${s.questions_answered} passed at the first attempt`;
  const extra = [];
  if (s.first_attempt_partial) extra.push(`${s.first_attempt_partial} partly right`);
  if (s.reviews) extra.push(`${s.reviews_passed} of ${s.reviews} ${s.reviews === 1 ? "review" : "reviews"} passed`);
  // The rate counts a partly right answer as half a pass and every review as one more first try.
  return extra.length
    ? `${base}, ${extra.join(", ")} (${rateText(s.first_attempt_pass_rate)} overall)`
    : `${base} (${rateText(s.first_attempt_pass_rate)})`;
}

/** The thresholds as one sentence (GET /api/history/concepts `thresholds`). */
export function thresholdsText(t) {
  return (
    `From the questions about each concept: weak below ${rateText(t.weak_below)} passed at the first attempt; ` +
    `strong from ${rateText(t.strong_from)} with at least ${t.strong_min_questions} questions answered; ` +
    "developing in between; not studied yet before any answer."
  );
}

/** Map concept id -> status, from the standing records. */
export function statusById(concepts) {
  const map = new Map();
  for (const c of concepts) {
    statusLabel(c.status); // throws for an unknown status
    map.set(c.concept.id, c.status);
  }
  return map;
}

/** How many concepts are in each status: [[status, n], ...] in STATUSES order. */
export function statusCounts(concepts) {
  const counts = new Map(STATUSES.map((s) => [s, 0]));
  for (const c of concepts) {
    if (!counts.has(c.status)) throw new Error(`unknown standing status ${c.status}`);
    counts.set(c.status, counts.get(c.status) + 1);
  }
  return [...counts];
}

const ORDER = { weak: 0, developing: 1, strong: 2, unstudied: 3 };

/**
 * The standing rows the history page lists: concepts that any study-page block is tagged with
 * (or that were seen), weak first, then developing, strong, not studied; within a status the
 * most recently studied first, then by name. `all` keeps the concepts nothing covers too.
 */
export function standingRows(concepts, { all = false } = {}) {
  return concepts
    .filter((c) => all || c.blocks || c.incidental_blocks || c.seen)
    .slice()
    .sort(
      (a, b) =>
        ORDER[a.status] - ORDER[b.status] ||
        (b.last_studied_at || "").localeCompare(a.last_studied_at || "") ||
        a.concept.name.localeCompare(b.concept.name)
    );
}

/** The links a suggestion's action offers: [{href, text, note}]. */
export function actionLinks(action) {
  if (action.kind === "study_page") {
    return action.pages.map((p) => ({
      href: `/artifacts/${encodeURIComponent(p.slug)}`,
      text: p.title,
      note: [
        p.role === "main" ? (p.questions ? `${p.questions} ${p.questions === 1 ? "question" : "questions"}` : "about it") : "mentions it",
        p.superseded ? "older version" : null,
      ]
        .filter(Boolean)
        .join(", "),
    }));
  }
  if (action.kind === "source") {
    const notes = {
      no_study_page: "no study page yet",
      page_not_tagged: "its study page is not tagged yet",
      page_does_not_cover: "its study page does not cover it",
    };
    return action.sources.map((s) => {
      if (!notes[s.state]) throw new Error(`unknown source state ${s.state}`);
      return { href: `/sources/${encodeURIComponent(s.slug)}`, text: s.title, note: notes[s.state] };
    });
  }
  if (action.kind === "none") return [];
  if (action.kind === "review_queue") {
    // A review suggestion whose concept has questions in the review queue: the queue first,
    // then the pages that teach it.
    const r = action.review;
    return [
      { href: r.url, text: "Review queue", note: r.due ? `${r.due} due now` : "nothing due now" },
      ...actionLinks({ ...action, kind: "study_page" }),
    ];
  }
  throw new Error(`unknown suggestion action ${action.kind}`);
}

/** The numbers behind a suggestion, as short phrases for the page. */
export function evidenceParts(s) {
  const e = s.evidence;
  const passed = passedText(e);
  const parts = [`${statusLabel(e.status)}: ${passed[0].toLowerCase()}${passed.slice(1)}`];
  if (e.attempts) parts.push(`${e.attempts} ${e.attempts === 1 ? "attempt" : "attempts"} in all`);
  if (s.kind === "fill_gap") {
    parts.push(
      `needed for ${e.supports.map((w) => `${w.name} (${rateText(w.first_attempt_pass_rate)} first time)`).join(", ")}`
    );
  }
  if (s.kind === "ready_next" && e.prerequisites.length) {
    parts.push(`builds on ${e.prerequisites.map((p) => `${p.name} (${statusLabel(p.status).toLowerCase()})`).join(", ")}`);
  }
  if (e.days_since !== undefined) parts.push(`last studied ${e.days_since} days ago`);
  return parts;
}

/** A question of GET /api/history/concepts/{slug} `tested_by`, as one line of words. */
export function questionText(q) {
  const verdict = !q.attempts
    ? "not answered yet"
    : `${q.attempts} ${q.attempts === 1 ? "attempt" : "attempts"}, ${q.first_attempt_passed ? "passed the first time" : q.eventually_passed ? "passed after a retry" : "not passed"}`;
  return `${q.block} (${q.block_type === "blurb" ? "written answer" : "multiple choice"}, ${q.role === "main" ? "tests it" : "mentions it"}): ${verdict}`;
}
