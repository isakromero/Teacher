// The review page's logic that needs no DOM, so that tests/web/js/review_test.mjs can run it
// under node: the queue's address, what the header says, the empty states (nothing yet,
// nothing due, today's reviews done), when something is due in words, the line under a
// graded review (when the question comes back), the label of the form asked, and what each
// page's review bank status means for the reader.

/** How many more reviews "Keep going" adds to today's cap; the API's highest cap. */
export const EXTEND_STEP = 10;
export const MAX_CAP = 500;
export const DEFAULT_CAP = 20;

const DAY_MS = 86400000;

/** GET /api/review/queue with these parameters. */
export function queueUrl({ tz, cap = DEFAULT_CAP, limit = 1, concept = null }) {
  if (!tz) throw new Error("The review queue needs the reader's time zone.");
  const params = new URLSearchParams({ tz, cap: String(cap), limit: String(limit) });
  if (concept) params.set("concept", concept);
  return `/api/review/queue?${params}`;
}

/** Today's cap after "Keep going". */
export function extendCap(cap) {
  return Math.min(cap + EXTEND_STEP, MAX_CAP);
}

function plural(n, one, many = `${one}s`) {
  return `${n} ${n === 1 ? one : many}`;
}

function toDate(value) {
  const date = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(date.getTime())) throw new Error(`Not a time: ${JSON.stringify(value)}`);
  return date;
}

/**
 * When `iso` is, from `now`, in words: "now", "in 40 minutes", "in 5 hours", "tomorrow",
 * "in 3 days", "in 4 weeks". (A time in the past is "now": it is due.)
 */
export function dueIn(iso, now) {
  const ms = toDate(iso) - toDate(now);
  if (ms <= 0) return "now";
  const minutes = Math.round(ms / 60000);
  if (minutes < 60) return `in ${plural(Math.max(minutes, 1), "minute")}`;
  const hours = Math.round(ms / 3600000);
  if (hours < 24) return `in ${plural(hours, "hour")}`;
  const days = Math.round(ms / DAY_MS);
  if (days === 1) return "tomorrow";
  if (days < 28) return `in ${days} days`;
  return `in ${Math.round(days / 7)} weeks`;
}

/** The header line: what is due and how far today has got. */
export function queueSummary(queue) {
  const parts = [];
  parts.push(queue.due ? `${plural(queue.due, "question")} due now` : "Nothing due now");
  parts.push(`${queue.done_today} of today's ${queue.cap} reviews done`);
  if (queue.items_total) parts.push(`${plural(queue.items_total, "question")} in the queue`);
  return parts.join(" · ");
}

/**
 * What to show when the queue serves no question: `{kind, text, canExtend}`. `kind` is
 * "empty" (no review items at all), "waiting" (nothing due yet), "capped" (today's reviews are
 * done but more are due). Null when there is a question to show.
 */
export function emptyState(queue) {
  if (queue.items.length) return null;
  if (!queue.items_total) {
    return {
      kind: "empty",
      text: queue.concept
        ? `No question about ${queue.concept.name} is in the review queue yet. A question joins it once you have answered it on a study page.`
        : "Nothing to review yet. A question joins the review queue once you have answered it on a study page.",
      canExtend: false,
    };
  }
  if (queue.due && queue.remaining_today === 0) {
    return {
      kind: "capped",
      text: `You have done today's ${queue.cap} reviews. ${plural(queue.due, "more question is", "more questions are")} due: stop here, or keep going.`,
      canExtend: queue.cap < MAX_CAP,
    };
  }
  if (!queue.next_due_at) {
    throw new Error("The review queue has questions but says none is due and none comes next.");
  }
  return {
    kind: "waiting",
    text: `Nothing is due now. The next question is due ${dueIn(queue.next_due_at, queue.now)} (${formatWhen(queue.next_due_at)}).`,
    canExtend: false,
  };
}

/** A time for people, in the reader's locale. */
export function formatWhen(iso) {
  return toDate(iso).toLocaleString(undefined, { weekday: "short", day: "numeric", month: "short", hour: "numeric", minute: "2-digit" });
}

/** Days as words: "1 day", "2.2 days", "3 weeks". */
export function intervalText(days) {
  if (!(days > 0)) throw new Error(`Not an interval: ${JSON.stringify(days)}`);
  if (days >= 14) return plural(Math.round(days / 7), "week");
  const rounded = Math.round(days * 10) / 10;
  return `${rounded} ${rounded === 1 ? "day" : "days"}`;
}

/** The line under a graded review: what happens to the question's schedule. */
export function scheduleText(result) {
  const when = intervalText(result.interval_days);
  switch (result.verdict) {
    case "passed":
      return `Right. This question comes back in ${when}, a longer gap than last time.`;
    case "partial":
      return `Partly right. This question comes back in ${when}, the same gap as last time.`;
    case "failed":
      return `Not this time. This question comes back in ${when}, so you can try it again soon.`;
    default:
      throw new Error(`The server sent a review without a known verdict (${JSON.stringify(result.verdict)}).`);
  }
}

/** Which form of the question is asked: for the card's label. */
export function formLabel(item) {
  if (item.form === "original") {
    return item.variants_available
      ? "The question as it is on the page"
      : "The question as it is on the page (this page has no variants)";
  }
  if (item.form === "variant") return `A fresh form of a question from the page (variant ${item.variant})`;
  throw new Error(`Unknown form ${JSON.stringify(item.form)}.`);
}

/** Why the item is where it is in the queue, if worth saying. */
export function itemNote(item) {
  if (item.confident_error) return "You said you were certain last time, and it was not right: this one comes first.";
  if (item.reviews === 0) return "First review since you answered it on the page.";
  return null;
}

const BANK_LABELS = {
  built: "Variants ready",
  building: "Building variants",
  failed: "Variants missing: the build failed",
  interrupted: "Variants missing: the build was interrupted",
  not_built: "Variants missing: never built",
  no_questions: "No questions",
};

/**
 * The bank panel: one row per page, `{slug, title, status, label, text, needsAttention,
 * canBuild, cannotBuildReason, buttonLabel}`. A page needs attention when its reviews ask the
 * original questions only (or partly) because variants are missing.
 */
export function bankRows(banks) {
  return banks.pages.map((p) => {
    const label = BANK_LABELS[p.status];
    if (!label) throw new Error(`Unknown review bank status ${JSON.stringify(p.status)}.`);
    const partial = p.status === "built" && p.questions_with_variants < p.questions;
    const needsAttention = ["failed", "interrupted", "not_built"].includes(p.status) || partial;
    return {
      slug: p.artifact.slug,
      title: p.artifact.title,
      status: p.status,
      label: partial ? "Some variants missing" : label,
      text: p.reason,
      needsAttention,
      canBuild: p.can_build && p.status !== "building" && p.status !== "no_questions",
      cannotBuildReason: p.cannot_build_reason,
      buttonLabel: p.status === "not_built" ? "Build variants" : "Rebuild variants",
    };
  });
}
