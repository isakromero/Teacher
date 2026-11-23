// The pieces of the lab sandbox that need no DOM: the iframe's document and CSP, the source of
// the Web Worker that runs Pyodide, and the checks of everything that crosses a boundary.
// lab_sandbox.js puts them together; tests/web/js/lab_sandbox_test.mjs tests them under node.
//
// Layout: the host page (lab_sandbox.js) <-> postMessage <-> an <iframe sandbox="allow-scripts"
// srcdoc=...> (opaque origin: no cookies, no storage, no same-origin access to the app) <->
// postMessage <-> a Web Worker created from a blob: URL, which loads Pyodide from the runtime
// URL and runs the shared driver (lab_driver.py, fetched by the host and passed down). The
// frame's CSP allows scripts and connections only to the runtime URL (plus wasm compilation
// and blob: workers), and a blob: worker inherits that policy, so Python cannot reach the
// app's API or anything else on the network. The worker also gives Python an empty `js`
// module and removes fetch, XMLHttpRequest and friends before the program runs (the same
// hardening as the Node runner, teacher/lab_runtime/node_runner.mjs).
//
// Each run gets a fresh worker (no state leaks between runs) and the frame keeps one warm
// worker ready (Pyodide, the packages and the driver loaded) so a run does not wait for a cold
// start; the time limit starts when the run is handed to a ready worker. A run past its limit
// is ended by terminating the worker.

export const FRAME_SOURCE = "teacher-lab-sandbox";
export const HOST_SOURCE = "teacher-lab-host";
export const ALLOWED_PACKAGES = Object.freeze(["numpy", "matplotlib"]);
export const FILE_NAME = /^[a-z_][a-z0-9_]*\.py$/;
export const WASM_MEMORY_LIMIT_MB = 1024;
// Mirrors teacher.pyodide_runtime (NOT_RUN, CHECKS_TIMED_OUT, timeout_message).
export const NOT_RUN = "the program did not run";
export const CHECKS_TIMED_OUT = "the checks did not finish in time";

/** The runtime URL as an absolute http(s) URL ending in "/" that is safe to put in a CSP. */
export function checkRuntimeUrl(url) {
  let parsed;
  try {
    parsed = new URL(url);
  } catch {
    throw new Error(`the runtime URL ${JSON.stringify(url)} is not an absolute URL`);
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    throw new Error(`the runtime URL ${parsed.href} must be http or https`);
  }
  if (parsed.username || parsed.password || parsed.search || parsed.hash) {
    throw new Error(`the runtime URL ${parsed.href} must not have credentials, a query or a fragment`);
  }
  if (!parsed.pathname.endsWith("/")) {
    throw new Error(`the runtime URL ${parsed.href} must end with "/"`);
  }
  if (/[\s;,'"<>\\]/.test(parsed.href)) {
    throw new Error(`the runtime URL ${parsed.href} contains characters a CSP cannot take`);
  }
  return parsed.href;
}

/** The frame's Content-Security-Policy. 'unsafe-inline' is the frame's own script (srcdoc);
 * the runtime URL is pyodide.mjs, pyodide.asm.mjs and the files Pyodide fetches; blob: is the
 * worker. No 'unsafe-eval', no images, no other origin, and no path outside the runtime. */
export function frameCsp(runtimeUrl) {
  const runtime = checkRuntimeUrl(runtimeUrl);
  return [
    "default-src 'none'",
    `script-src 'unsafe-inline' 'wasm-unsafe-eval' ${runtime}`,
    `connect-src ${runtime}`,
    "worker-src blob:",
    "form-action 'none'",
    "base-uri 'none'",
  ].join("; ");
}

export function checkPackages(packages) {
  if (!Array.isArray(packages) || !packages.every((p) => typeof p === "string")) {
    throw new TypeError("packages must be an array of package names");
  }
  const unknown = packages.filter((p) => !ALLOWED_PACKAGES.includes(p));
  if (unknown.length) {
    throw new Error(`package(s) ${unknown.join(", ")} are not available; labs can use ${ALLOWED_PACKAGES.join(", ")}`);
  }
  return [...new Set(packages)];
}

/** The driver's request for one run; throws on anything malformed (as the driver would). */
export function buildRunRequest({ files, entry, checks = [], packages }) {
  if (files === null || typeof files !== "object" || Array.isArray(files)) {
    throw new TypeError("files must be an object of file name to source");
  }
  const names = Object.keys(files);
  if (!names.length) throw new Error("a lab program needs at least one file");
  for (const name of names) {
    if (!FILE_NAME.test(name)) throw new Error(`file name ${JSON.stringify(name)} must match ${FILE_NAME.source}`);
    if (typeof files[name] !== "string") throw new TypeError(`the source of ${name} must be a string`);
  }
  if (!names.includes(entry)) throw new Error(`the entry ${JSON.stringify(entry)} is not one of the files`);
  if (!Array.isArray(checks) || !checks.every((c) => typeof c === "string")) {
    throw new TypeError("checks must be an array of strings");
  }
  return { files: { ...files }, entry, checks: [...checks], packages: checkPackages(packages) };
}

export function timeoutMessage(seconds, phase) {
  const limit = Number(seconds.toPrecision(6)); // "20", "0.5": like Python's {:g}
  return phase === "checks"
    ? `The checks did not finish within ${limit} seconds and were stopped.`
    : `The program did not finish within ${limit} seconds and was stopped.`;
}

/** The result of a run stopped at its time limit, from the driver's events seen until then
 * (mirrors teacher.pyodide_runtime.timed_out_result). */
export function timedOutResult(events, checks, seconds) {
  const output = { stdout: "", stderr: "" };
  let phase = "entry";
  for (const event of events) {
    if (event.event === "output" && (event.stream === "stdout" || event.stream === "stderr")) {
      output[event.stream] += String(event.text);
    } else if (event.event === "phase") {
      phase = String(event.phase);
    }
  }
  const checkError = phase === "checks" ? CHECKS_TIMED_OUT : NOT_RUN;
  return {
    run: {
      ok: false,
      stdout: output.stdout,
      stderr: output.stderr,
      error: timeoutMessage(seconds, phase),
      timedOut: true,
      figures: [],
    },
    checks: Array.from({ length: checks }, () => ({ passed: false, output: "", error: checkError })),
  };
}

const BASE64 = /^[A-Za-z0-9+/]*={0,2}$/;

/** The driver's result as the sandbox's run() result (figures as PNG data URLs); throws on
 * anything malformed. */
export function toSandboxResult(result, checks) {
  const bad = (what) => new Error(`the sandbox returned a malformed result (${what})`);
  if (!result || typeof result !== "object" || !result.run || typeof result.run !== "object") throw bad("no run");
  const run = result.run;
  if (typeof run.ok !== "boolean") throw bad("run.ok");
  if (typeof run.stdout !== "string" || typeof run.stderr !== "string") throw bad("run output");
  if (!(run.error === null || typeof run.error === "string")) throw bad("run.error");
  if (run.ok !== (run.error === null)) throw bad("run.ok disagrees with run.error");
  if (!Array.isArray(run.figures) || !run.figures.every((f) => typeof f === "string" && BASE64.test(f))) {
    throw bad("run.figures");
  }
  if (!Array.isArray(result.checks) || result.checks.length !== checks) throw bad(`expected ${checks} check result(s)`);
  const checkResults = result.checks.map((item) => {
    if (
      !item || typeof item !== "object" || typeof item.passed !== "boolean" || typeof item.output !== "string" ||
      !(item.error === null || typeof item.error === "string") || item.passed !== (item.error === null)
    ) {
      throw bad("a check result");
    }
    return { passed: item.passed, output: item.output, error: item.error };
  });
  return {
    run: {
      ok: run.ok,
      stdout: run.stdout,
      stderr: run.stderr,
      error: run.error,
      timedOut: false,
      figures: run.figures.map((b64) => `data:image/png;base64,${b64}`),
    },
    checks: checkResults,
  };
}

/**
 * Validate one message's data from the frame: `{ type: "booted" | "ready" }`,
 * `{ type: "failed", error }`, `{ type: "event", id, event }`, `{ type: "result", id, result }`,
 * `{ type: "timedOut", id }` or `{ type: "runError", id, error }`; null for anything else (the
 * host ignores it). Checking event.source is the caller's job; results are checked further by
 * toSandboxResult.
 */
export function parseSandboxMessage(data) {
  if (data === null || typeof data !== "object" || Array.isArray(data)) return null;
  if (data.source !== FRAME_SOURCE) return null;
  const hasId = Number.isInteger(data.id) && data.id > 0;
  switch (data.type) {
    case "booted":
    case "ready":
      return { type: data.type };
    case "failed":
      return typeof data.error === "string" ? { type: "failed", error: data.error } : null;
    case "event":
      return hasId && data.event && typeof data.event === "object" ? { type: "event", id: data.id, event: data.event } : null;
    case "result":
      return hasId ? { type: "result", id: data.id, result: data.result } : null;
    case "timedOut":
      return hasId ? { type: "timedOut", id: data.id } : null;
    case "runError":
      return hasId && typeof data.error === "string" ? { type: "runError", id: data.id, error: data.error } : null;
    default:
      return null;
  }
}

// -- code that runs inside the sandbox ------------------------------------------------------------
// These two functions are never called here: their source text is put into the frame document
// (frame) and into a blob: URL (worker). They must be self-contained (no references to this
// module), and must never contain a closing script tag or an HTML comment opener (the test
// checks).

/* eslint-disable no-restricted-globals */
function sandboxWorkerMain() {
  "use strict";
  const raw = { stdout: [], stderr: [] };
  let pyodide = null;
  let namespace = null;

  function post(message) {
    self.postMessage(message);
  }

  function errorText(error) {
    return error && error.message ? String(error.message) : String(error);
  }

  // Emscripten grows the heap through WebAssembly.Memory.prototype.grow; past the limit the
  // growth fails and Python raises MemoryError.
  function capWasmMemory(limitBytes) {
    const grow = WebAssembly.Memory.prototype.grow;
    WebAssembly.Memory.prototype.grow = function cappedGrow(pages) {
      if (this.buffer.byteLength + pages * 65536 > limitBytes) {
        throw new RangeError(`WebAssembly memory limit of ${limitBytes / 1048576} MiB reached`);
      }
      return grow.call(this, pages);
    };
  }

  function lockDown() {
    for (const name of [
      "fetch", "XMLHttpRequest", "WebSocket", "EventSource", "WebTransport", "Worker", "SharedWorker", "BroadcastChannel", "indexedDB", "caches", "navigator", "Request",
    ]) {
      Object.defineProperty(self, name, { value: undefined, writable: false, configurable: false });
    }
  }

  async function init(config) {
    capWasmMemory(config.memoryLimitMb * 1048576);
    // A classic worker (Chrome cannot start a module worker from a blob: URL in an opaque-origin
    // frame) that loads Pyodide as a module with import(). Pyodide 314 refuses to run in a
    // classic worker, which it detects by calling importScripts; with importScripts gone the
    // probe fails and the worker counts as a module worker, which is how it then behaves.
    Object.defineProperty(self, "importScripts", { value: undefined, writable: false, configurable: false });
    const { loadPyodide } = await import(`${config.runtimeUrl}pyodide.mjs`);
    pyodide = await loadPyodide({
      indexURL: config.runtimeUrl,
      jsglobals: Object.create(null),
      env: { HOME: "/home/pyodide" },
      stdin: () => null,
      stdout: (text) => raw.stdout.push(`${text}\n`),
      stderr: (text) => raw.stderr.push(`${text}\n`),
    });
    if (config.packages.length) {
      const errors = [];
      await pyodide.loadPackage(config.packages, {
        messageCallback: () => {},
        errorCallback: (message) => errors.push(message),
        checkIntegrity: true,
      });
      if (errors.length) throw new Error(`the packages could not be loaded: ${errors.join("; ")}`);
    }
    namespace = pyodide.globals.get("dict")();
    pyodide.runPython(config.driver, { globals: namespace, filename: "<lab driver>" });
    namespace.get("prepare")(JSON.stringify(config.packages));
    pyodide.runPython("import sys; sys.modules.pop('pyodide_js', None)");
    pyodide.unregisterJsModule("pyodide_js");
    lockDown();
    post({ type: "ready" });
  }

  function run(request) {
    const emit = (eventJson) => post({ type: "event", event: JSON.parse(eventJson) });
    const result = JSON.parse(namespace.get("run_lab")(JSON.stringify(request), emit));
    result.run.stdout += raw.stdout.join("");
    result.run.stderr += raw.stderr.join("");
    post({ type: "result", result });
  }

  self.onmessage = (event) => {
    const message = event.data;
    if (message.type === "init") {
      init(message).catch((error) => post({ type: "failed", error: errorText(error) }));
    } else if (message.type === "run") {
      try {
        run(message.request);
      } catch (error) {
        post({ type: "failed", error: errorText(error) });
      }
    }
  };
}

function sandboxFrameMain(constants) {
  "use strict";
  const { FRAME_SOURCE, HOST_SOURCE, WORKER_SOURCE } = constants;
  const host = window.parent;
  let config = null;
  let workerUrl = null;
  let warm = null; // the next worker: { worker, state, ready }
  let current = null; // the run in progress: { id, entry }
  let readySent = false;

  function post(message) {
    message.source = FRAME_SOURCE;
    host.postMessage(message, "*");
  }

  function startWorker() {
    const worker = new Worker(workerUrl);
    const entry = { worker, state: "starting", timer: null };
    entry.ready = new Promise((resolve, reject) => {
      worker.onmessage = (event) => {
        const message = event.data;
        if (entry.state === "starting") {
          if (message.type === "ready") {
            entry.state = "ready";
            resolve();
          } else if (message.type === "failed") {
            entry.state = "failed";
            worker.terminate();
            reject(new Error(message.error));
          }
        } else if (entry.state === "running") {
          onRunMessage(entry, message);
        }
      };
      worker.onerror = (event) => {
        event.preventDefault();
        const error = event.message || "the Python worker failed";
        if (entry.state === "starting") {
          entry.state = "failed";
          worker.terminate();
          reject(new Error(error));
        } else if (entry.state === "running") {
          finishRun(entry, { type: "runError", error });
        }
      };
    });
    entry.ready.catch(() => {}); // a run waiting on it reports the failure
    worker.postMessage({ type: "init", ...config });
    return entry;
  }

  function warmUp() {
    const entry = startWorker();
    warm = entry;
    entry.ready.then(
      () => {
        if (!readySent) {
          readySent = true;
          post({ type: "ready" });
        }
      },
      (error) => {
        if (!readySent) post({ type: "failed", error: error.message });
      }
    );
  }

  function onRunMessage(entry, message) {
    if (message.type === "event") post({ type: "event", id: current.id, event: message.event });
    else if (message.type === "result") finishRun(entry, { type: "result", result: message.result });
    else if (message.type === "failed") finishRun(entry, { type: "runError", error: message.error });
  }

  function finishRun(entry, message) {
    if (!current || current.entry !== entry || entry.state !== "running") return;
    entry.state = "done";
    clearTimeout(entry.timer);
    entry.worker.terminate();
    message.id = current.id;
    current = null;
    post(message);
    warmUp();
  }

  async function startRun(message) {
    if (current) {
      post({ type: "runError", id: message.id, error: "a run is already in progress" });
      return;
    }
    const entry = warm;
    current = { id: message.id, entry };
    try {
      await entry.ready;
    } catch (error) {
      current = null;
      post({ type: "runError", id: message.id, error: `the Python runtime could not be loaded: ${error.message}` });
      warmUp();
      return;
    }
    entry.state = "running";
    entry.timer = setTimeout(() => finishRun(entry, { type: "timedOut" }), message.timeoutMs);
    entry.worker.postMessage({ type: "run", request: message.request });
  }

  window.addEventListener("message", (event) => {
    if (event.source !== host) return;
    const message = event.data;
    if (!message || typeof message !== "object" || message.source !== HOST_SOURCE) return;
    if (message.type === "init" && config === null) {
      config = {
        runtimeUrl: message.runtimeUrl,
        packages: message.packages,
        driver: message.driver,
        memoryLimitMb: message.memoryLimitMb,
      };
      workerUrl = URL.createObjectURL(new Blob([WORKER_SOURCE], { type: "text/javascript" }));
      warmUp();
    } else if (message.type === "run" && config !== null) {
      startRun(message);
    }
  });
  post({ type: "booted" });
}
/* eslint-enable no-restricted-globals */

export const WORKER_SOURCE = `(${sandboxWorkerMain.toString()})();\n`;
export const FRAME_SCRIPT = `(${sandboxFrameMain.toString()})(${JSON.stringify({
  FRAME_SOURCE,
  HOST_SOURCE,
  WORKER_SOURCE,
}).replace(/</g, "\\u003c")});`;

/** The frame's complete HTML document. The CSP meta is the first element of head. */
export function buildSandboxDocument(runtimeUrl) {
  return `<!doctype html>
<html lang="en">
<head>
<meta http-equiv="Content-Security-Policy" content="${frameCsp(runtimeUrl)}">
<meta charset="utf-8">
<script>${FRAME_SCRIPT}</script>
</head>
<body></body>
</html>
`;
}
