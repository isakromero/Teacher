// Pure helpers for the home page's search (no DOM, so node can test them): when a query is
// ready to send, the words for a result (what it is, where it matched, the reader's standing
// with it), moving through the results with the keyboard, and the page's address.
// Data: GET /api/search (teacher/web/routers/library_search.py; the rules in teacher/search.py).

import { formatDuration } from "./history_model.js";

export const MIN_QUERY_CHARS = 2; // teacher.search.MIN_QUERY_CHARS
export const MAX_QUERY_CHARS = 200; // teacher.search.MAX_QUERY_CHARS
export const RESULT_LIMIT = 20;
export const DEBOUNCE_MS = 180;

/** Whether `text` can be searched for: `{ready, reason}`; the reason says what to type. The
 * page only sends ready queries, so the API's 422 is left for the unexpected. */
export function queryState(text) {
  const value = String(text ?? "");
  if (!value.trim()) return { ready: false, reason: null };
  if (value.length > MAX_QUERY_CHARS) return { ready: false, reason: `The search is too long: at most ${MAX_QUERY_CHARS} characters.` };
  const chars = (value.match(/[\p{L}\p{N}]/gu) || []).length;
  if (!chars) return { ready: false, reason: "Search for words: type letters or digits." };
  if (chars < MIN_QUERY_CHARS) return { ready: false, reason: `Type at least ${MIN_QUERY_CHARS} letters or digits to search.` };
  return { ready: true, reason: null };
}

export function searchUrl(text, limit = RESULT_LIMIT) {
  return `/api/search?q=${encodeURIComponent(text)}&limit=${limit}`;
}

/** The home page's address for a query (deep link), or "/" without one. */
export function homeAddress(text) {
  const value = String(text ?? "").trim();
  return value ? `/?q=${encodeURIComponent(value)}` : "/";
}

const KIND_LABELS = { study_page: "Study page", lab: "Lab", source: "Source" };

/** The words for a result kind; an unknown kind is an error, not a guess. */
export function kindLabel(kind) {
  if (!KIND_LABELS[kind]) throw new Error(`unknown search result kind ${kind}`);
  return KIND_LABELS[kind];
}

/** Where a result matched, in words: "In section “Attention”", "In the glossary: “KL”". */
export function matchLabel(match) {
  if (match.locked) return "In a part of the page you have not unlocked yet";
  switch (match.field) {
    case "title":
      return "In the title";
    case "authors":
      return "In the authors";
    case "summary":
      return "In the summary";
    case "concept":
      return `Linked concept “${match.concept}”`;
    case "glossary":
      return `In the glossary: “${match.term}”`;
    case "heading":
      return `In section “${match.section}”`;
    case "body":
      if (match.term) return `In the glossary: “${match.term}”`;
      return match.section ? `In section “${match.section}”` : "In the text";
    default:
      throw new Error(`unknown search match field ${match.field}`);
  }
}

/** "Ashish Vaswani, Noam Shazeer and 6 others · 2017" (or null when there is neither). */
export function metaText(result) {
  const names = result.authors || [];
  const shown = names.length > 3 ? `${names.slice(0, 2).join(", ")} and ${names.length - 2} others` : names.join(", ");
  const parts = [shown, result.year].filter(Boolean);
  return parts.length ? parts.join(" · ") : null;
}

const STATUS_WORDS = { failed: "Generation failed", generating: "Still generating" };

/** The reader's standing with a result, in words ("In progress · 12 min · 3 of 7 questions
 * cleared"); an unknown state is an error. */
export function studyText(result) {
  const s = result.study;
  const time = s.active_seconds >= 1 ? formatDuration(s.active_seconds) : null;
  const gates = s.gates_total ? `${s.gates_cleared} of ${s.gates_total} ${s.gates_total === 1 ? "question" : "questions"} cleared` : null;
  switch (s.state) {
    case "not_ready":
      return STATUS_WORDS[result.status] || `Not ready (${result.status})`;
    case "not_opened":
      return result.kind === "source" && s.study_pages
        ? `Not opened yet · ${s.study_pages} ${s.study_pages === 1 ? "study page" : "study pages"}`
        : "Not opened yet";
    case "in_progress":
      return ["In progress", time, gates].filter(Boolean).join(" · ");
    case "complete":
      return ["Finished", time].filter(Boolean).join(" · ");
    case "opened":
      return ["Opened", time, s.study_pages ? `${s.study_pages} ${s.study_pages === 1 ? "study page" : "study pages"}` : "no study page yet"]
        .filter(Boolean)
        .join(" · ");
    default:
      throw new Error(`unknown study state ${s.state}`);
  }
}

/** Check a list of text segments ({text, mark}) before it is drawn; a malformed one is an
 * error, so nothing unexpected reaches the page. Returns the list. */
export function checkSegments(segments) {
  if (!Array.isArray(segments)) throw new Error("search result text is not a list of segments");
  for (const s of segments) {
    if (!s || typeof s.text !== "string" || typeof s.mark !== "boolean") {
      throw new Error("search result text has a malformed segment");
    }
  }
  return segments;
}

/** "3 results for “attention”", "Showing 20 of 34 results for “x”", "No results for “x”." */
export function resultsStatus(data) {
  const q = `“${data.query}”`;
  if (!data.total) return `No results for ${q}.`;
  const n = `${data.total} ${data.total === 1 ? "result" : "results"}`;
  return data.results.length < data.total ? `Showing the best ${data.results.length} of ${n} for ${q}.` : `${n} for ${q}.`;
}

/**
 * The result to move to from `current` (-1: the search box) by `delta` (+1 down, -1 up) among
 * `count` results: up from the first goes back to the box (-1); down from the last stays.
 */
export function moveIndex(current, delta, count) {
  if (!count) return -1;
  const next = current + delta;
  if (next < -1) return -1;
  if (next >= count) return count - 1;
  return next;
}
