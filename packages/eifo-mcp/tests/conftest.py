"""The MCP server against the real API, in process.

Nothing is stubbed between the two: the server's client is an ``httpx.Client``
and FastAPI's ``TestClient`` is one, so every tool call here goes through the
real routes, the real scope check and the real database - which is what an
assistant on somebody's laptop will be talking to.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mcp.server.mcpserver import MCPServer
from mcp_calls import token_for
from mcp_catalog import Catalog, seed
from sqlalchemy.orm import Session, sessionmaker

from eifo_api.app import create_app
from eifo_api.routers.catalog import forget_recommendations, forget_totals
from eifo_core.migrate import upgrade
from eifo_core.settings import Settings
from eifo_mcp.client import EifoClient
from eifo_mcp.server import build_server

BASE = "https://testserver"


@pytest.fixture(autouse=True)
def _forget_totals() -> Iterator[None]:
    forget_totals()
    forget_recommendations()
    yield
    forget_totals()
    forget_recommendations()


@pytest.fixture
def app(tmp_path: Path) -> Iterator[FastAPI]:
    db_url = f"sqlite:///{tmp_path / 'mcp.db'}"
    upgrade(db_url)
    settings = Settings(
        _env_file=None,
        db_url=db_url,
        images_dir=tmp_path / "images",
        public_origin=BASE,
        secret_key="x" * 48,
    )
    app = create_app(settings)
    yield app
    app.state.engine.dispose()


@pytest.fixture
def session_factory(app: FastAPI) -> sessionmaker[Session]:
    factory: sessionmaker[Session] = app.state.session_factory
    return factory


@pytest.fixture
def catalog(session_factory: sessionmaker[Session]) -> Catalog:
    with session_factory() as session:
        return seed(session)


@pytest.fixture
def token(session_factory: sessionmaker[Session], catalog: Catalog) -> str:
    """A read-only token: what Settings offers first, and what this should be given."""
    return token_for(session_factory, catalog.viewer)


@pytest.fixture
def eifo(app: FastAPI, token: str) -> Iterator[EifoClient]:
    with TestClient(app, base_url=BASE) as http:
        client = EifoClient(BASE, token, http=http)
        yield client


@pytest.fixture
def server(eifo: EifoClient) -> MCPServer:
    return build_server(eifo)
