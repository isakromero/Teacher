"""The lab runtime: install (MockTransport, no network), lookup, the runner's protocol (fake
`node` scripts), the shared driver (run under CPython), and real runs through Node and Pyodide.

The integration tests at the end need an installed runtime: they run when $TEACHER_PYODIDE_DIR
points at one (made with `teacher labs setup`) and Node is on PATH, and are skipped otherwise.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import socket
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import httpx2
import pytest

from teacher import pyodide_runtime as rt
from teacher.config import Settings
from teacher.pyodide_runtime import (
    ENV_PYODIDE_DIR,
    MANIFEST_FILE,
    PYODIDE_VERSION,
    CheckResult,
    LabRun,
    PythonRunner,
    RunnerError,
    RunResult,
    RuntimeInstallError,
    RuntimeUnavailableError,
    check_runtime,
    install_runtime,
    package_closure,
    runtime_dir,
)

STATIC_JS = Path(rt.__file__).parent / "web" / "static" / "js"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Settings on a temp root, with $TEACHER_PYODIDE_DIR unset."""
    monkeypatch.delenv(ENV_PYODIDE_DIR, raising=False)
    return Settings(tmp_path / "home")


def write_fake_runtime(directory: Path, **manifest_overrides: Any) -> Path:
    """A directory `check_runtime` accepts (fake file contents, a correct manifest)."""
    directory.mkdir(parents=True, exist_ok=True)
    files = {}
    for name in ("pyodide-lock.json", "pyodide.mjs", "pyodide.asm.wasm", "numpy-1.whl"):
        data = f"fake {name}".encode()
        (directory / name).write_bytes(data)
        files[name] = {"size": len(data), "sha256": sha(data)}
    manifest = {"version": PYODIDE_VERSION, "packages": ["matplotlib", "numpy", "six"],
                "files": files, **manifest_overrides}
    (directory / MANIFEST_FILE).write_text(json.dumps(manifest), encoding="utf-8")
    return directory


# -- the lock file ----------------------------------------------------------------------------


def fake_lock() -> dict[str, Any]:
    def entry(name: str, depends: list[str], data: bytes) -> dict[str, Any]:
        return {"name": name, "depends": depends, "file_name": f"{name}-1.0-py3-none-any.whl",
                "sha256": sha(data)}

    return {
        "info": {"version": PYODIDE_VERSION},
        "packages": {
            "numpy": entry("numpy", [], b"wheel numpy"),
            "matplotlib": entry("matplotlib", ["numpy", "cycler"], b"wheel matplotlib"),
            "cycler": entry("cycler", ["six"], b"wheel cycler"),
            "six": entry("six", [], b"wheel six"),
            "scipy": entry("scipy", ["numpy"], b"wheel scipy"),
        },
    }


def test_package_closure_is_transitive_and_sorted() -> None:
    lock = fake_lock()
    assert package_closure(lock, ["matplotlib"]) == ["cycler", "matplotlib", "numpy", "six"]
    assert package_closure(lock, ["numpy"]) == ["numpy"]
    assert package_closure(lock, ["numpy", "matplotlib", "numpy"]) == [
        "cycler", "matplotlib", "numpy", "six"]


def test_package_closure_names_the_missing_dependency() -> None:
    lock = fake_lock()
    lock["packages"]["cycler"]["depends"] = ["nope"]
    with pytest.raises(RuntimeInstallError, match=r"no package 'nope' \(a dependency of cycler\)"):
        package_closure(lock, ["matplotlib"])
    with pytest.raises(RuntimeInstallError, match="no package 'pandas'$"):
        package_closure(lock, ["pandas"])
    with pytest.raises(RuntimeInstallError, match="no 'packages' table"):
        package_closure({}, ["numpy"])


def test_package_files_rejects_bad_entries() -> None:
    lock = fake_lock()
    assert rt.package_files(lock, ["numpy"]) == {
        "numpy-1.0-py3-none-any.whl": sha(b"wheel numpy")}
    lock["packages"]["numpy"]["sha256"] = "abc"
    with pytest.raises(RuntimeInstallError, match="bad sha256"):
        rt.package_files(lock, ["numpy"])
    lock = fake_lock()
    lock["packages"]["numpy"]["file_name"] = "../numpy.whl"
    with pytest.raises(RuntimeInstallError, match="bad file_name"):
        rt.package_files(lock, ["numpy"])


# -- installing (MockTransport) ---------------------------------------------------------------


class FakeCdn:
    """Serves the fake release; `corrupt` names files served with wrong bytes, `missing` 404s."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.lock_bytes = json.dumps(fake_lock()).encode()
        self.files = {rt.LOCK_FILE: self.lock_bytes}
        for name in ("pyodide.mjs", "pyodide.asm.mjs", "pyodide.asm.wasm", "python_stdlib.zip"):
            self.files[name] = f"core {name}".encode()
        for name in ("numpy", "matplotlib", "cycler", "six", "scipy"):
            self.files[f"{name}-1.0-py3-none-any.whl"] = f"wheel {name}".encode()
        monkeypatch.setattr(rt, "CORE_FILES", {
            name: sha(self.files[name])
            for name in (rt.LOCK_FILE, "pyodide.mjs", "pyodide.asm.mjs", "pyodide.asm.wasm",
                         "python_stdlib.zip")
        })
        self.requests: list[str] = []
        self.corrupt: set[str] = set()
        self.missing: set[str] = set()

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        url = str(request.url)
        assert url.startswith(rt.CDN_URL), url
        name = url[len(rt.CDN_URL):]
        self.requests.append(name)
        if name in self.missing or name not in self.files:
            return httpx2.Response(404)
        body = self.files[name] + (b"tampered" if name in self.corrupt else b"")
        return httpx2.Response(200, content=body)

    def client(self) -> httpx2.Client:
        return httpx2.Client(transport=httpx2.MockTransport(self.handler))


def test_install_downloads_verifies_and_writes_a_manifest(home: Settings, monkeypatch) -> None:
    cdn = FakeCdn(monkeypatch)
    messages: list[str] = []
    path = install_runtime(home, client=cdn.client(), progress=messages.append)
    assert path == home.derived_dir / "pyodide" / PYODIDE_VERSION
    assert check_runtime(home) == path
    expected = {"pyodide-lock.json", "pyodide.mjs", "pyodide.asm.mjs", "pyodide.asm.wasm",
                "python_stdlib.zip", "numpy-1.0-py3-none-any.whl",
                "matplotlib-1.0-py3-none-any.whl", "cycler-1.0-py3-none-any.whl",
                "six-1.0-py3-none-any.whl"}
    assert sorted(cdn.requests) == sorted(expected)  # not scipy: only the closure
    assert {p.name for p in path.iterdir()} == expected | {MANIFEST_FILE}
    manifest = json.loads((path / MANIFEST_FILE).read_text())
    assert manifest["version"] == PYODIDE_VERSION
    assert manifest["packages"] == ["cycler", "matplotlib", "numpy", "six"]
    assert manifest["files"]["six-1.0-py3-none-any.whl"] == {
        "size": len(b"wheel six"), "sha256": sha(b"wheel six")}
    assert messages[0].startswith(f"Downloading Pyodide {PYODIDE_VERSION}")
    assert messages[-1] == f"Installed Pyodide {PYODIDE_VERSION} at {path}"
    assert rt.runtime_files(home) == frozenset(expected)
    assert [p.name for p in path.parent.iterdir()] == [PYODIDE_VERSION]  # no staging left

    # Already installed: nothing is downloaded.
    cdn.requests.clear()
    messages.clear()
    assert install_runtime(home, client=cdn.client(), progress=messages.append) == path
    assert cdn.requests == []
    assert messages == [f"Pyodide {PYODIDE_VERSION} is already installed at {path}"]


@pytest.mark.parametrize("corrupt", ["six-1.0-py3-none-any.whl", "pyodide.asm.wasm",
                                     "pyodide-lock.json"])
def test_a_hash_mismatch_installs_nothing(home: Settings, monkeypatch, corrupt: str) -> None:
    cdn = FakeCdn(monkeypatch)
    cdn.corrupt.add(corrupt)
    with pytest.raises(RuntimeInstallError, match=rf"{corrupt} does not match its pinned sha256"):
        install_runtime(home, client=cdn.client())
    parent = home.derived_dir / "pyodide"
    assert list(parent.iterdir()) == []  # neither the target nor a staging directory
    with pytest.raises(RuntimeUnavailableError):
        check_runtime(home)


def test_a_failed_download_keeps_an_existing_directory_and_installs_nothing(
    home: Settings, monkeypatch
) -> None:
    cdn = FakeCdn(monkeypatch)
    cdn.missing.add("numpy-1.0-py3-none-any.whl")
    target = runtime_dir(home)
    target.mkdir(parents=True)
    (target / "leftover.txt").write_text("from an interrupted copy")
    with pytest.raises(RuntimeInstallError, match="numpy-1.0-py3-none-any.whl answered with HTTP 404"):
        install_runtime(home, client=cdn.client())
    assert [p.name for p in target.iterdir()] == ["leftover.txt"]
    assert [p.name for p in target.parent.iterdir()] == [PYODIDE_VERSION]

    # A later successful install replaces the incomplete directory.
    cdn.missing.clear()
    install_runtime(home, client=cdn.client())
    assert not (target / "leftover.txt").exists()
    assert check_runtime(home) == target
    assert [p.name for p in target.parent.iterdir()] == [PYODIDE_VERSION]


def test_transport_errors_are_install_errors(home: Settings, monkeypatch) -> None:
    FakeCdn(monkeypatch)

    def refuse(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("no route", request=request)

    client = httpx2.Client(transport=httpx2.MockTransport(refuse))
    with pytest.raises(RuntimeInstallError, match="could not be downloaded .ConnectError"):
        install_runtime(home, client=client)
    assert list((home.derived_dir / "pyodide").iterdir()) == []


# -- finding and checking the runtime -------------------------------------------------------------


def test_runtime_dir_default_and_override(home: Settings, monkeypatch, tmp_path: Path) -> None:
    assert runtime_dir(home) == home.derived_dir / "pyodide" / PYODIDE_VERSION
    monkeypatch.setenv(ENV_PYODIDE_DIR, str(tmp_path / "shared"))
    assert runtime_dir(home) == (tmp_path / "shared").resolve()
    monkeypatch.setenv(ENV_PYODIDE_DIR, "  ")
    with pytest.raises(ValueError, match="TEACHER_PYODIDE_DIR is set but empty"):
        runtime_dir(home)


def test_check_runtime_says_what_is_wrong(home: Settings) -> None:
    target = runtime_dir(home)
    with pytest.raises(RuntimeUnavailableError) as missing:
        check_runtime(home)
    assert "is not installed" in str(missing.value)
    assert "`teacher labs setup`" in str(missing.value)
    assert "$TEACHER_PYODIDE_DIR" in str(missing.value)

    target.mkdir(parents=True)
    with pytest.raises(RuntimeUnavailableError, match=f"has no {MANIFEST_FILE}.*teacher labs setup"):
        check_runtime(home)

    (target / MANIFEST_FILE).write_text("{not json")
    with pytest.raises(RuntimeUnavailableError, match="is unreadable"):
        check_runtime(home)

    write_fake_runtime(target, version="0.27.0")
    with pytest.raises(RuntimeUnavailableError, match=f"holds Pyodide '0.27.0'.*needs {PYODIDE_VERSION}"):
        check_runtime(home)

    write_fake_runtime(target, packages=["numpy"])
    with pytest.raises(RuntimeUnavailableError, match="lacks the package.s. matplotlib"):
        check_runtime(home)

    write_fake_runtime(target)
    assert check_runtime(home) == target
    (target / "pyodide.mjs").write_bytes(b"changed!")
    with pytest.raises(RuntimeUnavailableError, match="pyodide.mjs has the wrong size"):
        check_runtime(home)
    (target / "pyodide.mjs").unlink()
    with pytest.raises(RuntimeUnavailableError, match="is missing pyodide.mjs"):
        check_runtime(home)


# -- requests and results --------------------------------------------------------------------------


def test_build_request_validates() -> None:
    files = {"main.py": "print(1)", "helper_2.py": ""}
    request = rt.build_request(files, "main.py", ["assert True"], ["numpy", "numpy"])
    assert request == {"files": files, "entry": "main.py", "checks": ["assert True"],
                       "packages": ["numpy"]}
    with pytest.raises(ValueError, match="at least one file"):
        rt.build_request({}, "main.py", [], [])
    for bad_name in ("Main.py", "main.txt", "../x.py", "dir/x.py", "1x.py"):
        with pytest.raises(ValueError, match="must match"):
            rt.build_request({bad_name: ""}, bad_name, [], [])
    with pytest.raises(ValueError, match="entry 'other.py' is not one of the files"):
        rt.build_request(files, "other.py", [], [])
    with pytest.raises(ValueError, match="scipy are not available"):
        rt.build_request(files, "main.py", [], ["scipy"])
    with pytest.raises(ValueError, match="not a string"):
        rt.build_request(files, "main.py", "assert True", [])
    with pytest.raises(ValueError, match="source of main.py must be a string"):
        rt.build_request({"main.py": b"x"}, "main.py", [], [])  # type: ignore[dict-item]


def test_parse_result_and_its_malformed_cases() -> None:
    good = {"run": {"ok": True, "stdout": "hi\n", "stderr": "", "error": None, "figures": 2},
            "checks": [{"passed": False, "output": "", "error": "AssertionError"}]}
    assert rt.parse_result(good, 1) == LabRun(
        RunResult(True, "hi\n", "", None, False, 2),
        (CheckResult(False, "", "AssertionError"),),
    )
    bad_cases = [
        None,
        {"run": {**good["run"], "ok": "yes"}, "checks": good["checks"]},
        {"run": {**good["run"], "error": "boom"}, "checks": good["checks"]},
        {"run": {**good["run"], "figures": ["png"]}, "checks": good["checks"]},
        {"run": good["run"], "checks": []},
        {"run": good["run"], "checks": [{"passed": True, "output": "", "error": "x"}]},
    ]
    for bad in bad_cases:
        with pytest.raises(RunnerError, match="malformed result"):
            rt.parse_result(bad, 1)


def test_timed_out_result_keeps_the_output_so_far() -> None:
    events = [{"event": "output", "stream": "stdout", "text": "1\n"},
              {"event": "output", "stream": "stderr", "text": "warn\n"},
              {"event": "output", "stream": "stdout", "text": "2\n"}]
    result = rt.timed_out_result(events, 2, 1.5)
    assert result.run == RunResult(
        False, "1\n2\n", "warn\n",
        "The program did not finish within 1.5 seconds and was stopped.", True, 0)
    assert result.checks == (CheckResult(False, "", "the program did not run"),) * 2
    in_checks = rt.timed_out_result([*events, {"event": "phase", "phase": "checks"}], 1, 20)
    assert in_checks.run.error == "The checks did not finish within 20 seconds and were stopped."
    assert in_checks.checks == (CheckResult(False, "", "the checks did not finish in time"),)


def test_the_browser_side_mirrors_the_shared_texts() -> None:
    frame = (STATIC_JS / "lab_sandbox_frame.js").read_text(encoding="utf-8")
    assert f'NOT_RUN = "{rt.NOT_RUN}"' in frame
    assert f'CHECKS_TIMED_OUT = "{rt.CHECKS_TIMED_OUT}"' in frame
    assert f"WASM_MEMORY_LIMIT_MB = {rt.WASM_MEMORY_LIMIT_MB}" in frame
    assert 'ALLOWED_PACKAGES = Object.freeze(["numpy", "matplotlib"])' in frame
    assert "did not finish within ${limit} seconds and was stopped." in frame
    driver = load_driver()
    assert driver.NOT_RUN == rt.NOT_RUN
    assert driver.ALLOWED_PACKAGES == rt.ALLOWED_PACKAGES
    assert driver.FILE_NAME.pattern == rt.FILE_NAME.pattern


# -- the runner's protocol, with fake `node` executables -----------------------------------------


@pytest.fixture
def fake_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv(ENV_PYODIDE_DIR, str(write_fake_runtime(tmp_path / "runtime")))
    return Settings(tmp_path / "home")


def fake_node(tmp_path: Path, body: str, version: str = "v24.1.0") -> str:
    """An executable that answers --version with `version` and otherwise runs `body` (Python,
    with `request` the parsed stdin and `send(obj)` writing one JSON line)."""
    script = tmp_path / f"fake-node-{len(list(tmp_path.iterdir()))}"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, sys, time\n"
        f"if sys.argv[1:] == ['--version']:\n    print({version!r}); sys.exit(0)\n"
        "request = json.loads(sys.stdin.read())\n"
        "def send(obj):\n    print(json.dumps(obj), flush=True)\n"
        + body + "\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


FILES = {"main.py": "print('hi')"}


def test_a_missing_node_is_unavailable(fake_runtime: Settings) -> None:
    runner = PythonRunner(fake_runtime, node="no-such-node-binary-xyz")
    with pytest.raises(RuntimeUnavailableError, match="'no-such-node-binary-xyz'.* was not found"):
        runner.check_available()
    with pytest.raises(RuntimeUnavailableError, match="was not found"):
        runner.execute(FILES, entry="main.py")


def test_an_old_node_is_unavailable(fake_runtime: Settings, tmp_path: Path) -> None:
    runner = PythonRunner(fake_runtime, node=fake_node(tmp_path, "", version="v20.11.0"))
    with pytest.raises(RuntimeUnavailableError, match="is Node v20.11.0; labs need Node 24.0 or newer"):
        runner.check_available()


def test_a_missing_runtime_is_unavailable(home: Settings, tmp_path: Path) -> None:
    runner = PythonRunner(home, node=fake_node(tmp_path, "send({'type': 'ready'})"))
    with pytest.raises(RuntimeUnavailableError, match="teacher labs setup"):
        runner.execute(FILES, entry="main.py")


def test_a_bad_request_fails_before_node_is_started(fake_runtime: Settings, tmp_path: Path) -> None:
    runner = PythonRunner(fake_runtime, node="no-such-node-binary-xyz")
    with pytest.raises(ValueError, match="entry"):
        runner.execute(FILES, entry="other.py")
    with pytest.raises(ValueError, match="timeout"):
        PythonRunner(fake_runtime, timeout=0)


def test_the_runner_protocol_result(fake_runtime: Settings, tmp_path: Path) -> None:
    node = fake_node(tmp_path, """
assert request["entry"] == "main.py" and request["checks"] == ["assert True"], request
assert sys.argv[-4].endswith("node_runner.mjs") and sys.argv[-1] == "1024", sys.argv
assert "--permission" in sys.argv and "--disallow-code-generation-from-strings" in sys.argv
import os
assert os.environ.get("HOME") is None, "the runner must get an empty environment"
send({"type": "ready"})
send({"type": "event", "event": {"event": "output", "stream": "stdout", "text": "hi\\n"}})
send({"type": "result", "result": {"run": {"ok": True, "stdout": "hi\\n", "stderr": "",
      "error": None, "figures": 1}, "checks": [{"passed": True, "output": "", "error": None}]}})
""")
    result = PythonRunner(fake_runtime, node=node).execute(
        FILES, entry="main.py", checks=["assert True"], packages=["numpy"])
    assert result == LabRun(RunResult(True, "hi\n", "", None, False, 1),
                            (CheckResult(True, "", None),))


@pytest.mark.parametrize(("body", "message"), [
    ("sys.stderr.write('kaboom'); sys.exit(3)",
     r"exited \(status 3\) without a result; its error output:\nkaboom"),
    ("sys.exit(0)", r"exited \(status 0\) without a result \(and printed nothing on stderr\)"),
    ("print('not json', flush=True); time.sleep(30)", "wrote an unreadable line"),
    ("send({'type': 'fatal', 'error': 'pyodide.asm.wasm: bad magic'})",
     "The Node runner failed: pyodide.asm.wasm: bad magic"),
    ("send({'type': 'ready'}); send({'type': 'surprise'})", "unknown message 'surprise'"),
    ("send({'type': 'ready'}); send({'type': 'result', 'result': {'run': {}}})",
     "malformed result"),
])
def test_runner_failures_are_runner_errors(
    fake_runtime: Settings, tmp_path: Path, body: str, message: str
) -> None:
    runner = PythonRunner(fake_runtime, node=fake_node(tmp_path, body))
    started = time.monotonic()
    with pytest.raises(RunnerError, match=message):
        runner.execute(FILES, entry="main.py")
    assert time.monotonic() - started < 10  # the process is killed, not waited for


def test_the_time_limit_starts_at_ready_and_kills(fake_runtime: Settings, tmp_path: Path) -> None:
    pid_file = tmp_path / "pid"
    node = fake_node(tmp_path, f"""
import os
open({str(pid_file)!r}, "w").write(str(os.getpid()))
time.sleep(0.8)  # start-up: not counted
send({{"type": "ready"}})
send({{"type": "event", "event": {{"event": "output", "stream": "stdout", "text": "so far\\n"}}}})
time.sleep(60)
""")
    runner = PythonRunner(fake_runtime, timeout=0.5, node=node)
    started = time.monotonic()
    result = runner.execute(FILES, entry="main.py", checks=["pass"])
    assert 1.2 < time.monotonic() - started < 10
    assert result.run == RunResult(
        False, "so far\n", "", "The program did not finish within 0.5 seconds and was stopped.",
        True, 0)
    assert result.checks == (CheckResult(False, "", "the program did not run"),)
    pid = int(pid_file.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_a_start_up_that_never_ends(fake_runtime: Settings, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(rt, "STARTUP_TIMEOUT", 0.5)
    runner = PythonRunner(fake_runtime, node=fake_node(tmp_path, "time.sleep(60)"))
    with pytest.raises(RunnerError, match="Pyodide did not start within 0.5 seconds"):
        runner.execute(FILES, entry="main.py")


# -- the driver, under CPython ---------------------------------------------------------------------


def load_driver():
    spec = importlib.util.spec_from_file_location("lab_driver_under_test", rt.DRIVER_FILE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def drive(files: dict[str, str], entry: str = "main.py", checks=(), events=None):
    driver = load_driver()
    request = json.dumps({"files": files, "entry": entry, "checks": list(checks), "packages": []})
    emit = None if events is None else (lambda event: events.append(json.loads(event)))
    return json.loads(driver.run_lab(request, emit))


def test_driver_runs_the_entry_as_main_with_the_other_files_importable() -> None:
    before = (os.getcwd(), list(sys.path), list(sys.argv))
    result = drive({
        "main.py": "import sys, os\nfrom helper import double\nprint(double(21), __name__)\n"
                   "print(sys.argv, os.path.basename(os.getcwd()).startswith('lab-'))\n"
                   "print('careful', file=sys.stderr)\n",
        "helper.py": "def double(x):\n    return 2 * x\n",
    })
    assert result["run"] == {"ok": True, "stdout": "42 __main__\n['main.py'] True\n",
                             "stderr": "careful\n", "error": None, "figures": []}
    assert result["checks"] == []
    assert (os.getcwd(), sys.path, sys.argv) == before
    assert "helper" not in sys.modules


def test_driver_error_messages_have_no_driver_frames() -> None:
    result = drive({"main.py": "from helper import f\nf()\n",
                    "helper.py": "def f():\n    return 1 / 0\n"})
    error = result["run"]["error"]
    assert result["run"]["ok"] is False
    assert error.startswith("Traceback (most recent call last):\n  File \"main.py\", line 2")
    assert '  File "helper.py", line 2, in f\n    return 1 / 0' in error
    assert error.endswith("ZeroDivisionError: division by zero")
    for driver_part in ("lab_driver", "run_lab", "_run_entry", "frozen", "/lab-"):
        assert driver_part not in error

    syntax = drive({"main.py": "x = = 1\n"})["run"]["error"]
    assert syntax == '  File "main.py", line 1\n    x = = 1\n        ^\nSyntaxError: invalid syntax'

    imported = drive({"main.py": "import helper\n", "helper.py": "def (:\n"})["run"]["error"]
    assert 'File "main.py", line 1, in <module>' in imported
    assert 'File "helper.py", line 1' in imported and "importlib" not in imported


def test_driver_checks_run_fresh_and_fail_with_the_message() -> None:
    files = {"main.py": "import state\nstate.counter += 1\nprint('main ran')\n",
             "state.py": "counter = 0\n"}
    checks = [
        "import state\nassert state.counter == 0\nstate.counter = 99\nprint('checked')",
        "import state\nassert state.counter == 0, f'counter is {state.counter}'",
        "assert 1 == 2, 'one is not two'",
        "x = undefined_name",
        "import sys\nsys.exit(0)",
    ]
    result = drive(files, checks=checks)
    assert result["run"]["ok"] is True
    first, second, third, fourth, fifth = result["checks"]
    assert first == {"passed": True, "output": "checked\n", "error": None}
    assert second == {"passed": True, "output": "", "error": None}  # module state is fresh
    assert third["passed"] is False and third["output"] == ""
    assert third["error"].startswith(
        'Traceback (most recent call last):\n  File "<check 3>", line 1, in <module>\n'
        "    assert 1 == 2, 'one is not two'\n")
    assert third["error"].endswith("\nAssertionError: one is not two")
    assert len(third["error"].splitlines()) == 5  # header, frame, source, caret, message
    assert fourth["error"].endswith("NameError: name 'undefined_name' is not defined")
    assert fifth["passed"] is True


def test_driver_checks_are_not_run_when_the_entry_fails() -> None:
    result = drive({"main.py": "raise ValueError('bad input')\n"}, checks=["pass", "pass"])
    assert result["run"]["error"].endswith("ValueError: bad input")
    assert result["checks"] == [{"passed": False, "output": "", "error": "the program did not run"}] * 2


def test_driver_system_exit() -> None:
    assert drive({"main.py": "import sys\nprint('x')\nsys.exit()\n"})["run"]["ok"] is True
    failed = drive({"main.py": "import sys\nsys.exit(2)\n"})["run"]
    assert failed["error"] == "The program exited with status 2 (SystemExit)."
    assert drive({"main.py": "raise SystemExit('stopping')"})["run"]["error"] == (
        "The program exited: stopping")


def test_driver_emits_output_and_phase_and_caps_output(monkeypatch) -> None:
    events: list[dict] = []
    result = drive({"main.py": "print('a')\nimport sys\nprint('b', file=sys.stderr)\n"},
                   checks=["pass"], events=events)
    assert result["run"]["stdout"] == "a\n"
    outputs = [e for e in events if e["event"] == "output"]
    assert outputs[0] == {"event": "output", "stream": "stdout", "text": "a"}  # first: at once
    for stream, text in (("stdout", "a\n"), ("stderr", "b\n")):
        assert "".join(e["text"] for e in outputs if e["stream"] == stream) == text
    assert events[-1] == {"event": "phase", "phase": "checks"}

    driver = load_driver()
    driver.MAX_STREAM_CHARS = 50
    out = json.loads(driver.run_lab(json.dumps(
        {"files": {"main.py": "for i in range(100):\n    print('xxxxxxxxx')\n"},
         "entry": "main.py"})))["run"]["stdout"]
    assert out == "xxxxxxxxx\n" * 5 + "\n[output cut: more than 50 characters]\n"


def test_driver_rejects_bad_requests() -> None:
    driver = load_driver()
    for request, message in [
        ({"files": {"x.txt": ""}, "entry": "x.txt"}, "must match"),
        ({"files": {"main.py": ""}, "entry": "other.py"}, "not one of the files"),
        ({"files": {"main.py": ""}, "entry": "main.py", "packages": ["scipy"]}, "not available"),
        ({"files": {"main.py": ""}, "entry": "main.py", "extra": 1}, "unknown request field"),
    ]:
        with pytest.raises(ValueError, match=message):
            driver.run_lab(json.dumps(request))


# -- real runs through Node and Pyodide ------------------------------------------------------------


@pytest.fixture(scope="module")
def runtime_settings(tmp_path_factory: pytest.TempPathFactory) -> Settings:
    """Settings whose runtime is the install $TEACHER_PYODIDE_DIR points at; skips without it."""
    if not os.environ.get(ENV_PYODIDE_DIR):
        pytest.skip(f"no Pyodide runtime: set ${ENV_PYODIDE_DIR} to an install made with "
                    "`teacher labs setup` to run the integration tests")
    settings = Settings(tmp_path_factory.mktemp("runtime-home"))
    check_runtime(settings)  # set but broken is a failure, not a skip
    try:
        PythonRunner(settings)._find_node()
    except RuntimeUnavailableError as exc:
        pytest.skip(f"no usable Node.js: {exc}")
    return settings


@pytest.fixture
def runner(runtime_settings: Settings) -> PythonRunner:
    return PythonRunner(runtime_settings, timeout=30)


def test_hello_world(runner: PythonRunner) -> None:
    result = runner.execute({"main.py": "print('hello, world')"}, entry="main.py")
    assert result == LabRun(RunResult(True, "hello, world\n", "", None, False, 0), ())


def test_numpy(runner: PythonRunner) -> None:
    result = runner.execute(
        {"main.py": "import numpy as np\nfrom stats import mean\nprint(mean(np.arange(5)))\n",
         "stats.py": "import numpy as np\n\ndef mean(x):\n    return float(np.mean(x))\n"},
        entry="main.py", packages=["numpy"])
    assert result.run.ok, result.run.error
    assert result.run.stdout == "2.0\n"


def test_matplotlib_figures_are_counted(runner: PythonRunner) -> None:
    result = runner.execute(
        {"main.py": "import matplotlib\nimport matplotlib.pyplot as plt\n"
                    "print(matplotlib.get_backend())\n"
                    "plt.plot([1, 2, 3])\nplt.title('one')\nfig = plt.figure()\n"
                    "fig.gca().bar([1, 2], [3, 4])\nplt.figure()\nplt.close()\n"},
        entry="main.py", packages=["matplotlib"])
    assert result.run.ok, result.run.error
    assert result.run.stdout.strip().lower() == "agg"
    assert result.run.figures == 2
    assert result.run.stderr == ""  # no MatplotlibDeprecationWarning noise


def test_a_syntax_error(runner: PythonRunner) -> None:
    result = runner.execute({"main.py": "def f(:\n    pass\n"}, entry="main.py", checks=["pass"])
    assert result.run.ok is False
    assert result.run.error.startswith('  File "main.py", line 1\n    def f(:\n')
    assert "SyntaxError" in result.run.error
    assert result.checks == (CheckResult(False, "", "the program did not run"),)


def test_a_runtime_exception_is_short_and_has_no_driver_frames(runner: PythonRunner) -> None:
    result = runner.execute(
        {"main.py": "from model import step\nstep([1, 2])\n",
         "model.py": "import numpy as np\n\ndef step(x):\n    return np.array(x)[5]\n"},
        entry="main.py", packages=["numpy"])
    error = result.run.error
    assert error.startswith('Traceback (most recent call last):\n  File "main.py", line 2')
    assert 'File "model.py", line 4, in step' in error
    assert error.endswith("IndexError: index 5 is out of bounds for axis 0 with size 2")
    assert len(error.splitlines()) < 12
    for hidden in ("lab driver", "run_lab", "runpy", "frozen", "/tmp/lab-"):
        assert hidden not in error


def test_an_infinite_loop_hits_the_time_limit(runtime_settings: Settings) -> None:
    runner = PythonRunner(runtime_settings, timeout=2)
    started = time.monotonic()
    result = runner.execute(
        {"main.py": "print('starting', flush=True)\nwhile True:\n    pass\n"},
        entry="main.py", checks=["pass"])
    assert time.monotonic() - started < 20
    assert result.run.timed_out is True and result.run.ok is False
    assert result.run.error == "The program did not finish within 2 seconds and was stopped."
    assert result.run.stdout == "starting\n"
    assert result.checks == (CheckResult(False, "", "the program did not run"),)
    # A check that never ends is stopped the same way.
    result = runner.execute({"main.py": "x = 1\n"}, entry="main.py", checks=["while True: pass"])
    assert result.run.timed_out is True
    assert result.run.error == "The checks did not finish within 2 seconds and were stopped."
    assert result.checks == (CheckResult(False, "", "the checks did not finish in time"),)


def test_checks_pass_and_fail(runner: PythonRunner) -> None:
    files = {
        "main.py": "from attention import softmax\nprint(softmax([1.0, 2.0]).round(3))\n",
        "attention.py": "import numpy as np\n\ndef softmax(x):\n    e = np.exp(np.asarray(x))\n"
                        "    return e / e.sum()\n",
    }
    result = runner.execute(files, entry="main.py", packages=["numpy"], checks=[
        "from attention import softmax\nimport numpy as np\n"
        "assert np.isclose(softmax([1, 2, 3]).sum(), 1)\nprint('sums to one')",
        "from attention import softmax\nassert softmax([0, 0])[0] == 0.25, 'should be uniform'",
    ])
    assert result.run.ok, result.run.error
    assert result.run.stdout == "[0.269 0.731]\n"
    ok, failed = result.checks
    assert ok == CheckResult(True, "sums to one\n", None)
    assert failed.passed is False
    assert failed.error.endswith("AssertionError: should be uniform")
    assert '"<check 2>", line 2' in failed.error


def test_no_state_leaks_between_calls(runner: PythonRunner) -> None:
    first = runner.execute(
        {"main.py": "import builtins, os, sys\nbuiltins.LEAK = 1\nsys.LEAK = 1\n"
                    "open('/tmp/leak.txt', 'w').write('x')\nos.environ['LEAK'] = '1'\n"
                    "import helper\n", "helper.py": "VALUE = 1\n"},
        entry="main.py")
    assert first.run.ok, first.run.error
    second = runner.execute(
        {"main.py": "import builtins, os, sys\n"
                    "print(hasattr(builtins, 'LEAK'), hasattr(sys, 'LEAK'), "
                    "os.path.exists('/tmp/leak.txt'), 'LEAK' in os.environ, 'helper' in sys.modules)\n"},
        entry="main.py")
    assert second.run.stdout == "False False False False False\n"


def test_memory_is_capped(runner: PythonRunner) -> None:
    result = runner.execute({"main.py": "x = bytearray(1500 * 2**20)\n"}, entry="main.py")
    assert result.run.error.endswith("MemoryError")


def test_input_gets_end_of_file(runner: PythonRunner) -> None:
    result = runner.execute({"main.py": "input('name? ')\n"}, entry="main.py")
    assert result.run.error.endswith("EOFError: EOF when reading a line")


# -- confinement -----------------------------------------------------------------------------------


@pytest.fixture
def secret(tmp_path: Path) -> Path:
    path = tmp_path / "secret" / "notes.txt"
    path.parent.mkdir()
    path.write_text("CANARY-SECRET-1234\n", encoding="utf-8")
    return path


class Listener:
    """A local TCP server that records every connection made to it."""

    def __init__(self) -> None:
        self.socket = socket.socket()
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen()
        self.socket.settimeout(0.2)
        self.port = self.socket.getsockname()[1]
        self.connections: list[bytes] = []
        self.stopped = False
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        while not self.stopped:
            try:
                connection, _ = self.socket.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with connection:
                connection.settimeout(1)
                try:
                    self.connections.append(connection.recv(200))
                    connection.sendall(b"HTTP/1.0 200 OK\r\n\r\nok")
                except OSError:
                    self.connections.append(b"")

    def close(self) -> None:
        self.stopped = True
        self.thread.join(timeout=2)
        self.socket.close()


@pytest.fixture
def listener():
    server = Listener()
    yield server
    server.close()


def attempts(body: str) -> str:
    """A Python program that runs each `attempt(label, fn)` in `body` and prints one line per
    attempt: `label: OPEN <value>` if it worked, `label: blocked <error>` if it raised."""
    return (
        "import sys\n"
        "def attempt(label, fn):\n"
        "    try:\n"
        "        value = fn()\n"
        "        print(f'{label}: OPEN {str(value)[:100]}')\n"
        "    except BaseException as exc:\n"
        "        print(f'{label}: blocked {type(exc).__name__}: {str(exc)[:100]}')\n"
        "api = sys.modules['pyodide_js._api']\n"
        "pub = api.public_api\n" + body
    )


def lines(result: LabRun) -> dict[str, str]:
    assert result.run.ok, result.run.error
    return dict(line.split(": ", 1) for line in result.run.stdout.splitlines())


def test_python_cannot_reach_process_or_eval(runner: PythonRunner) -> None:
    result = runner.execute({"main.py": attempts(
        "import js\n"
        "attempt('js.process', lambda: js.process)\n"
        "attempt('js.require', lambda: js.require)\n"
        "attempt('js.fetch', lambda: js.fetch)\n"
        "attempt('import pyodide_js', lambda: __import__('pyodide_js'))\n"
        "attempt('Function constructor', lambda: api.constructor.constructor('return process')())\n"
        "from pyodide.code import run_js\n"
        "attempt('run_js', lambda: run_js('process'))\n"
        "attempt('subprocess', lambda: __import__('subprocess').run(['ls']))\n"
        "attempt('os.system', lambda: __import__('os').system('ls'))\n"
    )}, entry="main.py")
    outcome = lines(result)
    assert all(value.startswith("blocked") for value in outcome.values()), outcome
    assert "EvalError: Code generation from strings disallowed" in outcome["Function constructor"]
    assert outcome["os.system"] == "blocked OSError: [Errno 52] labs cannot start other programs"


def test_the_raw_system_call_is_refused_by_the_module_lock(runner: PythonRunner) -> None:
    """posix.system (behind the driver's os.system stub) makes emscripten require()
    child_process; the runner's module hooks refuse that, which ends the run as a runner
    failure (and the permission model would refuse the child process anyway)."""
    with pytest.raises(RunnerError, match=r"loading modules is disabled in labs \(node:child_process\)"):
        runner.execute({"main.py": "import posix\nposix.system('touch /tmp/pwned')\n"},
                       entry="main.py")


def test_python_cannot_read_files_outside_the_runtime(runner: PythonRunner, secret: Path) -> None:
    result = runner.execute({"main.py": attempts(
        f"path = {str(secret)!r}\n"
        "attempt('open', lambda: open(path).read())\n"
        "def mount():\n"
        f"    pub.mountNodeFS('/host', {str(secret.parent)!r})\n"
        "    return open('/host/notes.txt').read()\n"
        "attempt('mountNodeFS', mount)\n"
        "def mount_root():\n"
        "    pub.mountNodeFS('/root_fs', '/')\n"
        "    return open('/root_fs' + path).read()\n"
        "attempt('mountNodeFS /', mount_root)\n"
    )}, entry="main.py")
    outcome = lines(result)
    assert all(value.startswith("blocked") for value in outcome.values()), outcome
    assert "CANARY" not in result.run.stdout + result.run.stderr


def test_python_cannot_reach_the_network(runner: PythonRunner, listener: Listener) -> None:
    url = f"http://127.0.0.1:{listener.port}/x"
    result = runner.execute({"main.py": attempts(
        f"url = {url!r}\n"
        "import asyncio, socket, urllib.request\n"
        "attempt('socket', lambda: socket.create_connection(('127.0.0.1', "
        f"{listener.port}), timeout=2))\n"
        "attempt('urllib', lambda: urllib.request.urlopen(url, timeout=2).read())\n"
        "from pyodide.http import pyfetch, pyxhr\n"
        "attempt('pyxhr', lambda: pyxhr.get(url).text)\n"
        # Promise-returning calls: the program never yields to the event loop, but start them
        # anyway (a fetch would leave at once if one were reachable).
        "attempt('loadPackage url', lambda: pub.loadPackage(url + '/evil-1.0-py3-none-any.whl'))\n"
        "attempt('loadBinaryFile url', lambda: api.loadBinaryFile(url))\n"
        "attempt('useNodeSockFS', lambda: pub.useNodeSockFS())\n"
        "attempt('pyfetch', lambda: asyncio.ensure_future(pyfetch(url)))\n"
    )}, entry="main.py")
    outcome = lines(result)
    for label in ("socket", "urllib", "pyxhr"):
        assert outcome[label].startswith("blocked"), outcome
    time.sleep(0.5)
    assert listener.connections == []


def test_the_permission_flags_themselves(
    runner: PythonRunner, runtime_settings: Settings, tmp_path: Path, secret: Path,
    listener: Listener
) -> None:
    """Plain JavaScript under the runner's node flags (no Pyodide, no runner hardening): file
    reads outside the allowed paths, writes, child processes and workers are refused. The
    network is not: in Node 24 the permission model does not cover it, which is why the runner
    locks module loading and removes the network globals (tested above)."""
    probe = tmp_path / "probe.mjs"
    probe.write_text(f"""
import fs from "node:fs";
import child_process from "node:child_process";
import {{ Worker }} from "node:worker_threads";
import net from "node:net";
const out = {{}};
function attempt(label, fn) {{
  try {{ fn(); out[label] = "OPEN"; }} catch (e) {{ out[label] = "blocked " + e.code; }}
}}
attempt("read secret", () => fs.readFileSync({json.dumps(str(secret))}));
attempt("write file", () => fs.writeFileSync({json.dumps(str(tmp_path / "written.txt"))}, "x"));
attempt("child process", () => child_process.execFileSync("/bin/echo", ["hi"]));
attempt("worker", () => new Worker("setTimeout(() => {{}}, 1)", {{ eval: true }}));
const socket = net.connect({listener.port}, "127.0.0.1");
socket.on("connect", () => {{ out.network = "OPEN"; socket.end(); console.log(JSON.stringify(out)); }});
socket.on("error", (e) => {{ out.network = "blocked " + e.code; console.log(JSON.stringify(out)); }});
""", encoding="utf-8")
    runtime = runtime_dir(runtime_settings)
    command = [*runner.node_command(runtime, extra_read=[probe]), str(probe)]
    done = subprocess.run(command, capture_output=True, text=True, timeout=30, env={}, cwd=runtime)
    outcome = json.loads(done.stdout)
    assert outcome == {
        "read secret": "blocked ERR_ACCESS_DENIED",
        "write file": "blocked ERR_ACCESS_DENIED",
        "child process": "blocked ERR_ACCESS_DENIED",
        "worker": "blocked ERR_ACCESS_DENIED",
        "network": "OPEN",
    }, done.stderr
    assert not (tmp_path / "written.txt").exists()
