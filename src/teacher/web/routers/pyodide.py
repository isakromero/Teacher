"""The lab runtime over HTTP: the Pyodide files for the browser, and whether they are installed.

- `GET /pyodide/{version}/{path}` serves a file of the installed runtime
  (`teacher.pyodide_runtime`). Only the pinned version and only the files its install manifest
  lists are served (anything else is a 404, as is everything when the runtime is not
  installed). The files are versioned and immutable, so they are cached for a year, and they
  carry `Access-Control-Allow-Origin: *` because the lab sandbox that loads them
  (`static/js/lab_sandbox.js`) runs in an iframe with an opaque origin, whose requests are
  cross-origin even to this server.
- `GET /api/labs/runtime` -> `{"installed", "version", "url", "packages", "detail"}`: `url` is
  where the files are served (`/pyodide/<version>/`), `packages` the packages labs may use, and
  `detail` the setup instruction when the runtime is not installed (else null). Messages have
  the data root and temp-dir prefixes removed, as the app's error responses do.

The lab driver the sandbox runs is not served here: it is a static file
(`static/js/lab_driver.py`), fetched by the host page itself and handed to the sandbox.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from teacher.pyodide_runtime import (
    ALLOWED_PACKAGES,
    PYODIDE_VERSION,
    RuntimeUnavailableError,
    check_runtime,
    runtime_dir,
    runtime_files,
)
from teacher.web.deps import SettingsDep
from teacher.web.errors import _scrub_paths

router = APIRouter(tags=["labs"])

RUNTIME_URL = f"/pyodide/{PYODIDE_VERSION}/"
CACHE_CONTROL = "public, max-age=31536000, immutable"
MEDIA_TYPES = {
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".wasm": "application/wasm",
    ".json": "application/json",
    ".zip": "application/zip",
    ".whl": "application/zip",
}


@router.get("/pyodide/{version}/{path:path}", include_in_schema=False)
def runtime_file(version: str, path: str, settings: SettingsDep) -> FileResponse:
    if version != PYODIDE_VERSION:
        raise HTTPException(404, f"Pyodide {version} is not the version Teacher serves "
                                 f"({PYODIDE_VERSION})")
    try:
        names = runtime_files(settings)
    except RuntimeUnavailableError as exc:
        raise HTTPException(404, _scrub_paths(str(exc), settings.root)) from exc
    if path not in names:
        raise HTTPException(404, f"{path!r} is not a file of the Pyodide runtime")
    suffix = path[path.rfind("."):] if "." in path else ""
    return FileResponse(
        runtime_dir(settings) / path,
        media_type=MEDIA_TYPES.get(suffix, "application/octet-stream"),
        headers={"Access-Control-Allow-Origin": "*", "Cache-Control": CACHE_CONTROL},
    )


@router.get("/api/labs/runtime")
def runtime_status(settings: SettingsDep) -> dict[str, Any]:
    try:
        check_runtime(settings)
    except RuntimeUnavailableError as exc:
        installed, detail = False, _scrub_paths(str(exc), settings.root)
    else:
        installed, detail = True, None
    return {
        "installed": installed,
        "version": PYODIDE_VERSION,
        "url": RUNTIME_URL,
        "packages": list(ALLOWED_PACKAGES),
        "detail": detail,
    }
