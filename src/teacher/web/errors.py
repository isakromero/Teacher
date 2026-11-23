"""Exception handlers: every error body is `{"detail": "<message>"}`.

| exception                                  | status |
|--------------------------------------------|--------|
| `NotFoundError`                            | 404    |
| `DuplicateError`, `SourceInUseError`       | 409    |
| any other `ManagerError`, `ValueError`     | 422    |
| `GradingUnavailableError`                  | 503    |
| `MetadataUnavailableError`                 | 503    |
| `TutorRequestError`                        | 422    |
| `TutorUnavailableError`                    | 503    |
| `GlossaryUnavailableError`                 | 503    |
| `GlossaryOutputError`                      | 502    |
| `GlossaryInputError` (a `ValueError`)      | 422    |
| `ConceptSearchUnavailableError`            | 503    |
| `RelationsUnavailableError`                | 503    |
| `RelationsIndexError`                      | 503    |
| `RelationsOutputError`                     | 502    |
| `RelationsGraphChangedError`               | 409    |
| any other `ConceptRelatingError`           | 500    |
| request validation (bad body/query/form)   | 422    |
| `fastapi.HTTPException`                    | its own status (FastAPI's default handler) |
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from teacher.concept_relating import (
    ConceptRelatingError,
    RelationsGraphChangedError,
    RelationsIndexError,
    RelationsOutputError,
    RelationsUnavailableError,
)
from teacher.db.fts import ConceptSearchUnavailableError
from teacher.glossary import (
    GlossaryInputError,
    GlossaryOutputError,
    GlossaryUnavailableError,
)
from teacher.grading import GradingUnavailableError
from teacher.managers.errors import (
    DuplicateError,
    ManagerError,
    NotFoundError,
    SourceInUseError,
)
from teacher.metadata import MetadataUnavailableError
from teacher.tutor import TutorRequestError, TutorUnavailableError

STATUS_BY_EXCEPTION: dict[type[Exception], int] = {
    NotFoundError: 404,
    DuplicateError: 409,
    SourceInUseError: 409,
    ManagerError: 422,
    ValueError: 422,
    GradingUnavailableError: 503,
    MetadataUnavailableError: 503,
    TutorRequestError: 422,
    TutorUnavailableError: 503,
    GlossaryUnavailableError: 503,
    GlossaryOutputError: 502,
    GlossaryInputError: 422,
    ConceptSearchUnavailableError: 503,
    RelationsUnavailableError: 503,
    RelationsIndexError: 503,
    RelationsOutputError: 502,
    RelationsGraphChangedError: 409,
    ConceptRelatingError: 500,
}


def _scrub_paths(message: str, root: Path | None) -> str:
    """Remove absolute data-root and temp-dir prefixes so no filesystem layout leaks out."""
    prefixes = [Path(tempfile.gettempdir()).resolve(), Path(tempfile.gettempdir())]
    if root is not None:
        prefixes.insert(0, root)
    for prefix in prefixes:
        message = message.replace(f"{prefix}{os.sep}", "").replace(str(prefix), ".")
    return message


def _detail(request: Request, exc: Exception) -> str:
    settings = getattr(request.app.state, "settings", None)
    root = getattr(settings, "root", None)
    return _scrub_paths(str(exc), root) or exc.__class__.__name__


def _make_handler(status: int):
    async def handler(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse({"detail": _detail(request, exc)}, status_code=status)

    return handler


def _location(loc: tuple) -> str:
    # Drop the leading "body"/"query"/"path"/"form" marker unless it is all there is.
    parts = [str(p) for p in loc]
    if len(parts) > 1:
        parts = parts[1:]
    return ".".join(parts)


async def request_validation_handler(
    _request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Flatten FastAPI's list of validation errors into one message string."""
    messages = [
        f"{_location(err.get('loc', ()))}: {err.get('msg', 'invalid')}" for err in exc.errors()
    ]
    return JSONResponse({"detail": "; ".join(messages) or "invalid request"}, status_code=422)


def install_error_handlers(app: FastAPI) -> None:
    """Register the handlers above on `app` (Starlette picks the most specific class by MRO)."""
    for exc_class, status in STATUS_BY_EXCEPTION.items():
        app.add_exception_handler(exc_class, _make_handler(status))
    app.add_exception_handler(RequestValidationError, request_validation_handler)


__all__ = ["STATUS_BY_EXCEPTION", "install_error_handlers", "request_validation_handler"]
