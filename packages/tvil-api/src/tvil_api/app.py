"""Application factory."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from tvil_api import __version__
from tvil_api.caching import CatalogCacheMiddleware
from tvil_api.errors import install_error_handlers
from tvil_api.logging_privacy import install_log_filters
from tvil_api.routers import auth, catalog, me, meta
from tvil_api.static import mount_client, mount_images
from tvil_core.db import create_engine_from_settings, make_session_factory, require_schema
from tvil_core.settings import Settings, get_settings

API_PREFIX = "/api/v1"

logger = logging.getLogger("tvil.api")


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Verify the database is migrated before serving, and dispose it after.

    Starting against an unmigrated database fails here with an actionable
    message rather than 500ing on the first request.
    """
    engine = app.state.engine
    try:
        # Inside the try: a failed readiness check must still release the pool.
        require_schema(engine, app.state.settings.db_url)
        logger.info("tvil-api %s ready", __version__)
        yield
    finally:
        engine.dispose()


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application.

    Args:
        settings: overrides the process-wide settings; used by the test suite.
    """
    settings = settings or get_settings()

    app = FastAPI(
        title="TVIL API",
        version=__version__,
        summary="What is streaming in Israel, and is it any good?",
        lifespan=_lifespan,
    )

    engine = create_engine_from_settings(settings)
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = make_session_factory(engine)

    install_error_handlers(app)
    install_log_filters()
    app.add_middleware(CatalogCacheMiddleware)
    app.include_router(meta.router, prefix=API_PREFIX)
    app.include_router(catalog.router, prefix=API_PREFIX)
    app.include_router(auth.router, prefix=API_PREFIX)
    app.include_router(me.router, prefix=API_PREFIX)

    mount_images(app, Path(settings.images_dir))
    # Registered last: its catch-all route must not shadow the API.
    mount_client(app, _web_dir(settings))

    return app


def _web_dir(settings: Settings) -> Path:
    """Where the static client lives.

    Counting parent directories from the module only works in a source
    checkout; once the package is installed into a virtualenv the same
    arithmetic lands inside the venv, and the client silently stops being
    served. So the location is looked for rather than derived: an explicit
    setting first, then the working directory (which is how the container is
    laid out), then the source tree.
    """
    if settings.web_dir is not None:
        return settings.web_dir

    candidates = (
        Path.cwd() / "web",
        Path(__file__).resolve().parents[4] / "web",
    )
    for candidate in candidates:
        if (candidate / "index.html").is_file():
            return candidate

    return candidates[0]
