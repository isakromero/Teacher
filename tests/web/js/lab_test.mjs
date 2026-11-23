// Run by tests/web/test_pages.py (`node lab_test.mjs`): the lab page's DOM-free logic
// (static/js/lab_model.js): files between the API's and the sandbox's form, what a run or a
// check reports (output cut to the API's limit), step states, the editor's indent edits and the
// autosave's bookkeeping.

import assert from "node:assert/strict";

const STATIC = new URL("../../../src/teacher/web/static/", import.meta.url);
const lab = await import(new URL("js/lab_model.js", STATIC));
const {
  MAX_RECORDED_OUTPUT,
  createSaver,
  filesToList,
  filesToObject,
  indentEdit,
  joinSections,
  lineAndColumn,
  newlineEdit,
  runReport,
  sameFiles,
  stepProgress,
  stepState,
  truncateText,
} = lab;

// -- files ----------------------------------------------------------------------------------

const list = [
  { path: "main.py", content: "import helper\n" },
  { path: "helper.py", content: "X = 1\n" },
];
assert.deepEqual(filesToObject(list), { "main.py": "import helper\n", "helper.py": "X = 1\n" });
assert.deepEqual(filesToList(filesToObject(list), ["main.py", "helper.py"]), list);
// Paths not in the order follow, sorted; paths in the order that are gone are skipped.
assert.deepEqual(
  filesToList({ "b.py": "", "a.py": "", "main.py": "" }, ["main.py", "gone.py"]).map((f) => f.path),
  ["main.py", "a.py", "b.py"]
);
assert.throws(() => filesToObject([...list, { path: "main.py", content: "" }]), /listed twice/);
assert.throws(() => filesToObject([{ path: "main.py" }]), TypeError);
assert.throws(() => filesToObject({ "main.py": "" }), TypeError);
assert.ok(sameFiles(list, [...list].reverse()));
assert.ok(!sameFiles(list, [list[0], { path: "helper.py", content: "X = 2\n" }]));
assert.ok(!sameFiles(list, [list[0]]));

// -- output cut to the limit ----------------------------------------------------------------

assert.equal(truncateText("short", 100), "short");
for (const [length, limit] of [[25000, 20000], [10 ** 6, 20000], [101, 100], [5000, 60]]) {
  const cut = truncateText("x".repeat(length), limit);
  assert.ok(cut.length <= limit, `${length} -> ${cut.length} > ${limit}`);
  const note = cut.match(/\[cut here: (\d+) more characters were not recorded\]$/);
  assert.ok(note, cut.slice(-80));
  const kept = cut.length - note[0].length - 1; // the note starts on a new line
  assert.equal(kept + Number(note[1]), length, "the note counts exactly what was cut");
}

assert.equal(joinSections([{ label: "", text: "out" }, { label: "stderr", text: "" }]), "out");
assert.equal(
  joinSections([{ label: "", text: "out" }, { label: "stderr", text: "warn" }]),
  "out\n--- stderr ---\nwarn"
);
{
  // A runaway stdout is cut; the stderr and the check's output survive whole.
  const joined = joinSections([
    { label: "", text: "y".repeat(50000) },
    { label: "stderr", text: "Warning: something" },
    { label: "check output", text: "checked" },
  ]);
  assert.ok(joined.length <= MAX_RECORDED_OUTPUT, String(joined.length));
  assert.ok(joined.includes("more characters were not recorded"));
  assert.ok(joined.endsWith("--- stderr ---\nWarning: something\n--- check output ---\nchecked"));
}

// -- what a run and a check report ----------------------------------------------------------

const okRun = { ok: true, stdout: "hello\n", stderr: "", error: null, timedOut: false, figures: [] };
const failedRun = {
  ok: false,
  stdout: "",
  stderr: "",
  error: 'Traceback (most recent call last):\n  File "main.py", line 1\nNameError: name x',
  timedOut: false,
  figures: [],
};

assert.deepEqual(runReport({ kind: "run", step: 1, files: list, result: { run: okRun, checks: [] } }), {
  kind: "run",
  step: 1,
  files: list,
  passed: true,
  output: "hello\n",
  error: null,
});
{
  const report = runReport({
    kind: "run",
    step: null,
    files: list,
    result: { run: { ...okRun, figures: ["AAAA", "BBBB"] }, checks: [] },
  });
  assert.equal(report.step, null);
  assert.equal(report.output, "hello\n\n[The program drew 2 figures.]");
}
{
  const report = runReport({ kind: "run", step: 0, files: list, result: { run: failedRun, checks: [] } });
  assert.equal(report.passed, false);
  assert.equal(report.error, failedRun.error);
  assert.equal(report.output, "");
}
{
  const big = { ...okRun, stdout: "z".repeat(MAX_RECORDED_OUTPUT * 3) };
  const report = runReport({ kind: "run", step: 0, files: list, result: { run: big, checks: [] } });
  assert.ok(report.output.length <= MAX_RECORDED_OUTPUT);
}
assert.deepEqual(
  runReport({
    kind: "check",
    step: 2,
    files: list,
    result: { run: okRun, checks: [{ passed: true, output: "fine\n", error: null }] },
  }),
  { kind: "check", step: 2, files: list, passed: true, output: "hello\n\n--- check output ---\nfine\n", error: null }
);
assert.equal(
  runReport({
    kind: "check",
    step: 2,
    files: list,
    result: { run: okRun, checks: [{ passed: false, output: "", error: "AssertionError: rows must sum to 1" }] },
  }).error,
  "AssertionError: rows must sum to 1"
);
// The program failed, so the check did not run: both reasons are recorded.
assert.equal(
  runReport({
    kind: "check",
    step: 2,
    files: list,
    result: { run: failedRun, checks: [{ passed: false, output: "", error: "the program did not run" }] },
  }).error,
  `the program did not run: ${failedRun.error}`
);
assert.throws(() => runReport({ kind: "check", step: null, files: list, result: { run: okRun, checks: [] } }), /step/);
assert.throws(() => runReport({ kind: "check", step: 1, files: list, result: { run: okRun, checks: [] } }), /one check/);
assert.throws(() => runReport({ kind: "other", step: 1, files: list, result: { run: okRun, checks: [] } }), /kind/);

// -- steps ----------------------------------------------------------------------------------

const steps = [
  { index: 0, check: null, passed: false, attempts: 0 },
  { index: 1, check: "assert True", passed: false, attempts: 0 },
  { index: 2, check: "assert True", passed: false, attempts: 3 },
  { index: 3, check: "assert True", passed: true, attempts: 2 },
];
assert.deepEqual(steps.map(stepState), ["unchecked", "not-started", "attempted", "passed"]);
assert.deepEqual(stepProgress(steps), { checked: 3, passed: 1 });
assert.deepEqual(stepProgress([steps[0]]), { checked: 0, passed: 0 });

// -- editor edits ---------------------------------------------------------------------------

/** Apply an edit as the page does and return [value, selStart, selEnd]. */
function apply(value, edit) {
  return [value.slice(0, edit.start) + edit.text + value.slice(edit.end), edit.selStart, edit.selEnd];
}
/** `|` marks the caret, `[` and `]` a selection. */
function parse(marked) {
  const caret = marked.indexOf("|");
  if (caret !== -1) return [marked.replace("|", ""), caret, caret];
  const start = marked.indexOf("[");
  const end = marked.indexOf("]") - 1;
  return [marked.replace("[", "").replace("]", ""), start, end];
}
function show([value, start, end]) {
  if (start === end) return value.slice(0, start) + "|" + value.slice(start);
  return value.slice(0, start) + "[" + value.slice(start, end) + "]" + value.slice(end);
}
const tab = (marked, options) => {
  const [value, start, end] = parse(marked);
  return show(apply(value, indentEdit(value, start, end, options)));
};
const enter = (marked) => {
  const [value, start, end] = parse(marked);
  return show(apply(value, newlineEdit(value, start, end)));
};

// Tab with a caret: spaces to the next multiple of four columns.
assert.equal(tab("|x = 1"), "    |x = 1");
assert.equal(tab("ab|c"), "ab  |c");
assert.equal(tab("def f():\n    |pass"), "def f():\n        |pass");
// Tab over a selection inside one line replaces it.
assert.equal(tab("a[bc]d"), "a   |d");
// Tab over several lines indents each (blank lines stay blank) and keeps them selected.
assert.equal(tab("[a = 1\n\nb = 2]"), "[    a = 1\n\n    b = 2]");
assert.equal(tab("x\na[ = 1\nb] = 2\ny"), "x\n[    a = 1\n    b = 2]\ny");
// A selection ending at the start of a line does not touch that line.
assert.equal(tab("[a\nb\n]c"), "[    a\n    b]\nc");
// Shift+Tab outdents by up to four spaces, the caret staying where it was in the text.
assert.equal(tab("        x|y", { outdent: true }), "    x|y");
assert.equal(tab("  |xy", { outdent: true }), "|xy");
assert.equal(tab("xy|", { outdent: true }), "xy|");
assert.equal(tab("[    a\n  b\nc]", { outdent: true }), "[a\nb\nc]");

// Enter keeps the indentation, one level deeper after a ":".
assert.equal(enter("x = 1|"), "x = 1\n|");
assert.equal(enter("    x = 1|"), "    x = 1\n    |");
assert.equal(enter("def f():|"), "def f():\n    |");
assert.equal(enter("    for i in range(3):  |"), "    for i in range(3):  \n        |");
assert.equal(enter("    a[bc]"), "    a\n    |");

assert.deepEqual(lineAndColumn("ab\ncd", 0), { line: 1, column: 1 });
assert.deepEqual(lineAndColumn("ab\ncd", 4), { line: 2, column: 2 });

// -- autosave -------------------------------------------------------------------------------

function fakeTimers() {
  let next = 1;
  const pending = new Map();
  return {
    setTimeout(fn, ms) {
      const id = next++;
      pending.set(id, { fn, ms });
      return id;
    },
    clearTimeout(id) {
      pending.delete(id);
    },
    count: () => pending.size,
    fire() {
      const entries = [...pending];
      pending.clear();
      for (const [, { fn }] of entries) fn();
    },
  };
}
const tickPromises = () => new Promise((resolve) => setImmediate(resolve));

{
  const timers = fakeTimers();
  const states = [];
  const saved = [];
  let content = "a";
  const saver = createSaver({
    save: async () => saved.push(content),
    delay: 1000,
    onState: (state) => states.push(state),
    timers,
  });
  assert.equal(saver.isDirty(), false);
  saver.changed();
  content = "ab";
  saver.changed();
  assert.equal(timers.count(), 1, "debounced: one pending save");
  assert.equal(saver.isDirty(), true);
  timers.fire();
  await tickPromises();
  assert.deepEqual(saved, ["ab"]);
  assert.deepEqual(states, ["dirty", "dirty", "saving", "saved"]);
  assert.equal(saver.isDirty(), false);
  // Nothing unsaved: flush does not save.
  await saver.flush();
  assert.deepEqual(saved, ["ab"]);
  // flush saves at once and cancels the pending timer.
  content = "abc";
  saver.changed();
  await saver.flush();
  assert.deepEqual(saved, ["ab", "abc"]);
  assert.equal(timers.count(), 0);
}

{
  // A failed save is reported, keeps the changes unsaved and is retried by the next flush.
  const timers = fakeTimers();
  const states = [];
  let fail = true;
  let calls = 0;
  const saver = createSaver({
    save: async () => {
      calls += 1;
      if (fail) throw new Error("server down");
    },
    onState: (state, error) => states.push(error ? `${state}: ${error.message}` : state),
    timers,
  });
  saver.changed();
  await assert.rejects(saver.flush(), /server down/);
  assert.equal(saver.isDirty(), true);
  assert.deepEqual(states, ["dirty", "saving", "failed: server down"]);
  fail = false;
  await saver.flush();
  assert.equal(calls, 2);
  assert.equal(states.at(-1), "saved");
  // A failure in a timed save is reported, not thrown.
  fail = true;
  saver.changed();
  timers.fire();
  await tickPromises();
  assert.equal(states.at(-1), "failed: server down");
}

{
  // An edit during a save is saved by the next one; a flush during a save waits for it.
  const timers = fakeTimers();
  let release;
  const saved = [];
  let content = "one";
  const saver = createSaver({
    save: () =>
      new Promise((resolve) => {
        saved.push(content);
        release = resolve;
      }),
    timers,
  });
  saver.changed();
  const first = saver.flush();
  await tickPromises();
  content = "two";
  saver.changed(); // during the save
  const second = saver.flush();
  release();
  await first;
  await tickPromises();
  release();
  await second;
  assert.deepEqual(saved, ["one", "two"]);
  assert.equal(saver.isDirty(), false);
}

console.log("lab ok");
