// Run with `node lab_sandbox_test.mjs`: the pure parts of the lab sandbox (lab_sandbox_frame.js):
// the runtime URL check and the CSP built from it, the frame document, the sources that run
// inside the sandbox, request validation, the result conversion, the timeout result and the
// validation of messages from the frame. The sandbox itself (iframe, worker, Pyodide) needs a
// browser and is not covered here.

import assert from "node:assert/strict";

const STATIC = new URL("../../../src/teacher/web/static/", import.meta.url);
const frame = await import(new URL("js/lab_sandbox_frame.js", STATIC));
const {
  ALLOWED_PACKAGES,
  CHECKS_TIMED_OUT,
  FRAME_SCRIPT,
  FRAME_SOURCE,
  HOST_SOURCE,
  NOT_RUN,
  WORKER_SOURCE,
  buildRunRequest,
  buildSandboxDocument,
  checkPackages,
  checkRuntimeUrl,
  frameCsp,
  parseSandboxMessage,
  timedOutResult,
  timeoutMessage,
  toSandboxResult,
} = frame;

const RUNTIME = "http://127.0.0.1:8000/pyodide/314.0.8/";
const count = (haystack, needle) => haystack.split(needle).length - 1;

// -- the runtime URL and the CSP --------------------------------------------------------------

assert.equal(checkRuntimeUrl(RUNTIME), RUNTIME);
assert.equal(checkRuntimeUrl("https://example.org/a/b/"), "https://example.org/a/b/");
for (const bad of [
  "/pyodide/314.0.8/", // relative: the host resolves it first
  "ftp://127.0.0.1/pyodide/",
  "http://127.0.0.1:8000/pyodide/314.0.8", // no trailing slash
  "http://user:pw@127.0.0.1/pyodide/",
  "http://127.0.0.1/pyodide/?x=1",
  "http://127.0.0.1/pyodide/#x",
  "http://127.0.0.1/py'odide/",
  "http://127.0.0.1/py;odide/",
  "http://127.0.0.1/py,odide/",
  "javascript:alert(1)//",
]) {
  assert.throws(() => checkRuntimeUrl(bad), Error, bad);
}

const csp = frameCsp(RUNTIME);
assert.equal(
  csp,
  "default-src 'none'; " +
    `script-src 'unsafe-inline' 'wasm-unsafe-eval' ${RUNTIME}; ` +
    `connect-src ${RUNTIME}; ` +
    "worker-src blob:; form-action 'none'; base-uri 'none'"
);
assert.ok(!csp.includes("'unsafe-eval'"));
assert.ok(!csp.includes("'self'"));
assert.ok(!/connect-src[^;]*(\*|blob:|data:)/.test(csp));
assert.throws(() => frameCsp("http://127.0.0.1/x;script-src */"));

// -- the frame document ------------------------------------------------------------------------

const doc = buildSandboxDocument(RUNTIME);
assert.ok(doc.startsWith("<!doctype html>"));
{
  const headStart = doc.indexOf("<head>");
  const meta = `<meta http-equiv="Content-Security-Policy" content="${csp}">`;
  assert.equal(count(doc, meta), 1);
  assert.equal(doc.slice(headStart + "<head>".length, doc.indexOf(meta)).trim(), "");
  assert.equal(count(doc, "<script>"), 1);
  assert.equal(count(doc, "</script>"), 1);
  assert.ok(doc.indexOf(meta) < doc.indexOf("<script>"));
  assert.equal(count(doc, FRAME_SCRIPT), 1);
}
// The sources that run in the sandbox: self-contained, parseable, and safe inside a script tag.
for (const [name, source] of [["frame", FRAME_SCRIPT], ["worker", WORKER_SOURCE]]) {
  assert.ok(!/<\/script/i.test(source), `${name} source contains a closing script tag`);
  assert.ok(!source.includes("<!--"), `${name} source contains an HTML comment opener`);
  assert.doesNotThrow(() => new Function(source), `${name} source does not parse`);
}
assert.ok(FRAME_SCRIPT.includes(JSON.stringify(FRAME_SOURCE)));
assert.ok(FRAME_SCRIPT.includes(JSON.stringify(HOST_SOURCE)));
// The worker hardening is in place: empty `js`, pyodide_js unregistered, network globals gone.
for (const part of ["jsglobals: Object.create(null)", 'unregisterJsModule("pyodide_js")', '"fetch", "XMLHttpRequest", "WebSocket"', "checkIntegrity: true"]) {
  assert.ok(WORKER_SOURCE.includes(part), part);
}

// -- requests ----------------------------------------------------------------------------------

assert.deepEqual(checkPackages(["numpy", "numpy", "matplotlib"]), ["numpy", "matplotlib"]);
assert.throws(() => checkPackages(["scipy"]), /scipy are not available; labs can use numpy, matplotlib/);
assert.throws(() => checkPackages("numpy"), TypeError);
assert.deepEqual([...ALLOWED_PACKAGES], ["numpy", "matplotlib"]);

const files = { "main.py": "print(1)", "helper.py": "" };
assert.deepEqual(buildRunRequest({ files, entry: "main.py", packages: ["numpy"] }), {
  files,
  entry: "main.py",
  checks: [],
  packages: ["numpy"],
});
assert.throws(() => buildRunRequest({ files: {}, entry: "main.py", packages: [] }), /at least one file/);
assert.throws(() => buildRunRequest({ files: [{ path: "main.py" }], entry: "main.py", packages: [] }), TypeError);
assert.throws(() => buildRunRequest({ files: { "Main.py": "" }, entry: "Main.py", packages: [] }), /must match/);
assert.throws(() => buildRunRequest({ files: { "../x.py": "" }, entry: "../x.py", packages: [] }), /must match/);
assert.throws(() => buildRunRequest({ files, entry: "other.py", packages: [] }), /not one of the files/);
assert.throws(() => buildRunRequest({ files, entry: "main.py", checks: "assert True", packages: [] }), TypeError);
assert.throws(() => buildRunRequest({ files: { "main.py": 1 }, entry: "main.py", packages: [] }), TypeError);

// -- results -----------------------------------------------------------------------------------

const PNG = "iVBORw0KGgo=";
const driverResult = {
  run: { ok: true, stdout: "hi\n", stderr: "", error: null, figures: [PNG] },
  checks: [{ passed: false, output: "", error: "AssertionError" }],
};
assert.deepEqual(toSandboxResult(driverResult, 1), {
  run: { ok: true, stdout: "hi\n", stderr: "", error: null, timedOut: false, figures: [`data:image/png;base64,${PNG}`] },
  checks: [{ passed: false, output: "", error: "AssertionError" }],
});
for (const bad of [
  null,
  { run: { ...driverResult.run, ok: "yes" }, checks: driverResult.checks },
  { run: { ...driverResult.run, error: "boom" }, checks: driverResult.checks },
  { run: { ...driverResult.run, figures: ["<svg onload=alert(1)>"] }, checks: driverResult.checks },
  { run: { ...driverResult.run, figures: 1 }, checks: driverResult.checks },
  { run: driverResult.run, checks: [] },
  { run: driverResult.run, checks: [{ passed: true, output: "", error: "x" }] },
]) {
  assert.throws(() => toSandboxResult(bad, 1), /malformed result/);
}

assert.equal(timeoutMessage(20, "entry"), "The program did not finish within 20 seconds and was stopped.");
assert.equal(timeoutMessage(0.5, "entry"), "The program did not finish within 0.5 seconds and was stopped.");
assert.equal(timeoutMessage(1.5, "checks"), "The checks did not finish within 1.5 seconds and were stopped.");

const events = [
  { event: "output", stream: "stdout", text: "1\n" },
  { event: "output", stream: "stderr", text: "warn\n" },
  { event: "output", stream: "stdout", text: "2\n" },
];
assert.deepEqual(timedOutResult(events, 2, 3), {
  run: {
    ok: false,
    stdout: "1\n2\n",
    stderr: "warn\n",
    error: "The program did not finish within 3 seconds and was stopped.",
    timedOut: true,
    figures: [],
  },
  checks: [
    { passed: false, output: "", error: NOT_RUN },
    { passed: false, output: "", error: NOT_RUN },
  ],
});
{
  const inChecks = timedOutResult([...events, { event: "phase", phase: "checks" }], 1, 3);
  assert.equal(inChecks.run.error, "The checks did not finish within 3 seconds and were stopped.");
  assert.deepEqual(inChecks.checks, [{ passed: false, output: "", error: CHECKS_TIMED_OUT }]);
}

// -- messages from the frame -------------------------------------------------------------------

const msg = (data) => parseSandboxMessage({ source: FRAME_SOURCE, ...data });
assert.deepEqual(msg({ type: "booted" }), { type: "booted" });
assert.deepEqual(msg({ type: "ready", extra: 1 }), { type: "ready" });
assert.deepEqual(msg({ type: "failed", error: "404" }), { type: "failed", error: "404" });
assert.deepEqual(msg({ type: "event", id: 3, event: { event: "phase", phase: "checks" } }), {
  type: "event",
  id: 3,
  event: { event: "phase", phase: "checks" },
});
assert.deepEqual(msg({ type: "result", id: 1, result: driverResult }), { type: "result", id: 1, result: driverResult });
assert.deepEqual(msg({ type: "timedOut", id: 2 }), { type: "timedOut", id: 2 });
assert.deepEqual(msg({ type: "runError", id: 2, error: "x" }), { type: "runError", id: 2, error: "x" });
for (const bad of [
  null,
  "ready",
  [],
  { type: "ready" }, // no source
  { source: "teacher-interactable", type: "ready" },
  { source: FRAME_SOURCE, type: "failed" },
  { source: FRAME_SOURCE, type: "result", result: driverResult }, // no id
  { source: FRAME_SOURCE, type: "result", id: 0, result: driverResult },
  { source: FRAME_SOURCE, type: "event", id: 1, event: "text" },
  { source: FRAME_SOURCE, type: "runError", id: 1 },
  { source: FRAME_SOURCE, type: "height", height: 10 },
]) {
  assert.equal(parseSandboxMessage(bad), null, JSON.stringify(bad));
}

console.log("lab_sandbox_test: ok");
