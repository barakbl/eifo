"""Conditional-request support for catalog responses.

Catalog data changes once a day at most, so the same grid is requested far more
often than it changes. An ETag lets a client revalidate cheaply and lets us
answer an unchanged request with 304 instead of re-serialising a page.
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

#: Catalog reads are public and briefly cacheable; user data never is.
CATALOG_CACHE_CONTROL = "public, max-age=300"

#: Paths whose responses are safe to cache and revalidate.
CACHEABLE_PREFIXES = ("/api/v1/titles", "/api/v1/sources", "/api/v1/genres")


def etag_for(body: bytes) -> str:
    """A weak validator over the response body."""
    return f'W/"{hashlib.sha256(body).hexdigest()[:32]}"'


class CatalogCacheMiddleware(BaseHTTPMiddleware):
    """Attach cache headers and answer matching conditional requests with 304."""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response = await call_next(request)

        if request.method != "GET" or response.status_code != 200:
            return response
        if not any(request.url.path.startswith(prefix) for prefix in CACHEABLE_PREFIXES):
            return response

        body = b"".join([chunk async for chunk in response.body_iterator])  # type: ignore[attr-defined]
        etag = etag_for(body)

        headers = dict(response.headers)
        headers["ETag"] = etag
        headers["Cache-Control"] = CATALOG_CACHE_CONTROL
        headers.pop("content-length", None)

        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers=headers)

        return Response(
            content=body,
            status_code=response.status_code,
            headers=headers,
            media_type=response.media_type,
        )
