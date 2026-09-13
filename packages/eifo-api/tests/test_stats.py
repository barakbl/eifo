"""The statistics endpoints: growth per sync, and each service as it stands."""

from __future__ import annotations

import datetime as dt
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from seed import NOW, Seeded
from sqlalchemy.orm import Session, sessionmaker

from eifo_core.enums import FetchPhase, FetchStatus, OfferType, TitleKind
from eifo_core.models import Availability, FetchRun, Title


def _run(
    session_factory: sessionmaker[Session],
    source_key: str | None,
    *,
    days_ago: float,
    stats: dict[str, Any],
    status: FetchStatus = FetchStatus.OK,
    phase: FetchPhase = FetchPhase.SYNC,
) -> int:
    started = dt.datetime.now(dt.UTC) - dt.timedelta(days=days_ago)
    with session_factory() as session:
        run = FetchRun(
            source_key=source_key,
            phase=phase,
            status=status,
            started_at=started,
            finished_at=started + dt.timedelta(minutes=3),
            stats=stats,
        )
        session.add(run)
        session.commit()
        return run.id


def _tally(created: int, titles: int, *, retired: int = 0) -> dict[str, Any]:
    return {
        "items_seen": created + 100,
        "availability_created": created,
        "availability_updated": 100,
        "titles_created": titles,
        "retired": retired,
        "errors": [],
    }


class TestGrowth:
    def test_reads_each_sync_off_its_own_tally(
        self, client: TestClient, catalog: Seeded, session_factory: sessionmaker[Session]
    ) -> None:
        run_id = _run(session_factory, "mako", days_ago=2, stats=_tally(7, 3, retired=1))

        [row] = client.get("/api/v1/stats/growth").json()

        assert row["run_id"] == run_id
        assert row["source_key"] == "mako"
        assert row["source_name"] == "Mako VOD (Keshet 12)"
        assert (row["offers_added"], row["titles_created"], row["offers_retired"]) == (7, 3, 1)
        assert row["items_seen"] == 107

    def test_oldest_first_across_services(
        self, client: TestClient, catalog: Seeded, session_factory: sessionmaker[Session]
    ) -> None:
        late = _run(session_factory, "mako", days_ago=1, stats=_tally(1, 0))
        early = _run(session_factory, "netflix_il", days_ago=5, stats=_tally(2, 2))

        rows = client.get("/api/v1/stats/growth").json()

        assert [row["run_id"] for row in rows] == [early, late]

    def test_flags_a_sync_that_loaded_a_catalog_rather_than_grew_one(
        self, client: TestClient, catalog: Seeded, session_factory: sessionmaker[Session]
    ) -> None:
        """Found is not arrived: a sweep that added most of what it saw is a load."""
        capped = _run(
            session_factory, "mako", days_ago=10, stats=_tally(80, 80) | {"items_seen": 80}
        )
        full = _run(
            session_factory, "mako", days_ago=9, stats=_tally(900, 800) | {"items_seen": 2000}
        )
        _run(session_factory, "mako", days_ago=8, stats=_tally(40, 30) | {"items_seen": 2040})
        # Most of what a tiny service showed, but a handful is a busy night.
        _run(session_factory, "netflix_il", days_ago=8, stats=_tally(6, 6) | {"items_seen": 12})

        flagged = [
            row["run_id"]
            for row in client.get("/api/v1/stats/growth").json()
            if row["catalog_load"]
        ]

        assert flagged == [capped, full]

    def test_leaves_out_runs_that_counted_nothing_and_other_phases(
        self, client: TestClient, catalog: Seeded, session_factory: sessionmaker[Session]
    ) -> None:
        _run(
            session_factory,
            "mako",
            days_ago=1,
            stats={"errors": ["could not reach the API"]},
            status=FetchStatus.CRASHED,
        )
        _run(session_factory, None, days_ago=1, stats={"titles_seen": 10}, phase=FetchPhase.ENRICH)
        _run(session_factory, None, days_ago=1, stats=_tally(5, 5))

        assert client.get("/api/v1/stats/growth").json() == []

    def test_a_window_of_days(
        self, client: TestClient, catalog: Seeded, session_factory: sessionmaker[Session]
    ) -> None:
        recent = _run(session_factory, "mako", days_ago=3, stats=_tally(1, 1))
        _run(session_factory, "mako", days_ago=40, stats=_tally(1, 1))

        rows = client.get("/api/v1/stats/growth", params={"days": 30}).json()

        assert [row["run_id"] for row in rows] == [recent]

    def test_a_malformed_figure_reads_as_zero(
        self, client: TestClient, catalog: Seeded, session_factory: sessionmaker[Session]
    ) -> None:
        _run(
            session_factory,
            "mako",
            days_ago=1,
            stats={"items_seen": 3, "availability_created": "lots", "titles_created": True},
        )

        [row] = client.get("/api/v1/stats/growth").json()

        assert (row["offers_added"], row["titles_created"]) == (0, 0)


def _offer(
    session_factory: sessionmaker[Session],
    title_id: int,
    source_id: int,
    offer_type: OfferType,
    *,
    price_minor: int | None = None,
    is_current: bool = True,
) -> None:
    with session_factory() as session:
        session.add(
            Availability(
                title_id=title_id,
                source_id=source_id,
                offer_type=offer_type,
                price_minor=price_minor,
                price_currency="ILS" if price_minor is not None else None,
                is_current=is_current,
                first_seen=NOW,
                last_seen=NOW,
            )
        )
        session.commit()


def _movie(session_factory: sessionmaker[Session], name: str) -> int:
    with session_factory() as session:
        title = Title(type=TitleKind.MOVIE, name_en=name, year=2020)
        session.add(title)
        session.commit()
        return title.id


def _service(body: dict[str, Any], key: str) -> dict[str, Any]:
    return next(row for row in body["services"] if row["key"] == key)


class TestServices:
    def test_counts_distinct_titles_and_offers_per_service(
        self, client: TestClient, catalog: Seeded, session_factory: sessionmaker[Session]
    ) -> None:
        film = _movie(session_factory, "Rented Film")
        _offer(session_factory, film, catalog.netflix, OfferType.RENT)
        _offer(session_factory, film, catalog.netflix, OfferType.BUY)

        netflix = _service(client.get("/api/v1/stats/services").json(), "netflix_il")

        # Fauda to stream, the film to rent and to buy. Foxtrot left.
        assert (netflix["titles"], netflix["offers"]) == (2, 3)
        assert (netflix["movies"], netflix["series"]) == (1, 1)
        assert netflix["offers_by_type"] == {"stream": 1, "rent": 1, "buy": 1}

    def test_price_coverage_per_deal(
        self, client: TestClient, catalog: Seeded, session_factory: sessionmaker[Session]
    ) -> None:
        films = [_movie(session_factory, f"Film {n}") for n in range(3)]
        _offer(session_factory, films[0], catalog.netflix, OfferType.RENT, price_minor=1690)
        _offer(session_factory, films[1], catalog.netflix, OfferType.RENT)
        _offer(session_factory, films[2], catalog.netflix, OfferType.BUY, price_minor=4990)
        # Gone, and priced: history, not coverage.
        _offer(
            session_factory,
            films[2],
            catalog.netflix,
            OfferType.RENT,
            price_minor=990,
            is_current=False,
        )

        netflix = _service(client.get("/api/v1/stats/services").json(), "netflix_il")

        assert netflix["rent"] == {"offers": 2, "priced": 1, "unpriced": 1, "titles_priced": 1}
        assert netflix["buy"] == {"offers": 1, "priced": 1, "unpriced": 0, "titles_priced": 1}

    def test_a_service_with_nothing_current_still_has_a_row(
        self, client: TestClient, catalog: Seeded
    ) -> None:
        retired = _service(client.get("/api/v1/stats/services").json(), "free_tv")

        # Shtisel's offer is current on a retired source: counted, and badged.
        assert retired["active"] is False
        assert retired["titles"] == 1

    def test_catalog_totals_count_a_title_once(self, client: TestClient, catalog: Seeded) -> None:
        body = client.get("/api/v1/stats/services").json()

        # Fauda on two services is one title; Shtisel on the retired one is another.
        assert (body["titles"], body["offers"]) == (2, 3)
        assert (body["movies"], body["series"]) == (0, 2)


class TestGate:
    def test_closed_to_strangers_on_a_members_only_instance(
        self, client: TestClient, app: FastAPI, catalog: Seeded
    ) -> None:
        app.state.settings.members_only = True

        assert client.get("/api/v1/stats/growth").status_code == 401
        assert client.get("/api/v1/stats/services").status_code == 401

    def test_cacheable_like_the_rest_of_the_catalog(
        self, client: TestClient, catalog: Seeded
    ) -> None:
        assert "ETag" in client.get("/api/v1/stats/services").headers
