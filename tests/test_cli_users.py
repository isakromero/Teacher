"""`teacher users list|add|set-password|disable|enable`: the hidden prompt (replaced here), the
rules, the errors, and that no password is ever printed."""

from __future__ import annotations

import getpass
import warnings
from collections.abc import Callable
from typing import Any

import pytest

from teacher import cli
from teacher.config import Settings
from teacher.db.engine import session_scope
from teacher.managers import SignInError, UserManager

PASSWORD = "a long enough password"


@pytest.fixture
def home(settings: Settings, settings_env: Any) -> Settings:
    settings_env(settings)
    return settings


@pytest.fixture
def answers(monkeypatch: pytest.MonkeyPatch) -> Callable[..., list[str]]:
    """`answers(*typed)`: what the hidden prompt returns, in order; returns the prompts shown."""

    def install(*typed: str) -> list[str]:
        queue = list(typed)
        prompts: list[str] = []

        def prompt(text: str) -> str:
            prompts.append(text)
            if not queue:
                raise AssertionError(f"unexpected prompt {text!r}")
            return queue.pop(0)

        monkeypatch.setattr(cli, "prompt_password", prompt)
        return prompts

    return install


def _run(capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, str, str]:
    status = cli.main(["users", *args])
    out, err = capsys.readouterr()
    for secret in (PASSWORD, "another good password"):
        assert secret not in out and secret not in err
    return status, out, err


def _authenticate(settings: Settings, email: str, password: str) -> bool:
    with session_scope(settings) as session:
        try:
            UserManager(session, settings).authenticate(email, password)
        except SignInError:
            return False
    return True


def test_add_and_list(
    home: Settings, answers: Callable[..., list[str]], capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run(capsys, "list") == (0, "No accounts yet.\n", "")
    prompts = answers(PASSWORD, PASSWORD)
    status, out, _err = _run(capsys, "add", " Friend@Example.com")
    assert status == 0
    assert out == "Added the account friend@example.com.\n"
    assert prompts == ["New password for friend@example.com: ", "Repeat the password: "]
    assert _authenticate(home, "friend@example.com", PASSWORD)
    status, out, _err = _run(capsys, "list")
    assert status == 0
    assert out.startswith("friend@example.com  created ")
    assert "last sign-in never  active" in out


def test_add_refuses_an_existing_account_before_asking(
    home: Settings, answers: Callable[..., list[str]], capsys: pytest.CaptureFixture[str]
) -> None:
    answers(PASSWORD, PASSWORD)
    assert _run(capsys, "add", "friend@example.com")[0] == 0
    prompts = answers()
    status, _out, err = _run(capsys, "add", "FRIEND@example.com")
    assert status == 1
    assert "already exists" in err and prompts == []


@pytest.mark.parametrize(
    ("typed", "message"),
    [
        (("short", "short"), "at least 8 characters"),
        ((PASSWORD, "something else"), "the two passwords differ"),
    ],
)
def test_add_checks_the_password(
    home: Settings,
    answers: Callable[..., list[str]],
    capsys: pytest.CaptureFixture[str],
    typed: tuple[str, ...],
    message: str,
) -> None:
    answers(*typed)
    status, _out, err = _run(capsys, "add", "friend@example.com")
    assert status == 1 and message in err
    assert _run(capsys, "list")[1] == "No accounts yet.\n"


def test_add_checks_the_email(
    home: Settings, answers: Callable[..., list[str]], capsys: pytest.CaptureFixture[str]
) -> None:
    prompts = answers()
    status, _out, err = _run(capsys, "add", "not-an-email")
    assert status == 1 and "must look like name@example.com" in err and prompts == []


def test_set_password_disable_enable(
    home: Settings, answers: Callable[..., list[str]], capsys: pytest.CaptureFixture[str]
) -> None:
    with session_scope(home) as session:
        users = UserManager(session, home)
        users.create_session(users.create("friend@example.com", PASSWORD))
    answers("another good password", "another good password")
    status, out, _err = _run(capsys, "set-password", "friend@example.com")
    assert status == 0
    assert out == "Set a new password for friend@example.com; signed out 1 session(s).\n"
    assert not _authenticate(home, "friend@example.com", PASSWORD)
    assert _authenticate(home, "friend@example.com", "another good password")

    assert _run(capsys, "disable", "Friend@example.com") == (
        0, "Disabled friend@example.com; signed out 0 session(s).\n", ""
    )
    assert not _authenticate(home, "friend@example.com", "another good password")
    assert "  disabled " in _run(capsys, "list")[1]
    status, _out, err = _run(capsys, "disable", "friend@example.com")
    assert status == 1 and "already disabled" in err
    assert _run(capsys, "enable", "friend@example.com") == (0, "Enabled friend@example.com.\n", "")
    assert _authenticate(home, "friend@example.com", "another good password")
    status, _out, err = _run(capsys, "enable", "friend@example.com")
    assert status == 1 and "is not disabled" in err


@pytest.mark.parametrize("action", ["set-password", "disable", "enable"])
def test_unknown_email(
    home: Settings,
    answers: Callable[..., list[str]],
    capsys: pytest.CaptureFixture[str],
    action: str,
) -> None:
    prompts = answers()
    status, _out, err = _run(capsys, action, "nobody@example.com")
    assert status == 1
    assert "error: no account with the email nobody@example.com" in err
    assert prompts == []  # never asked for a password


def test_the_email_is_required() -> None:
    with pytest.raises(SystemExit):
        cli.main(["users", "add"])
    with pytest.raises(SystemExit):
        cli.main(["users", "add", "a@b.io", "--password", PASSWORD])  # never from an argument


def test_no_terminal_is_an_error(
    home: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """getpass falls back to reading stdin (with echo) when there is no terminal: refused."""

    def no_tty(prompt: str = "") -> str:
        warnings.warn("Can not control echo on the terminal.", getpass.GetPassWarning)
        return PASSWORD

    monkeypatch.setattr(getpass, "getpass", no_tty)
    status, _out, err = _run(capsys, "add", "friend@example.com")
    assert status == 1 and "no terminal to read the password from" in err
    assert _run(capsys, "list")[1] == "No accounts yet.\n"


def test_cancelled_prompt(
    home: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def cancelled(prompt: str = "") -> str:
        raise KeyboardInterrupt

    monkeypatch.setattr(getpass, "getpass", cancelled)
    status, _out, err = _run(capsys, "add", "friend@example.com")
    assert status == 1 and "cancelled" in err
