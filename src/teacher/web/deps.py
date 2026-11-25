"""FastAPI dependencies: the app's settings, one database session per request and the signed-in
user.

Use the `Annotated` aliases in endpoint signatures:

    def endpoint(session: SessionDep, settings: SettingsDep, user: CurrentUserDep) -> ...

`CurrentUserDep` (`current_user`) is the `teacher.web.auth.CurrentUser` of the request (id,
email, created_at, session_id), or None when the app has no account gate (local use: nobody
signs in) or, with the gate on, on an open path without a session. `UserDep` (`require_user`)
is for routes that need an account: the user, or 401 without one; with the gate off there are
no accounts, so it is a 404 that says so.

`SessionDep` closes the session when the endpoint function returns (`scope="function"`), so the
commit happens before the response is sent and a failing commit becomes an error response. Build
the Pydantic response inside the endpoint: ORM objects are detached once it returns.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from teacher.config import Settings
from teacher.db.engine import session_factory


#: What a request without a valid session is told (401), by the account gate and `require_user`.
NO_SESSION = "Sign in first: there is no session, or it has expired."


@dataclass(frozen=True, slots=True)
class CurrentUser:
    """The signed-in user of a request (`request.state.user`, set by
    `teacher.web.auth.AuthGateMiddleware`) and the id of the session it came with."""

    id: int
    email: str
    created_at: datetime
    session_id: int


def get_settings(request: Request) -> Settings:
    """The `Settings` the app was created with (`app.state.settings`)."""
    return request.app.state.settings


def get_session(settings: Annotated[Settings, Depends(get_settings)]) -> Iterator[Session]:
    """Yield a session; commit if the endpoint succeeds, roll back if it raises, always close."""
    session = session_factory(settings)()
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()


def current_user(request: Request) -> CurrentUser | None:
    """The signed-in user (`request.state.user`, set by `AuthGateMiddleware`), or None: no
    gate (`app.state.auth_gate` is None), or no session on an open path."""
    if request.app.state.auth_gate is None:
        return None
    state = request.scope.get("state", {})
    if "user" not in state:  # the gate's middleware sets it on every request it lets through
        raise RuntimeError("request.state.user is not set although the account gate is on")
    return state["user"]


def require_user(
    request: Request, user: Annotated[CurrentUser | None, Depends(current_user)]
) -> CurrentUser:
    """The signed-in user; 401 without a session, 404 when the app has no accounts."""
    if request.app.state.auth_gate is None:
        raise HTTPException(404, "There are no accounts: this Teacher runs without sign-in.")
    if user is None:
        raise HTTPException(401, NO_SESSION)
    return user


SettingsDep = Annotated[Settings, Depends(get_settings)]
SessionDep = Annotated[Session, Depends(get_session, scope="function")]
CurrentUserDep = Annotated[CurrentUser | None, Depends(current_user)]
UserDep = Annotated[CurrentUser, Depends(require_user)]

__all__ = [
    "NO_SESSION",
    "CurrentUser",
    "CurrentUserDep",
    "SessionDep",
    "SettingsDep",
    "UserDep",
    "current_user",
    "get_session",
    "get_settings",
    "require_user",
]
