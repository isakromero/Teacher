// Runs one lab program in Pyodide under Node, for `teacher.pyodide_runtime.PythonRunner`.
//
//   node --permission ... node_runner.mjs <runtime dir> <driver file> <wasm memory limit, MiB>
//
// Reads the request JSON ({files, entry, checks, packages}) from stdin and writes JSON lines to
// stdout, each written synchronously so that what was sent survives a kill at the time limit:
//
//   {"type": "ready"}                          Pyodide and the packages are loaded
//   {"type": "event", "event": {...}}          the driver's progress (output chunks, phase)
//   {"type": "result", "result": {...}}        the driver's result, figures replaced by a count
//   {"type": "fatal", "error": "..."}          the runner itself failed (exit status 1)
//
// The program's work is done by the shared Python driver (web/static/js/lab_driver.py); this
// file only loads Pyodide, hardens the JavaScript side and moves JSON around. Python can reach
// JavaScript objects (through `js`, `pyodide_js` and the Pyodide API object its own modules
// keep), so beyond the permission flags the parent passes (see teacher/pyodide_runtime.py):
//   - once Pyodide and the packages are loaded, module hooks refuse every further import, so
//     no `import("node:net")`, `data:` module or require() call (emscripten's own, reached by
//     posix.system, included) can load anything (the permission model does not restrict the
//     network in Node 24; this is what does);
//   - the program runs synchronously and the process exits right after the result, so no
//     JavaScript task the program schedules (a promise, a timer) ever runs;
//   - Python's `js` module is an empty object, not the global scope, and `pyodide_js` is
//     unregistered (the API object stays reachable through Pyodide's internals);
//   - fetch, WebSocket and friends and the dangerous parts of `process` (getBuiltinModule,
//     binding, dlopen, kill, env, ...) are removed before the program runs;
//   - the WebAssembly memory can grow only up to the given limit (Python then raises
//     MemoryError); the JS heap is capped by the parent's --max-old-space-size;
//   - console output goes to stderr, so nothing but this protocol is written to stdout.

import { constants as fsConstants, readFileSync, writeSync } from "node:fs";
import { registerHooks } from "node:module";
import { pathToFileURL } from "node:url";

let modulesLocked = false;
registerHooks({
  resolve(specifier, context, nextResolve) {
    if (modulesLocked) throw new Error(`loading modules is disabled in labs (${specifier})`);
    return nextResolve(specifier, context);
  },
  load(url, context, nextLoad) {
    if (modulesLocked) throw new Error(`loading modules is disabled in labs (${url})`);
    return nextLoad(url, context);
  },
});

function send(message) {
  writeSync(1, `${JSON.stringify(message)}\n`);
}

function writeErr(text) {
  writeSync(2, `${text}\n`);
}

for (const method of ["log", "info", "warn", "error", "debug", "trace"]) {
  console[method] = (...args) => writeErr(args.map(String).join(" "));
}

function fail(error) {
  send({ type: "fatal", error: error && error.stack ? String(error.stack) : String(error) });
  process.exit(1);
}

/** Make WebAssembly.Memory.prototype.grow refuse to go past `limitBytes` (emscripten's heap
 * growth calls it from JavaScript and turns the failure into a failed malloc). */
function capWasmMemory(limitBytes) {
  const grow = WebAssembly.Memory.prototype.grow;
  WebAssembly.Memory.prototype.grow = function cappedGrow(pages) {
    if (this.buffer.byteLength + pages * 65536 > limitBytes) {
      throw new RangeError(`WebAssembly memory limit of ${limitBytes / 1048576} MiB reached`);
    }
    return grow.call(this, pages);
  };
}

/** Remove what could reach the network or the host from the global scope and `process`. */
function lockDown() {
  modulesLocked = true;
  for (const name of [
    "fetch", "WebSocket", "EventSource", "XMLHttpRequest", "Request", "Response", "Headers",
    "FormData", "navigator", "require", "module", "Worker", "SharedWorker", "BroadcastChannel",
    "MessageChannel", "Deno", "Bun",
  ]) {
    delete globalThis[name];
    if (name in globalThis) globalThis[name] = undefined;
  }
  for (const name of [
    "getBuiltinModule", "binding", "_linkedBinding", "dlopen", "kill", "chdir", "setuid",
    "setgid", "seteuid", "setegid", "setgroups", "initgroups", "_kill", "_rawDebug",
    "openStdin", "report", "mainModule", "send", "channel", "env", "execve", "loadEnvFile",
  ]) {
    Object.defineProperty(process, name, { value: undefined, configurable: false, writable: false });
  }
}

async function main() {
  const [runtimeDir, driverPath, limitMb] = process.argv.slice(2);
  if (!runtimeDir || !driverPath || !(Number(limitMb) > 0)) {
    throw new Error("usage: node_runner.mjs <runtime dir> <driver file> <wasm memory limit MiB>");
  }
  const request = JSON.parse(readFileSync(0, "utf8"));
  const driverSource = readFileSync(driverPath, "utf8");
  const packages = Array.isArray(request.packages) ? request.packages : [];

  // Pyodide's NODEFS reads open() flags with process.binding("constants"), which the
  // permission model forbids; the same numbers are in fs.constants.
  process.binding = (name) => {
    if (name === "constants") return { fs: fsConstants };
    throw new Error(`process.binding(${JSON.stringify(name)}) is not available`);
  };
  capWasmMemory(Number(limitMb) * 1048576);

  const raw = { stdout: [], stderr: [] };
  const indexURL = runtimeDir.endsWith("/") ? runtimeDir : `${runtimeDir}/`;
  const { loadPyodide } = await import(pathToFileURL(`${indexURL}pyodide.mjs`).href);
  const pyodide = await loadPyodide({
    indexURL,
    jsglobals: Object.create(null),
    env: { HOME: "/home/pyodide" },
    stdin: () => null,
    stdout: (text) => raw.stdout.push(`${text}\n`),
    stderr: (text) => raw.stderr.push(`${text}\n`),
  });
  if (packages.length) {
    const errors = [];
    await pyodide.loadPackage(packages, {
      messageCallback: () => {},
      errorCallback: (message) => errors.push(message),
      checkIntegrity: true,
    });
    if (errors.length) throw new Error(`the packages could not be loaded: ${errors.join("; ")}`);
  }
  const namespace = pyodide.globals.get("dict")();
  pyodide.runPython(driverSource, { globals: namespace, filename: "<lab driver>" });
  namespace.get("prepare")(JSON.stringify(packages));
  pyodide.runPython("import sys; sys.modules.pop('pyodide_js', None)");
  pyodide.unregisterJsModule("pyodide_js");
  lockDown();
  send({ type: "ready" });

  // From here on everything is synchronous and ends in process.exit (also on failure), so the
  // event loop never turns again and no task the program queued can run.
  try {
    const emit = (eventJson) => send({ type: "event", event: JSON.parse(eventJson) });
    const result = JSON.parse(namespace.get("run_lab")(JSON.stringify(request), emit));
    result.run.stdout += raw.stdout.join("");
    result.run.stderr += raw.stderr.join("");
    result.run.figures = result.run.figures.length;
    send({ type: "result", result });
  } catch (error) {
    fail(error);
  }
  process.exit(0);
}

main().catch(fail);
