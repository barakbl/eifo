"""Application factory."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from eifo_api import __version__
from eifo_api.caching import CatalogCacheMiddleware
from eifo_api.errors import install_error_handlers
from eifo_api.logging_privacy import install_log_filters
from eifo_api.routers import admin, auth, catalog, me, meta, reviews
from eifo_api.static import mount_client, mount_images
from eifo_core.db import create_engine_from_settings, make_session_factory, require_schema
from eifo_core.fts import ensure_search_triggers, missing_triggers
from eifo_core.migrate import ensure_current
from eifo_core.settings import Settings, get_settings

API_PREFIX = "/api/v1"

logger = logging.getLogger("eifo.api")


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Ready the database before serving, and dispose it after.

    With ``auto_migrate`` on (the default) the schema is brought to head here,
    so deploying a version that adds a migration is a restart and nothing else,
    and search triggers lost to a table rebuild are put back. With it off, an
    absent schema fails here with an actionable message rather than 500ing on
    the first request, and a search index nothing is updating is reported rather
    than quietly repaired - this process was told not to touch the schema.
    """
    engine = app.state.engine
    settings = app.state.settings
    try:
        # Inside the try: a failed readiness check must still release the pool.
        if settings.auto_migrate:
            applied = ensure_current(engine, settings.db_url)
            if applied is not None:
                logger.info("database migrated to %s", applied)
            ensure_search_triggers(engine)
        else:
            require_schema(engine, settings.db_url)
            with engine.connect() as connection:
                if missing_triggers(connection):
                    logger.warning(
                        "search index is not being updated: its triggers are missing. "
                        "Run `eifo-fetch db upgrade` to restore them."
                    )
        logger.info("eifo-api %s ready", __version__)
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
        title="Eifo API",
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
    app.include_router(admin.router, prefix=API_PREFIX)
    app.include_router(reviews.router, prefix=API_PREFIX)

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
