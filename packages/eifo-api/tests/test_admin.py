"""``/api/v1/admin`` - the operator's tab.

Two things are being checked here and they are not the same. One is that the
numbers and the switch are right. The other is that none of it exists for
anybody who is not an administrator - which is the part that matters, because
this is the first surface in the product where being signed in is not enough.
"""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient
from helpers import MakeAdmin, SeedSource, SignIn
from seed import Seeded
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from eifo_api.security import CSRF_HEADER
from eifo_core.enums import FetchPhase, FetchStatus
from eifo_core.models import FetchRun, MatchReview, Source

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
