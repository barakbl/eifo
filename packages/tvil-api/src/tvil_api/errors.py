"""RFC 9457 problem-details error responses.

Every error the API emits has the same shape, so the client has exactly one
error path to handle (docs.internal/07-api.md).
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from tvil_api.static import client_fallback, is_api_path

PROBLEM_MEDIA_TYPE = "application/problem+json"


class Problem(BaseModel):
    """The problem-details body."""

    type: str = "about:blank"
    title: str
    status: int
    detail: str | None = None


def problem_response(
    status: int,
    title: str,
    detail: str | None = None,
    *,
    type_: str = "about:blank",
    extra: dict[str, Any] | None = None,
) -> JSONResponse:
    """Build a problem-details response."""
    body = Problem(type=type_, title=title, status=status, detail=detail).model_dump()
    if extra:
        body.update(extra)
    return JSONResponse(status_code=status, content=body, media_type=PROBLEM_MEDIA_TYPE)


async def _http_exception_handler(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, StarletteHTTPException)

    # A 404 outside the API is a client deep link: hand back the app rather
    # than a problem document the browser cannot render.
    if exc.status_code == 404 and not is_api_path(request.url.path):
        fallback = client_fallback(request.app)
        if fallback is not None:
            return fallback

    detail = exc.detail if isinstance(exc.detail, str) else None
    return problem_response(
        status=exc.status_code,
        title=_TITLES.get(exc.status_code, "Request failed"),
        detail=detail,
    )


async def _validation_exception_handler(_request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    return problem_response(
        status=422,
        title="Invalid request",
        detail="One or more parameters failed validation.",
        extra={"errors": _serialisable_errors(exc)},
    )


def _serialisable_errors(exc: RequestValidationError) -> list[dict[str, Any]]:
    """Validation errors trimmed to what a client can act on.

    Pydantic puts the offending input under ``ctx``/``input``, which is not
    always JSON-serialisable, so only the stable fields are kept.
    """
    return [
        {
            "location": list(error.get("loc", ())),
            "message": error.get("msg", ""),
            "type": error.get("type", ""),
        }
        for error in exc.errors()
    ]


_TITLES = {
    400: "Bad request",
    401: "Authentication required",
    403: "Forbidden",
    404: "Not found",
    409: "Conflict",
    429: "Too many requests",
    500: "Internal server error",
}


def install_error_handlers(app: FastAPI) -> None:
    """Register the problem-details handlers on an application."""
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
    app.add_exception_handler(RequestValidationError, _validation_exception_handler)
