"""UserManager: user accounts and their sign-in sessions.

Accounts: `create` (registration, and `teacher users add`) stores a normalised email
(`normalise_email`: stripped, lowercased, one "@" with text on both sides, no whitespace or
control character, at most `MAX_EMAIL_CHARS` characters) and a scrypt hash of the password
(`teacher.passwords`: 8 to 1024 characters, used exactly as given). An email that already has
an account is a DuplicateError. `set_password`, `disable` and `enable` change an account;
changing the password or disabling the account deletes all its sessions, so it is signed out
everywhere. Disabling an account that is disabled (or enabling one that is not) is a
ValueError.

Signing in: `authenticate(email, password)` returns the user, or raises `SignInError`, whose
message never says whether the email or the password was wrong (or that the account is
disabled). For an email without an account it still verifies the password against
`teacher.passwords.dummy_hash()`, so the time taken does not tell which emails have accounts.
After a successful sign-in a hash made with older scrypt parameters is replaced
(`needs_rehash`).

Sessions: `create_session(user)` makes a random token (`secrets.token_urlsafe(TOKEN_BYTES)`),
stores only its SHA-256 (hex) and returns the token once; the session is valid for
`SESSION_LIFETIME` (30 days) from its creation and sets the user's `last_login_at`.
`resolve_session(token)` returns the session (with its user) while it is valid: not expired and
the user not disabled. An expired session found this way is deleted. `last_seen_at` is updated
only when it is at least `SEEN_INTERVAL` old, so a busy client does not write on every request.
`revoke_session` deletes one session, `revoke_all_sessions` all of a user's.

Time comes from `clock` (naive UTC, default `teacher.db.models.utcnow`). Like every manager it
flushes and never commits. Passwords and tokens are never logged or put in an error message.
"""

from __future__ import annotations

import builtins
import hashlib
import re
import secrets
from collections.abc import Callable
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from teacher.config import Settings
from teacher.db.models import User, UserSession, utcnow
from teacher.managers.artifacts import _is_unique_violation, _require_persistent
from teacher.managers.errors import DuplicateError, ManagerError, NotFoundError
from teacher.passwords import (
    DEFAULT_PARAMS,
    MAX_PASSWORD_CHARS,
    ScryptParams,
    check_password_rules,
    dummy_hash,
    hash_password,
    needs_rehash,
    verify_password,
)

MAX_EMAIL_CHARS = 254
MAX_LABEL_CHARS = 100
TOKEN_BYTES = 32
SESSION_LIFETIME = timedelta(days=30)
SEEN_INTERVAL = timedelta(minutes=5)
#: What `secrets.token_urlsafe` produces; anything else is not looked up.
_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{16,256}")

SIGN_IN_FAILED = "Wrong email or password."


class SignInError(ManagerError):
    """The email and password do not sign in; the message is always `SIGN_IN_FAILED`."""

    def __init__(self) -> None:
        super().__init__(SIGN_IN_FAILED)


def normalise_email(email: object) -> str:
    """`email` stripped and lowercased, when it has the basic shape of an address (see the
    module docstring); else a ValueError saying what is wrong."""
    if not isinstance(email, str):
        raise ValueError("The email must be text.")
    value = email.strip().lower()
    if not value:
        raise ValueError("Enter an email address.")
    if len(value) > MAX_EMAIL_CHARS:
        raise ValueError(f"The email address must have at most {MAX_EMAIL_CHARS} characters.")
    if any(c.isspace() or ord(c) < 0x20 or ord(c) == 0x7F for c in value):
        raise ValueError("The email address must not contain spaces.")
    local, at, domain = value.partition("@")
    if not at or not local or not domain or "@" in domain:
        raise ValueError("The email address must look like name@example.com.")
    return value


def token_sha256(token: str) -> str:
    """The hex SHA-256 of a session token: what `user_sessions.token_sha256` holds."""
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _label(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("a session label must be a non-empty string or None")
    value = value.strip()
    if len(value) > MAX_LABEL_CHARS:
        raise ValueError(f"a session label must have at most {MAX_LABEL_CHARS} characters")
    return value


class UserManager:
    """Create, read and change user accounts; create, resolve and revoke their sessions."""

    def __init__(
        self,
        session: Session,
        settings: Settings,
        *,
        clock: Callable[[], datetime] = utcnow,
        params: ScryptParams = DEFAULT_PARAMS,
    ) -> None:
        self.session = session
        self.settings = settings
        self.clock = clock
        self.params = params

    # -- accounts ------------------------------------------------------------------------

    def create(self, email: str, password: str) -> User:
        """A new account; DuplicateError when the email has one already, ValueError when the
        email or password breaks the rules."""
        address = normalise_email(email)
        check_password_rules(password)
        if self.find(address) is not None:
            raise DuplicateError(f"an account with the email {address} already exists")
        user = User(
            email=address,
            password_hash=hash_password(password, self.params),
            created_at=self.clock(),
        )
        try:
            with self.session.begin_nested():
                self.session.add(user)
                self.session.flush()
        except IntegrityError as err:  # registered at the same moment by another request
            if _is_unique_violation(err):
                raise DuplicateError(f"an account with the email {address} already exists") from err
            raise
        return user

    def find(self, email: str) -> User | None:
        """The account with this (normalised) email, or None."""
        return self.session.scalar(select(User).where(User.email == normalise_email(email)))

    def get(self, user_id: int) -> User:
        user = self.session.get(User, user_id)
        if user is None:
            raise NotFoundError(f"no user with id {user_id}")
        return user

    def get_by_email(self, email: str) -> User:
        user = self.find(email)
        if user is None:
            raise NotFoundError(f"no account with the email {normalise_email(email)}")
        return user

    def list(self) -> builtins.list[User]:
        """Every account, oldest first."""
        return list(self.session.scalars(select(User).order_by(User.id)))

    def set_password(self, user: User, password: str) -> int:
        """Store a new password hash and sign the user out everywhere; how many sessions were
        deleted."""
        _require_persistent(user, "user")
        user.password_hash = hash_password(check_password_rules(password), self.params)
        return self.revoke_all_sessions(user)

    def disable(self, user: User) -> int:
        """Stop the user from signing in and sign them out everywhere; how many sessions were
        deleted."""
        _require_persistent(user, "user")
        if user.disabled_at is not None:
            raise ValueError(f"the account {user.email} is already disabled")
        user.disabled_at = self.clock()
        return self.revoke_all_sessions(user)

    def enable(self, user: User) -> None:
        _require_persistent(user, "user")
        if user.disabled_at is None:
            raise ValueError(f"the account {user.email} is not disabled")
        user.disabled_at = None
        self.session.flush()

    # -- signing in ----------------------------------------------------------------------

    def authenticate(self, email: object, password: object) -> User:
        """The enabled user with this email and password; anything else is a SignInError."""
        try:
            address = normalise_email(email)
        except ValueError:
            address = None
        if not isinstance(password, str) or not password or len(password) > MAX_PASSWORD_CHARS:
            raise SignInError()  # no account can have it; says nothing about the email
        user = self.session.scalar(select(User).where(User.email == address)) if address else None
        if user is None:
            verify_password(password, dummy_hash(self.params))
            raise SignInError()
        if not verify_password(password, user.password_hash) or user.disabled_at is not None:
            raise SignInError()
        if needs_rehash(user.password_hash, self.params):
            user.password_hash = hash_password(password, self.params)
            self.session.flush()
        return user

    # -- sessions ------------------------------------------------------------------------

    def create_session(self, user: User, *, label: str | None = None) -> tuple[UserSession, str]:
        """A new session for the enabled `user` and its token (shown only here)."""
        _require_persistent(user, "user")
        if user.disabled_at is not None:
            raise ValueError(f"the account {user.email} is disabled")
        now = self.clock()
        token = secrets.token_urlsafe(TOKEN_BYTES)
        row = UserSession(
            user_id=user.id,
            token_sha256=token_sha256(token),
            created_at=now,
            last_seen_at=now,
            expires_at=now + SESSION_LIFETIME,
            label=_label(label),
        )
        self.session.add(row)
        user.last_login_at = now
        # Forget the user's expired sessions while here.
        self.session.execute(
            delete(UserSession).where(UserSession.user_id == user.id, UserSession.expires_at <= now)
        )
        self.session.flush()
        self.session.expire(user, ["sessions"])
        return row, token

    def resolve_session(self, token: object) -> UserSession | None:
        """The valid session of `token` (its `user` loaded), or None: no such session, it has
        expired (then it is deleted) or its user is disabled."""
        if not isinstance(token, str) or _TOKEN_RE.fullmatch(token) is None:
            return None
        row = self.session.scalar(
            select(UserSession)
            .options(joinedload(UserSession.user))
            .where(UserSession.token_sha256 == token_sha256(token))
        )
        if row is None:
            return None
        now = self.clock()
        if row.expires_at <= now:
            self.session.delete(row)
            self.session.flush()
            return None
        if row.user.disabled_at is not None:
            return None
        if now - row.last_seen_at >= SEEN_INTERVAL:
            row.last_seen_at = now
            self.session.flush()
        return row

    def revoke_session(self, row: UserSession) -> None:
        _require_persistent(row, "session")
        self.session.delete(row)
        self.session.flush()

    def revoke_token(self, token: object) -> bool:
        """Delete the session of `token`, if there is one (expired or not); whether there was."""
        if not isinstance(token, str) or _TOKEN_RE.fullmatch(token) is None:
            return False
        result = self.session.execute(
            delete(UserSession).where(UserSession.token_sha256 == token_sha256(token))
        )
        self.session.flush()
        return bool(result.rowcount)

    def revoke_all_sessions(self, user: User) -> int:
        """Delete every session of `user`; how many there were."""
        _require_persistent(user, "user")
        result = self.session.execute(delete(UserSession).where(UserSession.user_id == user.id))
        self.session.flush()
        self.session.expire(user, ["sessions"])
        return int(result.rowcount)

    def sessions(self, user: User) -> builtins.list[UserSession]:
        """The user's sessions (valid or expired), oldest first."""
        _require_persistent(user, "user")
        return list(
            self.session.scalars(
                select(UserSession).where(UserSession.user_id == user.id).order_by(UserSession.id)
            )
        )


__all__ = [
    "MAX_EMAIL_CHARS",
    "SEEN_INTERVAL",
    "SESSION_LIFETIME",
    "SIGN_IN_FAILED",
    "SignInError",
    "UserManager",
    "normalise_email",
    "token_sha256",
]
