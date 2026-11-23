// Pure helpers for the study history (no DOM, so node can test them): durations for people,
// the block in view on a study page, and the summaries the history page draws from a session's
// timeline (GET /api/history/sessions/{id}; see teacher/web/routers/history.py).

/** Seconds for people: "45 s", "12 min", "1 h 5 min". */
export function formatDuration(seconds) {
  const s = Math.max(0, Math.round(Number(seconds) || 0));
  if (s < 60) return `${s} s`;
  const minutes = Math.round(s / 60);
  if (minutes < 60) return `${minutes} min`;
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  return m ? `${h} h ${m} min` : `${h} h`;
}

export const READ_LINE = 0.35; // the block crossing this fraction of the window's height is read

/**
 * The key of the block being read: the one crossing the reading line (READ_LINE of the
 * window's height), else the visible block nearest to it, else null. `blocks` are
 * `{key, top, bottom}` in window coordinates (getBoundingClientRect), in page order.
 */
export function blockInView(blocks, viewportHeight) {
  const line = viewportHeight * READ_LINE;
  let best = null;
  let bestDistance = Infinity;
  for (const b of blocks) {
    if (b.top <= line && b.bottom >= line) return b.key;
    if (b.bottom < 0 || b.top > viewportHeight) continue; // not on screen
    const distance = b.top > line ? b.top - line : line - b.bottom;
    if (distance < bestDistance) {
      best = b.key;
      bestDistance = distance;
    }
  }
  return best;
}

/** A name for what an attention span or entry was on. */
export function pageName(entry) {
  if (entry.surface === "graph") return "Concept graph";
  if (entry.surface === "source") return entry.source ? entry.source.title : "A deleted source";
  if (entry.surface === "lab") return entry.lab ? entry.lab.title : "A deleted lab";
  return entry.artifact ? entry.artifact.title : "A deleted study page";
}

/**
 * A block for people: its key and its label (the heading or opening words the API gives as
 * `block_label`), `b03 “Why softmax?”`; the key alone without a label, `gone` without a key (the
 * block was deleted).
 */
export function blockName(key, label, gone = "a deleted block") {
  if (!key) return gone;
  return label ? `${key} “${label}”` : key;
}

/**
 * What the reader answered (a timeline answer's `answer`, see teacher/web/routers/history.py) as
 * `{lead, text, form}`: the words before it and the answer itself. A raw answer (a shape the
 * history does not know) is shown as stored and says so; an unknown form is an error.
 */
export function answerView(answer) {
  switch (answer.form) {
    case "choice":
      return { form: "choice", lead: `Your answer, option ${answer.choice + 1}:`, text: answer.text };
    case "text":
      return { form: "text", lead: "Your answer:", text: answer.text };
    case "raw":
      return { form: "raw", lead: `Your answer as stored, in a form this page cannot show in words (${answer.note}):`, text: answer.text };
    default:
      throw new Error(`The history has an answer this page does not know: ${answer.form}`);
  }
}

/**
 * Time per page in a timeline: `[{name, surface, seconds, blocks: [{block, label, seconds}]}]`,
 * most time first; blocks in the order they were first read.
 */
export function timeOnPages(timeline) {
  const pages = new Map();
  for (const entry of timeline) {
    if (entry.type !== "span") continue;
    const key = `${entry.surface}|${pageName(entry)}`;
    if (!pages.has(key)) {
      pages.set(key, { name: pageName(entry), surface: entry.surface, seconds: 0, blocks: new Map() });
    }
    const page = pages.get(key);
    page.seconds += entry.active_seconds;
    if (entry.block) {
      const block = page.blocks.get(entry.block) || { block: entry.block, label: entry.block_label, seconds: 0 };
      block.seconds += entry.active_seconds;
      page.blocks.set(entry.block, block);
    }
  }
  return [...pages.values()]
    .map((p) => ({ ...p, blocks: [...p.blocks.values()] }))
    .sort((a, b) => b.seconds - a.seconds);
}

/**
 * The timeline as the page lists it: spans are left out (they are summed by timeOnPages), and
 * a run of the same event (kind and subject) in a row, such as autosaves of a lab's code,
 * becomes one item with `count`. Returns `[{entry, count, last}]`.
 */
export function listItems(timeline) {
  const items = [];
  for (const entry of timeline) {
    if (entry.type === "span") continue;
    const prev = items[items.length - 1];
    if (prev && entry.type === "event" && prev.entry.type === "event" && sameEvent(prev.entry, entry)) {
      prev.count += 1;
      prev.last = entry;
      continue;
    }
    items.push({ entry, count: 1, last: entry });
  }
  return items;
}

const ref = (r) => (r ? r.slug : null);

function sameEvent(a, b) {
  return (
    a.kind === b.kind &&
    ref(a.artifact) === ref(b.artifact) &&
    ref(a.lab) === ref(b.lab) &&
    ref(a.source) === ref(b.source) &&
    ref(a.concept) === ref(b.concept) &&
    a.block === b.block &&
    (a.kind === "lab_workspace_saved" || JSON.stringify(a.payload) === JSON.stringify(b.payload))
  );
}

/** "3 of 5 passed" style totals line for a session record. */
export function sessionTotals(s) {
  const parts = [];
  const answers = s.answers + s.chat_answers;
  if (answers) parts.push(`${answers} ${answers === 1 ? "answer" : "answers"} (${s.answers_passed + s.chat_answers_passed} passed)`);
  if (s.user_messages) parts.push(`${s.user_messages} ${s.user_messages === 1 ? "message" : "messages"} to the tutor`);
  const runs = s.lab_runs + s.lab_checks;
  if (runs) parts.push(`${runs} lab ${runs === 1 ? "run" : "runs"} (${s.lab_checks_passed} of ${s.lab_checks} checks passed)`);
  return parts;
}

/**
 * Start (inclusive) and end (exclusive) of the local calendar days `from` and `to`
 * ("YYYY-MM-DD", either may be empty) as ISO instants for the API's `since`/`until`.
 */
export function dayRange(from, to) {
  const start = from ? new Date(`${from}T00:00:00`) : null;
  let end = null;
  if (to) {
    end = new Date(`${to}T00:00:00`);
    end.setDate(end.getDate() + 1);
  }
  return { since: start ? start.toISOString() : null, until: end ? end.toISOString() : null };
}
