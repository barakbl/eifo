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
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
from live import FETCHER_TOKEN, LiveApi, serving
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


@pytest.fixture(autouse=True)
def never_a_real_api(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make sure no test can reach a real API.

    Settings read ``.env``, so a developer with a token in theirs and a server
    on :3436 had their suite quietly talking to a live catalog - which is how
    "an unmigrated database exits fatally" passed on CI and failed on the one
    machine that had both. With no token configured, anything that tries to
    reach the API stops before it opens a socket.

    Autouse, because the guarantee is about the suite rather than about the
    tests that happen to remember it.
    """
    # The file, not just the environment: pydantic-settings reads `.env`
    # whatever os.environ says, so deleting the variable is not enough.
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    monkeypatch.delenv("EIFO_API_TOKEN", raising=False)
    monkeypatch.delenv("EIFO_API_BASE_URL", raising=False)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        db_url=f"sqlite:///{tmp_path / 'fetcher.db'}",
        images_dir=tmp_path / "images",
        # Every phase writes through the API, so a fetcher without one of these
        # is not a configured fetcher - unless it can mint one, which is what
        # eifo_fetcher.credentials is for and what its own tests cover.
        api_token=FETCHER_TOKEN,
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
def live_api(settings: Settings) -> Iterator[LiveApi]:
    """A migrated catalog, the application serving it, and a client that may write."""
    with serving(settings) as running:
        yield running


@pytest.fixture
def api(live_api: LiveApi) -> IngestClient:
    """The fetcher's half of the conversation, pointed at the real other half."""
    return live_api.api


@pytest.fixture
def api_everywhere(live_api: LiveApi, monkeypatch: pytest.MonkeyPatch) -> LiveApi:
    """Lend the in-process client to code that would open one for itself.

    For the tests that drive a whole command or the daemon: those build their
    own connection from settings and would reach for a socket. What is under
    test there is the ordering, the locking and the exit code - not the
    transport, which the tests that call the client directly already cover.

    Lent rather than replaced, so the block still gets a client that talks to
    the real application: this changes where the connection comes from and
    nothing about what is on the other end of it.
    """

    @contextmanager
    def lend(_settings: Settings, *, http: object = None) -> Iterator[IngestClient]:
        yield live_api.api

    for module in ("eifo_fetcher.runner", "eifo_fetcher.daemon", "eifo_fetcher.cli"):
        monkeypatch.setattr(f"{module}.api_client", lend, raising=False)
    return live_api


@pytest.fixture
def ingest_api() -> Iterator[FakeIngest]:
    """A stand-in for ``/api/v1/ingest``, for the phase that writes through it.

    The artwork phase no longer opens the database: it asks the API what needs
    doing and posts back what it downloaded. Tests that exercise it need
    something at the other end, and it must not be the network.
    """
    yield FakeIngest()


class FakeIngest:
    """Answers the routes a phase touches on the way in, and remembers them.

    A deliberately small double, for the tests that are about the fetcher's own
    bookkeeping - what it opened, what it closed, what it kept when it could not
    reach anybody. Anything about what the catalog *makes* of what it is sent
    goes through ``live_api`` instead, against the real application.


    Built from :mod:`eifo_core.ingest` rather than from hand-written JSON, so
    the shapes here are the shapes the real endpoint reads. A change to the
    contract fails on both sides instead of only on the one that was edited.
    """

    def __init__(self) -> None:
        self.pending: list[dict[str, object]] = []
        self.uploads: list[bytes] = []
        self.opened: list[dict[str, object]] = []
        self.closed: list[dict[str, object]] = []
        self.declared: list[dict[str, object]] = []

    def client(self) -> IngestClient:
        return IngestClient(
            "https://eifo.test",
            "eifo_pat_test",
            http=httpx.Client(transport=httpx.MockTransport(self.handle)),
        )

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/enrich/providers"):
            # Part of the surface every phase now touches on its way in, so a
            # double that did not answer it would fail every test that opens a
            # phase rather than the one testing providers.
            self.declared.append(json.loads(request.content))
            return httpx.Response(200, json={"changed": []})
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
