"""The pinned Pyodide runtime that runs lab code: installing it, finding it, running Python with it.

A lab's Python runs in Pyodide (CPython compiled to WebAssembly) in two places: in the reader's
browser (a sandboxed iframe with a Web Worker, `web/static/js/lab_sandbox.js`) and on the server
under Node, only when a lab is generated, to check that it works (`PythonRunner`). Both load the
same pinned release from the same directory and hand the program to the same Python driver,
`web/static/js/lab_driver.py`, which writes the files, runs the entry as `__main__`, captures
the output and the matplotlib figures and runs the checks. So behaviour cannot drift between
the two; only the code around the driver (load Pyodide, enforce the time limit) is per place.

Install (`install_runtime`, the `teacher labs setup` command): the release's core files and
exactly the wheels of `ALLOWED_PACKAGES` and their dependency closure (read from the release's
`pyodide-lock.json`) are downloaded from the official CDN. The core files and the lock file are
checked against sha256 hashes pinned here (taken from the npm package of the same release), each
wheel against the hash in the lock file. Everything is written to a temporary directory next to
the target and renamed into place only when complete, and a manifest (`MANIFEST_FILE`) is
written last, so a failed download never leaves a directory `check_runtime` accepts. The
directory is `$TEACHER_PYODIDE_DIR` when set (a shared install, tests), else
`derived/pyodide/<version>` under the Teacher home.

Server execution (`PythonRunner.execute`): one `node` process per call runs
`lab_runtime/node_runner.mjs` with the request as JSON on stdin; it answers with JSON lines on
stdout (`ready` once Pyodide and the packages are loaded, `event`s for the program's output and
progress, then one `result` or `fatal`). The time limit starts at `ready`, so it covers the
program and not Pyodide's start-up, exactly as in the browser; when it passes the process is
killed and the result is assembled from the events seen so far (`timed_out_result`).

Confinement of the Node process (model-written code runs there on the owner's machine). Node's
permission model: file reads only inside the runtime directory and the two runner files, no file
writes, no child processes, no worker threads, no native addons, no WASI, no inspector. Code
generation from strings (eval, `Function`) is disabled. The process gets an empty environment,
its working directory is the runtime directory, and its JavaScript heap and the WebAssembly
memory Python can grow into are capped (`JS_HEAP_LIMIT_MB`, `WASM_MEMORY_LIMIT_MB`; past the
latter Python raises MemoryError).

Node 24's permission model does not restrict the network, and Python can reach JavaScript (the
`js` module, and the Pyodide API object its internals keep, whatever the runner unregisters).
So the runner adds JavaScript-level hardening once Pyodide and the packages are loaded: module
hooks refuse every further import (no `node:net`, no `data:` module, no require()), Python's
`js` is an empty object, fetch/WebSocket/XMLHttpRequest and the risky parts of `process`
(getBuiltinModule, binding, dlopen, kill, env, ...) are removed, and the program runs
synchronously to the end, so no JavaScript task it schedules ever runs. The tests try the known
ways out (tests/test_pyodide_runtime.py). This hardening is not an OS boundary: an escape from
it inside V8 or Pyodide would reach the network (not files or processes, which the permission
model still refuses).
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx2

from teacher.config import Settings

PYODIDE_VERSION = "314.0.8"
ALLOWED_PACKAGES = ("numpy", "matplotlib")
ENV_PYODIDE_DIR = "TEACHER_PYODIDE_DIR"

CDN_URL = f"https://cdn.jsdelivr.net/pyodide/v{PYODIDE_VERSION}/full/"
LOCK_FILE = "pyodide-lock.json"
MANIFEST_FILE = "teacher-runtime.json"
SETUP_COMMAND = "teacher labs setup"

# The release's core files with their sha256 (identical in the npm tarball pyodide@314.0.8 and
# on the CDN). pyodide.mjs is the loader both Node and the browser's (module) worker import; it
# loads pyodide.asm.mjs, the wasm and the standard library. (pyodide.js, the classic-script
# loader, is not needed: this release refuses to run in classic workers.)
CORE_FILES: Mapping[str, str] = {
    LOCK_FILE: "5dc2fc119108bc148c7457dc86e7675b5c87e1cafd420b9c34c1eaef7b36c010",
    "pyodide.mjs": "1bb8cc14f9405aaa7cd4e00c12edd0a8539de9cc026618d6ee5a9663f367dc70",
    "pyodide.asm.mjs": "22514b8be6980e28a02c8b1d6387ce8dfe666fc4edf8d547cff5dce29ff4d972",
    "pyodide.asm.wasm": "e14d49ceca9b4a0e274ea918f3a898de2d3880b6e5573a6cfe1a2267ecc70329",
    "python_stdlib.zip": "2bd3394a145bfbbe5bbc9d9d6ab9136abbb986c6fef6c142b3a2962227aa5c28",
}

RUNNER_DIR = Path(__file__).resolve().parent / "lab_runtime"
NODE_RUNNER = RUNNER_DIR / "node_runner.mjs"
DRIVER_FILE = Path(__file__).resolve().parent / "web" / "static" / "js" / "lab_driver.py"

MIN_NODE_VERSION = (24, 0)
JS_HEAP_LIMIT_MB = 512
WASM_MEMORY_LIMIT_MB = 1024
# Loading Pyodide and the packages takes a few seconds; this only catches a hung start-up.
STARTUP_TIMEOUT = 120.0
DOWNLOAD_TIMEOUT = 120.0

FILE_NAME = re.compile(r"^[a-z_][a-z0-9_]*\.py$")
# Mirrors the driver's NOT_RUN and the timeout texts of lab_sandbox.js (keep them in sync).
NOT_RUN = "the program did not run"
CHECKS_TIMED_OUT = "the checks did not finish in time"


class RuntimeUnavailableError(Exception):
    """The runtime (or Node, on the server) is missing or unusable; the message says what to do."""


class RuntimeInstallError(Exception):
    """Installing the runtime failed (download, hash or file error); nothing was installed."""


class RunnerError(RuntimeError):
    """The Node runner itself failed (crashed, or answered with something unreadable). Not an
    error of the program it ran: those are reported in `RunResult.error`."""


# -- where the runtime lives ------------------------------------------------------------------


def runtime_dir(settings: Settings) -> Path:
    """`$TEACHER_PYODIDE_DIR` if set (blank is an error), else derived/pyodide/<version>."""
    override = os.environ.get(ENV_PYODIDE_DIR)
    if override is not None:
        if not override.strip():
            raise ValueError(f"${ENV_PYODIDE_DIR} is set but empty; unset it to use the default")
        return Path(override.strip()).expanduser().resolve()
    return settings.derived_dir / "pyodide" / PYODIDE_VERSION


def _setup_hint() -> str:
    return f"run `{SETUP_COMMAND}` (or point ${ENV_PYODIDE_DIR} at an installed runtime)"


def check_runtime(settings: Settings) -> Path:
    """The runtime directory if a complete install of `PYODIDE_VERSION` is there, else
    RuntimeUnavailableError saying what is wrong and how to fix it. Checks the manifest, the
    presence and size of every file it lists and that the allowed packages are covered (the
    hashes were verified when the files were installed)."""
    directory = runtime_dir(settings)
    manifest_path = directory / MANIFEST_FILE
    if not directory.is_dir():
        raise RuntimeUnavailableError(
            f"The Python runtime for labs (Pyodide {PYODIDE_VERSION}) is not installed at "
            f"{directory}; {_setup_hint()}."
        )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise RuntimeUnavailableError(
            f"{directory} is not a complete Pyodide install (it has no {MANIFEST_FILE}); "
            f"{_setup_hint()}."
        ) from None
    except (OSError, ValueError) as exc:
        raise RuntimeUnavailableError(
            f"{manifest_path} is unreadable ({exc}); {_setup_hint()}."
        ) from exc
    if not isinstance(manifest, dict) or manifest.get("version") != PYODIDE_VERSION:
        found = manifest.get("version") if isinstance(manifest, dict) else None
        raise RuntimeUnavailableError(
            f"{directory} holds Pyodide {found!r}, but Teacher needs {PYODIDE_VERSION}; "
            f"{_setup_hint()}."
        )
    missing_packages = sorted(set(ALLOWED_PACKAGES) - set(manifest.get("packages", [])))
    if missing_packages:
        raise RuntimeUnavailableError(
            f"The Pyodide install at {directory} lacks the package(s) "
            f"{', '.join(missing_packages)}; {_setup_hint()}."
        )
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise RuntimeUnavailableError(f"{manifest_path} lists no files; {_setup_hint()}.")
    for name, entry in files.items():
        path = directory / name
        if not path.is_file():
            raise RuntimeUnavailableError(
                f"The Pyodide install at {directory} is missing {name}; {_setup_hint()}."
            )
        if path.stat().st_size != entry.get("size"):
            raise RuntimeUnavailableError(
                f"{path} has the wrong size (damaged or changed since it was installed); "
                f"{_setup_hint()}."
            )
    return directory


def runtime_files(settings: Settings) -> frozenset[str]:
    """The names of the installed runtime's files (the ones the HTTP route may serve)."""
    directory = check_runtime(settings)
    manifest = json.loads((directory / MANIFEST_FILE).read_text(encoding="utf-8"))
    return frozenset(manifest["files"])


# -- installing -------------------------------------------------------------------------------


def package_closure(lock: Mapping[str, Any], names: Iterable[str]) -> list[str]:
    """The packages `names` need, themselves included, from a parsed pyodide-lock.json, sorted.
    A name the lock does not know (as a dependency too) is a RuntimeInstallError."""
    packages = lock.get("packages")
    if not isinstance(packages, dict):
        raise RuntimeInstallError("the lock file has no 'packages' table")
    found: set[str] = set()
    pending = [(name, None) for name in names]
    while pending:
        name, needed_by = pending.pop()
        if name in found:
            continue
        entry = packages.get(name)
        if not isinstance(entry, dict):
            via = f" (a dependency of {needed_by})" if needed_by else ""
            raise RuntimeInstallError(f"the lock file has no package {name!r}{via}")
        found.add(name)
        pending.extend((dependency, name) for dependency in entry.get("depends", []))
    return sorted(found)


def package_files(lock: Mapping[str, Any], names: Iterable[str]) -> dict[str, str]:
    """{file name: sha256} of the wheels of `names` (closure included), from the lock."""
    files: dict[str, str] = {}
    for name in package_closure(lock, names):
        entry = lock["packages"][name]
        file_name, digest = entry.get("file_name"), entry.get("sha256")
        if not isinstance(file_name, str) or "/" in file_name or not file_name:
            raise RuntimeInstallError(f"the lock file's entry for {name!r} has a bad file_name")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise RuntimeInstallError(f"the lock file's entry for {name!r} has a bad sha256")
        files[file_name] = digest
    return files


def _download(client: Any, name: str, expected_sha256: str, target: Path) -> int:
    """GET CDN_URL + name into `target`, check its sha256; return its size."""
    url = CDN_URL + name
    digest = hashlib.sha256()
    size = 0
    try:
        with client.stream("GET", url, timeout=DOWNLOAD_TIMEOUT, follow_redirects=True) as response:
            if response.status_code != 200:
                raise RuntimeInstallError(f"{url} answered with HTTP {response.status_code}")
            with target.open("wb") as out:
                for chunk in response.iter_bytes():
                    digest.update(chunk)
                    size += len(chunk)
                    out.write(chunk)
    except httpx2.HTTPError as exc:
        raise RuntimeInstallError(
            f"{url} could not be downloaded ({exc.__class__.__name__}: {exc})"
        ) from exc
    if digest.hexdigest() != expected_sha256:
        raise RuntimeInstallError(
            f"{url} does not match its pinned sha256 (expected {expected_sha256}, got "
            f"{digest.hexdigest()}); nothing was installed"
        )
    return size


def install_runtime(
    settings: Settings,
    *,
    client: Any = None,
    progress: Callable[[str], None] | None = None,
) -> Path:
    """Download and verify the runtime into `runtime_dir(settings)`; return that directory.

    A complete install already there is kept (nothing is downloaded). Otherwise the files go to a
    temporary sibling directory that replaces the target only when every file has been
    downloaded and verified; on any failure it is removed and RuntimeInstallError raised.
    `client` is an `httpx2.Client`-compatible object (tests pass one with a MockTransport); by
    default one is created. `progress(message)` is told about each step.
    """
    say = progress or (lambda message: None)
    target = runtime_dir(settings)
    try:
        check_runtime(settings)
    except RuntimeUnavailableError:
        pass
    else:
        say(f"Pyodide {PYODIDE_VERSION} is already installed at {target}")
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.parent / f".{target.name}.partial-{os.getpid()}-{time.time_ns()}"
    staging.mkdir()
    try:
        if client is None:
            with httpx2.Client() as own_client:
                _install_into(own_client, staging, say)
        else:
            _install_into(client, staging, say)
        _replace_dir(staging, target)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    say(f"Installed Pyodide {PYODIDE_VERSION} at {target}")
    return target


def _install_into(client: Any, staging: Path, say: Callable[[str], None]) -> None:
    sizes: dict[str, int] = {}
    say(f"Downloading Pyodide {PYODIDE_VERSION} from {CDN_URL}")
    sizes[LOCK_FILE] = _download(client, LOCK_FILE, CORE_FILES[LOCK_FILE], staging / LOCK_FILE)
    try:
        lock = json.loads((staging / LOCK_FILE).read_text(encoding="utf-8"))
    except ValueError as exc:  # cannot happen with the pinned hash, but say so if it does
        raise RuntimeInstallError(f"{LOCK_FILE} is not JSON ({exc})") from exc
    wheels = package_files(lock, ALLOWED_PACKAGES)
    wanted = {name: digest for name, digest in CORE_FILES.items() if name != LOCK_FILE}
    wanted.update(wheels)
    for number, (name, digest) in enumerate(sorted(wanted.items()), start=1):
        say(f"[{number}/{len(wanted)}] {name}")
        sizes[name] = _download(client, name, digest, staging / name)
    digests = {**wanted, LOCK_FILE: CORE_FILES[LOCK_FILE]}
    manifest = {
        "version": PYODIDE_VERSION,
        "source": CDN_URL,
        "packages": package_closure(lock, ALLOWED_PACKAGES),
        "requested": list(ALLOWED_PACKAGES),
        "files": {
            name: {"size": sizes[name], "sha256": digests[name]} for name in sorted(sizes)
        },
    }
    total = sum(sizes.values())
    say(f"Verified {len(sizes)} files ({total / 1_000_000:.1f} MB)")
    (staging / MANIFEST_FILE).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def _replace_dir(staging: Path, target: Path) -> None:
    """Put `staging` at `target`; an existing (incomplete or old) `target` is removed."""
    if target.exists():
        old = target.parent / f".{target.name}.old-{os.getpid()}-{time.time_ns()}"
        target.rename(old)
        staging.rename(target)
        shutil.rmtree(old, ignore_errors=True)
    else:
        staging.rename(target)


# -- running Python under Node ----------------------------------------------------------------


@dataclass(frozen=True)
class RunResult:
    """How the entry ran. `figures` is the number of matplotlib figures it left open (saved as
    PNG by the driver; only the browser gets the images)."""

    ok: bool
    stdout: str
    stderr: str
    error: str | None
    timed_out: bool
    figures: int


@dataclass(frozen=True)
class CheckResult:
    passed: bool
    output: str
    error: str | None


@dataclass(frozen=True)
class LabRun:
    run: RunResult
    checks: tuple[CheckResult, ...]


def build_request(
    files: Mapping[str, str], entry: str, checks: Sequence[str], packages: Sequence[str]
) -> dict[str, Any]:
    """The driver's request; ValueError when it is malformed (the driver checks the same)."""
    if isinstance(checks, str) or isinstance(packages, str):
        raise ValueError("checks and packages must be sequences of strings, not a string")
    if not files:
        raise ValueError("a lab program needs at least one file")
    for name, source in files.items():
        if not isinstance(name, str) or not FILE_NAME.match(name):
            raise ValueError(f"file name {name!r} must match {FILE_NAME.pattern}")
        if not isinstance(source, str):
            raise ValueError(f"the source of {name} must be a string")
    if entry not in files:
        raise ValueError(f"the entry {entry!r} is not one of the files ({', '.join(files)})")
    if not all(isinstance(check, str) for check in checks):
        raise ValueError("every check must be a string of Python code")
    unknown = sorted(set(packages) - set(ALLOWED_PACKAGES))
    if unknown:
        raise ValueError(
            f"package(s) {', '.join(map(str, unknown))} are not available; labs can use "
            f"{', '.join(ALLOWED_PACKAGES)}"
        )
    return {
        "files": dict(files),
        "entry": entry,
        "checks": list(checks),
        "packages": list(dict.fromkeys(packages)),
    }


def timeout_message(seconds: float, phase: str) -> str:
    """The reader-facing error of a run stopped at the time limit (`phase` "entry" or
    "checks"); lab_sandbox.js words it the same way."""
    if phase == "checks":
        return f"The checks did not finish within {seconds:g} seconds and were stopped."
    return f"The program did not finish within {seconds:g} seconds and was stopped."


def timed_out_result(events: Sequence[Mapping[str, Any]], checks: int, seconds: float) -> LabRun:
    """The result of a run killed at the time limit, from the driver's events seen until then:
    the output so far, and every check failed."""
    output = {"stdout": [], "stderr": []}
    phase = "entry"
    for event in events:
        if event.get("event") == "output" and event.get("stream") in output:
            output[event["stream"]].append(str(event.get("text", "")))
        elif event.get("event") == "phase":
            phase = str(event.get("phase"))
    run = RunResult(
        ok=False,
        stdout="".join(output["stdout"]),
        stderr="".join(output["stderr"]),
        error=timeout_message(seconds, phase),
        timed_out=True,
        figures=0,
    )
    check_error = CHECKS_TIMED_OUT if phase == "checks" else NOT_RUN
    return LabRun(run, tuple(CheckResult(False, "", check_error) for _ in range(checks)))


def parse_result(result: Any, checks: int) -> LabRun:
    """A `LabRun` from the runner's `result` message; RunnerError if it is malformed."""

    def bad(what: str) -> RunnerError:
        return RunnerError(f"The Node runner returned a malformed result ({what})")

    if not isinstance(result, dict) or not isinstance(result.get("run"), dict):
        raise bad("no run")
    run = result["run"]
    for key in ("stdout", "stderr"):
        if not isinstance(run.get(key), str):
            raise bad(f"run.{key}")
    if not isinstance(run.get("ok"), bool):
        raise bad("run.ok")
    if run.get("error") is not None and not isinstance(run["error"], str):
        raise bad("run.error")
    if run["ok"] != (run.get("error") is None):
        raise bad("run.ok disagrees with run.error")
    figures = run.get("figures")
    if isinstance(figures, bool) or not isinstance(figures, int) or figures < 0:
        raise bad("run.figures")
    items = result.get("checks")
    if not isinstance(items, list) or len(items) != checks:
        raise bad(f"expected {checks} check result(s)")
    parsed = []
    for item in items:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("passed"), bool)
            or not isinstance(item.get("output"), str)
            or not (item.get("error") is None or isinstance(item["error"], str))
            or item["passed"] != (item.get("error") is None)
        ):
            raise bad("a check result")
        parsed.append(CheckResult(item["passed"], item["output"], item["error"]))
    return LabRun(
        RunResult(run["ok"], run["stdout"], run["stderr"], run["error"], False, figures),
        tuple(parsed),
    )


def _parse_node_version(text: str) -> tuple[int, ...]:
    match = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)\S*", text.strip())
    if not match:
        raise RuntimeUnavailableError(f"`node --version` printed {text.strip()!r}, not a version")
    return tuple(int(part) for part in match.groups())


class PythonRunner:
    """Runs lab programs in Pyodide under Node (one confined `node` process per `execute`).

    `timeout` is the wall-clock limit in seconds for the program and its checks; it starts when
    Pyodide and the packages are loaded. `node` is the Node.js executable (a name on PATH or a
    path), version MIN_NODE_VERSION or newer.
    """

    def __init__(self, settings: Settings, *, timeout: float = 60.0, node: str = "node") -> None:
        if isinstance(timeout, bool) or not isinstance(timeout, int | float) or timeout <= 0:
            raise ValueError("timeout must be a positive number of seconds")
        self.settings = settings
        self.timeout = float(timeout)
        self.node = node
        self._node_path: str | None = None

    # -- availability ---------------------------------------------------------------------

    def _find_node(self) -> str:
        if self._node_path is not None:
            return self._node_path
        path = shutil.which(self.node)
        if path is None:
            raise RuntimeUnavailableError(
                f"Node.js ({self.node!r}) was not found; checking labs on the server needs "
                f"Node {MIN_NODE_VERSION[0]} or newer on PATH."
            )
        try:
            done = subprocess.run(
                [path, "--version"], capture_output=True, text=True, timeout=30, env={}
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeUnavailableError(f"{path} --version failed ({exc})") from exc
        if done.returncode != 0:
            raise RuntimeUnavailableError(
                f"{path} --version failed with status {done.returncode}: {done.stderr.strip()}"
            )
        version = _parse_node_version(done.stdout)
        if version[:2] < MIN_NODE_VERSION:
            raise RuntimeUnavailableError(
                f"{path} is Node {done.stdout.strip()}; labs need Node "
                f"{'.'.join(map(str, MIN_NODE_VERSION))} or newer (for its permission model "
                "and module hooks)."
            )
        self._node_path = path
        return path

    def check_available(self) -> None:
        """RuntimeUnavailableError unless Node (new enough), the runner files and the runtime
        are all there."""
        self._find_node()
        for path in (NODE_RUNNER, DRIVER_FILE):
            if not path.is_file():
                raise RuntimeUnavailableError(f"{path} is missing (a broken Teacher install)")
        check_runtime(self.settings)

    def node_command(self, runtime: Path, *, extra_read: Sequence[Path] = ()) -> list[str]:
        """The confined `node` command line, without the runner script and its arguments.
        `extra_read` adds readable paths (tests use it to run probe scripts confined the same
        way)."""
        readable = [runtime, NODE_RUNNER, DRIVER_FILE, *extra_read]
        for path in readable:
            if "," in str(path):
                raise RuntimeUnavailableError(f"{path} contains a comma, which Node's "
                                              "--allow-fs-read cannot take")
        return [
            self._find_node(),
            "--permission",
            *(f"--allow-fs-read={path}" for path in readable),
            "--disallow-code-generation-from-strings",
            f"--max-old-space-size={JS_HEAP_LIMIT_MB}",
            "--disable-warning=ExperimentalWarning",
        ]

    # -- running --------------------------------------------------------------------------

    def execute(
        self,
        files: Mapping[str, str],
        *,
        entry: str,
        checks: Sequence[str] = (),
        packages: Sequence[str] = (),
    ) -> LabRun:
        """Run `entry` (with the other `files` importable), then each check; see the module
        docstring. ValueError for a malformed request, RuntimeUnavailableError when the runtime
        or Node is missing, RunnerError when the runner itself fails."""
        request = build_request(files, entry, checks, packages)
        self.check_available()
        runtime = runtime_dir(self.settings)
        command = [
            *self.node_command(runtime),
            str(NODE_RUNNER),
            str(runtime),
            str(DRIVER_FILE),
            str(WASM_MEMORY_LIMIT_MB),
        ]
        return self._run(command, runtime, json.dumps(request), len(request["checks"]))

    def _run(self, command: list[str], cwd: Path, request_json: str, checks: int) -> LabRun:
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=cwd,
                env={},
            )
        except OSError as exc:
            raise RuntimeUnavailableError(f"{command[0]} could not be started ({exc})") from exc
        lines: queue.Queue[bytes | None] = queue.Queue()
        errors: list[bytes] = []
        readers = [
            threading.Thread(target=_read_lines, args=(process.stdout, lines), daemon=True),
            threading.Thread(target=_read_tail, args=(process.stderr, errors), daemon=True),
        ]
        for reader in readers:
            reader.start()
        try:
            assert process.stdin is not None
            try:
                process.stdin.write(request_json.encode("utf-8"))
                process.stdin.close()
            except BrokenPipeError:
                pass  # it died at once; the missing result below says how
            return self._collect(process, lines, errors, checks)
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
            for reader in readers:
                reader.join(timeout=5)

    def _collect(
        self,
        process: subprocess.Popen[bytes],
        lines: queue.Queue[bytes | None],
        errors: list[bytes],
        checks: int,
    ) -> LabRun:
        events: list[dict[str, Any]] = []
        deadline = time.monotonic() + STARTUP_TIMEOUT
        started = False
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                process.kill()
                if started:
                    return timed_out_result(events, checks, self.timeout)
                raise RunnerError(
                    f"Pyodide did not start within {STARTUP_TIMEOUT:g} seconds"
                    f"{_stderr_note(errors)}"
                )
            try:
                line = lines.get(timeout=remaining)
            except queue.Empty:
                continue
            if line is None:
                status = process.wait()
                raise RunnerError(
                    f"The Node runner exited (status {status}) without a result"
                    f"{_stderr_note(errors)}"
                )
            try:
                message = json.loads(line)
                kind = message["type"]
            except (ValueError, KeyError, TypeError) as exc:
                raise RunnerError(
                    f"The Node runner wrote an unreadable line ({exc}): {line[:200]!r}"
                ) from exc
            if kind == "ready":
                started = True
                deadline = time.monotonic() + self.timeout
            elif kind == "event":
                events.append(message.get("event") or {})
            elif kind == "result":
                return parse_result(message.get("result"), checks)
            elif kind == "fatal":
                raise RunnerError(f"The Node runner failed: {message.get('error')}")
            else:
                raise RunnerError(f"The Node runner sent an unknown message {kind!r}")


MAX_STDERR_BYTES = 20_000


def _read_lines(stream: Any, lines: queue.Queue[bytes | None]) -> None:
    try:
        for line in stream:
            lines.put(line)
    finally:
        lines.put(None)


def _read_tail(stream: Any, errors: list[bytes]) -> None:
    """Keep the last MAX_STDERR_BYTES of the process's stderr."""
    kept = b""
    for chunk in iter(lambda: stream.read1(4096), b""):
        kept = (kept + chunk)[-MAX_STDERR_BYTES:]
        errors[:] = [kept]


def _stderr_note(errors: list[bytes]) -> str:
    text = b"".join(errors).decode("utf-8", "replace").strip()
    return f"; its error output:\n{text}" if text else " (and printed nothing on stderr)"


__all__ = [
    "ALLOWED_PACKAGES",
    "CDN_URL",
    "CORE_FILES",
    "DRIVER_FILE",
    "ENV_PYODIDE_DIR",
    "NODE_RUNNER",
    "PYODIDE_VERSION",
    "CheckResult",
    "LabRun",
    "PythonRunner",
    "RunResult",
    "RunnerError",
    "RuntimeInstallError",
    "RuntimeUnavailableError",
    "check_runtime",
    "install_runtime",
    "package_closure",
    "runtime_dir",
    "runtime_files",
]
