"""The password gate (`teacher.web.auth`): off by default (the app is unchanged), and on: pages
redirect to the sign-in page, the API answers 401, signing in and out, the session cookie
(bad, tampered, expired, from another password), `next` validation, the lockout after wrong
passwords, `/healthz`, the Origin check, `$TEACHER_PASSWORD` and `teacher serve` refusing a
public host without a password. No network: the TestClient calls the app in process.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from teacher import cli
from teacher.config import (
    ENV_HOME,
    ENV_PASSWORD,
    ConfigError,
    Settings,
    teacher_password_from_env,
)
from teacher.generation import StubGenerator
from teacher.lab_generator import StubLabGenerator
from teacher.web import auth
from teacher.web.app import create_app

PASSWORD = "correct horse battery staple"


class Clock:
    """A settable clock for the gate (Unix seconds)."""

    def __init__(self, now: float = 1_800_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def gated_app(settings: Settings, clock: Clock) -> FastAPI:
    app = create_app(
        settings, generator=StubGenerator(), lab_generator=StubLabGenerator(), password=PASSWORD
    )
    gate = app.state.password_gate
    gate.clock = clock
    gate.per_address.clock = clock
    gate.overall.clock = clock
    return app


@pytest.fixture
def gated(gated_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(gated_app, follow_redirects=False) as test_client:
        yield test_client


def sign_in(client: TestClient, password: str = PASSWORD, next_path: str = "/", **kw: Any):
    return client.post("/login", data={"password": password, "next": next_path}, **kw)


# -- off ------------------------------------------------------------------------------------


def test_gate_off_by_default(client: TestClient) -> None:
    assert client.app.state.password_gate is None
    home = client.get("/", follow_redirects=False)
    assert home.status_code == 200
    assert "Sign out" not in home.text
    assert client.get("/api/sources").status_code == 200
    assert client.get("/healthz").status_code == 200
    assert client.get("/login").status_code == 404  # no sign-in page without a password


def test_gate_off_ignores_origin(client: TestClient) -> None:
    response = client.post(
        "/api/sources", headers={"Origin": "https://evil.example"}, data={}, files={}
    )
    assert response.status_code != 403


def test_blank_password_is_an_error(settings: Settings) -> None:
    with pytest.raises(ValueError, match="non-blank"):
        create_app(settings, generator=StubGenerator(), lab_generator=StubLabGenerator(),
                   password="  ")


# -- $TEACHER_PASSWORD ----------------------------------------------------------------------


def test_password_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_PASSWORD, raising=False)
    assert teacher_password_from_env() is None
    monkeypatch.setenv(ENV_PASSWORD, " spaced ")
    assert teacher_password_from_env() == " spaced "  # used exactly as given
    monkeypatch.setenv(ENV_PASSWORD, "   ")
    with pytest.raises(ConfigError, match=ENV_PASSWORD):
        teacher_password_from_env()


# -- teacher serve --------------------------------------------------------------------------


@pytest.fixture
def uvicorn_calls(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[dict[str, Any]]:
    import uvicorn

    calls: list[dict[str, Any]] = []
    monkeypatch.setenv(ENV_HOME, str(tmp_path / "home"))
    monkeypatch.delenv(ENV_PASSWORD, raising=False)
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: calls.append({"app": app, **kw}))
    return calls


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.5", "::", "example.com", ""])
def test_serve_refuses_a_public_host_without_password(
    host: str, uvicorn_calls: list[dict[str, Any]], capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["serve", "--host", host]) == 1
    assert uvicorn_calls == []
    err = capsys.readouterr().err
    assert f"${ENV_PASSWORD}" in err and "refusing to serve" in err


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.2", "::1", "localhost"])
def test_serve_allows_loopback_without_password(
    host: str, uvicorn_calls: list[dict[str, Any]]
) -> None:
    assert cli.main(["serve", "--host", host]) == 0
    (call,) = uvicorn_calls
    assert call["app"].state.password_gate is None


def test_serve_with_password_gates_the_app(
    uvicorn_calls: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENV_PASSWORD, PASSWORD)
    assert cli.main(["serve", "--host", "0.0.0.0", "--port", "8080"]) == 0
    (call,) = uvicorn_calls
    assert isinstance(call["app"].state.password_gate, auth.PasswordGate)
    assert PASSWORD not in repr(call["app"].state.password_gate)


def test_serve_with_blank_password_fails(
    uvicorn_calls: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv(ENV_PASSWORD, "")
    assert cli.main(["serve"]) == 1
    assert uvicorn_calls == []
    assert f"${ENV_PASSWORD} is set but empty" in capsys.readouterr().err


# -- on: what is open, what is not ----------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "location"),
    [
        ("/", "/login"),
        ("/library", "/login?next=%2Flibrary"),
        ("/?q=attention%20heads", "/login?next=%2F%3Fq%3Dattention%2520heads"),
        ("/artifacts/some-page", "/login?next=%2Fartifacts%2Fsome-page"),
        ("/static/js/api.js", "/login?next=%2Fstatic%2Fjs%2Fapi.js"),
        ("/docs", "/login?next=%2Fdocs"),
    ],
)
def test_pages_redirect_to_sign_in(gated: TestClient, path: str, location: str) -> None:
    response = gated.get(path)
    assert response.status_code == 303
    assert response.headers["location"] == location
    assert "set-cookie" not in response.headers


@pytest.mark.parametrize(
    ("method", "path"),
    [("GET", "/api/sources"), ("GET", "/api/health"), ("POST", "/api/sources"),
     ("DELETE", "/api/sources/x"), ("GET", "/api"), ("POST", "/library")],
)
def test_api_answers_401(gated: TestClient, method: str, path: str) -> None:
    response = gated.request(method, path)
    assert response.status_code == 401
    assert response.json() == {"detail": "Sign in first: there is no session, or it has expired."}
    assert "location" not in response.headers


def test_open_paths(gated: TestClient) -> None:
    health = gated.get("/healthz")
    assert health.status_code == 200 and health.content == b""
    assert gated.head("/healthz").status_code == 200
    for path in sorted(auth.OPEN_STATIC):
        assert gated.get(path).status_code == 200, path
    page = gated.get("/login")
    assert page.status_code == 200
    assert page.headers["cache-control"] == "no-store"


def test_sign_in_page(gated: TestClient) -> None:
    html = gated.get("/login?next=/library").text
    assert '<form class="form login-form" method="post" action="/login">' in html
    assert 'type="password"' in html and 'name="password"' in html
    assert '<input type="hidden" name="next" value="/library">' in html
    assert 'autocomplete="current-password"' in html
    assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in html
    # Only what the gate serves without a session, and no script (it would need the API).
    refs = [part.split('"')[0] for part in html.split('href="')[1:]]
    assert set(refs) == auth.OPEN_STATIC
    assert "<script" not in html
    assert "Sign out" not in html


def test_sign_in_page_text_is_16px_or_more() -> None:
    from teacher.web.routers.pages import STATIC_DIR

    css = (STATIC_DIR / "css" / "login.css").read_text(encoding="utf-8")
    assert "font-size: max(1rem, 16px);" in css


# -- signing in -----------------------------------------------------------------------------


def test_sign_in_and_use_the_app(gated: TestClient, clock: Clock) -> None:
    response = sign_in(gated, next_path="/library")
    assert response.status_code == 303
    assert response.headers["location"] == "/library"
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{auth.SESSION_COOKIE}=v1.{int(clock.now)}.")
    lowered = cookie.lower()
    for part in ("httponly", "samesite=lax", "path=/", f"max-age={auth.SESSION_MAX_AGE}"):
        assert part in lowered, part
    assert "secure" not in lowered  # plain http
    assert PASSWORD not in cookie

    home = gated.get("/")
    assert home.status_code == 200
    assert '<form class="nav-signout" method="post" action="/logout">' in home.text
    assert gated.get("/api/sources").status_code == 200
    assert gated.get("/static/js/api.js").status_code == 200
    # Signed in, the sign-in page goes on to `next`.
    again = gated.get("/login?next=/history")
    assert (again.status_code, again.headers["location"]) == (303, "/history")


def test_cookie_is_secure_over_https(gated_app: FastAPI) -> None:
    with TestClient(gated_app, base_url="https://testserver", follow_redirects=False) as client:
        response = sign_in(client)
        assert response.status_code == 303
        assert "; secure" in response.headers["set-cookie"].lower()
        assert client.get("/api/sources").status_code == 200


def test_wrong_password(gated: TestClient) -> None:
    response = sign_in(gated, "wrong", next_path="/library")
    assert response.status_code == 401
    assert "Wrong password. Try again." in response.text
    assert 'role="alert"' in response.text
    assert '<input type="hidden" name="next" value="/library">' in response.text
    assert "set-cookie" not in response.headers
    assert sign_in(gated, "").status_code == 401
    assert gated.post("/login", data={"next": "/"}).status_code == 401  # no field at all
    assert gated.get("/api/sources").status_code == 401


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("/library", "/library"),
        ("/sources/a?tab=b#c", "/sources/a?tab=b#c"),
        ("//evil.example/", "/"),
        ("/\\evil.example", "/"),
        ("https://evil.example/", "/"),
        ("javascript:alert(1)", "/"),
        ("library", "/"),
        ("/\tevil", "/"),
        ("/login", "/"),
        ("/logout", "/"),
        ("", "/"),
    ],
)
def test_next_must_be_a_local_path(gated: TestClient, given: str, expected: str) -> None:
    assert auth.safe_next(given) == expected
    response = sign_in(gated, next_path=given)
    assert response.status_code == 303
    assert response.headers["location"] == expected


# -- the cookie -----------------------------------------------------------------------------


def _cookie(client: TestClient) -> str:
    response = sign_in(client)
    assert response.status_code == 303
    return response.cookies[auth.SESSION_COOKIE]


def _with_cookie(client: TestClient, value: str) -> int:
    client.cookies.clear()
    client.cookies.set(auth.SESSION_COOKIE, value)
    return client.get("/api/sources").status_code


def test_bad_and_tampered_cookies(gated: TestClient) -> None:
    good = _cookie(gated)
    version, issued, signature = good.split(".")
    assert _with_cookie(gated, good) == 200
    flipped = ("0" if signature[0] != "0" else "1") + signature[1:]
    for bad in (
        "",
        "nonsense",
        f"{version}.{issued}",
        f"{version}.{issued}.{flipped}",
        f"{version}.{int(issued) + 1}.{signature}",  # a later issue time, the old signature
        f"v2.{issued}.{signature}",
        f"{version}.{issued}.{signature.upper()}",
        f"{good}.extra",
    ):
        assert _with_cookie(gated, bad) == 401, bad


def test_cookie_expires_after_30_days(gated: TestClient, clock: Clock) -> None:
    good = _cookie(gated)
    clock.now += auth.SESSION_MAX_AGE - 1
    assert _with_cookie(gated, good) == 200
    clock.now += 1
    assert _with_cookie(gated, good) == 401
    redirect = gated.get("/library")
    assert redirect.headers["location"] == "/login?next=%2Flibrary"


def test_cookie_from_the_future_is_refused(gated: TestClient, clock: Clock) -> None:
    clock.now += auth.CLOCK_SKEW + 10
    future = _cookie(gated)
    clock.now -= auth.CLOCK_SKEW + 10
    assert _with_cookie(gated, future) == 401


def test_a_new_password_signs_everyone_out(clock: Clock) -> None:
    old = auth.PasswordGate("first password", clock=clock)
    new = auth.PasswordGate("second password", clock=clock)
    cookie = old.issue_cookie()
    assert old.cookie_valid(cookie)
    assert not new.cookie_valid(cookie)


# -- signing out ----------------------------------------------------------------------------


def test_sign_out(gated: TestClient) -> None:
    sign_in(gated)
    assert gated.get("/api/sources").status_code == 200
    response = gated.post("/logout")
    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    cleared = response.headers["set-cookie"].lower()
    assert cleared.startswith(f"{auth.SESSION_COOKIE}=") and "max-age=0" in cleared
    assert gated.get("/api/sources").status_code == 401
    assert gated.get("/logout").status_code == 405


# -- the lockout ----------------------------------------------------------------------------


def test_lockout_after_five_wrong_passwords(gated: TestClient, clock: Clock) -> None:
    for _ in range(auth.FREE_FAILURES - 1):
        assert "Try again." in sign_in(gated, "wrong").text
    fifth = sign_in(gated, "wrong")  # the fifth is still free; the sixth locks
    assert (fifth.status_code, "Try again." in fifth.text) == (401, True)
    locked = sign_in(gated, "wrong")
    assert locked.status_code == 401
    assert "wait 30 seconds" in locked.text
    # Locked out: even the right password is not checked.
    refused = sign_in(gated, PASSWORD)
    assert refused.status_code == 429
    assert refused.headers["retry-after"] == "30"
    assert "Too many wrong passwords. Wait 30 seconds, then try again." in refused.text
    assert "set-cookie" not in refused.headers
    clock.now += 10
    assert "Wait 20 seconds" in sign_in(gated, PASSWORD).text
    # The lockout grows: 30 s, then 60 s, ...
    clock.now += 20
    doubled = sign_in(gated, "wrong")
    assert "wait 1 minute," in doubled.text
    clock.now += 60
    # The right password signs in and clears the count.
    assert sign_in(gated, PASSWORD).status_code == 303
    assert gated.app.state.password_gate.per_address.failures("testclient") == 0
    assert "Try again." in sign_in(gated, "wrong").text


def test_lockout_is_per_address(clock: Clock) -> None:
    gate = auth.PasswordGate(PASSWORD, clock=clock)
    for _ in range(auth.FREE_FAILURES + 1):
        gate.login_failed("10.0.0.1")
    assert gate.login_wait("10.0.0.1") == 30
    assert gate.login_wait("10.0.0.2") == 0


def test_lockout_grows_to_an_hour_at_most(clock: Clock) -> None:
    limiter = auth.AttemptLimiter(free_failures=5, clock=clock)
    lockouts = [limiter.record_failure("a") for _ in range(15)]
    assert lockouts[:5] == [0] * 5
    assert lockouts[5:10] == [30, 60, 120, 240, 480]
    assert lockouts[-1] == auth.LOCKOUT_MAX
    limiter.record_success("a")
    assert limiter.wait_seconds("a") == 0


def test_overall_backstop_across_addresses(clock: Clock) -> None:
    gate = auth.PasswordGate(PASSWORD, clock=clock)
    for number in range(auth.OVERALL_FREE_FAILURES + 1):
        gate.login_failed(f"10.0.{number // 250}.{number % 250}")
    assert gate.login_wait("192.0.2.1") == 30  # a fresh address waits too


def test_limiter_forgets_old_failures(clock: Clock) -> None:
    limiter = auth.AttemptLimiter(free_failures=1, clock=clock)
    limiter.record_failure("a")
    clock.now += auth.FORGET_AFTER + 1
    assert limiter.record_failure("a") == 0  # counted from one again


# -- the Origin check -----------------------------------------------------------------------


def test_cross_origin_state_changes_are_refused(gated: TestClient) -> None:
    sign_in(gated)
    for origin in ("https://evil.example", "null", "http://testserver.evil.example"):
        response = gated.post("/api/sources", headers={"Origin": origin}, data={}, files={})
        assert response.status_code == 403, origin
        assert response.json()["detail"] == "Refused: this request came from another site."
        assert gated.delete("/api/sources/x", headers={"Origin": origin}).status_code == 403
    # Refused before signing in as well, so a forged sign-in or sign-out does nothing.
    assert gated.post("/logout", headers={"Origin": "https://evil.example"}).status_code == 403
    assert sign_in(gated, headers={"Origin": "https://evil.example"}).status_code == 403
    # Reads are never refused for their Origin; same-site writes pass.
    assert gated.get("/api/sources", headers={"Origin": "https://evil.example"}).status_code == 200
    same = sign_in(gated, headers={"Origin": "http://testserver"})
    assert same.status_code == 303


def test_origin_matches() -> None:
    assert auth.origin_matches(None, "example.com")
    assert auth.origin_matches("https://example.com", "example.com")
    assert auth.origin_matches("http://127.0.0.1:8040", "127.0.0.1:8040")
    assert auth.origin_matches("https://Example.com", "example.COM")
    assert not auth.origin_matches("https://example.com:8443", "example.com")
    assert not auth.origin_matches("null", "example.com")
    assert not auth.origin_matches("https://example.com", None)


# -- nothing secret in the logs -------------------------------------------------------------


def test_password_and_cookie_are_never_logged(
    gated: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("DEBUG")
    sign_in(gated, "a wrong guess")
    cookie = _cookie(gated)
    gated.post("/api/sources", headers={"Origin": "https://evil.example"})
    text = caplog.text
    assert "Wrong password from testclient" in text
    for secret in (PASSWORD, "a wrong guess", cookie):
        assert secret not in text


def test_front_end_sends_a_401_to_the_sign_in_page() -> None:
    """Every API failure becomes an `ApiError` (api.js, the home search, the tutor's stream);
    its constructor is the one place a 401 goes to the sign-in page."""
    from teacher.web.routers.pages import STATIC_DIR

    code = (STATIC_DIR / "js" / "api.js").read_text(encoding="utf-8")
    assert "if (status === 401 && typeof window !== \"undefined\" && window.location) {" in code
    assert "window.location.assign(`/login?next=${encodeURIComponent(here)}`);" in code
    for name in ("home_search.js", "chat.js"):
        assert "new ApiError(" in (STATIC_DIR / "js" / name).read_text(encoding="utf-8"), name
