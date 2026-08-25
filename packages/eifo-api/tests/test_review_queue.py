"""``/api/v1/reviews`` - the triage view's API.

Every ruling here writes catalog data that everybody else reads, so the guard
matters as much as the ruling. What is checked below: that a ruling takes
effect immediately, that the same ruling twice is caught rather than applied
twice, and that a queue two people are draining does not lose a selection
because one row in it was answered a moment ago.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from fastapi.testclient import TestClient
from helpers import MakeAdmin, SignIn
from seed import Seeded
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from eifo_api.security import CSRF_HEADER
from eifo_core.enums import MatchDecision, TitleKind
from eifo_core.models import Availability, MatchReview, Title

MARCH = dt.datetime(2026, 3, 1, 9, 0, tzinfo=dt.UTC)


@pytest.fixture
def admin(sign_in: SignIn, make_admin: MakeAdmin) -> dict[str, str]:
    csrf = sign_in()
    make_admin()
    return {CSRF_HEADER: csrf}


def park(
    factory: sessionmaker[Session],
    *,
    source_key: str = "netflix_il",
    name: str = "סרוגים 2",
    created_at: dt.datetime = MARCH,
    closest: dict[str, Any] | None = None,
    **payload: Any,
) -> int:
    with factory() as session:
        review = MatchReview(
            source_key=source_key,
            raw_payload={"name": name, "kind": TitleKind.SERIES.value, "year": 2008, **payload},
            candidates={"closest": closest} if closest else {},
            created_at=created_at,
        )
        session.add(review)
        session.commit()
        return review.id


class TestReadingTheQueue:
    def test_it_carries_both_sides_of_the_question(
        self,
        client: TestClient,
        admin: dict[str, str],
        catalog: Seeded,
        session_factory: sessionmaker[Session],
    ) -> None:
        """A reviewer comparing two things should not have to fetch the second."""
        park(
            session_factory,
            name="פאודה עונה 4",
            closest={"title_id": catalog.fauda, "name_he": "פאודה", "similarity": 84.2},
        )

        item = client.get("/api/v1/reviews").json()["items"][0]

        assert item["name"] == "פאודה עונה 4"
        assert item["source_name"] == "Netflix"
        assert item["closest"]["title_id"] == catalog.fauda
        assert item["closest"]["similarity"] == 84.2

    def test_a_listing_with_no_suggestion_says_so(
        self, client: TestClient, admin: dict[str, str], session_factory: sessionmaker[Session]
    ) -> None:
        park(session_factory)

        assert client.get("/api/v1/reviews").json()["items"][0]["closest"] is None

    def test_the_suggestion_carries_the_stored_poster(
        self,
        client: TestClient,
        admin: dict[str, str],
        catalog: Seeded,
        session_factory: sessionmaker[Session],
    ) -> None:
        park(session_factory, closest={"title_id": catalog.fauda, "similarity": 90})

        closest = client.get("/api/v1/reviews").json()["items"][0]["closest"]

        assert closest["poster_url"] == "/images/posters/1/w500.jpg"

    def test_it_can_be_narrowed_to_one_source(
        self, client: TestClient, admin: dict[str, str], session_factory: sessionmaker[Session]
    ) -> None:
        park(session_factory, source_key="mako")
        park(session_factory, source_key="netflix_il")

        assert client.get("/api/v1/reviews?source=mako").json()["total"] == 1

    def test_closest_first_when_asked(
        self, client: TestClient, admin: dict[str, str], session_factory: sessionmaker[Session]
    ) -> None:
        park(session_factory, name="חלש", closest={"title_id": 1, "similarity": 9})
        park(session_factory, name="חזק", closest={"title_id": 2, "similarity": 80})

        page = client.get("/api/v1/reviews?order=similarity").json()

        assert [item["name"] for item in page["items"]] == ["חזק", "חלש"]

    def test_the_counts_feed_the_filter_chips(
        self, client: TestClient, admin: dict[str, str], session_factory: sessionmaker[Session]
    ) -> None:
        park(session_factory, source_key="mako")
        park(session_factory, source_key="mako", name="ועוד")
        park(session_factory, source_key="netflix_il")

        counts = client.get("/api/v1/reviews/count").json()

        assert counts["total"] == 3
        assert counts["by_source"] == {"mako": 2, "netflix_il": 1}


class TestRulings:
    def test_attaching_gives_the_offer_to_that_title_now(
        self,
        client: TestClient,
        admin: dict[str, str],
        catalog: Seeded,
        session_factory: sessionmaker[Session],
    ) -> None:
        """Now, rather than at the source's next sync - the whole point of it."""
        review_id = park(session_factory, deep_link_url="https://netflix.test/watch/9")

        response = client.post(
            f"/api/v1/reviews/{review_id}/attach",
            json={"title_id": catalog.fauda},
            headers=admin,
        )

        assert response.status_code == 200
        with session_factory() as session:
            stored = session.get(MatchReview, review_id)
            assert stored is not None
            assert stored.decision is MatchDecision.ATTACHED
            offer = session.scalars(
                select(Availability).where(Availability.title_id == catalog.fauda)
            ).all()
            assert any(row.deep_link_url == "https://netflix.test/watch/9" for row in offer)

    def test_attaching_without_a_title_is_rejected(
        self, client: TestClient, admin: dict[str, str], session_factory: sessionmaker[Session]
    ) -> None:
        review_id = park(session_factory)

        response = client.post(f"/api/v1/reviews/{review_id}/attach", json={}, headers=admin)

        assert response.status_code == 422

    def test_attaching_to_a_title_that_is_not_there(
        self, client: TestClient, admin: dict[str, str], session_factory: sessionmaker[Session]
    ) -> None:
        review_id = park(session_factory)

        response = client.post(
            f"/api/v1/reviews/{review_id}/attach", json={"title_id": 99999}, headers=admin
        )

        assert response.status_code == 404

    def test_creating_gives_the_listing_a_title_of_its_own(
        self,
        client: TestClient,
        admin: dict[str, str],
        catalog: Seeded,
        session_factory: sessionmaker[Session],
    ) -> None:
        review_id = park(session_factory, name="סרוגים 2", name_alt="Srugim 2")

        response = client.post(f"/api/v1/reviews/{review_id}/create", headers=admin)

        assert response.status_code == 200
        with session_factory() as session:
            created = session.scalars(select(Title).where(Title.name_he == "סרוגים 2")).one()
            assert created.name_en == "Srugim 2"
            assert session.get(MatchReview, review_id).decision is MatchDecision.CREATED

    def test_dismissing_creates_nothing(
        self,
        client: TestClient,
        admin: dict[str, str],
        catalog: Seeded,
        session_factory: sessionmaker[Session],
    ) -> None:
        review_id = park(session_factory, name="פרומו לעונה 2")

        response = client.post(f"/api/v1/reviews/{review_id}/dismiss", headers=admin)

        assert response.status_code == 200
        with session_factory() as session:
            stored = session.get(MatchReview, review_id)
            assert stored.decision is MatchDecision.DISMISSED
            assert stored.resolved_title_id is None
            made = session.scalars(select(Title).where(Title.name_he == "פרומו לעונה 2")).all()
            assert made == []

    def test_the_same_ruling_twice_is_caught(
        self,
        client: TestClient,
        admin: dict[str, str],
        catalog: Seeded,
        session_factory: sessionmaker[Session],
    ) -> None:
        """Two tabs, or two people. The second must not create a second title."""
        review_id = park(session_factory)
        client.post(f"/api/v1/reviews/{review_id}/dismiss", headers=admin)

        again = client.post(f"/api/v1/reviews/{review_id}/dismiss", headers=admin)

        assert again.status_code == 409

    def test_a_ruling_on_a_listing_that_is_not_there(
        self, client: TestClient, admin: dict[str, str]
    ) -> None:
        assert client.post("/api/v1/reviews/9999/dismiss", headers=admin).status_code == 404

    def test_a_source_that_has_since_gone_is_reported_not_500(
        self,
        client: TestClient,
        admin: dict[str, str],
        catalog: Seeded,
        session_factory: sessionmaker[Session],
    ) -> None:
        review_id = park(session_factory, source_key="a_source_nobody_has")

        response = client.post(f"/api/v1/reviews/{review_id}/create", headers=admin)

        assert response.status_code == 409
        assert "no longer in the catalog" in response.json()["detail"]

    @pytest.mark.parametrize("ruling", ["attach", "create", "dismiss"])
    def test_every_ruling_needs_its_csrf_token(
        self,
        client: TestClient,
        admin: dict[str, str],
        session_factory: sessionmaker[Session],
        ruling: str,
    ) -> None:
        review_id = park(session_factory)

        response = client.post(f"/api/v1/reviews/{review_id}/{ruling}", json={"title_id": 1})

        assert response.status_code == 403


class TestBulk:
    def test_one_ruling_across_a_selection(
        self,
        client: TestClient,
        admin: dict[str, str],
        catalog: Seeded,
        session_factory: sessionmaker[Session],
    ) -> None:
        ids = [park(session_factory, name=f"Sing Along {n}") for n in range(3)]

        response = client.post(
            "/api/v1/reviews/bulk", json={"ids": ids, "decision": "dismiss"}, headers=admin
        )

        assert response.json() == {"applied": 3, "skipped": []}
        with session_factory() as session:
            for review_id in ids:
                assert session.get(MatchReview, review_id).decision is MatchDecision.DISMISSED

    def test_creating_in_bulk_makes_one_title_each(
        self,
        client: TestClient,
        admin: dict[str, str],
        catalog: Seeded,
        session_factory: sessionmaker[Session],
    ) -> None:
        ids = [park(session_factory, name=f"סרט {n}") for n in range(2)]

        client.post("/api/v1/reviews/bulk", json={"ids": ids, "decision": "create"}, headers=admin)

        with session_factory() as session:
            made = session.scalars(select(Title).where(Title.name_he.like("סרט %"))).all()
            assert len(made) == 2

    def test_a_row_somebody_else_already_answered_is_skipped_not_fatal(
        self,
        client: TestClient,
        admin: dict[str, str],
        catalog: Seeded,
        session_factory: sessionmaker[Session],
    ) -> None:
        """Losing a whole selection to one stale row would be the wrong answer."""
        done = park(session_factory, name="כבר טופל")
        waiting = park(session_factory, name="עדיין ממתין")
        client.post(f"/api/v1/reviews/{done}/dismiss", headers=admin)

        response = client.post(
            "/api/v1/reviews/bulk",
            json={"ids": [done, waiting], "decision": "dismiss"},
            headers=admin,
        )

        assert response.json() == {"applied": 1, "skipped": [done]}

    def test_an_empty_selection_is_rejected(
        self, client: TestClient, admin: dict[str, str]
    ) -> None:
        response = client.post(
            "/api/v1/reviews/bulk", json={"ids": [], "decision": "dismiss"}, headers=admin
        )

        assert response.status_code == 422

    def test_a_selection_larger_than_the_cap_is_rejected(
        self, client: TestClient, admin: dict[str, str]
    ) -> None:
        """One request is one transaction, and a mistake should stay reviewable."""
        response = client.post(
            "/api/v1/reviews/bulk",
            json={"ids": list(range(1, 500)), "decision": "dismiss"},
            headers=admin,
        )

        assert response.status_code == 422

    def test_attaching_is_not_a_bulk_ruling(
        self, client: TestClient, admin: dict[str, str]
    ) -> None:
        """It names a different title each time, so there is nothing to apply at once."""
        response = client.post(
            "/api/v1/reviews/bulk", json={"ids": [1], "decision": "attach"}, headers=admin
        )

        assert response.status_code == 422
