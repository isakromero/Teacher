"""The lab driver: runs one lab program inside Pyodide, the same way in Node and in the browser.

This one file does the work in both places (`teacher/lab_runtime/node_runner.mjs` on the server,
the worker of `lab_sandbox.js` in the browser), so a lab behaves identically in both. It is
plain Python with no Pyodide imports, so it also runs (and is unit-tested) under CPython. The
host executes this source in a fresh namespace, calls `prepare(packages_json)` once (before the
time limit starts), then `run_lab(request_json, emit)` once, and reads the JSON it returns:

    request: {"files": {name: source}, "entry": name, "checks": [snippet], "packages": [name]}
    result:  {"run": {"ok", "stdout", "stderr", "error", "figures": [base64 PNG]},
              "checks": [{"passed", "output", "error"}]}

`emit(event_json)` is called while the program runs so the host still has something to show
if it has to kill the interpreter at the time limit: `{"event": "output", "stream", "text"}`
for the entry's stdout and stderr, in chunks, and `{"event": "phase", "phase": "checks"}` when
the entry is done and the checks start.

What happens: the files are written to a fresh working directory, which becomes the current
directory and the first entry of `sys.path`; the entry runs as `__main__` with stdout and stderr
captured (each capped at MAX_STREAM_CHARS); then every figure matplotlib still has open is saved
as PNG; then each check runs in its own fresh namespace (the lab's modules are imported afresh
for each check) and passes iff it raises nothing. If the entry failed, no check runs and each is
reported failed with NOT_RUN. Error messages are tracebacks with the driver's own frames (and
runpy's and importlib's) removed and paths shown relative to the working directory.
"""

import base64
import errno
import io
import json
import linecache
import os
import re
import sys
import tempfile
import time
import traceback
import types

FILE_NAME = re.compile(r"^[a-z_][a-z0-9_]*\.py$")
NOT_RUN = "the program did not run"
MAX_STREAM_CHARS = 200_000
MAX_ERROR_CHARS = 6_000
# Output is passed to emit() in chunks of about this size, or this often.
EMIT_CHARS = 4_096
EMIT_SECONDS = 0.1
ALLOWED_PACKAGES = ("numpy", "matplotlib")
_CHECK_FILE = "<check {number}>"


# -- the request -------------------------------------------------------------------------------


def parse_request(request_json):
    """The request as (files, entry, checks, packages); ValueError if it is malformed."""
    request = json.loads(request_json)
    if not isinstance(request, dict):
        raise ValueError("the request must be a JSON object")
    unknown = set(request) - {"files", "entry", "checks", "packages"}
    if unknown:
        raise ValueError(f"unknown request field(s): {', '.join(sorted(unknown))}")
    files = request.get("files")
    if not isinstance(files, dict) or not files:
        raise ValueError("files must be a non-empty object of file name to source")
    for name, source in files.items():
        if not FILE_NAME.match(name):
            raise ValueError(f"file name {name!r} must match {FILE_NAME.pattern}")
        if not isinstance(source, str):
            raise ValueError(f"the source of {name} must be a string")
    entry = request.get("entry")
    if entry not in files:
        raise ValueError(f"the entry {entry!r} is not one of the files")
    checks = request.get("checks", [])
    if not isinstance(checks, list) or not all(isinstance(c, str) for c in checks):
        raise ValueError("checks must be a list of strings")
    packages = parse_packages(request.get("packages", []))
    return files, entry, checks, packages


def parse_packages(packages):
    if not isinstance(packages, list) or not all(isinstance(p, str) for p in packages):
        raise ValueError("packages must be a list of strings")
    unknown = sorted(set(packages) - set(ALLOWED_PACKAGES))
    if unknown:
        raise ValueError(
            f"package(s) {', '.join(unknown)} are not available; labs can use "
            f"{', '.join(ALLOWED_PACKAGES)}"
        )
    return packages


def _no_programs(*args, **kwargs):
    raise OSError(errno.ENOSYS, "labs cannot start other programs")


def prepare(packages_json):
    """Import the requested packages (already loaded by the host) and force matplotlib's Agg
    backend, before the time limit starts. Called once, before `run_lab`.

    Also replaces os.system, the one way Pyodide offers to start a program (under Node it asks
    the host for a child process, which the runner refuses by crashing the interpreter), with a
    function raising OSError, so a lab gets the same error in Node and in the browser."""
    packages = parse_packages(json.loads(packages_json))
    os.system = _no_programs
    os.environ["MPLBACKEND"] = "Agg"
    if "numpy" in packages:
        import numpy  # noqa: F401
    if "matplotlib" in packages:
        import warnings

        import matplotlib

        matplotlib.use("Agg", force=True)
        import matplotlib.pyplot  # noqa: F401

        # Pyodide's matplotlib 3.10.8 build warns about its own float coordinates on every
        # text draw; the reader can do nothing about it, so it is not shown.
        warnings.filterwarnings(
            "ignore",
            message=r"The [xy] parameter as float was deprecated",
            category=matplotlib.MatplotlibDeprecationWarning,
        )


# -- capturing output --------------------------------------------------------------------------


class _Capture(io.TextIOBase):
    """A text stream that keeps what is written (up to MAX_STREAM_CHARS) and passes it on to
    `emit` in chunks: the first write at once, then whenever EMIT_CHARS have piled up or
    EMIT_SECONDS have passed since the last chunk, and on flush()."""

    def __init__(self, stream, emit):
        super().__init__()
        self.stream = stream
        self._emit = emit
        self._parts = []
        self._size = 0
        self._cut = False
        self._pending = []
        self._pending_size = 0
        self._last_emit = float("-inf")  # the first write goes out at once

    @property
    def encoding(self):
        return "utf-8"

    def writable(self):
        return True

    def write(self, text):
        if not isinstance(text, str):
            raise TypeError(f"write() argument must be str, not {type(text).__name__}")
        if self._cut or not text:
            return len(text)
        room = MAX_STREAM_CHARS - self._size
        kept = text[:room]
        if len(text) > room:
            kept += f"\n[output cut: more than {MAX_STREAM_CHARS} characters]\n"
            self._cut = True
        self._parts.append(kept)
        self._size += len(kept)
        if self._emit is not None:
            self._pending.append(kept)
            self._pending_size += len(kept)
            now = time.monotonic()
            if self._cut or self._pending_size >= EMIT_CHARS or now - self._last_emit >= EMIT_SECONDS:
                self.flush_emit(now)
        return len(text)

    def flush(self):
        self.flush_emit()

    def flush_emit(self, now=None):
        if self._pending and self._emit is not None:
            text = "".join(self._pending)
            self._emit(json.dumps({"event": "output", "stream": self.stream, "text": text}))
        self._pending = []
        self._pending_size = 0
        self._last_emit = time.monotonic() if now is None else now

    def getvalue(self):
        return "".join(self._parts)


class _Redirect:
    """Point sys.stdout and sys.stderr at captures for the duration of a `with`."""

    def __init__(self, out, err):
        self.out, self.err = out, err

    def __enter__(self):
        self.saved = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = self.out, self.err
        return self

    def __exit__(self, *exc_info):
        sys.stdout, sys.stderr = self.saved
        return False


# -- error messages ----------------------------------------------------------------------------


def _is_user_frame(filename, workdir):
    return filename.startswith(workdir + os.sep) or filename.startswith("<check ")


def _is_hidden_frame(filename):
    # The driver's own file name is whatever the host compiled this source as.
    return filename.startswith("<frozen ") or filename == _is_hidden_frame.__code__.co_filename


def format_error(exc, workdir):
    """A reader-facing traceback of `exc`: frames before the first one in the lab's own code (or
    a check) are dropped, as are runpy and importlib frames anywhere, paths are shown relative
    to the working directory, and the text is cut to its last MAX_ERROR_CHARS characters."""
    tb = exc.__traceback__
    while tb is not None and not _is_user_frame(tb.tb_frame.f_code.co_filename, workdir):
        tb = tb.tb_next
    summary = traceback.TracebackException(type(exc), exc, tb, compact=True)
    _drop_hidden_frames(summary)
    text = "".join(summary.format()).rstrip("\n")
    text = text.replace(workdir + os.sep, "")
    if len(text) > MAX_ERROR_CHARS:
        text = "…" + text[-(MAX_ERROR_CHARS - 1):]
    return text


def _drop_hidden_frames(summary, seen=None):
    seen = set() if seen is None else seen
    if id(summary) in seen:
        return
    seen.add(id(summary))
    kept = [frame for frame in summary.stack if not _is_hidden_frame(frame.filename)]
    summary.stack = traceback.StackSummary.from_list(kept)
    for linked in (summary.__cause__, summary.__context__):
        if linked is not None:
            _drop_hidden_frames(linked, seen)


# -- running -----------------------------------------------------------------------------------


def _forget_lab_modules(workdir):
    """Drop the lab's own modules from sys.modules, so the next import runs them afresh."""
    for name, module in list(sys.modules.items()):
        path = getattr(module, "__file__", None)
        if isinstance(path, str) and path.startswith(workdir + os.sep):
            del sys.modules[name]


def _run_entry(path):
    """Run the file at `path` as __main__ (a fresh module in sys.modules for the duration)."""
    with open(path, encoding="utf-8") as handle:
        source = handle.read()
    code = compile(source, path, "exec", dont_inherit=True)
    module = types.ModuleType("__main__")
    module.__file__ = path
    module.__builtins__ = __builtins__
    saved = sys.modules.get("__main__")
    sys.modules["__main__"] = module
    try:
        exec(code, module.__dict__)
    finally:
        if saved is not None:
            sys.modules["__main__"] = saved
        else:
            del sys.modules["__main__"]


def _exit_error(exc):
    """None for a SystemExit that means success (no code or 0), else its message."""
    code = exc.code
    if code is None or code == 0:
        return None
    if isinstance(code, int):
        return f"The program exited with status {code} (SystemExit)."
    return f"The program exited: {code}"


def _capture_figures():
    """Every open matplotlib figure as base64 PNG, then close them all. [] when the program
    did not use pyplot."""
    pyplot = sys.modules.get("matplotlib.pyplot")
    if pyplot is None:
        return []
    figures = []
    for number in pyplot.get_fignums():
        buffer = io.BytesIO()
        pyplot.figure(number).savefig(buffer, format="png")
        figures.append(base64.b64encode(buffer.getvalue()).decode("ascii"))
    pyplot.close("all")
    return figures


def _run_check(number, snippet, workdir):
    filename = _CHECK_FILE.format(number=number)
    linecache.cache[filename] = (len(snippet), None, snippet.splitlines(True), filename)
    out = _Capture("stdout", None)
    error = None
    _forget_lab_modules(workdir)
    with _Redirect(out, out):
        try:
            code = compile(snippet, filename, "exec", dont_inherit=True)
            exec(code, {"__name__": "__check__", "__builtins__": __builtins__})
        except SystemExit as exc:
            error = _exit_error(exc)
        except BaseException as exc:  # noqa: BLE001 - every failure is the check's result
            error = format_error(exc, workdir)
    return {"passed": error is None, "output": out.getvalue(), "error": error}


def run_lab(request_json, emit=None):
    """Run one request (see the module docstring); return the result as JSON."""
    files, entry, checks, _packages = parse_request(request_json)
    workdir = os.path.realpath(tempfile.mkdtemp(prefix="lab-"))
    for name, source in files.items():
        with open(os.path.join(workdir, name), "w", encoding="utf-8") as handle:
            handle.write(source)
    saved_cwd, saved_path, saved_argv = os.getcwd(), list(sys.path), list(sys.argv)
    os.chdir(workdir)
    sys.path.insert(0, workdir)
    sys.argv = [entry]
    out, err = _Capture("stdout", emit), _Capture("stderr", emit)
    error = None
    figures = []
    try:
        with _Redirect(out, err):
            try:
                _run_entry(os.path.join(workdir, entry))
            except SystemExit as exc:
                error = _exit_error(exc)
            except BaseException as exc:  # noqa: BLE001 - every failure is the program's result
                error = format_error(exc, workdir)
            try:
                figures = _capture_figures()
            except Exception as exc:  # noqa: BLE001
                figure_error = (
                    "A matplotlib figure could not be saved as PNG: "
                    + format_error(exc, workdir)
                )
                error = figure_error if error is None else f"{error}\n\n{figure_error}"
        out.flush_emit()
        err.flush_emit()
        run = {
            "ok": error is None,
            "stdout": out.getvalue(),
            "stderr": err.getvalue(),
            "error": error,
            "figures": figures,
        }
        if error is not None:
            results = [{"passed": False, "output": "", "error": NOT_RUN} for _ in checks]
        else:
            if emit is not None and checks:
                emit(json.dumps({"event": "phase", "phase": "checks"}))
            results = [
                _run_check(number, snippet, workdir)
                for number, snippet in enumerate(checks, start=1)
            ]
    finally:
        _forget_lab_modules(workdir)
        os.chdir(saved_cwd)
        sys.path[:] = saved_path
        sys.argv[:] = saved_argv
    return json.dumps({"run": run, "checks": results})
