"""Shared fixtures for the fetcher test suite.

No test in this package touches the network: every HTTP call is mocked with
respx or served from a recorded fixture.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from eifo_core.db import create_engine_from_settings, make_session_factory
from eifo_core.models import Base
from eifo_core.settings import Settings
from eifo_fetcher.http import HttpClient, RateLimiter
from eifo_fetcher.runs import FETCHER_LOGGER
from eifo_fetcher.sources.base import FetchContext


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        db_url=f"sqlite:///{tmp_path / 'fetcher.db'}",
        images_dir=tmp_path / "images",
    )


@pytest.fixture
def engine(settings: Settings) -> Iterator[Engine]:
    engine = create_engine_from_settings(settings)
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return make_session_factory(engine)


@pytest.fixture
def session(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    with session_factory() as session:
        yield session


@pytest.fixture
def http() -> Iterator[HttpClient]:
    """A client that never really sleeps, so rate limits cost no test time."""
    client = HttpClient(rate_limiter=RateLimiter(default_rps=0), sleep=lambda _seconds: None)
    yield client
    client.close()


@pytest.fixture
def ctx(http: HttpClient, settings: Settings) -> FetchContext:
    return FetchContext(source_key="test_source", http=http, settings=settings)


@pytest.fixture
def fetcher_logs_at_info() -> Iterator[None]:
    """What ``eifo-fetch`` configures for itself; the capture takes what it finds."""
    target = logging.getLogger(FETCHER_LOGGER)
    previous = target.level
    target.setLevel(logging.INFO)
    yield
    target.setLevel(previous)
