"""Application factory."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from tvil_api import __version__
from tvil_api.errors import install_error_handlers
from tvil_api.routers import meta
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
    app.include_router(meta.router, prefix=API_PREFIX)

    return app
