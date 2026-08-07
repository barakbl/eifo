"""Request-scoped dependencies.

Handlers stay short by pulling settings and a database session from here rather
than constructing anything themselves.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.orm import Session, sessionmaker

from tvil_core.settings import Settings


def get_settings(request: Request) -> Settings:
    """Settings resolved when the application was created."""
    settings: Settings = request.app.state.settings
    return settings


def get_session(request: Request) -> Iterator[Session]:
    """A read-only-by-convention session for one request.

    The API never writes catalog data; user writes commit explicitly.
    """
    factory: sessionmaker[Session] = request.app.state.session_factory
    session = factory()
    try:
        yield session
    finally:
        session.close()


SettingsDep = Annotated[Settings, Depends(get_settings)]
SessionDep = Annotated[Session, Depends(get_session)]
