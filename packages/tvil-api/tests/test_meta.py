"""GET /api/v1/meta - freshness, attribution and the health signal."""

from __future__ import annotations

import datetime as dt

from fastapi.testclient import TestClient
from helpers import SeedSource
from sqlalchemy.orm import Session, sessionmaker

from tvil_core.enums import FetchPhase, FetchStatus, TitleKind
from tvil_core.models import FetchRun, Title


def test_reports_version_and_empty_catalog(client: TestClient) -> None:
    response = client.get("/api/v1/meta")

    assert response.status_code == 200
    body = response.json()
    assert body["version"]
    assert body["title_count"] == 0
    assert body["sources"] == []
    assert body["generated_at"]


def test_counts_titles(client: TestClient, session_factory: sessionmaker[Session]) -> None:
    with session_factory() as session:
        session.add_all(
            [
                Title(type=TitleKind.MOVIE, name_he="הערת שוליים", year=2011),
                Title(type=TitleKind.SERIES, name_en="Fauda", year=2015),
            ]
        )
        session.commit()

    assert client.get("/api/v1/meta").json()["title_count"] == 2


def test_lists_required_attribution(client: TestClient) -> None:
    """The data licences require these credits, so the API always sends them."""
    attribution = client.get("/api/v1/meta").json()["attribution"]

    texts = " ".join(item["text"] for item in attribution)
    assert "JustWatch" in texts
    assert "TMDB" in texts
    assert "IMDb" in texts


class TestFreshness:
    def test_recent_sync_is_not_stale(self, client: TestClient, seed_source: SeedSource) -> None:
        seed_source(synced_at=dt.datetime.now(dt.UTC) - dt.timedelta(hours=2))

        source = client.get("/api/v1/meta").json()["sources"][0]

        assert source["key"] == "cellcom_tv"
        assert source["stale"] is False
        assert source["last_sync_at"] is not None
        assert source["last_sync_status"] == "ok"

    def test_old_sync_is_stale(self, client: TestClient, seed_source: SeedSource) -> None:
        seed_source(synced_at=dt.datetime.now(dt.UTC) - dt.timedelta(hours=72))

        assert client.get("/api/v1/meta").json()["sources"][0]["stale"] is True

    def test_never_synced_source_is_stale(
        self, client: TestClient, seed_source: SeedSource
    ) -> None:
        seed_source()

        source = client.get("/api/v1/meta").json()["sources"][0]

        assert source["stale"] is True
        assert source["last_sync_at"] is None

    def test_retired_source_is_reported_but_never_stale(
        self, client: TestClient, seed_source: SeedSource
    ) -> None:
        """A source we stopped tracking is not a failure to report on."""
        seed_source(key="free_tv", name="Free TV", active=False)

        source = client.get("/api/v1/meta").json()["sources"][0]

        assert source["active"] is False
        assert source["stale"] is False

    def test_failed_run_does_not_count_as_a_sync(
        self, client: TestClient, seed_source: SeedSource
    ) -> None:
        seed_source(
            synced_at=dt.datetime.now(dt.UTC) - dt.timedelta(hours=1),
            status=FetchStatus.FAILED,
        )

        source = client.get("/api/v1/meta").json()["sources"][0]

        assert source["last_sync_at"] is None
        assert source["last_sync_status"] == "failed"
        assert source["stale"] is True

    def test_reports_last_success_alongside_a_newer_failure(
        self,
        client: TestClient,
        seed_source: SeedSource,
        session_factory: sessionmaker[Session],
    ) -> None:
        """A source that worked yesterday but is failing now must show both."""
        now = dt.datetime.now(dt.UTC)
        seed_source(synced_at=now - dt.timedelta(hours=6))
        with session_factory() as session:
            session.add(
                FetchRun(
                    source_key="cellcom_tv",
                    phase=FetchPhase.SYNC,
                    started_at=now - dt.timedelta(minutes=5),
                    finished_at=now - dt.timedelta(minutes=4),
                    status=FetchStatus.ABORTED_SUSPICIOUS,
                    stats={"items_seen": 3},
                )
            )
            session.commit()

        source = client.get("/api/v1/meta").json()["sources"][0]

        assert source["last_sync_status"] == "aborted_suspicious"
        assert source["last_sync_at"] is not None
        assert source["stale"] is False

    def test_sources_are_sorted_by_name(self, client: TestClient, seed_source: SeedSource) -> None:
        seed_source(key="yes_plus", name="yes+")
        seed_source(key="apple_tv_plus", name="Apple TV+")

        names = [source["name"] for source in client.get("/api/v1/meta").json()["sources"]]

        assert names == ["Apple TV+", "yes+"]
