"""The account gate (`teacher.web.auth`): off by default (the app is unchanged, nobody signs
in), and on: pages redirect to the sign-in page, the API answers 401, registering with the
access code, signing in and out (pages and JSON), the session cookie and the bearer token (bad,
expired, revoked), the current user on the request (`teacher.web.deps`), `next` validation, the
lockout after failed attempts, `/healthz`, the Origin check, `$TEACHER_PASSWORD` and `teacher
serve` refusing a public host without it. No network: the TestClient calls the app in process.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from teacher import cli
from teacher.config import (
    ENV_HOME,
    ENV_PASSWORD,
    ConfigError,
    Settings,
    teacher_password_from_env,
)
from teacher.db.engine import session_scope
from teacher.db.models import User, UserSession
from teacher.generation import StubGenerator
from teacher.lab_generator import StubLabGenerator
from teacher.managers import UserManager
from teacher.managers import users as users_module
from teacher.web import auth
from teacher.web.app import create_app
from teacher.web.deps import CurrentUser, CurrentUserDep, UserDep

CODE = "correct horse battery staple"
EMAIL = "reader@example.com"
PASSWORD = "a long enough password"


class Clock:
    """A settable clock for the gate (Unix seconds)."""

    def __init__(self, now: float = 1_800_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _whoami(user: CurrentUserDep) -> dict[str, Any]:
    return {"user": None if user is None else {"id": user.id, "email": user.email}}


def _must(user: UserDep) -> dict[str, Any]:
    return {"id": user.id}


def _add_probe_routes(app: FastAPI) -> None:
    """Two routes that read the current user the way later routes will."""
    app.add_api_route("/api/test/whoami", _whoami, methods=["GET"])
    app.add_api_route("/api/test/must", _must, methods=["GET"])


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def gated_app(settings: Settings, clock: Clock) -> FastAPI:
    app = create_app(
        settings, generator=StubGenerator(), lab_generator=StubLabGenerator(), access_code=CODE
    )
    gate = app.state.auth_gate
    gate.clock = clock
    gate.per_address.clock = clock
    gate.overall.clock = clock
    _add_probe_routes(app)
    return app


@pytest.fixture
def gated(gated_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(gated_app, follow_redirects=False) as test_client:
        yield test_client


def register(
    client: TestClient,
    email: str = EMAIL,
    password: str = PASSWORD,
    code: str = CODE,
    next_path: str = "/",
    **kw: Any,
):
    data = {"email": email, "password": password, "access_code": code, "next": next_path}
    return client.post("/register", data=data, **kw)


def sign_in(
    client: TestClient,
    email: str = EMAIL,
    password: str = PASSWORD,
    next_path: str = "/",
    **kw: Any,
):
    return client.post(
        "/login", data={"email": email, "password": password, "next": next_path}, **kw
    )


@pytest.fixture
def account(settings: Settings, gated: TestClient) -> int:
    """An account (EMAIL, PASSWORD) made through the manager; the client is not signed in."""
    with session_scope(settings) as session:
        return UserManager(session, settings).create(EMAIL, PASSWORD).id


def _sessions(settings: Settings) -> list[UserSession]:
    with session_scope(settings) as session:
        return list(session.scalars(select(UserSession).order_by(UserSession.id)))


# -- off ------------------------------------------------------------------------------------


def test_gate_off_by_default(client: TestClient) -> None:
    assert client.app.state.auth_gate is None
    home = client.get("/", follow_redirects=False)
    assert home.status_code == 200
    assert "Sign out" not in home.text and "nav-user" not in home.text
    assert client.get("/api/sources").status_code == 200
    assert client.get("/healthz").status_code == 200
    # No sign-in pages and no account routes without an access code.
    assert client.get("/login").status_code == 404
    assert client.get("/register").status_code == 404
    assert client.post("/api/auth/login", json={}).status_code == 404
    assert client.get("/api/auth/me").status_code == 404


def test_gate_off_has_no_current_user(app: FastAPI) -> None:
    _add_probe_routes(app)
    with TestClient(app) as client:
        assert client.get("/api/test/whoami").json() == {"user": None}
        refused = client.get("/api/test/must")
        assert refused.status_code == 404
        assert "runs without sign-in" in refused.json()["detail"]


def test_gate_off_ignores_origin(client: TestClient) -> None:
    response = client.post(
        "/api/sources", headers={"Origin": "https://evil.example"}, data={}, files={}
    )
    assert response.status_code != 403


def test_blank_access_code_is_an_error(settings: Settings) -> None:
    with pytest.raises(ValueError, match="non-blank"):
        create_app(settings, generator=StubGenerator(), lab_generator=StubLabGenerator(),
                   access_code="  ")


# -- $TEACHER_PASSWORD ----------------------------------------------------------------------


def test_access_code_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
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
def test_serve_refuses_a_public_host_without_access_code(
    host: str, uvicorn_calls: list[dict[str, Any]], capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["serve", "--host", host]) == 1
    assert uvicorn_calls == []
    err = capsys.readouterr().err
    assert f"${ENV_PASSWORD}" in err and "refusing to serve" in err
    assert "access code" in err


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.2", "::1", "localhost"])
def test_serve_allows_loopback_without_access_code(
    host: str, uvicorn_calls: list[dict[str, Any]]
) -> None:
    assert cli.main(["serve", "--host", host]) == 0
    (call,) = uvicorn_calls
    assert call["app"].state.auth_gate is None


def test_serve_with_access_code_gates_the_app(
    uvicorn_calls: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENV_PASSWORD, CODE)
    assert cli.main(["serve", "--host", "0.0.0.0", "--port", "8080"]) == 0
    (call,) = uvicorn_calls
    assert isinstance(call["app"].state.auth_gate, auth.AuthGate)
    assert CODE not in repr(call["app"].state.auth_gate)


def test_serve_with_blank_access_code_fails(
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
     ("DELETE", "/api/sources/x"), ("GET", "/api"), ("POST", "/library"),
     ("GET", "/api/auth/me"), ("POST", "/api/auth/logout"), ("GET", "/api/test/whoami")],
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
    for path in ("/login", "/register"):
        page = gated.get(path)
        assert page.status_code == 200, path
        assert page.headers["cache-control"] == "no-store"


def _links(html: str) -> tuple[set[str], set[str]]:
    """(what the page loads: <link href>, where it links to: <a href>)."""
    loads = {part.split('href="')[1].split('"')[0] for part in html.split("<link ")[1:]}
    anchors = {part.split('href="')[1].split('"')[0] for part in html.split("<a ")[1:]}
    return loads, anchors


def test_sign_in_page(gated: TestClient) -> None:
    html = gated.get("/login?next=/library").text
    assert '<form class="form login-form" method="post" action="/login">' in html
    assert 'type="email"' in html and 'name="email"' in html and 'autocomplete="username"' in html
    assert 'type="password"' in html and 'name="password"' in html
    assert 'autocomplete="current-password"' in html
    assert 'name="access_code"' not in html
    assert '<input type="hidden" name="next" value="/library">' in html
    assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in html
    # Only what the gate serves without a session, and no script (it would need the API).
    loads, anchors = _links(html)
    assert loads == auth.OPEN_STATIC
    assert anchors == {"/register?next=%2Flibrary"}
    assert "<script" not in html
    assert "Sign out" not in html


def test_register_page(gated: TestClient) -> None:
    html = gated.get("/register?next=/history").text
    assert '<form class="form login-form" method="post" action="/register">' in html
    assert 'name="email"' in html and 'name="password"' in html and 'name="access_code"' in html
    assert 'autocomplete="new-password"' in html and 'minlength="8"' in html
    assert '<input type="hidden" name="next" value="/history">' in html
    loads, anchors = _links(html)
    assert loads == auth.OPEN_STATIC
    assert anchors == {"/login?next=%2Fhistory"}
    assert "<script" not in html
    plain = gated.get("/register").text
    assert _links(plain)[1] == {"/login"}


def test_sign_in_page_text_is_16px_or_more() -> None:
    from teacher.web.routers.pages import STATIC_DIR

    css = (STATIC_DIR / "css" / "login.css").read_text(encoding="utf-8")
    assert "font-size: max(1rem, 16px);" in css
    assert '.login-form input[type="email"],' in css


# -- registering ----------------------------------------------------------------------------


def test_register_and_use_the_app(gated: TestClient, settings: Settings, clock: Clock) -> None:
    response = register(gated, email="  Reader@Example.COM ", next_path="/library")
    assert response.status_code == 303
    assert response.headers["location"] == "/library"
    cookie = response.headers["set-cookie"]
    token = response.cookies[auth.SESSION_COOKIE]
    assert cookie.startswith(f"{auth.SESSION_COOKIE}={token};")
    assert len(token) >= 43  # 32 random bytes, URL-safe base64
    lowered = cookie.lower()
    for part in ("httponly", "samesite=lax", "path=/", f"max-age={auth.SESSION_MAX_AGE}"):
        assert part in lowered, part
    assert "secure" not in lowered  # plain http
    assert PASSWORD not in cookie and CODE not in cookie

    # Stored: the normalised email, a hash of the password, the token's SHA-256 only.
    with session_scope(settings) as session:
        (user,) = session.scalars(select(User)).all()
        assert user.email == EMAIL
        assert user.password_hash.startswith("scrypt$") and PASSWORD not in user.password_hash
        assert user.last_login_at is not None
    (row,) = _sessions(settings)
    assert row.token_sha256 == hashlib.sha256(token.encode()).hexdigest()
    assert row.label == "web" and row.expires_at - row.created_at == timedelta(days=30)

    home = gated.get("/")
    assert home.status_code == 200
    assert '<form class="nav-signout" method="post" action="/logout">' in home.text
    assert (
        '<span class="nav-user" title="Signed in as reader@example.com">'
        '<span class="visually-hidden">Signed in as </span>reader@example.com</span>'
    ) in home.text
    assert gated.get("/api/sources").status_code == 200
    assert gated.get("/static/js/api.js").status_code == 200
    me = gated.get("/api/auth/me").json()
    assert me == {"id": user.id, "email": EMAIL, "created_at": me["created_at"]}
    assert me["created_at"].endswith("+00:00")
    assert gated.get("/api/test/whoami").json() == {"user": {"id": user.id, "email": EMAIL}}
    assert gated.get("/api/test/must").json() == {"id": user.id}
    # Signed in, the sign-in pages go on to `next`.
    for path in ("/login", "/register"):
        again = gated.get(f"{path}?next=/history")
        assert (again.status_code, again.headers["location"]) == (303, "/history")


def test_register_with_a_wrong_access_code(gated: TestClient, settings: Settings) -> None:
    response = register(gated, code="not the code", next_path="/library")
    assert response.status_code == 401
    assert "Wrong access code. Try again." in response.text
    assert 'role="alert"' in response.text
    assert f'value="{EMAIL}"' in response.text  # the email is kept, the password is not
    assert PASSWORD not in response.text
    assert '<input type="hidden" name="next" value="/library">' in response.text
    assert "set-cookie" not in response.headers
    assert gated.post("/register", data={"email": EMAIL, "password": PASSWORD}).status_code == 401
    with session_scope(settings) as session:
        assert session.scalars(select(User)).all() == []


@pytest.mark.parametrize(
    ("email", "password", "message"),
    [
        ("no-at-sign", PASSWORD, "must look like name@example.com"),
        ("two@at@example.com", PASSWORD, "must look like name@example.com"),
        ("has space@example.com", PASSWORD, "must not contain spaces"),
        ("", PASSWORD, "Enter an email address."),
        ("a" * 250 + "@x.io", PASSWORD, "at most 254 characters"),
        (EMAIL, "short", "at least 8 characters"),
        (EMAIL, "x" * 1025, "at most 1024 characters"),
        (EMAIL, "", "at least 8 characters"),
    ],
)
def test_register_checks_email_and_password(
    gated: TestClient, settings: Settings, email: str, password: str, message: str
) -> None:
    response = register(gated, email=email, password=password)
    assert response.status_code == 422
    assert message in response.text
    assert "set-cookie" not in response.headers
    with session_scope(settings) as session:
        assert session.scalars(select(User)).all() == []


def test_register_an_email_twice(gated: TestClient, account: int) -> None:
    response = register(gated, email="READER@example.com")
    assert response.status_code == 409
    assert "An account with this email already exists. Sign in instead." in response.text
    assert "set-cookie" not in response.headers


# -- signing in -----------------------------------------------------------------------------


def test_sign_in_and_out(gated: TestClient, settings: Settings, account: int) -> None:
    response = sign_in(gated, email=" READER@example.com", next_path="/library")
    assert response.status_code == 303
    assert response.headers["location"] == "/library"
    token = response.cookies[auth.SESSION_COOKIE]
    assert gated.get("/api/sources").status_code == 200
    assert len(_sessions(settings)) == 1

    out = gated.post("/logout")
    assert out.status_code == 303
    assert out.headers["location"] == "/login"
    cleared = out.headers["set-cookie"].lower()
    assert cleared.startswith(f"{auth.SESSION_COOKIE}=") and "max-age=0" in cleared
    assert _sessions(settings) == []  # the session is deleted, not just the cookie
    gated.cookies.set(auth.SESSION_COOKIE, token)  # the old token is worthless now
    assert gated.get("/api/sources").status_code == 401
    assert gated.get("/logout").status_code == 405
    assert gated.post("/logout").status_code == 303  # without a session: still fine


def test_cookie_is_secure_over_https(gated_app: FastAPI, account: int) -> None:
    with TestClient(gated_app, base_url="https://testserver", follow_redirects=False) as client:
        response = sign_in(client)
        assert response.status_code == 303
        assert "; secure" in response.headers["set-cookie"].lower()
        assert client.get("/api/sources").status_code == 200


def test_wrong_password_and_unknown_email_look_the_same(
    gated: TestClient, account: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    verified: list[str] = []
    real = users_module.verify_password

    def counting(password: str, stored: str) -> bool:
        verified.append(stored)
        return real(password, stored)

    monkeypatch.setattr(users_module, "verify_password", counting)
    wrong_password = sign_in(gated, password="not the password", next_path="/library")
    unknown_email = sign_in(gated, email="nobody@example.com", next_path="/library")
    for response in (wrong_password, unknown_email):
        assert response.status_code == 401
        assert "Wrong email or password. Try again." in response.text
        assert 'role="alert"' in response.text
        assert '<input type="hidden" name="next" value="/library">' in response.text
        assert "set-cookie" not in response.headers
    # The unknown email still cost one password verification (against the dummy hash).
    assert len(verified) == 2 and verified[0] != verified[1]
    assert sign_in(gated, password="").status_code == 401
    assert gated.post("/login", data={"next": "/"}).status_code == 401  # no fields at all
    assert gated.get("/api/sources").status_code == 401


def test_a_disabled_account_cannot_sign_in_and_is_signed_out(
    gated: TestClient, settings: Settings, account: int
) -> None:
    assert sign_in(gated).status_code == 303
    assert gated.get("/api/sources").status_code == 200
    with session_scope(settings) as session:
        users = UserManager(session, settings)
        assert users.disable(users.get(account)) == 1
    assert gated.get("/api/sources").status_code == 401
    refused = sign_in(gated)
    assert refused.status_code == 401
    assert "Wrong email or password." in refused.text  # says nothing more
    with session_scope(settings) as session:
        users = UserManager(session, settings)
        users.enable(users.get(account))
    assert sign_in(gated).status_code == 303


def test_a_new_password_signs_the_user_out(
    gated: TestClient, settings: Settings, account: int
) -> None:
    assert sign_in(gated).status_code == 303
    with session_scope(settings) as session:
        users = UserManager(session, settings)
        users.set_password(users.get(account), "a brand new password")
    assert gated.get("/api/sources").status_code == 401
    assert sign_in(gated).status_code == 401
    assert sign_in(gated, password="a brand new password").status_code == 303


def test_two_users_have_their_own_sessions(gated_app: FastAPI, settings: Settings) -> None:
    with (
        TestClient(gated_app, follow_redirects=False) as first,
        TestClient(gated_app, follow_redirects=False) as second,
    ):
        assert register(first, email="one@example.com").status_code == 303
        assert register(second, email="two@example.com").status_code == 303
        assert first.get("/api/auth/me").json()["email"] == "one@example.com"
        assert second.get("/api/auth/me").json()["email"] == "two@example.com"
        assert first.post("/logout").status_code == 303
        assert first.get("/api/auth/me").status_code == 401
        assert second.get("/api/auth/me").status_code == 200


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
        ("/register", "/"),
        ("", "/"),
    ],
)
def test_next_must_be_a_local_path(
    gated: TestClient, account: int, given: str, expected: str
) -> None:
    assert auth.safe_next(given) == expected
    response = sign_in(gated, next_path=given)
    assert response.status_code == 303
    assert response.headers["location"] == expected


# -- the session token ----------------------------------------------------------------------


def _token(client: TestClient) -> str:
    response = sign_in(client)
    assert response.status_code == 303
    return response.cookies[auth.SESSION_COOKIE]


def _with_cookie(client: TestClient, value: str) -> int:
    client.cookies.clear()
    client.cookies.set(auth.SESSION_COOKIE, value)
    return client.get("/api/sources").status_code


def _with_header(client: TestClient, value: str) -> int:
    client.cookies.clear()
    return client.get("/api/sources", headers={"Authorization": value}).status_code


def test_bad_and_tampered_tokens(gated: TestClient, account: int) -> None:
    good = _token(gated)
    assert _with_cookie(gated, good) == 200
    flipped = good[:-1] + ("A" if good[-1] != "A" else "B")
    for bad in ("", "nonsense", flipped, good[:-1], f"{good}x", good.upper(), f"{good}.extra",
                hashlib.sha256(good.encode()).hexdigest()):
        assert _with_cookie(gated, bad) == 401, bad


def test_bearer_token(gated: TestClient, account: int) -> None:
    good = _token(gated)
    assert _with_header(gated, f"Bearer {good}") == 200
    assert _with_header(gated, f"bearer  {good} ") == 200
    for bad in (f"Basic {good}", "Bearer", "Bearer ", good, f"Bearer {good}x"):
        assert _with_header(gated, bad) == 401, bad
    # A header is used instead of the cookie, never as well: a bad header is not rescued.
    gated.cookies.set(auth.SESSION_COOKIE, good)
    assert gated.get("/api/sources", headers={"Authorization": "Bearer nope"}).status_code == 401
    redirected = gated.get("/library", headers={"Authorization": "Basic x"})
    assert redirected.status_code == 303


def test_session_expires_after_30_days(
    gated: TestClient, settings: Settings, clock: Clock
) -> None:
    with session_scope(settings) as session:
        UserManager(session, settings).create(EMAIL, PASSWORD)
    good = _token(gated)
    clock.now += auth.SESSION_MAX_AGE - 1
    assert _with_cookie(gated, good) == 200
    clock.now += 1
    assert _with_cookie(gated, good) == 401
    assert _sessions(settings) == []  # the expired session was deleted when it was presented
    redirect = gated.get("/library")
    assert redirect.headers["location"] == "/login?next=%2Flibrary"


def test_last_seen_is_written_at_most_every_few_minutes(
    gated: TestClient, settings: Settings, clock: Clock, account: int
) -> None:
    _token(gated)
    (row,) = _sessions(settings)
    first_seen = row.last_seen_at
    clock.now += 60
    assert gated.get("/api/sources").status_code == 200
    assert _sessions(settings)[0].last_seen_at == first_seen
    clock.now += 5 * 60
    assert gated.get("/api/sources").status_code == 200
    assert _sessions(settings)[0].last_seen_at == first_seen + timedelta(seconds=6 * 60)


# -- JSON, for clients that are not browsers ------------------------------------------------


def test_api_register_login_me_logout(gated: TestClient, settings: Settings) -> None:
    created = gated.post(
        "/api/auth/register",
        json={"email": " Phone@Example.com", "password": PASSWORD, "access_code": CODE,
              "client": "iPhone"},
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert set(body) == {"token", "expires_at", "user"}
    assert body["user"]["email"] == "phone@example.com"
    assert set(body["user"]) == {"id", "email", "created_at"}
    assert "set-cookie" not in created.headers  # the client keeps the token itself
    assert gated.cookies.get(auth.SESSION_COOKIE) is None

    logged_in = gated.post(
        "/api/auth/login", json={"email": "phone@example.com", "password": PASSWORD}
    )
    assert logged_in.status_code == 200
    token = logged_in.json()["token"]
    assert token != body["token"]
    assert [row.label for row in _sessions(settings)] == ["iPhone", "api"]

    bearer = {"Authorization": f"Bearer {token}"}
    me = gated.get("/api/auth/me", headers=bearer)
    assert me.json() == body["user"]
    assert gated.get("/api/sources", headers=bearer).status_code == 200
    out = gated.post("/api/auth/logout", headers=bearer)
    assert out.status_code == 204
    assert gated.get("/api/auth/me", headers=bearer).status_code == 401
    # The other session is still there.
    other = {"Authorization": f"Bearer {body['token']}"}
    assert gated.get("/api/auth/me", headers=other).status_code == 200


def test_api_register_errors(gated: TestClient, account: int, settings: Settings) -> None:
    wrong = gated.post(
        "/api/auth/register",
        json={"email": "new@example.com", "password": PASSWORD, "access_code": "nope"},
    )
    assert (wrong.status_code, wrong.json()) == (401, {"detail": "Wrong access code. Try again."})
    duplicate = gated.post(
        "/api/auth/register", json={"email": EMAIL, "password": PASSWORD, "access_code": CODE}
    )
    assert duplicate.status_code == 409
    short = gated.post(
        "/api/auth/register",
        json={"email": "new@example.com", "password": "short", "access_code": CODE},
    )
    assert short.status_code == 422
    assert "at least 8 characters" in short.json()["detail"]
    missing = gated.post("/api/auth/register", json={"email": "new@example.com"})
    assert missing.status_code == 422
    with session_scope(settings) as session:
        assert [u.email for u in session.scalars(select(User))] == [EMAIL]


def test_api_login_errors(gated: TestClient, account: int) -> None:
    for body in ({"email": EMAIL, "password": "wrong password"},
                 {"email": "nobody@example.com", "password": PASSWORD}):
        response = gated.post("/api/auth/login", json=body)
        assert response.status_code == 401
        assert response.json() == {"detail": "Wrong email or password. Try again."}
    assert gated.post("/api/auth/login", json={"email": EMAIL}).status_code == 422
    too_long = {"email": EMAIL, "password": PASSWORD, "client": "x" * 101}
    assert gated.post("/api/auth/login", json=too_long).status_code == 422


# -- the lockout ----------------------------------------------------------------------------


def test_lockout_after_five_failed_sign_ins(
    gated: TestClient, clock: Clock, account: int
) -> None:
    for _ in range(auth.FREE_FAILURES - 1):
        assert "Try again." in sign_in(gated, password="wrong password").text
    fifth = sign_in(gated, password="wrong password")  # the fifth is still free; the sixth locks
    assert (fifth.status_code, "Try again." in fifth.text) == (401, True)
    locked = sign_in(gated, password="wrong password")
    assert locked.status_code == 401
    assert "wait 30 seconds" in locked.text
    # Locked out: even the right password is not checked.
    refused = sign_in(gated)
    assert refused.status_code == 429
    assert refused.headers["retry-after"] == "30"
    assert "Too many failed attempts. Wait 30 seconds, then try again." in refused.text
    assert "set-cookie" not in refused.headers
    api = gated.post("/api/auth/login", json={"email": EMAIL, "password": PASSWORD})
    assert (api.status_code, api.headers["retry-after"]) == (429, "30")
    assert register(gated, email="new@example.com").status_code == 429  # shared
    clock.now += 10
    assert "Wait 20 seconds" in sign_in(gated).text
    # The lockout grows: 30 s, then 60 s, ...
    clock.now += 20
    doubled = sign_in(gated, password="wrong password")
    assert "wait 1 minute," in doubled.text
    clock.now += 60
    # The right password signs in and clears the count.
    assert sign_in(gated).status_code == 303
    assert gated.app.state.auth_gate.per_address.failures("testclient") == 0
    assert "Try again." in sign_in(gated, password="wrong password").text


def test_wrong_access_codes_lock_out_too(gated: TestClient, clock: Clock, account: int) -> None:
    for _ in range(auth.FREE_FAILURES):
        assert register(gated, code="guess").status_code == 401
    locked = register(gated, code="guess")
    assert locked.status_code == 401 and "wait 30 seconds" in locked.text
    refused = register(gated)  # the right code is not checked while locked out
    assert refused.status_code == 429
    assert refused.headers["retry-after"] == "30"
    api = gated.post(
        "/api/auth/register",
        json={"email": "new@example.com", "password": PASSWORD, "access_code": CODE},
    )
    assert api.status_code == 429
    assert sign_in(gated).status_code == 429  # one count for sign-in and registration
    clock.now += 30
    assert register(gated, email="new@example.com").status_code == 303
    assert gated.app.state.auth_gate.per_address.failures("testclient") == 0


def _gate(settings: Settings, clock: Clock) -> auth.AuthGate:
    return auth.AuthGate(CODE, settings, clock=clock)


def test_lockout_is_per_address(settings: Settings, clock: Clock) -> None:
    gate = _gate(settings, clock)
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


def test_overall_backstop_across_addresses(settings: Settings, clock: Clock) -> None:
    gate = _gate(settings, clock)
    for number in range(auth.OVERALL_FREE_FAILURES + 1):
        gate.login_failed(f"10.0.{number // 250}.{number % 250}")
    assert gate.login_wait("192.0.2.1") == 30  # a fresh address waits too


def test_limiter_forgets_old_failures(clock: Clock) -> None:
    limiter = auth.AttemptLimiter(free_failures=1, clock=clock)
    limiter.record_failure("a")
    clock.now += auth.FORGET_AFTER + 1
    assert limiter.record_failure("a") == 0  # counted from one again


def test_access_code_check(settings: Settings, clock: Clock) -> None:
    gate = _gate(settings, clock)
    assert gate.check_access_code(CODE)
    for wrong in ("", CODE.upper(), f" {CODE}", CODE[:-1], "\udcff"):
        assert not gate.check_access_code(wrong)
    assert CODE not in repr(gate)


# -- the Origin check -----------------------------------------------------------------------


def test_cross_origin_state_changes_are_refused(gated: TestClient, account: int) -> None:
    sign_in(gated)
    for origin in ("https://evil.example", "null", "http://testserver.evil.example"):
        response = gated.post("/api/sources", headers={"Origin": origin}, data={}, files={})
        assert response.status_code == 403, origin
        assert response.json()["detail"] == "Refused: this request came from another site."
        assert gated.delete("/api/sources/x", headers={"Origin": origin}).status_code == 403
    # Refused before signing in as well, so a forged sign-in, sign-out or registration does
    # nothing.
    evil = {"Origin": "https://evil.example"}
    assert gated.post("/logout", headers=evil).status_code == 403
    assert sign_in(gated, headers=evil).status_code == 403
    assert register(gated, email="new@example.com", headers=evil).status_code == 403
    assert gated.post(
        "/api/auth/login", json={"email": EMAIL, "password": PASSWORD}, headers=evil
    ).status_code == 403
    # Reads are never refused for their Origin; same-site writes pass.
    assert gated.get("/api/sources", headers=evil).status_code == 200
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


# -- the current user -----------------------------------------------------------------------


def test_current_user_on_the_request(gated: TestClient, account: int) -> None:
    token = _token(gated)
    gated.cookies.clear()
    bearer = {"Authorization": f"Bearer {token}"}
    assert gated.get("/api/test/whoami", headers=bearer).json() == {
        "user": {"id": account, "email": EMAIL}
    }
    assert gated.get("/api/test/must", headers=bearer).json() == {"id": account}
    assert CurrentUser.__dataclass_fields__.keys() == {"id", "email", "created_at", "session_id"}


# -- nothing secret in the logs -------------------------------------------------------------


def test_secrets_are_never_logged(
    gated: TestClient, account: int, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("DEBUG")
    sign_in(gated, password="a wrong guess")
    register(gated, email="new@example.com", password="another password", code="a code guess")
    token = _token(gated)
    created = gated.post(
        "/api/auth/register",
        json={"email": "api@example.com", "password": "api password!", "access_code": CODE},
    )
    gated.post("/api/sources", headers={"Origin": "https://evil.example"})
    gated.post("/logout")
    text = caplog.text
    assert "Failed sign-in from testclient" in text
    assert "Wrong access code from testclient" in text
    for secret in (PASSWORD, "a wrong guess", "another password", "a code guess", CODE, token,
                   created.json()["token"], "api password!"):
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
