"""Fixtures for the API test suite: a migrated database and a client."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from helpers import SeedSource
from seed import Seeded, seed_catalog
from sqlalchemy.orm import Session, sessionmaker

from tvil_api.app import create_app
from tvil_core.enums import FetchPhase, FetchStatus, SourceKind
from tvil_core.migrate import upgrade
from tvil_core.models import FetchRun, Source
from tvil_core.settings import Settings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Settings for a migrated throwaway database."""
    db_url = f"sqlite:///{tmp_path / 'api.db'}"
    upgrade(db_url)
    return Settings(_env_file=None, db_url=db_url, images_dir=tmp_path / "images")


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    return create_app(settings)


@pytest.fixture
def session_factory(app: FastAPI) -> sessionmaker[Session]:
    factory: sessionmaker[Session] = app.state.session_factory
    return factory


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    """Client that runs the application lifespan (including the schema check)."""
    with TestClient(app) as client:
        yield client


@pytest.fixture
def seed_source(session_factory: sessionmaker[Session]) -> SeedSource:
    """Insert a source, optionally with a sync run recorded against it."""

    def _seed(
        key: str = "cellcom_tv",
        *,
        name: str = "Cellcom TV",
        active: bool = True,
        synced_at: dt.datetime | None = None,
        status: FetchStatus = FetchStatus.OK,
    ) -> None:
        with session_factory() as session:
            session.add(
                Source(
                    key=key,
                    name=name,
                    kind=SourceKind.SUBSCRIPTION,
                    website_url=f"https://{key}.example",
                    active=active,
                )
            )
            if synced_at is not None:
                session.add(
                    FetchRun(
                        source_key=key,
                        phase=FetchPhase.SYNC,
                        started_at=synced_at,
                        finished_at=synced_at,
                        status=status,
                        stats={"items_seen": 1000},
                    )
                )
            session.commit()

    return _seed


@pytest.fixture
def catalog(session_factory: sessionmaker[Session]) -> Seeded:
    """A seeded catalog covering the states the API must distinguish."""
    with session_factory() as session:
        return seed_catalog(session)
