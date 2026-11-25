"""UserManager: accounts (normalised emails, the password rules, duplicates, disabling), signing
in (one generic error, the dummy verification, rehashing) and sessions (only the token's hash
stored, expiry, the throttled last-seen time, revoking), on SQLite and Postgres."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from teacher.config import Settings
from teacher.db.models import User, UserSession
from teacher.managers import DuplicateError, NotFoundError, SignInError, UserManager
from teacher.managers import users as users_module
from teacher.managers.users import (
    SEEN_INTERVAL,
    SESSION_LIFETIME,
    SIGN_IN_FAILED,
    normalise_email,
    token_sha256,
)
from teacher.passwords import ScryptParams
from tests.conftest import CHECK_VIOLATION, UNIQUE_VIOLATION

EMAIL = "reader@example.com"
PASSWORD = "a long enough password"
#: Cheap parameters for the tests that make many hashes (a hash carries its parameters).
CHEAP = ScryptParams(log_n=4, r=8, p=1)
START = datetime(2026, 11, 25, 12, 0, 0)


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def users(session: Session, settings: Settings, clock: Clock) -> UserManager:
    return UserManager(session, settings, clock=clock, params=CHEAP)


@pytest.fixture
def user(users: UserManager) -> User:
    return users.create(EMAIL, PASSWORD)


# -- emails ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("reader@example.com", "reader@example.com"),
        ("  Reader@Example.COM\n", "reader@example.com"),
        ("a@b", "a@b"),
        ("first.last+tag@sub.example.org", "first.last+tag@sub.example.org"),
        ("ÄNNE@exämple.de", "änne@exämple.de"),
        ("x" * 248 + "@ab.io", "x" * 248 + "@ab.io"),  # 254 characters
    ],
)
def test_normalise_email(given: str, expected: str) -> None:
    assert normalise_email(given) == expected


@pytest.mark.parametrize(
    ("given", "message"),
    [
        ("", "Enter an email address."),
        ("   ", "Enter an email address."),
        ("plain", "must look like"),
        ("@example.com", "must look like"),
        ("reader@", "must look like"),
        ("a@b@c", "must look like"),
        ("re ader@example.com", "must not contain spaces"),
        ("reader@exa\tmple.com", "must not contain spaces"),
        ("reader@example.com\x00", "must not contain spaces"),
        ("x" * 249 + "@ab.io", "at most 254 characters"),
        (None, "must be text"),
    ],
)
def test_bad_emails(given: object, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        normalise_email(given)


# -- accounts -------------------------------------------------------------------------------


def test_create(users: UserManager, user: User, clock: Clock) -> None:
    assert user.id and user.email == EMAIL
    assert user.created_at == START
    assert user.last_login_at is None and user.disabled_at is None
    assert user.password_hash.startswith("scrypt$ln=4,r=8,p=1$")
    assert PASSWORD not in user.password_hash
    assert users.get(user.id) is user
    assert users.get_by_email(" READER@example.com") is user
    assert users.find("other@example.com") is None
    assert users.list() == [user]
    assert PASSWORD not in repr(user) and user.password_hash not in repr(user)


def test_the_default_parameters_are_used(session: Session, settings: Settings) -> None:
    created = UserManager(session, settings).create(EMAIL, PASSWORD)
    assert created.password_hash.startswith("scrypt$ln=15,r=8,p=1$")


def test_an_email_has_one_account(users: UserManager, user: User) -> None:
    with pytest.raises(DuplicateError, match="already exists"):
        users.create("Reader@Example.com ", "another password")
    users.create("other@example.com", PASSWORD)
    assert [u.email for u in users.list()] == [EMAIL, "other@example.com"]


def test_the_database_refuses_a_duplicate_too(session: Session, user: User) -> None:
    session.add(User(email=EMAIL, password_hash="x"))
    with pytest.raises(IntegrityError, match=UNIQUE_VIOLATION):
        session.flush()


def test_the_database_checks_the_email_length(session: Session) -> None:
    session.add(User(email="ab", password_hash="x"))
    with pytest.raises(IntegrityError, match=CHECK_VIOLATION):
        session.flush()


@pytest.mark.parametrize(
    ("password", "message"),
    [("seven..", "at least 8"), ("x" * 1025, "at most 1024"), (None, "must be text")],
)
def test_create_checks_the_password(users: UserManager, password: object, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        users.create(EMAIL, password)  # type: ignore[arg-type]
    assert users.list() == []


def test_lookups_that_find_nothing(users: UserManager) -> None:
    with pytest.raises(NotFoundError, match="no user with id 99"):
        users.get(99)
    with pytest.raises(NotFoundError, match="no account with the email nobody@example.com"):
        users.get_by_email("Nobody@example.com")


def test_disable_and_enable(users: UserManager, user: User, clock: Clock) -> None:
    users.create_session(user)
    users.create_session(user)
    clock.now += timedelta(hours=1)
    assert users.disable(user) == 2
    assert user.disabled_at == clock.now
    assert users.sessions(user) == []
    with pytest.raises(ValueError, match="already disabled"):
        users.disable(user)
    with pytest.raises(ValueError, match="is disabled"):
        users.create_session(user)
    users.enable(user)
    assert user.disabled_at is None
    with pytest.raises(ValueError, match="is not disabled"):
        users.enable(user)


def test_set_password_signs_out_everywhere(users: UserManager, user: User) -> None:
    _row, token = users.create_session(user)
    old_hash = user.password_hash
    assert users.set_password(user, "a brand new password") == 1
    assert user.password_hash != old_hash
    assert users.resolve_session(token) is None
    assert users.authenticate(EMAIL, "a brand new password") is user
    with pytest.raises(SignInError):
        users.authenticate(EMAIL, PASSWORD)
    with pytest.raises(ValueError, match="at least 8"):
        users.set_password(user, "short")


def test_deleting_a_user_deletes_their_sessions(
    session: Session, users: UserManager, user: User
) -> None:
    users.create_session(user)
    session.execute(text("DELETE FROM users WHERE id = :id"), {"id": user.id})
    assert session.scalar(select(func.count()).select_from(UserSession)) == 0


# -- signing in -----------------------------------------------------------------------------


def test_authenticate(users: UserManager, user: User) -> None:
    assert users.authenticate(" READER@example.com ", PASSWORD) is user
    assert user.last_login_at is None  # only a session counts as a sign-in


@pytest.mark.parametrize(
    ("email", "password"),
    [
        (EMAIL, "a long enough passworD"),
        (EMAIL, f" {PASSWORD}"),  # never stripped
        (EMAIL, ""),
        (EMAIL, "x" * 2000),
        ("nobody@example.com", PASSWORD),
        ("not an email", PASSWORD),
        (None, PASSWORD),
        (EMAIL, None),
    ],
)
def test_sign_in_failures_say_the_same(
    users: UserManager, user: User, email: object, password: object
) -> None:
    with pytest.raises(SignInError) as info:
        users.authenticate(email, password)
    assert str(info.value) == SIGN_IN_FAILED == "Wrong email or password."


def test_a_disabled_account_fails_like_a_wrong_password(users: UserManager, user: User) -> None:
    users.disable(user)
    with pytest.raises(SignInError, match="^Wrong email or password.$"):
        users.authenticate(EMAIL, PASSWORD)


def test_an_unknown_email_still_verifies_a_password(
    users: UserManager, user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    real = users_module.verify_password
    monkeypatch.setattr(
        users_module, "verify_password", lambda p, stored: calls.append(stored) or real(p, stored)
    )
    with pytest.raises(SignInError):
        users.authenticate("nobody@example.com", PASSWORD)
    (stored,) = calls
    assert stored == users_module.dummy_hash(CHEAP) and stored != user.password_hash


def test_an_old_hash_is_replaced_after_signing_in(
    session: Session, settings: Settings, users: UserManager, user: User
) -> None:
    stronger = ScryptParams(log_n=5, r=8, p=1)
    upgraded = UserManager(session, settings, params=stronger)
    old_hash = user.password_hash
    assert upgraded.authenticate(EMAIL, PASSWORD) is user
    assert user.password_hash != old_hash and user.password_hash.startswith("scrypt$ln=5,")
    assert upgraded.authenticate(EMAIL, PASSWORD) is user  # and it still signs in
    with pytest.raises(SignInError):
        upgraded.authenticate(EMAIL, "wrong password")
    assert user.password_hash.startswith("scrypt$ln=5,")


def test_a_corrupt_stored_hash_fails_loudly(users: UserManager, user: User) -> None:
    user.password_hash = "not a hash"
    with pytest.raises(ValueError, match="malformed"):
        users.authenticate(EMAIL, PASSWORD)


# -- sessions -------------------------------------------------------------------------------


def test_create_session(session: Session, users: UserManager, user: User, clock: Clock) -> None:
    clock.now += timedelta(minutes=1)
    row, token = users.create_session(user, label="  iPhone ")
    assert len(token) >= 43
    assert row.token_sha256 == token_sha256(token) == hashlib.sha256(token.encode()).hexdigest()
    assert (row.created_at, row.last_seen_at) == (clock.now, clock.now)
    assert row.expires_at == clock.now + SESSION_LIFETIME == clock.now + timedelta(days=30)
    assert row.label == "iPhone"
    assert user.last_login_at == clock.now
    # The token itself is stored nowhere.
    stored = session.execute(text("SELECT * FROM user_sessions")).one()
    assert token not in [str(value) for value in stored]
    assert token not in repr(row) and row.token_sha256 not in repr(row)
    other, other_token = users.create_session(user)
    assert other_token != token and other.label is None
    assert users.sessions(user) == [row, other]


@pytest.mark.parametrize(("label", "message"), [("", "non-empty"), ("x" * 101, "at most 100")])
def test_session_labels(users: UserManager, user: User, label: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        users.create_session(user, label=label)


def test_resolve_session(users: UserManager, user: User) -> None:
    row, token = users.create_session(user)
    found = users.resolve_session(token)
    assert found is row and found.user is user
    for bad in (None, "", "short", token + "x", token[:-1] + ("A" if token[-1] != "A" else "B"),
                token_sha256(token), "x" * 300, "bad token with spaces " * 2, 42):
        assert users.resolve_session(bad) is None, bad


def test_sessions_expire_and_are_then_deleted(
    users: UserManager, user: User, clock: Clock
) -> None:
    _row, token = users.create_session(user)
    clock.now += SESSION_LIFETIME - timedelta(seconds=1)
    assert users.resolve_session(token) is not None
    clock.now += timedelta(seconds=1)
    assert users.resolve_session(token) is None
    assert users.sessions(user) == []


def test_a_new_sign_in_forgets_expired_sessions(
    users: UserManager, user: User, clock: Clock
) -> None:
    users.create_session(user)
    clock.now += SESSION_LIFETIME
    fresh, _token = users.create_session(user)
    assert users.sessions(user) == [fresh]


def test_a_disabled_users_session_does_not_resolve(
    session: Session, users: UserManager, user: User
) -> None:
    _row, token = users.create_session(user)
    user.disabled_at = START  # set directly: the sessions stay, but count for nothing
    session.flush()
    assert users.resolve_session(token) is None


def test_last_seen_is_updated_at_most_every_few_minutes(
    users: UserManager, user: User, clock: Clock
) -> None:
    row, token = users.create_session(user)
    clock.now += SEEN_INTERVAL - timedelta(seconds=1)
    users.resolve_session(token)
    assert row.last_seen_at == START
    assert not inspect(row).modified
    clock.now += timedelta(seconds=1)
    users.resolve_session(token)
    assert row.last_seen_at == START + SEEN_INTERVAL


def test_revoke(users: UserManager, user: User) -> None:
    first, first_token = users.create_session(user)
    _second, second_token = users.create_session(user)
    third, third_token = users.create_session(user)
    users.revoke_session(first)
    assert users.resolve_session(first_token) is None
    assert users.revoke_token(second_token) is True
    assert users.revoke_token(second_token) is False
    assert users.revoke_token("not a token at all!") is False
    assert users.sessions(user) == [third]
    other = users.create("other@example.com", PASSWORD)
    _mine, other_token = users.create_session(other)
    assert users.revoke_all_sessions(user) == 1
    assert users.resolve_session(third_token) is None
    assert users.resolve_session(other_token) is not None  # another user's are kept


def test_the_database_checks_sessions(session: Session, user: User) -> None:
    def add(**overrides: object) -> None:
        values = {"user_id": user.id, "token_sha256": "a" * 64, "created_at": START,
                  "last_seen_at": START, "expires_at": START + timedelta(days=1)}
        values.update(overrides)
        nested = session.begin_nested()
        session.add(UserSession(**values))
        try:
            session.flush()
        finally:
            nested.rollback()

    with pytest.raises(IntegrityError, match=CHECK_VIOLATION):
        add(token_sha256="short")
    with pytest.raises(IntegrityError, match=CHECK_VIOLATION):
        add(expires_at=START)
    with pytest.raises(IntegrityError, match=CHECK_VIOLATION):
        add(label="x" * 101)
    session.add(UserSession(user_id=user.id, token_sha256="b" * 64, created_at=START,
                            last_seen_at=START, expires_at=START + timedelta(days=1)))
    session.flush()
    with pytest.raises(IntegrityError, match=UNIQUE_VIOLATION):
        add(token_sha256="b" * 64)
