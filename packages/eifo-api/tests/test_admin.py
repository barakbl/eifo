"""``/api/v1/admin`` - the operator's tab.

Two things are being checked here and they are not the same. One is that the
numbers and the switch are right. The other is that none of it exists for
anybody who is not an administrator - which is the part that matters, because
this is the first surface in the product where being signed in is not enough.
"""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from helpers import MakeAdmin, SeedSource, SignIn
from seed import Seeded
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from eifo_api.security import CSRF_HEADER
from eifo_core.enums import (
    EnrichOutcome,
    FetchPhase,
    FetchStatus,
    OfferType,
    SourceKind,
)
from eifo_core.models import (
    Availability,
    EnrichAttempt,
    FetchRun,
    MatchReview,
    Source,
)
from eifo_core.settings import SourceConfig

NOW = dt.datetime(2026, 8, 7, 12, 0, tzinfo=dt.UTC)


@pytest.fixture
def admin(sign_in: SignIn, make_admin: MakeAdmin) -> dict[str, str]:
    """Signed in, and named in the instance's administrator list."""
    csrf = sign_in()
    make_admin()
    return {CSRF_HEADER: csrf}


def add_run(
    factory: sessionmaker[Session],
    *,
    source_key: str | None = "netflix_il",
    phase: FetchPhase = FetchPhase.SYNC,
    status: FetchStatus = FetchStatus.OK,
    started_at: dt.datetime = NOW,
    log: str | None = None,
    stats: dict[str, int] | None = None,
) -> int:
    with factory() as session:
        run = FetchRun(
            source_key=source_key,
            phase=phase,
            started_at=started_at,
            finished_at=started_at + dt.timedelta(seconds=42),
            status=status,
            stats=stats or {"items_seen": 100},
            log=log,
        )
        session.add(run)
        session.commit()
        return run.id


class TestNobodyElseCanSeeIt:
    """404, not 403: a stranger is not owed the knowledge that this exists."""

    @pytest.mark.parametrize(
        "path",
        ["/api/v1/admin/sources", "/api/v1/admin/runs", "/api/v1/admin/stats", "/api/v1/reviews"],
    )
    def test_a_signed_out_visitor_is_asked_to_sign_in(self, client: TestClient, path: str) -> None:
        assert client.get(path).status_code == 401

    @pytest.mark.parametrize(
        "path",
        ["/api/v1/admin/sources", "/api/v1/admin/runs", "/api/v1/admin/stats", "/api/v1/reviews"],
    )
    def test_a_signed_in_stranger_is_told_it_is_not_there(
        self, client: TestClient, sign_in: SignIn, path: str
    ) -> None:
        sign_in()

        assert client.get(path).status_code == 404

    def test_and_cannot_write_either(
        self, client: TestClient, sign_in: SignIn, seed_source: SeedSource
    ) -> None:
        csrf = sign_in()
        seed_source(key="netflix_il")

        response = client.patch(
            "/api/v1/admin/sources/netflix_il",
            json={"enabled": False},
            headers={CSRF_HEADER: csrf},
        )

        assert response.status_code == 404

    def test_me_says_whether_the_tab_is_worth_offering(
        self, client: TestClient, sign_in: SignIn, make_admin: MakeAdmin
    ) -> None:
        sign_in()
        assert client.get("/api/v1/me").json()["is_admin"] is False

        make_admin()
        assert client.get("/api/v1/me").json()["is_admin"] is True

    def test_an_address_that_is_not_listed_is_not_promoted(
        self, client: TestClient, sign_in: SignIn, make_admin: MakeAdmin
    ) -> None:
        sign_in()
        make_admin(email="somebody.else@example.com")

        assert client.get("/api/v1/me").json()["is_admin"] is False


class TestSources:
    def test_it_reports_coverage_and_freshness(
        self, client: TestClient, admin: dict[str, str], catalog: Seeded
    ) -> None:
        rows = {row["key"]: row for row in client.get("/api/v1/admin/sources").json()}

        assert rows["netflix_il"]["title_count"] > 0
        assert rows["netflix_il"]["effective_enabled"] is True
        # Nothing has ever synced in this fixture, so everything active is stale.
        assert rows["netflix_il"]["stale"] is True

    def test_a_source_with_a_queue_says_how_deep_it_is(
        self,
        client: TestClient,
        admin: dict[str, str],
        catalog: Seeded,
        session_factory: sessionmaker[Session],
    ) -> None:
        with session_factory() as session:
            session.add(MatchReview(source_key="netflix_il", raw_payload={"name": "משהו"}))
            session.commit()

        rows = {row["key"]: row for row in client.get("/api/v1/admin/sources").json()}

        assert rows["netflix_il"]["pending_reviews"] == 1

    def test_a_recent_successful_sync_is_not_stale(
        self,
        client: TestClient,
        admin: dict[str, str],
        catalog: Seeded,
        session_factory: sessionmaker[Session],
    ) -> None:
        add_run(session_factory, started_at=dt.datetime.now(dt.UTC) - dt.timedelta(hours=1))

        rows = {row["key"]: row for row in client.get("/api/v1/admin/sources").json()}

        assert rows["netflix_il"]["stale"] is False
        assert rows["netflix_il"]["last_sync_status"] == "ok"


class TestTheSwitch:
    def test_turning_a_source_off_records_the_override(
        self,
        client: TestClient,
        admin: dict[str, str],
        catalog: Seeded,
        session_factory: sessionmaker[Session],
    ) -> None:
        response = client.patch(
            "/api/v1/admin/sources/netflix_il", json={"enabled": False}, headers=admin
        )

        assert response.status_code == 200
        assert response.json()["enabled"] is False
        assert response.json()["effective_enabled"] is False

        with session_factory() as session:
            stored = session.scalars(select(Source).where(Source.key == "netflix_il")).one()
            assert stored.enabled is False

    def test_null_hands_the_decision_back_to_the_config_file(
        self,
        client: TestClient,
        admin: dict[str, str],
        catalog: Seeded,
        session_factory: sessionmaker[Session],
    ) -> None:
        """Not the same as switching it on, which is why the field is nullable."""
        client.patch("/api/v1/admin/sources/netflix_il", json={"enabled": False}, headers=admin)

        response = client.patch(
            "/api/v1/admin/sources/netflix_il", json={"enabled": None}, headers=admin
        )

        assert response.json()["enabled"] is None
        # Nothing in this instance's config disables it, so it is on again.
        assert response.json()["effective_enabled"] is True
        with session_factory() as session:
            stored = session.scalars(select(Source).where(Source.key == "netflix_il")).one()
            assert stored.enabled is None

    def test_switching_one_on_asks_for_its_catalog(
        self,
        client: TestClient,
        admin: dict[str, str],
        catalog: Seeded,
        session_factory: sessionmaker[Session],
    ) -> None:
        """Turning a service on is a request for its titles, not just consent.

        Nobody switches a source on to look at an empty row until 03:00, so the
        ask is recorded here and the fetcher acts on it within the minute.
        """
        client.patch("/api/v1/admin/sources/netflix_il", json={"enabled": False}, headers=admin)

        response = client.patch(
            "/api/v1/admin/sources/netflix_il", json={"enabled": True}, headers=admin
        )

        assert response.json()["backfill_requested_at"] is not None
        with session_factory() as session:
            stored = session.scalars(select(Source).where(Source.key == "netflix_il")).one()
            assert stored.backfill_requested_at is not None

    def test_switching_one_off_withdraws_the_ask(
        self,
        client: TestClient,
        admin: dict[str, str],
        catalog: Seeded,
        session_factory: sessionmaker[Session],
    ) -> None:
        """A source nobody wants on should not be dragged through a full sync."""
        client.patch("/api/v1/admin/sources/netflix_il", json={"enabled": False}, headers=admin)
        client.patch("/api/v1/admin/sources/netflix_il", json={"enabled": True}, headers=admin)

        response = client.patch(
            "/api/v1/admin/sources/netflix_il", json={"enabled": False}, headers=admin
        )

        assert response.json()["backfill_requested_at"] is None

    def test_switching_on_something_already_on_asks_for_nothing(
        self, client: TestClient, admin: dict[str, str], catalog: Seeded
    ) -> None:
        """Only the change is the request. Otherwise every visit to the tab
        that touched a switch would queue a full sync of a healthy source."""
        response = client.patch(
            "/api/v1/admin/sources/netflix_il", json={"enabled": True}, headers=admin
        )

        assert response.json()["effective_enabled"] is True
        assert response.json()["backfill_requested_at"] is None

    def test_a_source_that_does_not_exist_is_a_404(
        self, client: TestClient, admin: dict[str, str]
    ) -> None:
        response = client.patch(
            "/api/v1/admin/sources/no_such_source", json={"enabled": False}, headers=admin
        )

        assert response.status_code == 404

    def test_the_write_needs_its_csrf_token(
        self, client: TestClient, admin: dict[str, str], catalog: Seeded
    ) -> None:
        response = client.patch("/api/v1/admin/sources/netflix_il", json={"enabled": False})

        assert response.status_code == 403


class TestRuns:
    def test_newest_first_without_the_logs(
        self, client: TestClient, admin: dict[str, str], session_factory: sessionmaker[Session]
    ) -> None:
        add_run(session_factory, started_at=NOW - dt.timedelta(days=1))
        add_run(session_factory, started_at=NOW, log="the newer one")

        page = client.get("/api/v1/admin/runs").json()

        assert page["total"] == 2
        assert page["items"][0]["started_at"].startswith("2026-08-07")
        assert page["items"][0]["has_log"] is True
        # The list carries no log text: one run's log is a fetch of its own.
        assert "log" not in page["items"][0]

    def test_a_run_reports_how_long_it_took(
        self, client: TestClient, admin: dict[str, str], session_factory: sessionmaker[Session]
    ) -> None:
        add_run(session_factory)

        assert client.get("/api/v1/admin/runs").json()["items"][0]["duration_seconds"] == 42

    def test_a_run_still_going_has_no_duration(
        self, client: TestClient, admin: dict[str, str], session_factory: sessionmaker[Session]
    ) -> None:
        with session_factory() as session:
            session.add(
                FetchRun(
                    source_key="mako",
                    phase=FetchPhase.SYNC,
                    started_at=NOW,
                    status=FetchStatus.RUNNING,
                    stats={},
                )
            )
            session.commit()

        assert client.get("/api/v1/admin/runs").json()["items"][0]["duration_seconds"] is None

    def test_it_can_be_narrowed(
        self, client: TestClient, admin: dict[str, str], session_factory: sessionmaker[Session]
    ) -> None:
        add_run(session_factory, source_key="mako", status=FetchStatus.FAILED)
        add_run(session_factory, source_key="netflix_il", status=FetchStatus.OK)

        assert client.get("/api/v1/admin/runs?source=mako").json()["total"] == 1
        assert client.get("/api/v1/admin/runs?status=failed").json()["total"] == 1
        assert client.get("/api/v1/admin/runs?phase=enrich").json()["total"] == 0

    def test_one_run_carries_what_it_said(
        self, client: TestClient, admin: dict[str, str], session_factory: sessionmaker[Session]
    ) -> None:
        run_id = add_run(session_factory, log="mako: 0 items\nmako: parser found no cards")

        detail = client.get(f"/api/v1/admin/runs/{run_id}").json()

        assert "no cards" in detail["log"]

    def test_a_run_that_left_no_log_says_so_rather_than_pretending(
        self, client: TestClient, admin: dict[str, str], session_factory: sessionmaker[Session]
    ) -> None:
        run_id = add_run(session_factory, log=None)

        detail = client.get(f"/api/v1/admin/runs/{run_id}").json()

        assert detail["has_log"] is False
        assert detail["log"] is None

    def test_an_unknown_run_is_a_404(self, client: TestClient, admin: dict[str, str]) -> None:
        assert client.get("/api/v1/admin/runs/9999").status_code == 404


class TestStats:
    def test_it_counts_what_an_operator_checks_first(
        self, client: TestClient, admin: dict[str, str], catalog: Seeded
    ) -> None:
        stats = client.get("/api/v1/admin/stats").json()

        assert stats["title_count"] > 0
        assert stats["current_offers"] > 0
        assert stats["pending_reviews"] == 0
        assert stats["stale_after_hours"] == 48

    def test_an_instance_that_has_never_run_says_so(
        self, client: TestClient, admin: dict[str, str], catalog: Seeded
    ) -> None:
        assert client.get("/api/v1/admin/stats").json()["last_run_at"] is None

    def test_a_queue_is_counted_because_it_is_content_that_is_missing(
        self,
        client: TestClient,
        admin: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        with session_factory() as session:
            session.add(MatchReview(source_key="mako", raw_payload={"name": "משהו"}))
            session.commit()

        assert client.get("/api/v1/admin/stats").json()["pending_reviews"] == 1


class TestNothingHereIsCached:
    """A run log is the catalog's inside voice and never belongs in a cache."""

    @pytest.mark.parametrize(
        "path", ["/api/v1/admin/sources", "/api/v1/admin/runs", "/api/v1/admin/stats"]
    )
    def test_no_store(self, client: TestClient, admin: dict[str, str], path: str) -> None:
        assert client.get(path).headers["cache-control"] == "no-store"

    def test_even_the_404_a_stranger_gets(self, client: TestClient, sign_in: SignIn) -> None:
        sign_in()

        response = client.get("/api/v1/admin/stats")

        assert response.status_code == 404
        assert response.headers["cache-control"] == "no-store"


class TestTheTabAgreesWithTheFetcher:
    """Three things decide whether a source is collected, and the screen has to
    read them in the same order the run does.

    It did not. A plugin that declares itself off - the Apple TV Store, which
    costs a request per film - showed as on, because the config file said
    nothing about it and "nothing" used to mean "on". The tab was describing a
    sync that was never going to happen.
    """

    def _source(
        self,
        factory: sessionmaker[Session],
        *,
        key: str = "apple_tv_store",
        default_enabled: bool = False,
        enabled: bool | None = None,
    ) -> None:
        with factory() as session:
            session.add(
                Source(
                    key=key,
                    name="Apple TV Store",
                    kind=SourceKind.RENT_BUY,
                    website_url="https://tv.apple.com/il",
                    default_enabled=default_enabled,
                    enabled=enabled,
                )
            )
            session.commit()

    def _row(self, client: TestClient, key: str = "apple_tv_store") -> dict[str, object]:
        rows = {row["key"]: row for row in client.get("/api/v1/admin/sources").json()}
        return rows[key]

    def test_a_plugin_that_declares_itself_off_reads_as_off(
        self, client: TestClient, admin: dict[str, str], session_factory: sessionmaker[Session]
    ) -> None:
        self._source(session_factory)

        assert self._row(client)["effective_enabled"] is False

    def test_the_config_file_still_wins_over_the_declaration(
        self,
        client: TestClient,
        admin: dict[str, str],
        app: FastAPI,
        session_factory: sessionmaker[Session],
    ) -> None:
        self._source(session_factory)
        app.state.settings.sources["apple_tv_store"] = SourceConfig(enabled=True)

        assert self._row(client)["effective_enabled"] is True

    def test_and_an_operator_switch_wins_over_both(
        self, client: TestClient, admin: dict[str, str], session_factory: sessionmaker[Session]
    ) -> None:
        self._source(session_factory, default_enabled=False, enabled=True)

        row = self._row(client)
        assert row["enabled"] is True
        assert row["effective_enabled"] is True

    def test_an_ordinary_source_still_defaults_to_on(
        self, client: TestClient, admin: dict[str, str], session_factory: sessionmaker[Session]
    ) -> None:
        """Which is why adding a plugin is one file rather than a config edit."""
        self._source(session_factory, key="some_new_plugin", default_enabled=True)

        assert self._row(client, "some_new_plugin")["effective_enabled"] is True


class TestAvailableNowMeansTitles:
    """ "Available now" reads as a number of titles, so it has to be one.

    It counted availability rows, which is a different and larger number: a
    title on two services is one title and two offers. The Apple TV Store made
    the gap conspicuous - it lists the same film as rentable and buyable, so
    every one of its films counted twice - but the label was wrong before that.
    """

    def _offers(self, factory: sessionmaker[Session], catalog: Seeded) -> None:
        """One title offered two ways by one source, plus one offered once."""
        with factory() as session:
            source = session.scalars(select(Source).where(Source.key == "netflix_il")).one()
            session.add_all(
                [
                    Availability(
                        title_id=catalog.foxtrot,
                        source_id=source.id,
                        offer_type=OfferType.RENT,
                        first_seen=NOW,
                        last_seen=NOW,
                        is_current=True,
                    ),
                    Availability(
                        title_id=catalog.foxtrot,
                        source_id=source.id,
                        offer_type=OfferType.BUY,
                        first_seen=NOW,
                        last_seen=NOW,
                        is_current=True,
                    ),
                ]
            )
            session.commit()

    def test_a_title_offered_two_ways_is_one_title(
        self,
        client: TestClient,
        admin: dict[str, str],
        catalog: Seeded,
        session_factory: sessionmaker[Session],
    ) -> None:
        before = client.get("/api/v1/admin/stats").json()
        self._offers(session_factory, catalog)

        after = client.get("/api/v1/admin/stats").json()

        # Two more ways to watch, but only one more thing to watch. That gap is
        # the whole point: the old number reported the 2.
        assert after["current_offers"] == before["current_offers"] + 2
        assert after["titles_available"] == before["titles_available"] + 1

    def test_it_never_exceeds_the_catalog(
        self, client: TestClient, admin: dict[str, str], catalog: Seeded
    ) -> None:
        """The old number could, and did: 50,990 "available" of 33,949 titles."""
        stats = client.get("/api/v1/admin/stats").json()

        assert stats["titles_available"] <= stats["title_count"]

    def test_a_title_on_nothing_is_not_available(
        self, client: TestClient, admin: dict[str, str], catalog: Seeded
    ) -> None:
        stats = client.get("/api/v1/admin/stats").json()

        assert stats["titles_available"] < stats["title_count"]


class TestPerSourceCompleteness:
    """The source table reports how filled-in each service's titles are.

    Counts rather than percentages: the denominator is title_count, and the
    client is the one deciding how to round and colour them - a server that
    sends 96.4 has already thrown away the numbers behind it.
    """

    def _row(self, client: TestClient, key: str = "netflix_il") -> dict[str, object]:
        rows = {row["key"]: row for row in client.get("/api/v1/admin/sources").json()}
        return rows[key]

    def test_it_counts_posters_scores_and_enrichment(
        self,
        client: TestClient,
        admin: dict[str, str],
        catalog: Seeded,
        session_factory: sessionmaker[Session],
    ) -> None:
        with session_factory() as session:
            session.add(
                EnrichAttempt(
                    title_id=catalog.fauda,
                    attempted_at=NOW,
                    outcome=EnrichOutcome.OK,
                    fruitless=0,
                    due_at=NOW,
                )
            )
            session.commit()

        row = self._row(client)

        assert row["title_count"] > 0
        assert row["titles_with_poster"] <= row["title_count"]
        assert row["titles_with_score"] <= row["title_count"]
        assert row["titles_enriched"] == 1

    @pytest.mark.parametrize(
        ("outcome", "enriched"),
        [
            (EnrichOutcome.OK, 1),
            # Nobody has rated it, which is most of a catalog this local and is
            # as complete as that title is ever going to get.
            (EnrichOutcome.NO_DATA, 1),
            # Unfinished business: one cannot be asked about until matching
            # improves, the other is a provider that was down.
            (EnrichOutcome.NO_MATCH, 0),
            (EnrichOutcome.ERROR, 0),
        ],
    )
    def test_a_failed_attempt_is_not_enrichment(
        self,
        client: TestClient,
        admin: dict[str, str],
        catalog: Seeded,
        session_factory: sessionmaker[Session],
        outcome: EnrichOutcome,
        enriched: int,
    ) -> None:
        """Otherwise the column goes green precisely when every attempt failed."""
        with session_factory() as session:
            session.add(
                EnrichAttempt(
                    title_id=catalog.fauda,
                    attempted_at=NOW,
                    outcome=outcome,
                    fruitless=0,
                    due_at=NOW,
                )
            )
            session.commit()

        assert self._row(client)["titles_enriched"] == enriched

    def test_a_source_offering_nothing_reports_zeroes_not_nulls(
        self, client: TestClient, admin: dict[str, str], seed_source: SeedSource
    ) -> None:
        """The client turns a zero denominator into "no figure"; the API's job
        is only to be honest that there is nothing there."""
        seed_source(key="quiet_source")

        row = self._row(client, "quiet_source")

        assert row["title_count"] == 0
        assert row["titles_with_poster"] == 0
        assert row["titles_with_score"] == 0
        assert row["titles_enriched"] == 0

    def test_only_current_offers_count(
        self,
        client: TestClient,
        admin: dict[str, str],
        catalog: Seeded,
        session_factory: sessionmaker[Session],
    ) -> None:
        """A title a service has stopped carrying is not its coverage."""
        before = self._row(client)["title_count"]
        with session_factory() as session:
            source = session.scalars(select(Source).where(Source.key == "netflix_il")).one()
            row = session.scalars(
                select(Availability).where(
                    Availability.source_id == source.id, Availability.is_current.is_(True)
                )
            ).first()
            assert row is not None
            row.is_current = False
            session.commit()

        assert self._row(client)["title_count"] == before - 1

    def test_stats_report_the_queue_it_was_measured_against(
        self,
        client: TestClient,
        admin: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """A pending count with no total cannot say whether anybody is keeping up."""
        with session_factory() as session:
            session.add_all(
                [
                    MatchReview(source_key="mako", raw_payload={"name": "ממתין"}),
                    MatchReview(source_key="mako", raw_payload={"name": "טופל"}, resolved_at=NOW),
                ]
            )
            session.commit()

        stats = client.get("/api/v1/admin/stats").json()

        assert stats["reviews_total"] == 2
        assert stats["pending_reviews"] == 1
