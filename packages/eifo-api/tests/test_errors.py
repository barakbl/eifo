"""Every error the API emits uses the RFC 9457 problem-details shape."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from eifo_api.app import create_app
from eifo_api.errors import PROBLEM_MEDIA_TYPE
from eifo_core.db import DatabaseNotReadyError
from eifo_core.migrate import current_revision, head_revision
from eifo_core.settings import Settings


def test_unknown_route_returns_a_problem_document(client: TestClient) -> None:
    response = client.get("/api/v1/no-such-endpoint")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith(PROBLEM_MEDIA_TYPE)
    body = response.json()
    assert body["status"] == 404
    assert body["title"] == "Not found"
    assert body["type"] == "about:blank"


def test_invalid_query_parameter_returns_422_with_details(app: FastAPI) -> None:
    """A route with a typed parameter rejects bad input in the standard shape."""

    @app.get("/api/v1/_test/echo")
    def _echo(count: int) -> dict[str, int]:  # pragma: no cover - exercised via HTTP
        return {"count": count}

    with TestClient(app) as client:
        response = client.get("/api/v1/_test/echo", params={"count": "not-a-number"})

    assert response.status_code == 422
    assert response.headers["content-type"].startswith(PROBLEM_MEDIA_TYPE)
    body = response.json()
    assert body["title"] == "Invalid request"
    assert body["errors"][0]["location"] == ["query", "count"]
    assert body["errors"][0]["message"]


def test_openapi_schema_is_served(client: TestClient) -> None:
    response = client.get("/openapi.json")

    assert response.status_code == 200
    assert "/api/v1/meta" in response.json()["paths"]


def test_an_unmigrated_database_is_migrated_on_start(tmp_path: object) -> None:
    """Deploying a version that adds a migration is a restart, nothing more."""
    db_url = f"sqlite:///{tmp_path}/fresh.db"
    app = create_app(Settings(_env_file=None, db_url=db_url))

    with TestClient(app) as client:
        assert client.get("/api/v1/meta").status_code == 200

    engine = create_engine(db_url)
    try:
        assert current_revision(engine) == head_revision()
    finally:
        engine.dispose()


def test_migrating_can_be_left_to_a_deliberate_step(tmp_path: object) -> None:
    """With auto_migrate off, a misconfigured start is one clear error."""
    settings = Settings(
        _env_file=None,
        db_url=f"sqlite:///{tmp_path}/never-migrated.db",
        auto_migrate=False,
    )
    app = create_app(settings)

    with pytest.raises(DatabaseNotReadyError, match="eifo-fetch db upgrade"), TestClient(app):
        pass  # pragma: no cover - the context manager raises on entry
