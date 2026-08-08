"""Cache policy: aggressive for the catalog, forbidden for user data.

Catalog data changes once a day at most, so the same grid is requested far more
often than it changes. An ETag lets a client revalidate cheaply and lets us
answer an unchanged request with 304 instead of re-serialising a page.

User data gets the opposite treatment. A shared cache holding someone's list —
or a browser handing it back after they signed out — is the kind of leak nobody
notices until it matters, so those responses are marked ``no-store`` whatever
they contain.
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

#: Catalog reads are public and briefly cacheable; user data never is.
CATALOG_CACHE_CONTROL = "public, max-age=300"
NO_STORE = "no-store"

#: Paths whose responses are safe to cache and revalidate.
CACHEABLE_PREFIXES = ("/api/v1/titles", "/api/v1/sources", "/api/v1/genres")

#: Paths whose responses must never be written down anywhere.
PRIVATE_PREFIXES = ("/api/v1/me", "/api/v1/auth")


def etag_for(body: bytes) -> str:
    """A weak validator over the response body."""
    return f'W/"{hashlib.sha256(body).hexdigest()[:32]}"'


def is_private_path(path: str) -> bool:
    """Whether a path may carry user data, whatever the response turned out to be."""
    return any(path.startswith(prefix) for prefix in PRIVATE_PREFIXES)


class CatalogCacheMiddleware(BaseHTTPMiddleware):
    """Attach cache headers and answer matching conditional requests with 304."""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response = await call_next(request)

        # Applied by path rather than by outcome: a 401 or a validation error on
        # a user route is exactly as unfit for a cache as the data itself.
        if is_private_path(request.url.path):
            response.headers["Cache-Control"] = NO_STORE
            return response

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
