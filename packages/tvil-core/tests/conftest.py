"""Shared fixtures for tvil-core tests."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from tvil_core.db import create_engine_from_settings, make_session_factory
from tvil_core.models import Base
from tvil_core.settings import Settings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Settings pointing at a throwaway database, ignoring ambient config."""
    return Settings(
        _env_file=None,
        db_url=f"sqlite:///{tmp_path / 'tvil.db'}",
        images_dir=tmp_path / "images",
    )


@pytest.fixture
def engine(settings: Settings) -> Iterator[Engine]:
    """Engine with the schema created directly from the models."""
    engine = create_engine_from_settings(settings)
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    factory = make_session_factory(engine)
    with factory() as session:
        yield session
