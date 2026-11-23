// The lab page's logic that needs no DOM, so that tests/web/js/lab_test.mjs can run it under
// node: converting files between the API's form and the sandbox's, what a run or a check
// reports to POST /api/labs/{slug}/runs (with the output cut to the size the API takes), the
// state of each step, the editor's indent edits, and the autosave's bookkeeping.

/** POST /api/labs/{slug}/runs refuses `output` longer than this (a 422). */
export const MAX_RECORDED_OUTPUT = 20000;
export const INDENT = "    ";
export const INDENT_WIDTH = INDENT.length;

// -- files ----------------------------------------------------------------------------------

/** The API's `[{path, content}]` as the sandbox's `{path: content}`; throws on a malformed
 * entry or a path given twice (the sandbox would silently keep only one). */
export function filesToObject(list) {
  if (!Array.isArray(list)) throw new TypeError("files must be a list of {path, content}");
  const files = {};
  for (const item of list) {
    if (!item || typeof item.path !== "string" || typeof item.content !== "string") {
      throw new TypeError("every file needs a string path and a string content");
    }
    if (Object.prototype.hasOwnProperty.call(files, item.path)) {
      throw new Error(`the file ${item.path} is listed twice`);
    }
    files[item.path] = item.content;
  }
  return files;
}

/** `{path: content}` back as `[{path, content}]`, in the order of `order` (paths missing from
 * `order` follow, sorted). */
export function filesToList(files, order = []) {
  const known = order.filter((path) => Object.prototype.hasOwnProperty.call(files, path));
  const rest = Object.keys(files).filter((path) => !known.includes(path)).sort();
  return [...known, ...rest].map((path) => ({ path, content: files[path] }));
}

/** True when two `[{path, content}]` lists hold the same files (in any order). */
export function sameFiles(a, b) {
  if (a.length !== b.length) return false;
  const other = filesToObject(b);
  return a.every((f) => Object.prototype.hasOwnProperty.call(other, f.path) && other[f.path] === f.content);
}

// -- what a run reports ----------------------------------------------------------------------

const cutNote = (count) => `\n[cut here: ${count} more characters were not recorded]`;

/** `text` cut to at most `limit` characters, the cut said in the text. */
export function truncateText(text, limit) {
  if (text.length <= limit) return text;
  // The note for the whole length has at least as many digits as the final one, so this fits.
  const keep = Math.max(0, limit - cutNote(text.length).length);
  return text.slice(0, keep) + cutNote(text.length - keep);
}

/**
 * Join labelled `sections` ([{label, text}], empty ones left out) into one text of at most
 * `limit` characters. Over the limit, the longest sections are cut first (a runaway print
 * loop in stdout must not push the error output or the check's output out).
 */
export function joinSections(sections, limit = MAX_RECORDED_OUTPUT) {
  const parts = sections
    .filter((s) => s.text)
    .map((s) => ({ head: s.label ? `--- ${s.label} ---\n` : "", text: s.text }));
  const total = () => parts.reduce((n, p, i) => n + p.head.length + p.text.length + (i ? 1 : 0), 0);
  let guard = parts.length * 2 + 2;
  while (total() > limit && guard-- > 0) {
    const longest = parts.reduce((a, b) => (b.text.length > a.text.length ? b : a));
    const target = Math.max(0, longest.text.length - (total() - limit));
    longest.text = truncateText(longest.text, target);
  }
  const text = parts.map((p) => p.head + p.text).join("\n");
  // Headers alone over the limit cannot happen with the few sections used here; be safe anyway.
  return text.length > limit ? truncateText(text, limit) : text;
}

function figuresNote(run) {
  const n = run.figures ? run.figures.length : 0;
  return n ? `[The program drew ${n} figure${n === 1 ? "" : "s"}.]` : "";
}

/**
 * The body of POST /api/labs/{slug}/runs for a sandbox `result`: kind "run" (the entry run;
 * `step` is the step the reader is working on, or null) or "check" (the check of `step`, run
 * as the only check). `files` is the `[{path, content}]` that was run.
 */
export function runReport({ kind, step, files, result }) {
  const run = result.run;
  const sections = [
    { label: "", text: run.stdout },
    { label: "stderr", text: run.stderr },
    { label: "", text: figuresNote(run) },
  ];
  if (kind === "run") {
    return {
      kind,
      step: step ?? null,
      files,
      passed: run.ok,
      output: joinSections(sections),
      error: run.error,
    };
  }
  if (kind !== "check") throw new Error(`unknown run kind ${kind}`);
  if (!Number.isInteger(step)) throw new Error("a check needs its step");
  if (!Array.isArray(result.checks) || result.checks.length !== 1) {
    throw new Error("a check run must have exactly one check result");
  }
  const check = result.checks[0];
  sections.push({ label: "check output", text: check.output });
  let error = null;
  if (!check.passed) error = run.ok || !run.error ? check.error : `${check.error}: ${run.error}`;
  return { kind, step, files, passed: check.passed, output: joinSections(sections), error };
}

// -- steps ----------------------------------------------------------------------------------

/** "passed", "attempted", "not-started", or "unchecked" for a step with no check (its
 * progress is not tracked: the reader judges it). */
export function stepState(step) {
  if (!step.check) return "unchecked";
  if (step.passed) return "passed";
  if (step.attempts > 0) return "attempted";
  return "not-started";
}

export const STEP_STATE_LABELS = {
  passed: "Passed",
  attempted: "Attempted",
  "not-started": "Not started",
  unchecked: "No check",
};

export const STEP_KIND_LABELS = { explore: "Explore", implement: "Implement", extend: "Extend" };

/** `{checked, passed}`: how many steps have a check, and how many of those passed. */
export function stepProgress(steps) {
  const checked = steps.filter((s) => s.check);
  return { checked: checked.length, passed: checked.filter((s) => s.passed).length };
}

// -- editor edits ---------------------------------------------------------------------------
//
// Each returns the edit as `{start, end, text, selStart, selEnd}`: replace value[start, end)
// with `text`, then select [selStart, selEnd) (positions in the new value). The page applies it
// as one undoable insertion.

function lineStartOf(value, pos) {
  return value.lastIndexOf("\n", pos - 1) + 1;
}

/**
 * Tab (or Shift+Tab with `outdent`). Without a selection across lines, Tab inserts spaces up
 * to the next multiple of four columns; across lines (or with `outdent`) every touched line is
 * indented or outdented by four spaces, and the touched lines stay selected.
 */
export function indentEdit(value, selStart, selEnd, { outdent = false } = {}) {
  const multiline = value.slice(selStart, selEnd).includes("\n");
  const first = lineStartOf(value, selStart);
  if (!outdent && !multiline) {
    const width = INDENT_WIDTH - ((selStart - first) % INDENT_WIDTH);
    const text = " ".repeat(width);
    return { start: selStart, end: selEnd, text, selStart: selStart + width, selEnd: selStart + width };
  }
  // The touched lines: a selection ending at the very start of a line does not touch it.
  let last = selEnd;
  if (multiline && selEnd > selStart && value[selEnd - 1] === "\n") last = selEnd - 1;
  let end = value.indexOf("\n", last);
  if (end === -1) end = value.length;
  const lines = value.slice(first, end).split("\n");
  const changed = lines.map((line) => {
    if (!outdent) return line.trim() ? INDENT + line : line;
    const spaces = line.match(/^ */)[0].length;
    return line.slice(Math.min(spaces, INDENT_WIDTH));
  });
  const text = changed.join("\n");
  if (!multiline) {
    // Outdent with a caret (or a selection) inside one line: keep it where it was in the text.
    const removed = lines[0].length - changed[0].length;
    const caret = (pos) => Math.max(first, pos - removed);
    return { start: first, end, text, selStart: caret(selStart), selEnd: caret(selEnd) };
  }
  return { start: first, end, text, selStart: first, selEnd: first + text.length };
}

/** Enter: a new line indented like the current one, one level deeper after a line ending in
 * ":" (a Python block opener). */
export function newlineEdit(value, selStart, selEnd) {
  const first = lineStartOf(value, selStart);
  const before = value.slice(first, selStart);
  let indent = before.match(/^ */)[0];
  if (before.trimEnd().endsWith(":")) indent += INDENT;
  const text = `\n${indent}`;
  return { start: selStart, end: selEnd, text, selStart: selStart + text.length, selEnd: selStart + text.length };
}

/** 1-based line and column of `pos` in `value`. */
export function lineAndColumn(value, pos) {
  const before = value.slice(0, pos);
  const line = before.split("\n").length;
  return { line, column: pos - lineStartOf(value, pos) + 1 };
}

// -- autosave -------------------------------------------------------------------------------

/**
 * The workspace's autosave. `changed()` after every edit saves `delay` ms after the last one;
 * `flush()` saves now if anything is unsaved (waiting for a save in progress first) and
 * rejects if that save fails. `save()` is called with no arguments and must read the current
 * files itself, so that edits made during a save are saved by the next one. `onState(state,
 * error)` reports "saved", "dirty" (unsaved changes), "saving" and "failed" (with the error).
 * A failed save leaves the changes unsaved: the next edit or flush tries again.
 */
export function createSaver({ save, delay = 1000, onState = () => {}, timers = globalThis }) {
  let dirty = false;
  let inflight = null;
  let timer = 0;

  function clear() {
    if (timer) timers.clearTimeout(timer);
    timer = 0;
  }

  function schedule() {
    clear();
    timer = timers.setTimeout(() => {
      timer = 0;
      flush().catch(() => {}); // shown through onState("failed")
    }, delay);
  }

  function changed() {
    dirty = true;
    if (!inflight) onState("dirty", null);
    schedule();
  }

  async function flush() {
    clear();
    while (inflight) {
      try {
        await inflight;
      } catch {
        /* that save's failure was reported; this flush tries again below */
      }
    }
    if (!dirty) return;
    dirty = false;
    onState("saving", null);
    inflight = Promise.resolve().then(save);
    try {
      await inflight;
    } catch (err) {
      dirty = true;
      onState("failed", err);
      throw err;
    } finally {
      inflight = null;
    }
    if (dirty) {
      onState("dirty", null);
      schedule();
    } else {
      onState("saved", null);
    }
  }

  return {
    changed,
    flush,
    isDirty: () => dirty || Boolean(inflight),
  };
}
