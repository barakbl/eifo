"""Serving artwork and the web client.

One process serves the API, the downloaded images and the static client, so a
deployment is a single container with no reverse-proxy rules to get right.

The client's deep-link fallback is a 404 handler rather than a catch-all route.
A catch-all would sit in the routing table shadowing anything registered after
it, and would turn a genuinely missing API path into a 200 page.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session, sessionmaker
from starlette.middleware.base import BaseHTTPMiddleware

from eifo_api.security import SESSION_COOKIE, bearer_token
from eifo_api.sessions import resolve_api_token, resolve_session
from eifo_core.settings import Settings

logger = logging.getLogger("eifo.api.static")

#: Image paths embed the variant, so a changed image is a changed URL.
IMAGE_CACHE_CONTROL = "public, max-age=31536000, immutable"
#: The client is small and changes on deploy; revalidate rather than cache hard.
CLIENT_CACHE_CONTROL = "public, max-age=60"

#: Paths that belong to the API. A 404 under one of these is a real 404.
API_PREFIXES = ("/api/", "/docs", "/redoc", "/openapi.json", "/images/")

#: Client subdirectories served verbatim when present.
ASSET_DIRS = ("css", "js", "assets")


class CachedStaticFiles(StaticFiles):
    """Static files served under a stated cache policy."""

    cache_control = ""

    def file_response(self, *args: object, **kwargs: object) -> Response:
        response = super().file_response(*args, **kwargs)  # type: ignore[arg-type]
        response.headers["Cache-Control"] = self.cache_control
        return response


class ImmutableStaticFiles(CachedStaticFiles):
    """Artwork: the path names the variant, so a changed image is a changed URL."""

    cache_control = IMAGE_CACHE_CONTROL


class ClientStaticFiles(CachedStaticFiles):
    """The client's own modules and stylesheets.

    They said nothing about caching, and a browser told nothing guesses - from
    how old the file already was, so a module untouched for a fortnight was
    still being served from cache long after it had been replaced on disk. The
    page itself has always carried this policy; the files it loads had been
    left out of it, which is a confusing way to ship a change.
    """

    cache_control = CLIENT_CACHE_CONTROL


def is_api_path(path: str) -> bool:
    """Whether a path belongs to the API rather than the client."""
    return any(path.startswith(prefix) for prefix in API_PREFIXES)


class MembersOnlyImages(BaseHTTPMiddleware):
    """Close the artwork to strangers when the catalog is closed to them.

    Artwork is catalog. A poster path is ``/images/posters/<title id>/w500.jpg``
    - guessable by counting - so leaving the mount open on a private instance
    would publish, one integer at a time, exactly the thing the sign-in was put
    there to keep private.

    Middleware rather than a dependency because a ``StaticFiles`` mount has no
    dependencies to hang one on. It does the same work ``require_membership``
    does, through the same functions, so the two cannot come to different
    conclusions.
    """

    async def dispatch(self, request: Request, call_next: Callable[..., Any]) -> Response:
        if not request.url.path.startswith("/images/"):
            return await call_next(request)

        settings: Settings = request.app.state.settings
        if not settings.members_only:
            return await call_next(request)

        factory: sessionmaker[Session] = request.app.state.session_factory
        with factory() as session:
            allowed = _has_a_caller(session, request)

        if allowed:
            return await call_next(request)
        # Never cached, whatever the mount says about artwork being immutable:
        # a shared cache holding a 401 would serve it to a member who signs in
        # afterwards, and holding the opposite would be worse.
        return JSONResponse(
            {"detail": "This catalog is for members. Please sign in."},
            status_code=401,
            headers={"Cache-Control": "no-store"},
        )


def _has_a_caller(session: Session, request: Request) -> bool:
    """Whether this request carries a live session or a live API token."""
    if resolve_session(session, request.cookies.get(SESSION_COOKIE)) is not None:
        return True
    token = bearer_token(request.headers.get("Authorization"))
    return resolve_api_token(session, token) is not None


def mount_images(app: FastAPI, images_dir: Path) -> None:
    """Serve downloaded artwork at ``/images``.

    The directory is created if missing: a fresh install has no artwork yet, and
    that should not stop the API from starting.
    """
    images_dir.mkdir(parents=True, exist_ok=True)
    app.add_middleware(MembersOnlyImages)
    app.mount("/images", ImmutableStaticFiles(directory=images_dir), name="images")


def mount_client(app: FastAPI, web_dir: Path) -> None:
    """Serve the static client and register its deep-link fallback."""
    index = web_dir / "index.html"
    if not index.is_file():
        logger.warning("no client at %s; serving the API only", web_dir)
        return

    for name in ASSET_DIRS:
        directory = web_dir / name
        if directory.is_dir():
            app.mount(f"/{name}", ClientStaticFiles(directory=directory), name=f"client-{name}")

    # Consulted by the 404 handler in eifo_api.errors.
    app.state.client_index = index

    @app.get("/", include_in_schema=False)
    async def serve_index() -> FileResponse:
        return FileResponse(index, headers={"Cache-Control": CLIENT_CACHE_CONTROL})


def client_fallback(app: FastAPI) -> FileResponse | None:
    """The client's entry page, when one is being served."""
    index: Path | None = getattr(app.state, "client_index", None)
    if index is None:
        return None
    return FileResponse(index, headers={"Cache-Control": CLIENT_CACHE_CONTROL})
