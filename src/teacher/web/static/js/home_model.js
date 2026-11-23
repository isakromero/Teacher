// Pure helpers for the home page's sections (no DOM, so node can test them): when something was
// studied, a page's progress, the week's numbers and the recent sessions, in words.
// Data: GET /api/history/continue, /api/history/summary and /api/history/suggestions
// (teacher/web/routers/history.py).

import { formatDuration } from "./history_model.js";

const DAY_MS = 24 * 60 * 60 * 1000;

function startOfDay(date) {
  const d = new Date(date);
  d.setHours(0, 0, 0, 0);
  return d;
}

/** "Today", "Yesterday", a weekday within the last week, else a date ("12 Nov", with the
 * year when it is not this year). An invalid date is an error. */
export function relativeDay(iso, now = new Date()) {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) throw new Error(`not a date: ${iso}`);
  const days = Math.round((startOfDay(now) - startOfDay(date)) / DAY_MS);
  if (days === 0) return "Today";
  if (days === 1) return "Yesterday";
  if (days > 1 && days < 7) return date.toLocaleDateString(undefined, { weekday: "long" });
  const sameYear = date.getFullYear() === now.getFullYear();
  return date.toLocaleDateString(undefined, sameYear ? { day: "numeric", month: "short" } : { day: "numeric", month: "short", year: "numeric" });
}

/** "Today at 14:05", "Monday at 09:10", "12 Nov at 18:30". */
export function whenText(iso, now = new Date()) {
  const time = new Date(iso).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  return `${relativeDay(iso, now)} at ${time}`;
}

const isLab = (artifact) => artifact.kind === "lab";

/** "5 of 7 questions cleared" (a study page) / "2 of 3 checked steps passed" (a lab), or null
 * when it is not ready or has nothing to clear. */
export function progressText(artifact) {
  const p = artifact.progress;
  if (artifact.status !== "ready" || !p || !p.gates_total) return null;
  const n = p.gates_total;
  return isLab(artifact)
    ? `${p.gates_cleared} of ${n} checked ${n === 1 ? "step" : "steps"} passed`
    : `${p.gates_cleared} of ${n} ${n === 1 ? "question" : "questions"} cleared`;
}

/** The share of gates cleared, 0..1, or null (see progressText). */
export function progressShare(artifact) {
  const p = artifact.progress;
  if (artifact.status !== "ready" || !p || !p.gates_total) return null;
  return Math.min(1, Math.max(0, p.gates_cleared / p.gates_total));
}

/** What the reader can still do with a page: "Finished", "Not ready: generation failed" and so on. */
export function artifactState(artifact) {
  if (artifact.status === "failed") return "Generation failed";
  if (artifact.status === "generating") return "Still generating";
  if (artifact.status !== "ready") throw new Error(`unknown artifact status ${artifact.status}`);
  return artifact.progress && artifact.progress.complete && artifact.progress.gates_total ? "Finished" : null;
}

/** One "Continue studying" item (GET /api/history/continue `items[]`) as lines of words. */
export function continueView(item, now = new Date()) {
  const a = item.artifact;
  return {
    href: item.url,
    title: a.title,
    kind: isLab(a) ? "Lab" : "Study page",
    older: Boolean(a.superseded_by),
    when: `Studied ${whenText(item.last_studied_at, now).replace(/^(Today|Yesterday)/, (m) => m.toLowerCase())}`,
    time: item.active_seconds >= 1 ? `${formatDuration(item.active_seconds)} on it` : null,
    progress: progressText(a),
    share: progressShare(a),
    state: artifactState(a),
  };
}

/** A rate 0..1 (or null) as "80%". */
export const percent = (rate) => (rate === null || rate === undefined ? null : `${Math.round(rate * 100)}%`);

/** The summary's numbers (GET /api/history/summary) as tiles: [{label, value, note}]. */
export function summaryTiles(summary) {
  const { today, week } = summary;
  const rate = percent(week.first_attempt_pass_rate);
  return [
    { label: "This week", value: formatDuration(week.active_seconds), note: `${today.active_seconds >= 1 ? formatDuration(today.active_seconds) : "Nothing"} today` },
    { label: "Sessions", value: String(week.sessions), note: `this week, ${today.sessions} today` },
    {
      label: "Questions",
      value: String(week.questions),
      note: week.questions ? "answered this week" : "none answered this week",
    },
    {
      label: "First time",
      value: rate ?? "-",
      note: week.first_attempts ? `${week.first_attempts_passed} of ${week.first_attempts} passed at once` : "no first attempts this week",
    },
  ];
}

/** One recent session (a SessionOut) as {href, when, length, what}. */
export function sessionView(s, now = new Date()) {
  const titles = [...s.artifacts.map((a) => a.title), ...s.sources.map((src) => src.title)];
  return {
    href: `/history#session-${encodeURIComponent(String(s.id))}`,
    when: whenText(s.started_at, now),
    length: s.backfilled ? "From earlier activity" : s.active_seconds >= 1 ? formatDuration(s.active_seconds) : "Under a minute on pages",
    what: titles.length ? titles.join(", ") : "No pages studied",
  };
}

/** A suggestion's one action: the first page or source to open ({href, text, note}), or null. */
export function suggestionLink(s, actionLinks) {
  const links = actionLinks(s.action);
  return links.length ? links[0] : null;
}
