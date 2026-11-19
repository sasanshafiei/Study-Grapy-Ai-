"""Errors returned to API clients.

Every handled error has the body {"detail": "<message for the user>", "code":
"<stable machine-readable code>"}, plus optional extra fields. The web app
shows `detail` as-is, so it is written for a person and never contains file
paths, SQL or exception text; those go to the server log only.
"""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse


class ApiError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        headers: dict[str, str] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.headers = headers
        self.extra = extra or {}


async def api_error_handler(_request: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.message, "code": exc.code, **exc.extra},
        headers=exc.headers,
    )


async def unhandled_error_handler(_request: Request, _exc: Exception) -> JSONResponse:
    """Last resort for unexpected errors: the same JSON shape, nothing about the cause.

    Starlette's ServerErrorMiddleware calls this and then re-raises the
    exception, so the server still logs the full traceback.
    """
    return JSONResponse(
        status_code=500,
        content={"detail": "Something went wrong on the server. Try again later.", "code": "server_error"},
    )
