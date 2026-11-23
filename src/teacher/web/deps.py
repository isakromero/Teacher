"""FastAPI dependencies: the app's settings and one database session per request.

Use the `Annotated` aliases in endpoint signatures:

    def endpoint(session: SessionDep, settings: SettingsDep) -> ...

`SessionDep` closes the session when the endpoint function returns (`scope="function"`), so the
commit happens before the response is sent and a failing commit becomes an error response. Build
the Pydantic response inside the endpoint: ORM objects are detached once it returns.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from teacher.config import Settings
from teacher.db.engine import session_factory


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


SettingsDep = Annotated[Settings, Depends(get_settings)]
SessionDep = Annotated[Session, Depends(get_session, scope="function")]

__all__ = ["SessionDep", "SettingsDep", "get_session", "get_settings"]
