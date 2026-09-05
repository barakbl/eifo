"""Shared fixtures for the fetcher test suite.

No test in this package touches the network: every HTTP call is mocked with
respx or served from a recorded fixture.
"""

from __future__ import annotations

import io
import json
import logging
import tarfile
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from eifo_core.db import create_engine_from_settings, make_session_factory
from eifo_core.ingest import MANIFEST_NAME, PosterManifest
from eifo_core.models import Base
from eifo_core.settings import Settings
from eifo_fetcher.http import HttpClient, RateLimiter
from eifo_fetcher.ingest import IngestClient
from eifo_fetcher.runs import FETCHER_LOGGER
from eifo_fetcher.sources.base import FetchContext


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        db_url=f"sqlite:///{tmp_path / 'fetcher.db'}",
        images_dir=tmp_path / "images",
        # The artwork phase writes through the API, so a fetcher without one of
        # these is not a configured fetcher.
        api_token="eifo_pat_test",
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


@pytest.fixture
def ingest_api() -> Iterator[FakeIngest]:
    """A stand-in for ``/api/v1/ingest``, for the phase that writes through it.

    The artwork phase no longer opens the database: it asks the API what needs
    doing and posts back what it downloaded. Tests that exercise it need
    something at the other end, and it must not be the network.
    """
    yield FakeIngest()


class FakeIngest:
    """Answers the four ingest routes, and remembers what it was told.

    Built from :mod:`eifo_core.ingest` rather than from hand-written JSON, so
    the shapes here are the shapes the real endpoint reads. A change to the
    contract fails on both sides instead of only on the one that was edited.
    """

    def __init__(self) -> None:
        self.pending: list[dict[str, object]] = []
        self.uploads: list[bytes] = []
        self.opened: list[dict[str, object]] = []
        self.closed: list[dict[str, object]] = []

    def client(self) -> IngestClient:
        return IngestClient(
            "https://eifo.test",
            "eifo_pat_test",
            http=httpx.Client(transport=httpx.MockTransport(self.handle)),
        )

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/posters/pending"):
            after = int(request.url.params.get("after", 0))
            return httpx.Response(
                200,
                json=[row for row in self.pending if int(row["title_id"]) > after],  # type: ignore[arg-type]
            )
        if path.endswith("/ingest/posters"):
            self.uploads.append(request.content)
            with tarfile.open(fileobj=io.BytesIO(request.content), mode="r:gz") as tar:
                handle = tar.extractfile(MANIFEST_NAME)
                assert handle is not None
                manifest = PosterManifest.from_json(handle.read())
            stored = {item.title_id for item in manifest.items}
            self.pending = [row for row in self.pending if row["title_id"] not in stored]
            return httpx.Response(200, json={"stored": len(stored), "rejected": []})
        if path.endswith("/ingest/runs"):
            self.opened.append(json.loads(request.content))
            return httpx.Response(201, json={"id": len(self.opened)})
        if "/ingest/runs/" in path:
            self.closed.append(json.loads(request.content))
            return httpx.Response(200, json={"id": 1})
        raise AssertionError(f"unexpected ingest call: {path}")
