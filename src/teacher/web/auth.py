"""The account gate: user accounts and sessions in front of every page and API route, for hosting.

Off unless the app is given an access code (`create_app(access_code=...)`; `teacher serve`
passes `$TEACHER_PASSWORD`, read by `teacher.config.teacher_password_from_env`). Off, the app
behaves exactly as without this module (no sign-in, no current user: `request.state` has no
`user`, and `teacher.web.deps.current_user` gives None), except that `GET /healthz` exists (it
always does: an empty 200 for the host's health check); `/login`, `/register` and `/api/auth/*`
do not exist (404).

On, everyone signs in with their own account (`teacher.managers.users`: an email and a
password). `$TEACHER_PASSWORD` is not a password anyone signs in with: it is the access code
that registering an account needs, so that strangers who find the site cannot make an account
(they would see the library and spend the owner's API keys). There is no email verification and
no password reset by email: the owner resets a password with `teacher users set-password`.
After deploying, the owner registers their own account like everyone else.

`AuthGateMiddleware` (the outermost middleware) reads the session token from the
`Authorization: Bearer <token>` header when there is one (for clients that are not browsers),
else from the session cookie (`SESSION_COOKIE`), resolves it (`UserManager.resolve_session`, in
the thread pool) and puts the result on the request: `request.state.user` is a
`teacher.web.deps.CurrentUser` (id, email, created_at, session_id) or None; routes read it with
the `CurrentUserDep` / `UserDep` dependencies. It lets a request through only with a valid
session, except for the open paths: `GET/POST /login` (`templates/login.html`), `GET/POST
/register` (`templates/register.html`), `POST /logout`, `POST /api/auth/login`, `POST
/api/auth/register`, `GET /healthz` and the two stylesheets and the tab icons the sign-in pages
load (`OPEN_STATIC`; for these and `/healthz` no session is looked up). Without a valid session
a page request (GET or HEAD) is redirected (303) to `/login?next=<path and query>`; anything
under `/api/` (and any other method) gets a 401 JSON error, which the front end
(`static/js/api.js`, `ApiError`) turns into a visit to the sign-in page. `next` is only
followed when it is a same-site relative path (`safe_next`: one leading slash, no scheme or
host, no backslash or control character, not a sign-in page); anything else goes to `/`. A
request whose method changes state (POST, PUT, PATCH, DELETE) and whose `Origin` header is
present but names another host than the request's `Host` is refused with 403 before anything
else (SameSite=Lax already keeps the cookie off cross-site form posts; this also covers
same-site other origins).

Pages: the sign-in form takes an email and a password; the registration form an email, a
password and the access code. Both sign the browser in on success (a new session, its token in
the cookie) and go on to `next`; signed in already, both go straight to `next`. `POST /logout`
deletes the session and the cookie and goes to `/login`. A failed sign-in says "Wrong email or
password." whichever was wrong (see `UserManager.authenticate`, which also spends one password
verification on an unknown email). JSON, for clients that are not browsers: `POST
/api/auth/register` (`{email, password, access_code, client?}`, 201) and `POST /api/auth/login`
(`{email, password, client?}`, 200) answer `{token, expires_at, user: {id, email,
created_at}}` and set no cookie; the client sends the token as `Authorization: Bearer <token>`.
`POST /api/auth/logout` deletes the session it is called with (204) and clears the cookie;
`GET /api/auth/me` is the signed-in user (`{id, email, created_at}`).

A session is a random token (`secrets.token_urlsafe`, 32 bytes) of which only the SHA-256 is
stored (`user_sessions`); it is valid for `SESSION_MAX_AGE` (30 days) from sign-in. Signing out
deletes it; changing a user's password or disabling the user deletes all of theirs. The cookie
is `HttpOnly`, `SameSite=Lax`, `Path=/`, and `Secure` when the request arrived over HTTPS
(`scope["scheme"]`, which uvicorn sets from `X-Forwarded-Proto` only for a proxy it trusts:
`$FORWARDED_ALLOW_IPS`, default loopback; the container trusts its host's proxy). The access
code is compared in constant time (`hmac.compare_digest` of SHA-256 digests). A session's
`last_seen_at` is written at most every few minutes. Accounts do not own data yet: everyone
signed in sees and changes the same library, pages and history.

Failed attempts are limited in memory (`AttemptLimiter`), per client address
(`scope["client"]`: the forwarded address only when uvicorn trusts the proxy): a failed sign-in
(the page or the JSON route) and a wrong access code (registration) count alike.
`FREE_FAILURES` (5) failures, then each further one locks the address out for `LOCKOUT_BASE`
(30 s) doubled per extra failure, at most `LOCKOUT_MAX` (1 hour); an attempt while locked out
is answered 429 with how long to wait, without checking anything. As a backstop against an
attacker who varies the address (a forwarded address can be forged when every proxy is
trusted), all failures together have the same policy after `OVERALL_FREE_FAILURES` (50). A
successful sign-in or a right access code clears the address's failures and the overall count;
the counts are lost on restart. No password, access code, token or cookie value is ever logged.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from teacher.config import Settings
from teacher.db.engine import session_scope
from teacher.managers.errors import DuplicateError
from teacher.managers.users import SESSION_LIFETIME, SignInError, UserManager
from teacher.passwords import dummy_hash
from teacher.web.common import to_iso
from teacher.web.deps import NO_SESSION, CurrentUser, UserDep

SESSION_COOKIE = "teacher_session"
SESSION_MAX_AGE = int(SESSION_LIFETIME.total_seconds())  # seconds

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
REGISTER_PATH = "/register"
HEALTH_PATH = "/healthz"
API_PREFIX = "/api/auth"
API_LOGIN_PATH = f"{API_PREFIX}/login"
API_REGISTER_PATH = f"{API_PREFIX}/register"
#: The static files the sign-in pages need, served without a session.
OPEN_STATIC = frozenset(
    {
        "/static/css/app.css",
        "/static/css/login.css",
        "/static/img/favicon.svg",
        "/static/img/favicon-32.png",
        "/static/img/apple-touch-icon.png",
    }
)
#: Open paths that may carry a session (so it is looked up): the sign-in pages and routes.
OPEN_PATHS = frozenset(
    {LOGIN_PATH, LOGOUT_PATH, REGISTER_PATH, API_LOGIN_PATH, API_REGISTER_PATH}
)
STATE_CHANGING = frozenset({"POST", "PUT", "PATCH", "DELETE"})

WRONG_ACCESS_CODE = "Wrong access code."
WEB_LABEL = "web"
API_LABEL = "api"

_log = logging.getLogger("teacher.web.auth")


class AttemptLimiter:
    """Counts failed attempts per key and locks a key out once it has more than
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
        """Count a failure for `key`; return the lockout it now has (seconds, 0: none)."""
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


@dataclass(frozen=True, slots=True)
class NewSession:
    """A session just created by signing in or registering; `token` is shown only once."""

    user: CurrentUser
    token: str
    expires_at: datetime


class AuthGate:
    """The access code, the database the accounts are in and the attempt limiters. `clock`
    (Unix seconds) is the time of the limiters and of the sessions. The access code is never
    shown (not in `repr`)."""

    def __init__(
        self, access_code: str, settings: Settings, *, clock: Callable[[], float] = time.time
    ) -> None:
        if not isinstance(access_code, str) or not access_code.strip():
            raise ValueError("the account gate needs a non-blank access code")
        self._code_digest = hashlib.sha256(access_code.encode("utf-8", "surrogatepass")).digest()
        self.settings = settings
        self.clock = clock
        self.per_address = AttemptLimiter(free_failures=FREE_FAILURES, clock=clock)
        self.overall = AttemptLimiter(free_failures=OVERALL_FREE_FAILURES, clock=clock)
        dummy_hash()  # made now, so the first sign-in with an unknown email is not slower

    def __repr__(self) -> str:
        return "AuthGate(<access code hidden>)"

    def now(self) -> datetime:
        """`clock` as a naive UTC datetime (what the session columns store)."""
        return datetime.fromtimestamp(self.clock(), UTC).replace(tzinfo=None)

    # -- the access code and the limiters ------------------------------------------------

    def check_access_code(self, candidate: str) -> bool:
        """Constant time: both sides are hashed to the same length, then compared."""
        digest = hashlib.sha256(candidate.encode("utf-8", "surrogatepass")).digest()
        return hmac.compare_digest(digest, self._code_digest)

    def login_wait(self, address: str) -> int:
        """Seconds until `address` may try again (0: now)."""
        return max(self.per_address.wait_seconds(address), self.overall.wait_seconds(OVERALL))

    def login_failed(self, address: str) -> int:
        """Count a failure; return the lockout now in force for `address` (seconds)."""
        mine = self.per_address.record_failure(address)
        overall = self.overall.record_failure(OVERALL)
        return max(mine, overall)

    def login_succeeded(self, address: str) -> None:
        self.per_address.record_success(address)
        self.overall.record_success(OVERALL)

    # -- accounts and sessions (blocking: call them in the thread pool) -------------------

    def _new_session(self, users: UserManager, user: Any, label: str) -> NewSession:
        row, token = users.create_session(user, label=label)
        current = CurrentUser(user.id, user.email, user.created_at, row.id)
        return NewSession(current, token, row.expires_at)

    def sign_in(self, email: object, password: object, *, label: str) -> NewSession:
        """A new session for the account; SignInError when the email and password do not
        sign in."""
        with session_scope(self.settings) as session:
            users = UserManager(session, self.settings, clock=self.now)
            return self._new_session(users, users.authenticate(email, password), label)

    def register(self, email: object, password: object, *, label: str) -> NewSession:
        """A new account and its first session (the access code is checked by the caller).
        ValueError for an email or password that breaks the rules, DuplicateError for an
        email that has an account."""
        if not isinstance(email, str) or not isinstance(password, str):
            raise ValueError("Enter an email address and a password.")
        with session_scope(self.settings) as session:
            users = UserManager(session, self.settings, clock=self.now)
            return self._new_session(users, users.create(email, password), label)

    def resolve(self, token: str) -> CurrentUser | None:
        """The user of a valid session token, or None."""
        with session_scope(self.settings) as session:
            row = UserManager(session, self.settings, clock=self.now).resolve_session(token)
            if row is None:
                return None
            return CurrentUser(row.user.id, row.user.email, row.user.created_at, row.id)

    def sign_out(self, token: str) -> bool:
        """Delete the session of `token`; whether there was one."""
        with session_scope(self.settings) as session:
            return UserManager(session, self.settings, clock=self.now).revoke_token(token)


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
    if parts.scheme or parts.netloc or parts.path in (LOGIN_PATH, LOGOUT_PATH, REGISTER_PATH):
        return "/"
    return value


def login_url(next_path: str, path: str = LOGIN_PATH) -> str:
    """`path` (the sign-in page, or the registration page) with `next` when it is not "/"."""
    target = safe_next(next_path)
    return path if target == "/" else f"{path}?next={quote(target, safe='')}"


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


def presented_token(request: Request) -> str | None:
    """The session token the request carries: the `Authorization: Bearer` header's when it
    has that header (any other scheme or an empty token: none), else the cookie's."""
    header = request.headers.get("authorization")
    if header is not None:
        scheme, _, value = header.strip().partition(" ")
        token = value.strip()
        return token if scheme.lower() == "bearer" and token else None
    return request.cookies.get(SESSION_COOKIE) or None


def _skips_lookup(path: str) -> bool:
    return path == HEALTH_PATH or path in OPEN_STATIC


def _is_api(path: str) -> bool:
    return path == "/api" or path.startswith("/api/")


# -- the middleware -----------------------------------------------------------------------


class AuthGateMiddleware:
    """A pure ASGI middleware: see the module docstring for what it lets through."""

    def __init__(self, app: Any, *, gate: AuthGate) -> None:
        self.app = app
        self.gate = gate

    async def _user(self, request: Request) -> CurrentUser | None:
        token = presented_token(request)
        user = await run_in_threadpool(self.gate.resolve, token) if token else None
        request.scope.setdefault("state", {})["user"] = user
        return user

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        if scope["type"] == "websocket":  # the app has none; never open one without a session
            if await self._user(request) is None:
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
        elif _skips_lookup(path):
            await self.app(scope, receive, send)
            return
        elif await self._user(request) is not None or path in OPEN_PATHS:
            await self.app(scope, receive, send)
            return
        elif method in ("GET", "HEAD") and not _is_api(path):
            query = scope.get("query_string", b"").decode("latin-1")
            response = RedirectResponse(
                login_url(path + (f"?{query}" if query else "")), status_code=303
            )
        else:
            response = JSONResponse({"detail": NO_SESSION}, status_code=401)
        response.headers["Cache-Control"] = "no-store"
        await response(scope, receive, send)


# -- the routes ---------------------------------------------------------------------------

router = APIRouter(include_in_schema=False)
api_router = APIRouter(prefix=API_PREFIX, tags=["auth"])


def _gate(request: Request) -> AuthGate:
    gate = getattr(request.app.state, "auth_gate", None)
    if gate is None:  # the routers are only included with a gate
        raise RuntimeError("the sign-in routes need app.state.auth_gate")
    return gate


def _request_user(request: Request) -> CurrentUser | None:
    """What the middleware found (it always sets it on these routes)."""
    if "user" not in request.scope.get("state", {}):
        raise RuntimeError("request.state.user is not set: is AuthGateMiddleware installed?")
    return request.scope["state"]["user"]


def _set_session_cookie(response: Response, request: Request, new: NewSession) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        new.token,
        max_age=SESSION_MAX_AGE,
        path="/",
        secure=request.url.scheme == "https",
        httponly=True,
        samesite="lax",
    )


def _clear_session_cookie(response: Response, request: Request) -> None:
    response.delete_cookie(
        SESSION_COOKIE,
        path="/",
        secure=request.url.scheme == "https",
        httponly=True,
        samesite="lax",
    )


def _form_text(form: Any, name: str) -> str:
    value = form.get(name)
    return value if isinstance(value, str) else ""


def _page(
    request: Request,
    template: str,
    *,
    next_path: str,
    email: str = "",
    error: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    from teacher.web.routers.pages import templates

    response = templates.TemplateResponse(
        request,
        template,
        {
            "next": next_path,
            "email": email,
            "error": error,
            "nav": None,
            "login_href": login_url(next_path),
            "register_href": login_url(next_path, REGISTER_PATH),
        },
        status_code=status_code,
    )
    response.headers["Cache-Control"] = "no-store"
    return response


def _locked_out(seconds: int) -> str:
    return f"Too many failed attempts. Wait {wait_text(seconds)}, then try again."


def _failed(message: str, lockout: int) -> str:
    if not lockout:
        return f"{message} Try again."
    return (
        f"{message} After too many failed attempts, trying is paused: wait "
        f"{wait_text(lockout)}, then try again."
    )


@router.get(LOGIN_PATH, response_class=HTMLResponse)
def login_form(request: Request, next: str | None = None) -> Response:
    target = safe_next(next)
    if _request_user(request) is not None:
        return RedirectResponse(target, status_code=303)
    return _page(request, "login.html", next_path=target)


@router.post(LOGIN_PATH, response_class=HTMLResponse)
async def login_submit(request: Request) -> Response:
    gate = _gate(request)
    form = await request.form()
    target = safe_next(form.get("next"))
    email = _form_text(form, "email")
    address = client_address(request.scope)
    wait = gate.login_wait(address)
    if wait:
        _log.warning("Sign-in refused for %s: locked out for %ss more", address, wait)
        response = _page(
            request, "login.html", next_path=target, email=email, error=_locked_out(wait),
            status_code=429,
        )
        response.headers["Retry-After"] = str(wait)
        return response
    try:
        new = await run_in_threadpool(
            gate.sign_in, email, _form_text(form, "password"), label=WEB_LABEL
        )
    except SignInError as exc:
        lockout = gate.login_failed(address)
        _log.warning("Failed sign-in from %s", address)
        return _page(
            request, "login.html", next_path=target, email=email,
            error=_failed(str(exc), lockout), status_code=401,
        )
    gate.login_succeeded(address)
    response = RedirectResponse(target, status_code=303)
    _set_session_cookie(response, request, new)
    return response


@router.get(REGISTER_PATH, response_class=HTMLResponse)
def register_form(request: Request, next: str | None = None) -> Response:
    target = safe_next(next)
    if _request_user(request) is not None:
        return RedirectResponse(target, status_code=303)
    return _page(request, "register.html", next_path=target)


@router.post(REGISTER_PATH, response_class=HTMLResponse)
async def register_submit(request: Request) -> Response:
    gate = _gate(request)
    form = await request.form()
    target = safe_next(form.get("next"))
    email = _form_text(form, "email")
    address = client_address(request.scope)

    def page(error: str, status_code: int) -> HTMLResponse:
        return _page(
            request, "register.html", next_path=target, email=email, error=error,
            status_code=status_code,
        )

    wait = gate.login_wait(address)
    if wait:
        _log.warning("Registration refused for %s: locked out for %ss more", address, wait)
        response = page(_locked_out(wait), 429)
        response.headers["Retry-After"] = str(wait)
        return response
    if not gate.check_access_code(_form_text(form, "access_code")):
        lockout = gate.login_failed(address)
        _log.warning("Wrong access code from %s", address)
        return page(_failed(WRONG_ACCESS_CODE, lockout), 401)
    gate.login_succeeded(address)
    try:
        new = await run_in_threadpool(
            gate.register, email, _form_text(form, "password"), label=WEB_LABEL
        )
    except DuplicateError:
        return page("An account with this email already exists. Sign in instead.", 409)
    except ValueError as exc:  # the email or password breaks a rule; the message says which
        return page(str(exc), 422)
    _log.info("Registered user %s", new.user.id)
    response = RedirectResponse(target, status_code=303)
    _set_session_cookie(response, request, new)
    return response


@router.post(LOGOUT_PATH)
async def logout(request: Request) -> Response:
    token = presented_token(request)
    if token:
        await run_in_threadpool(_gate(request).sign_out, token)
    response = RedirectResponse(LOGIN_PATH, status_code=303)
    _clear_session_cookie(response, request)
    return response


# -- JSON, for clients that are not browsers ----------------------------------------------


class LoginIn(BaseModel):
    email: str
    password: str
    # A short label for the session, e.g. "iPhone" (default "api").
    client: str | None = Field(default=None, min_length=1, max_length=100)


class RegisterIn(LoginIn):
    access_code: str


class UserOut(BaseModel):
    id: int
    email: str
    created_at: str


class SessionOut(BaseModel):
    token: str
    expires_at: str
    user: UserOut


def _user_out(user: CurrentUser) -> UserOut:
    return UserOut(id=user.id, email=user.email, created_at=to_iso(user.created_at) or "")


def _session_out(new: NewSession) -> SessionOut:
    return SessionOut(
        token=new.token, expires_at=to_iso(new.expires_at) or "", user=_user_out(new.user)
    )


def _json_locked_out(wait: int) -> JSONResponse:
    return JSONResponse(
        {"detail": _locked_out(wait)}, status_code=429, headers={"Retry-After": str(wait)}
    )


@api_router.post("/register", status_code=201, response_model=SessionOut)
async def api_register(body: RegisterIn, request: Request) -> Any:
    """A new account (it needs the access code) and a session for it."""
    gate = _gate(request)
    address = client_address(request.scope)
    wait = gate.login_wait(address)
    if wait:
        _log.warning("Registration refused for %s: locked out for %ss more", address, wait)
        return _json_locked_out(wait)
    if not gate.check_access_code(body.access_code):
        lockout = gate.login_failed(address)
        _log.warning("Wrong access code from %s", address)
        return JSONResponse({"detail": _failed(WRONG_ACCESS_CODE, lockout)}, status_code=401)
    gate.login_succeeded(address)
    # ValueError (422) and DuplicateError (409) go to the app's error handlers.
    new = await run_in_threadpool(
        gate.register, body.email, body.password, label=body.client or API_LABEL
    )
    _log.info("Registered user %s", new.user.id)
    return _session_out(new)


@api_router.post("/login", response_model=SessionOut)
async def api_login(body: LoginIn, request: Request) -> Any:
    """A new session for an account."""
    gate = _gate(request)
    address = client_address(request.scope)
    wait = gate.login_wait(address)
    if wait:
        _log.warning("Sign-in refused for %s: locked out for %ss more", address, wait)
        return _json_locked_out(wait)
    try:
        new = await run_in_threadpool(
            gate.sign_in, body.email, body.password, label=body.client or API_LABEL
        )
    except SignInError as exc:
        lockout = gate.login_failed(address)
        _log.warning("Failed sign-in from %s", address)
        return JSONResponse({"detail": _failed(str(exc), lockout)}, status_code=401)
    gate.login_succeeded(address)
    return _session_out(new)


@api_router.post("/logout", status_code=204)
async def api_logout(request: Request) -> Response:
    """Delete the session this request came with (and clear the cookie)."""
    token = presented_token(request)
    if token:
        await run_in_threadpool(_gate(request).sign_out, token)
    response = Response(status_code=204)
    _clear_session_cookie(response, request)
    return response


@api_router.get("/me", response_model=UserOut)
def api_me(user: UserDep) -> Any:
    """The signed-in user."""
    return _user_out(user)


def healthz() -> Response:
    """The host's health check: an empty 200, open even with the gate on."""
    return Response(status_code=200)


def install_auth_gate(
    app: FastAPI, access_code: str | None, settings: Settings
) -> AuthGate | None:
    """Add `GET /healthz` to `app` and, when `access_code` is given, the gate: the sign-in
    routes and `AuthGateMiddleware` (call it after the other middleware, so it is the
    outermost). Sets `app.state.auth_gate` (None without an access code: the header then shows
    no account and no "Sign out"). A blank access code is a ValueError."""
    gate = AuthGate(access_code, settings) if access_code is not None else None
    app.state.auth_gate = gate
    app.add_api_route(
        HEALTH_PATH, healthz, methods=["GET", "HEAD"], include_in_schema=False
    )
    if gate is not None:
        app.include_router(router)
        app.include_router(api_router)
        app.add_middleware(AuthGateMiddleware, gate=gate)
    return gate


__all__ = [
    "AttemptLimiter",
    "AuthGate",
    "AuthGateMiddleware",
    "CurrentUser",
    "NewSession",
    "install_auth_gate",
    "presented_token",
    "safe_next",
]
