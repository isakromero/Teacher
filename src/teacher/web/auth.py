"""The password gate: one shared password in front of every page and API route, for hosting.

Off unless the app is given a password (`create_app(password=...)`; `teacher serve` passes
`$TEACHER_PASSWORD`, read by `teacher.config.teacher_password_from_env`). Off, the app behaves
exactly as without this module, except that `GET /healthz` exists (it always does: an empty
200 for the host's health check).

On, `PasswordGateMiddleware` (the outermost middleware) lets a request through only with a
valid session cookie, except for the open paths: `GET/POST /login` (the sign-in form,
`templates/login.html`), `POST /logout`, `GET /healthz` and the two stylesheets the sign-in
page loads (`OPEN_STATIC`). Without a valid cookie, a page request (GET or HEAD) is redirected
(303) to `/login?next=<path and query>`; anything under `/api/` (and any other method) gets a
401 JSON error, which the front end (`static/js/api.js`, `ApiError`) turns into a visit to the
sign-in page. `next` is only followed when it is a same-site relative path (`safe_next`: one
leading slash, no scheme or host, no backslash or control character); anything else goes to
`/`. A request whose method changes state (POST, PUT, PATCH, DELETE) and whose `Origin` header
is present but names another host than the request's `Host` is refused with 403 (SameSite=Lax
already keeps the cookie off cross-site form posts; this also covers same-site other origins).

The session cookie (`SESSION_COOKIE`) is stateless and signed: `v1.<issued-at, Unix
seconds>.<hex HMAC-SHA256 of "v1.<issued-at>">`, keyed with HMAC(password, `KEY_CONTEXT`), so a
new password signs everyone out. It is valid for `SESSION_MAX_AGE` (30 days) from issue (a
time more than `CLOCK_SKEW` in the future is refused too); it is `HttpOnly`, `SameSite=Lax`,
`Path=/`, and `Secure` when the request arrived over HTTPS (`scope["scheme"]`, which uvicorn
sets from `X-Forwarded-Proto` only for a proxy it trusts: `$FORWARDED_ALLOW_IPS`, default
loopback; the container trusts its host's proxy). The password and signatures are compared in
constant time (`hmac.compare_digest`).

Wrong passwords are limited in memory (`AttemptLimiter`), per client address
(`scope["client"]`: the forwarded address only when uvicorn trusts the proxy): `FREE_FAILURES`
(5) wrong passwords, then each further one locks the address out for `LOCKOUT_BASE` (30 s)
doubled per extra failure, at most `LOCKOUT_MAX` (1 hour); a sign-in while locked out is
answered 429 with how long to wait, without checking the password. As a backstop against an
attacker who varies the address (a forwarded address can be forged when every proxy is
trusted), all failures together have the same policy after `OVERALL_FREE_FAILURES` (50). A
correct password clears the address's failures and the overall count; the counts are lost on
restart. Neither the password nor a cookie value is ever logged.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import math
import re
import threading
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

SESSION_COOKIE = "teacher_session"
SESSION_MAX_AGE = 30 * 24 * 60 * 60  # seconds
CLOCK_SKEW = 5 * 60  # seconds an issue time may lie in the future
COOKIE_VERSION = "v1"
KEY_CONTEXT = b"teacher.web.auth session cookie signing key v1"

FREE_FAILURES = 5
OVERALL_FREE_FAILURES = 50
LOCKOUT_BASE = 30  # seconds
LOCKOUT_MAX = 60 * 60  # seconds
#: Addresses whose last failure is older than this are forgotten (and pruned).
FORGET_AFTER = 24 * 60 * 60  # seconds
#: At most this many addresses are remembered (the oldest failures are dropped first).
MAX_ENTRIES = 10_000

LOGIN_PATH = "/login"
LOGOUT_PATH = "/logout"
HEALTH_PATH = "/healthz"
#: The static files the sign-in page needs, served without a session.
OPEN_STATIC = frozenset({"/static/css/app.css", "/static/css/login.css"})
STATE_CHANGING = frozenset({"POST", "PUT", "PATCH", "DELETE"})

_COOKIE_RE = re.compile(r"v1\.([0-9]{1,12})\.([0-9a-f]{64})")
_log = logging.getLogger("teacher.web.auth")


class AttemptLimiter:
    """Counts wrong passwords per key and locks a key out once it has more than
    `free_failures`: the lockout after failure `free_failures + n` (n >= 1) is `base * 2**(n-1)`
    seconds, at most `maximum`. Thread-safe; in memory only."""

    def __init__(
        self,
        *,
        free_failures: int,
        base: int = LOCKOUT_BASE,
        maximum: int = LOCKOUT_MAX,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if free_failures < 0 or base < 1 or maximum < base:
            raise ValueError("free_failures >= 0 and 1 <= base <= maximum are required")
        self.free_failures = free_failures
        self.base = base
        self.maximum = maximum
        self.clock = clock
        self._lock = threading.Lock()
        # key -> (failures, locked until, last failure)
        self._entries: dict[str, tuple[int, float, float]] = {}

    def wait_seconds(self, key: str) -> int:
        """Whole seconds until `key` may try again (0: it may try now)."""
        now = self.clock()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return 0
            remaining = entry[1] - now
        return math.ceil(remaining) if remaining > 0 else 0

    def record_failure(self, key: str) -> int:
        """Count a wrong password for `key`; return the lockout it now has (seconds, 0: none)."""
        now = self.clock()
        with self._lock:
            failures, _, last = self._entries.get(key, (0, 0.0, now))
            if now - last > FORGET_AFTER:
                failures = 0
            failures += 1
            extra = failures - self.free_failures
            lockout = min(self.base * 2 ** (extra - 1), self.maximum) if extra > 0 else 0
            self._entries[key] = (failures, now + lockout, now)
            if len(self._entries) > MAX_ENTRIES:
                self._prune(now)
        return lockout

    def record_success(self, key: str) -> None:
        with self._lock:
            self._entries.pop(key, None)

    def failures(self, key: str) -> int:
        with self._lock:
            entry = self._entries.get(key)
        return entry[0] if entry else 0

    def _prune(self, now: float) -> None:
        stale = [
            k for k, (_, until, last) in self._entries.items()
            if until <= now and now - last > FORGET_AFTER
        ]
        for k in stale:
            del self._entries[k]
        if len(self._entries) > MAX_ENTRIES:  # still too many: drop the oldest failures
            by_age = sorted(self._entries, key=lambda k: self._entries[k][2])
            for k in by_age[: len(self._entries) - MAX_ENTRIES]:
                del self._entries[k]


OVERALL = "*"  # the overall limiter's one key


class PasswordGate:
    """The password, the session cookie's signing key and the attempt limiters. The password
    is never shown (not in `repr`)."""

    def __init__(self, password: str, *, clock: Callable[[], float] = time.time) -> None:
        if not isinstance(password, str) or not password.strip():
            raise ValueError("the password gate needs a non-blank password")
        self._password_digest = hashlib.sha256(password.encode("utf-8")).digest()
        self._key = hmac.new(password.encode("utf-8"), KEY_CONTEXT, hashlib.sha256).digest()
        self.clock = clock
        self.per_address = AttemptLimiter(free_failures=FREE_FAILURES, clock=clock)
        self.overall = AttemptLimiter(free_failures=OVERALL_FREE_FAILURES, clock=clock)

    def __repr__(self) -> str:
        return "PasswordGate(<password hidden>)"

    # -- the password ------------------------------------------------------------------

    def check_password(self, candidate: str) -> bool:
        """Constant time: both sides are hashed to the same length, then compared."""
        digest = hashlib.sha256(candidate.encode("utf-8", "surrogatepass")).digest()
        return hmac.compare_digest(digest, self._password_digest)

    def login_wait(self, address: str) -> int:
        """Seconds until `address` may try a password again (0: now)."""
        return max(self.per_address.wait_seconds(address), self.overall.wait_seconds(OVERALL))

    def login_failed(self, address: str) -> int:
        """Count a wrong password; return the lockout now in force for `address` (seconds)."""
        mine = self.per_address.record_failure(address)
        overall = self.overall.record_failure(OVERALL)
        return max(mine, overall)

    def login_succeeded(self, address: str) -> None:
        self.per_address.record_success(address)
        self.overall.record_success(OVERALL)

    # -- the session cookie ------------------------------------------------------------

    def _sign(self, issued: int) -> str:
        message = f"{COOKIE_VERSION}.{issued}".encode("ascii")
        return hmac.new(self._key, message, hashlib.sha256).hexdigest()

    def issue_cookie(self) -> str:
        issued = int(self.clock())
        return f"{COOKIE_VERSION}.{issued}.{self._sign(issued)}"

    def cookie_valid(self, value: str | None) -> bool:
        if not value:
            return False
        match = _COOKIE_RE.fullmatch(value)
        if match is None:
            return False
        issued = int(match.group(1))
        if not hmac.compare_digest(self._sign(issued), match.group(2)):
            return False
        now = self.clock()
        return issued <= now + CLOCK_SKEW and now - issued < SESSION_MAX_AGE


# -- helpers ------------------------------------------------------------------------------


def safe_next(value: Any) -> str:
    """`value` when it is a same-site relative path to go to after signing in, else "/"."""
    if not isinstance(value, str) or not value.startswith("/"):
        return "/"
    if value.startswith("//") or "\\" in value:
        return "/"
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in value):
        return "/"
    parts = urlsplit(value)
    if parts.scheme or parts.netloc or parts.path in (LOGIN_PATH, LOGOUT_PATH):
        return "/"
    return value


def login_url(next_path: str) -> str:
    target = safe_next(next_path)
    return LOGIN_PATH if target == "/" else f"{LOGIN_PATH}?next={quote(target, safe='')}"


def origin_matches(origin: str | None, host: str | None) -> bool:
    """True when there is no `Origin` header, or it names the request's `Host`."""
    if origin is None:
        return True
    if not host:
        return False
    try:
        netloc = urlsplit(origin.strip()).netloc
    except ValueError:
        return False
    return bool(netloc) and netloc.lower() == host.strip().lower()


def client_address(scope: Any) -> str:
    client = scope.get("client")
    return client[0] if client and client[0] else "unknown"


def wait_text(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds} second{'s' if seconds != 1 else ''}"
    minutes = -(-seconds // 60)
    return f"{minutes} minute{'s' if minutes != 1 else ''}"


def _is_open(path: str) -> bool:
    return path in (LOGIN_PATH, LOGOUT_PATH, HEALTH_PATH) or path in OPEN_STATIC


def _is_api(path: str) -> bool:
    return path == "/api" or path.startswith("/api/")


# -- the middleware -----------------------------------------------------------------------


class PasswordGateMiddleware:
    """A pure ASGI middleware: see the module docstring for what it lets through."""

    def __init__(self, app: Any, *, gate: PasswordGate) -> None:
        self.app = app
        self.gate = gate

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        if scope["type"] == "websocket":  # the app has none; never open one unsigned
            if not self.gate.cookie_valid(request.cookies.get(SESSION_COOKIE)):
                await send({"type": "websocket.close", "code": 1008})
                return
            await self.app(scope, receive, send)
            return
        method = scope["method"]
        path = scope["path"]
        if method in STATE_CHANGING and not origin_matches(
            request.headers.get("origin"), request.headers.get("host")
        ):
            _log.warning("Refused %s %s: its Origin is another site", method, path)
            response: Response = JSONResponse(
                {"detail": "Refused: this request came from another site."}, status_code=403
            )
        elif _is_open(path) or self.gate.cookie_valid(request.cookies.get(SESSION_COOKIE)):
            await self.app(scope, receive, send)
            return
        elif method in ("GET", "HEAD") and not _is_api(path):
            query = scope.get("query_string", b"").decode("latin-1")
            response = RedirectResponse(
                login_url(path + (f"?{query}" if query else "")), status_code=303
            )
        else:
            response = JSONResponse(
                {"detail": "Sign in first: there is no session, or it has expired."},
                status_code=401,
            )
        response.headers["Cache-Control"] = "no-store"
        await response(scope, receive, send)


# -- the routes ---------------------------------------------------------------------------

router = APIRouter(include_in_schema=False)


def _gate(request: Request) -> PasswordGate:
    gate = getattr(request.app.state, "password_gate", None)
    if gate is None:  # the router is only included with a gate
        raise RuntimeError("the sign-in routes need app.state.password_gate")
    return gate


def _login_page(
    request: Request, *, next_path: str, error: str | None = None, status_code: int = 200
) -> HTMLResponse:
    from teacher.web.routers.pages import templates

    response = templates.TemplateResponse(
        request,
        "login.html",
        {"next": next_path, "error": error, "nav": None},
        status_code=status_code,
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get(LOGIN_PATH, response_class=HTMLResponse)
def login_form(request: Request, next: str | None = None) -> Response:
    target = safe_next(next)
    if _gate(request).cookie_valid(request.cookies.get(SESSION_COOKIE)):
        return RedirectResponse(target, status_code=303)
    return _login_page(request, next_path=target)


@router.post(LOGIN_PATH, response_class=HTMLResponse)
async def login_submit(request: Request) -> Response:
    gate = _gate(request)
    form = await request.form()
    target = safe_next(form.get("next"))
    password = form.get("password")
    address = client_address(request.scope)
    wait = gate.login_wait(address)
    if wait:
        _log.warning("Sign-in refused for %s: locked out for %ss more", address, wait)
        response = _login_page(
            request,
            next_path=target,
            error=f"Too many wrong passwords. Wait {wait_text(wait)}, then try again.",
            status_code=429,
        )
        response.headers["Retry-After"] = str(wait)
        return response
    if not isinstance(password, str) or not password or not gate.check_password(password):
        lockout = gate.login_failed(address)
        _log.warning("Wrong password from %s", address)
        error = "Wrong password. Try again."
        if lockout:
            error = (
                f"Wrong password. After too many wrong passwords, signing in is paused: wait "
                f"{wait_text(lockout)}, then try again."
            )
        return _login_page(request, next_path=target, error=error, status_code=401)
    gate.login_succeeded(address)
    response = RedirectResponse(target, status_code=303)
    response.set_cookie(
        SESSION_COOKIE,
        gate.issue_cookie(),
        max_age=SESSION_MAX_AGE,
        path="/",
        secure=request.url.scheme == "https",
        httponly=True,
        samesite="lax",
    )
    return response


@router.post(LOGOUT_PATH)
def logout(request: Request) -> Response:
    response = RedirectResponse(LOGIN_PATH, status_code=303)
    response.delete_cookie(
        SESSION_COOKIE,
        path="/",
        secure=request.url.scheme == "https",
        httponly=True,
        samesite="lax",
    )
    return response


def healthz() -> Response:
    """The host's health check: an empty 200, open even with the gate on."""
    return Response(status_code=200)


def install_password_gate(app: FastAPI, password: str | None) -> PasswordGate | None:
    """Add `GET /healthz` to `app` and, when `password` is given, the gate: the sign-in routes
    and `PasswordGateMiddleware` (call it after the other middleware, so it is the outermost).
    Sets `app.state.password_gate` (None without a password: the header then shows no "Sign
    out"). A blank password is a ValueError."""
    gate = PasswordGate(password) if password is not None else None
    app.state.password_gate = gate
    app.add_api_route(
        HEALTH_PATH, healthz, methods=["GET", "HEAD"], include_in_schema=False
    )
    if gate is not None:
        app.include_router(router)
        app.add_middleware(PasswordGateMiddleware, gate=gate)
    return gate


__all__ = [
    "AttemptLimiter",
    "PasswordGate",
    "PasswordGateMiddleware",
    "install_password_gate",
    "safe_next",
]
