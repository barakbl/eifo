"""Fixtures for the API test suite: a migrated database and a client."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from helpers import PUBLIC_ORIGIN, SECRET_KEY, SeedSource, SignIn
from providers import (
    GOOGLE_CLIENT_ID,
    GOOGLE_CLIENT_SECRET,
    X_CLIENT_ID,
    X_CLIENT_SECRET,
    mock_google,
    mock_x,
)
from seed import Seeded, seed_catalog
from sqlalchemy.orm import Session, sessionmaker

from tvil_api.app import create_app
from tvil_core.enums import AuthProvider, FetchPhase, FetchStatus, SourceKind
from tvil_core.migrate import upgrade
from tvil_core.models import FetchRun, Source
from tvil_core.settings import Settings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Settings for a migrated throwaway database, with login configured."""
    db_url = f"sqlite:///{tmp_path / 'api.db'}"
    upgrade(db_url)
    return Settings(
        _env_file=None,
        db_url=db_url,
        images_dir=tmp_path / "images",
        public_origin=PUBLIC_ORIGIN,
        secret_key=SECRET_KEY,
        google_client_id=GOOGLE_CLIENT_ID,
        google_client_secret=GOOGLE_CLIENT_SECRET,
        x_client_id=X_CLIENT_ID,
        x_client_secret=X_CLIENT_SECRET,
    )


@pytest.fixture
def app(settings: Settings) -> Iterator[FastAPI]:
    """An application whose engine is released even if no client runs its lifespan."""
    app = create_app(settings)
    yield app
    app.state.engine.dispose()


@pytest.fixture
def session_factory(app: FastAPI) -> sessionmaker[Session]:
    factory: sessionmaker[Session] = app.state.session_factory
    return factory


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    """Client that runs the application lifespan (including the schema check).

    Over HTTPS, because cookies are issued ``Secure`` on any deployment worth
    testing and a plain-HTTP client would silently drop every one of them.
    """
    with TestClient(app, base_url="https://testserver") as client:
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


@pytest.fixture
def sign_in(client: TestClient) -> SignIn:
    """Complete a real login against a mocked provider.

    Walks the actual redirect, state and PKCE round trip rather than inserting a
    session row, so every test that needs a user also re-proves the login works.
    Returns the CSRF token, which is what a client needs next.
    """

    def _sign_in(provider: AuthProvider = AuthProvider.GOOGLE) -> str:
        with respx.mock:
            mock_google() if provider is AuthProvider.GOOGLE else mock_x()

            start = client.get(f"/api/v1/auth/login/{provider.value}", follow_redirects=False)
            state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]

            client.get(
                f"/api/v1/auth/callback/{provider.value}",
                params={"code": "authorization-code", "state": state},
                follow_redirects=False,
            )

        token: str = client.get("/api/v1/me").json()["csrf_token"]
        return token

    return _sign_in
