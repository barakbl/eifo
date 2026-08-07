"""Serving artwork and the web client.

One process serves the API, the downloaded images and the static client, so a
deployment is a single container with no reverse-proxy rules to get right.

The client's deep-link fallback is a 404 handler rather than a catch-all route.
A catch-all would sit in the routing table shadowing anything registered after
it, and would turn a genuinely missing API path into a 200 page.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

logger = logging.getLogger("tvil.api.static")

#: Image paths embed the variant, so a changed image is a changed URL.
IMAGE_CACHE_CONTROL = "public, max-age=31536000, immutable"
#: The client is small and changes on deploy; revalidate rather than cache hard.
CLIENT_CACHE_CONTROL = "public, max-age=60"

#: Paths that belong to the API. A 404 under one of these is a real 404.
API_PREFIXES = ("/api/", "/docs", "/redoc", "/openapi.json", "/images/")

#: Client subdirectories served verbatim when present.
ASSET_DIRS = ("css", "js", "assets")


class ImmutableStaticFiles(StaticFiles):
    """Static files served with a long-lived cache policy."""

    def file_response(self, *args: object, **kwargs: object) -> Response:
        response = super().file_response(*args, **kwargs)  # type: ignore[arg-type]
        response.headers["Cache-Control"] = IMAGE_CACHE_CONTROL
        return response


def is_api_path(path: str) -> bool:
    """Whether a path belongs to the API rather than the client."""
    return any(path.startswith(prefix) for prefix in API_PREFIXES)


def mount_images(app: FastAPI, images_dir: Path) -> None:
    """Serve downloaded artwork at ``/images``.

    The directory is created if missing: a fresh install has no artwork yet, and
    that should not stop the API from starting.
    """
    images_dir.mkdir(parents=True, exist_ok=True)
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
            app.mount(f"/{name}", StaticFiles(directory=directory), name=f"client-{name}")

    # Consulted by the 404 handler in tvil_api.errors.
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
