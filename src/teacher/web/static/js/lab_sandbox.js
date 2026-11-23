// The lab sandbox: runs a lab's Python in the browser, in Pyodide inside a sandboxed iframe and a
// Web Worker (see lab_sandbox_frame.js for the layout and the security model).
//
//   const sandbox = createSandbox({ runtimeUrl, packages, container });
//   await sandbox.ready;                      // rejects if the runtime cannot be loaded
//   const { run, checks } = await sandbox.run({ files, entry, checks, timeoutMs });
//   sandbox.dispose();
//
// `runtimeUrl` is the `url` of GET /api/labs/runtime (relative URLs are resolved against the
// page), `packages` a subset of ALLOWED_PACKAGES, `container` the element the (hidden) iframe
// goes into. `files` is an object of file name to source (names like `main.py`), `entry` one
// of them, `checks` Python snippets that pass iff they raise nothing. The result:
//   run:    { ok, stdout, stderr, error, timedOut, figures: [PNG data URL] }
//   checks: [{ passed, output, error }]
// The semantics are those of the shared driver (lab_driver.py), identical to the server's
// `teacher.pyodide_runtime.PythonRunner`. Every run starts in a fresh interpreter. The time
// limit counts from the moment a loaded interpreter takes the run; at the limit the worker is
// terminated and the result has `timedOut: true` and the output printed until then.
// One run at a time: run() while another is in progress rejects.

import {
  HOST_SOURCE,
  WASM_MEMORY_LIMIT_MB,
  buildRunRequest,
  buildSandboxDocument,
  checkPackages,
  checkRuntimeUrl,
  parseSandboxMessage,
  timedOutResult,
  toSandboxResult,
} from "./lab_sandbox_frame.js";

export { ALLOWED_PACKAGES } from "./lab_sandbox_frame.js";

const DRIVER_URL = new URL("./lab_driver.py", import.meta.url);
export const DEFAULT_TIMEOUT_MS = 20000;
/** Loading Pyodide and the packages takes seconds; past this the sandbox reports a failure. */
export const STARTUP_TIMEOUT_MS = 120000;

async function fetchDriver() {
  let response;
  try {
    response = await fetch(DRIVER_URL);
  } catch (error) {
    throw new Error(`the lab driver (${DRIVER_URL}) could not be fetched: ${error.message}`);
  }
  if (!response.ok) throw new Error(`the lab driver (${DRIVER_URL}) could not be fetched: HTTP ${response.status}`);
  return response.text();
}

export function createSandbox({ runtimeUrl, packages = [], container } = {}) {
  if (typeof runtimeUrl !== "string" || !runtimeUrl) throw new TypeError("createSandbox needs the runtimeUrl");
  const runtime = checkRuntimeUrl(new URL(runtimeUrl, document.baseURI).href);
  const wanted = checkPackages(packages);
  if (!(container instanceof Element)) throw new TypeError("createSandbox needs a container element");

  let settle = null;
  const ready = new Promise((resolve, reject) => {
    settle = { resolve, reject };
  });
  let isReady = false;
  let disposed = false;
  let pending = null; // { id, checks, timeoutMs, events, resolve, reject, guard }
  let nextId = 1;
  let driver = null;

  const frame = document.createElement("iframe");
  frame.setAttribute("sandbox", "allow-scripts");
  frame.setAttribute("referrerpolicy", "no-referrer");
  frame.setAttribute("title", "Python sandbox");
  frame.setAttribute("aria-hidden", "true");
  frame.hidden = true;

  const startupTimer = setTimeout(
    () => failReady(`it did not start within ${STARTUP_TIMEOUT_MS / 1000} seconds`),
    STARTUP_TIMEOUT_MS
  );

  function failReady(message) {
    if (isReady || !settle) return;
    const { reject } = settle;
    settle = null;
    clearTimeout(startupTimer);
    reject(new Error(`The Python runtime could not be loaded: ${message}`));
  }

  function send(message) {
    frame.contentWindow.postMessage({ ...message, source: HOST_SOURCE }, "*");
  }

  function finish(record, outcome) {
    if (pending !== record) return;
    pending = null;
    clearTimeout(record.guard);
    if (outcome.error) record.reject(outcome.error);
    else record.resolve(outcome.value);
  }

  function onMessage(event) {
    if (disposed || event.source !== frame.contentWindow) return;
    const message = parseSandboxMessage(event.data);
    if (!message) return;
    if (message.type === "booted") {
      // The frame is created only once the driver is loaded.
      send({ type: "init", runtimeUrl: runtime, packages: wanted, driver, memoryLimitMb: WASM_MEMORY_LIMIT_MB });
    } else if (message.type === "ready") {
      if (settle && !isReady) {
        isReady = true;
        clearTimeout(startupTimer);
        settle.resolve();
        settle = null;
      }
    } else if (message.type === "failed") {
      failReady(message.error);
    } else if (pending && message.id === pending.id) {
      const record = pending;
      if (message.type === "event") {
        record.events.push(message.event);
      } else if (message.type === "result") {
        try {
          finish(record, { value: toSandboxResult(message.result, record.checks) });
        } catch (error) {
          finish(record, { error });
        }
      } else if (message.type === "timedOut") {
        finish(record, { value: timedOutResult(record.events, record.checks, record.timeoutMs / 1000) });
      } else if (message.type === "runError") {
        finish(record, { error: new Error(`The Python run failed: ${message.error}`) });
      }
    }
  }

  window.addEventListener("message", onMessage);
  fetchDriver().then(
    (source) => {
      if (disposed) return;
      driver = source;
      frame.srcdoc = buildSandboxDocument(runtime);
      container.append(frame);
    },
    (error) => failReady(error.message)
  );

  async function run({ files, entry, checks = [], timeoutMs = DEFAULT_TIMEOUT_MS } = {}) {
    if (disposed) throw new Error("the sandbox was disposed");
    const request = buildRunRequest({ files, entry, checks, packages: wanted });
    if (typeof timeoutMs !== "number" || !Number.isFinite(timeoutMs) || timeoutMs <= 0) {
      throw new TypeError("timeoutMs must be a positive number of milliseconds");
    }
    if (pending) throw new Error("a run is already in progress");
    const record = { id: nextId++, checks: request.checks.length, timeoutMs, events: [] };
    const result = new Promise((resolve, reject) => {
      record.resolve = resolve;
      record.reject = reject;
    });
    pending = record;
    try {
      await ready;
    } catch (error) {
      finish(record, { error });
      return result;
    }
    if (disposed || pending !== record) return result;
    // Backstop for a frame that stops answering; the frame itself enforces timeoutMs (after
    // waiting for its next worker to be loaded).
    record.guard = setTimeout(
      () => finish(record, { error: new Error("The Python sandbox stopped answering") }),
      timeoutMs + STARTUP_TIMEOUT_MS
    );
    send({ type: "run", id: record.id, request, timeoutMs });
    return result;
  }

  function dispose() {
    if (disposed) return;
    disposed = true;
    window.removeEventListener("message", onMessage);
    clearTimeout(startupTimer);
    if (settle) {
      settle.reject(new Error("the sandbox was disposed"));
      settle = null;
    }
    if (pending) finish(pending, { error: new Error("the sandbox was disposed") });
    frame.remove();
  }

  return { ready, run, dispose };
}

