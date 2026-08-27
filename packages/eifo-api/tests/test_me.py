"""Profile, lists and account deletion."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from helpers import SignIn
from seed import Seeded
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from eifo_api.security import CSRF_HEADER
from eifo_core.enums import AuthProvider
from eifo_core.models import User, UserItem, UserSession


@pytest.fixture
def csrf(sign_in: SignIn) -> str:
    return sign_in()


@pytest.fixture
def headers(csrf: str) -> dict[str, str]:
    return {CSRF_HEADER: csrf}


class TestProfile:
    def test_reports_the_defaults_a_new_account_starts_with(
        self, client: TestClient, csrf: str
    ) -> None:
        user = client.get("/api/v1/me").json()["user"]

        assert user["is_public"] is False
        assert user["handle"] is None
        assert user["my_source_ids"] == []

    def test_updates_only_the_fields_supplied(
        self, client: TestClient, headers: dict[str, str]
    ) -> None:
        before = client.get("/api/v1/me").json()["user"]

        after = client.patch("/api/v1/me", json={"display_name": "תמר"}, headers=headers).json()

        assert after["display_name"] == "תמר"
        assert after["avatar_url"] == before["avatar_url"]

    def test_trims_a_display_name(self, client: TestClient, headers: dict[str, str]) -> None:
        response = client.patch("/api/v1/me", json={"display_name": "  תמר  "}, headers=headers)

        assert response.json()["display_name"] == "תמר"

    def test_rejects_a_blank_display_name(
        self, client: TestClient, headers: dict[str, str]
    ) -> None:
        assert (
            client.patch("/api/v1/me", json={"display_name": "   "}, headers=headers).status_code
            == 422
        )

    def test_rejects_an_unknown_field(self, client: TestClient, headers: dict[str, str]) -> None:
        """Silently ignoring a typo'd field would look like a saved setting."""
        response = client.patch("/api/v1/me", json={"is_admin": True}, headers=headers)

        assert response.status_code == 422

    def test_saves_the_my_services_preset(
        self,
        client: TestClient,
        headers: dict[str, str],
        catalog: Seeded,
    ) -> None:
        response = client.patch(
            "/api/v1/me",
            json={"my_source_ids": [catalog.netflix, catalog.mako, catalog.netflix]},
            headers=headers,
        )

        assert response.status_code == 200
        assert response.json()["my_source_ids"] == [catalog.netflix, catalog.mako]

    def test_rejects_a_preset_naming_a_source_that_does_not_exist(
        self, client: TestClient, headers: dict[str, str]
    ) -> None:
        """Otherwise the preset silently filters the catalog down to nothing."""
        response = client.patch("/api/v1/me", json={"my_source_ids": [9999]}, headers=headers)

        assert response.status_code == 422
        assert "9999" in response.json()["detail"]

    def test_requires_a_session(self, client: TestClient) -> None:
        assert client.patch("/api/v1/me", json={"display_name": "x"}).status_code == 401


class TestHandleAndVisibility:
    def test_claims_a_handle(self, client: TestClient, headers: dict[str, str]) -> None:
        response = client.patch("/api/v1/me", json={"handle": "tamar"}, headers=headers)

        assert response.json()["handle"] == "tamar"

    def test_rejects_a_handle_that_would_not_read_in_a_url(
        self, client: TestClient, headers: dict[str, str]
    ) -> None:
        assert (
            client.patch("/api/v1/me", json={"handle": "Tamar Levi"}, headers=headers).status_code
            == 422
        )

    def test_rejects_a_handle_another_account_already_has(
        self,
        client: TestClient,
        headers: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        with session_factory() as session:
            session.add(
                User(
                    auth_provider="x",
                    auth_subject="someone-else",
                    display_name="Someone",
                    handle="tamar",
                )
            )
            session.commit()

        response = client.patch("/api/v1/me", json={"handle": "tamar"}, headers=headers)

        assert response.status_code == 409

    def test_going_public_without_a_handle_is_refused(
        self, client: TestClient, headers: dict[str, str]
    ) -> None:
        """A public profile is a URL, and the handle is what the URL says."""
        response = client.patch("/api/v1/me", json={"is_public": True}, headers=headers)

        assert response.status_code == 422
        assert client.get("/api/v1/me").json()["user"]["is_public"] is False

    def test_going_public_with_a_handle_works(
        self, client: TestClient, headers: dict[str, str]
    ) -> None:
        body = client.patch(
            "/api/v1/me", json={"handle": "tamar", "is_public": True}, headers=headers
        ).json()

        assert body["handle"] == "tamar"
        assert body["is_public"] is True


class TestItems:
    def test_adds_a_title_to_a_list(
        self,
        client: TestClient,
        headers: dict[str, str],
        catalog: Seeded,
    ) -> None:
        response = client.put(
            f"/api/v1/me/items/{catalog.fauda}",
            json={"want_to_watch": True},
            headers=headers,
        )

        assert response.status_code == 200
        assert response.json()["want_to_watch"] is True

    def test_updates_one_field_without_clearing_the_others(
        self,
        client: TestClient,
        headers: dict[str, str],
        catalog: Seeded,
    ) -> None:
        client.put(
            f"/api/v1/me/items/{catalog.fauda}",
            json={"watched": True, "rating": 9},
            headers=headers,
        )

        response = client.put(
            f"/api/v1/me/items/{catalog.fauda}", json={"rating": 10}, headers=headers
        )

        assert response.json()["watched"] is True
        assert response.json()["rating"] == 10

    def test_an_explicit_null_clears_a_field(
        self,
        client: TestClient,
        headers: dict[str, str],
        catalog: Seeded,
    ) -> None:
        client.put(
            f"/api/v1/me/items/{catalog.fauda}",
            json={"watched": True, "rating": 9},
            headers=headers,
        )

        response = client.put(
            f"/api/v1/me/items/{catalog.fauda}", json={"rating": None}, headers=headers
        )

        assert response.json()["rating"] is None
        assert response.json()["watched"] is True

    def test_clearing_everything_removes_the_entry(
        self,
        client: TestClient,
        headers: dict[str, str],
        catalog: Seeded,
        session_factory: sessionmaker[Session],
    ) -> None:
        """An entry that says nothing is not worth a row - or a list slot."""
        client.put(f"/api/v1/me/items/{catalog.fauda}", json={"watched": True}, headers=headers)

        client.put(f"/api/v1/me/items/{catalog.fauda}", json={"watched": False}, headers=headers)

        with session_factory() as session:
            assert session.scalars(select(UserItem)).all() == []

    def test_rejects_a_rating_outside_one_to_ten(
        self,
        client: TestClient,
        headers: dict[str, str],
        catalog: Seeded,
    ) -> None:
        response = client.put(
            f"/api/v1/me/items/{catalog.fauda}", json={"rating": 11}, headers=headers
        )

        assert response.status_code == 422

    def test_rejects_an_over_long_note(
        self,
        client: TestClient,
        headers: dict[str, str],
        catalog: Seeded,
    ) -> None:
        response = client.put(
            f"/api/v1/me/items/{catalog.fauda}", json={"note": "א" * 2001}, headers=headers
        )

        assert response.status_code == 422

    def test_a_whitespace_note_is_no_note(
        self,
        client: TestClient,
        headers: dict[str, str],
        catalog: Seeded,
    ) -> None:
        response = client.put(
            f"/api/v1/me/items/{catalog.fauda}",
            json={"watched": True, "note": "   "},
            headers=headers,
        )

        assert response.json()["note"] is None

    def test_rejects_a_title_that_does_not_exist(
        self, client: TestClient, headers: dict[str, str]
    ) -> None:
        assert (
            client.put("/api/v1/me/items/9999", json={"rating": 5}, headers=headers).status_code
            == 404
        )

    def test_requires_the_csrf_token(self, client: TestClient, csrf: str, catalog: Seeded) -> None:
        assert (
            client.put(f"/api/v1/me/items/{catalog.fauda}", json={"rating": 5}).status_code == 403
        )

    def test_removes_an_entry(
        self,
        client: TestClient,
        headers: dict[str, str],
        catalog: Seeded,
    ) -> None:
        client.put(f"/api/v1/me/items/{catalog.fauda}", json={"watched": True}, headers=headers)

        assert (
            client.delete(f"/api/v1/me/items/{catalog.fauda}", headers=headers).status_code == 204
        )
        assert client.get("/api/v1/me/items").json()["total"] == 0

    def test_removing_something_not_in_a_list_is_a_404(
        self,
        client: TestClient,
        headers: dict[str, str],
        catalog: Seeded,
    ) -> None:
        assert (
            client.delete(f"/api/v1/me/items/{catalog.fauda}", headers=headers).status_code == 404
        )


class TestMyList:
    @pytest.fixture
    def filled(
        self,
        client: TestClient,
        headers: dict[str, str],
        catalog: Seeded,
    ) -> Seeded:
        client.put(
            f"/api/v1/me/items/{catalog.fauda}",
            json={"watched": True, "rating": 9},
            headers=headers,
        )
        client.put(
            f"/api/v1/me/items/{catalog.foxtrot}",
            json={"want_to_watch": True},
            headers=headers,
        )
        client.put(f"/api/v1/me/items/{catalog.shtisel}", json={"rating": 7}, headers=headers)
        return catalog

    def test_returns_every_entry_with_its_title_card(
        self, client: TestClient, filled: Seeded
    ) -> None:
        page = client.get("/api/v1/me/items").json()

        assert page["total"] == 3
        assert all(item["title"]["id"] == item["title_id"] for item in page["items"])
        assert {item["title"]["name_he"] for item in page["items"]} == {
            "פאודה",
            "פוקסטרוט",
            "שטיסל",
        }

    def test_filters_by_list(self, client: TestClient, filled: Seeded) -> None:
        page = client.get("/api/v1/me/items", params={"status": "watched"}).json()

        assert [item["title_id"] for item in page["items"]] == [filled.fauda]

    def test_filters_to_what_has_been_rated(self, client: TestClient, filled: Seeded) -> None:
        """The third tab: rated, whether or not it is filed under a list."""
        page = client.get("/api/v1/me/items", params={"rated": True}).json()

        assert {item["title_id"] for item in page["items"]} == {filled.fauda, filled.shtisel}

    def test_filters_to_the_titles_asked_about(self, client: TestClient, filled: Seeded) -> None:
        """What the catalog grid asks: which of these am I already keeping."""
        page = client.get(
            "/api/v1/me/items", params={"title_ids": [filled.fauda, filled.shtisel]}
        ).json()

        assert {item["title_id"] for item in page["items"]} == {filled.fauda, filled.shtisel}

    def test_a_title_with_no_entry_simply_comes_back_absent(
        self, client: TestClient, filled: Seeded
    ) -> None:
        """The grid reads a missing entry as "not on any list", so asking about
        a title nobody has filed is not an error."""
        page = client.get(
            "/api/v1/me/items", params={"title_ids": [filled.fauda, filled.orphan]}
        ).json()

        assert [item["title_id"] for item in page["items"]] == [filled.fauda]

    def test_asking_about_titles_stays_inside_the_user(
        self,
        client: TestClient,
        filled: Seeded,
        sign_in: SignIn,
    ) -> None:
        """Naming somebody else's title id does not reach their entry."""
        client.cookies.clear()
        sign_in(AuthProvider.X)

        page = client.get("/api/v1/me/items", params={"title_ids": [filled.fauda]}).json()

        assert page["total"] == 0

    def test_pages(self, client: TestClient, filled: Seeded) -> None:
        page = client.get("/api/v1/me/items", params={"page_size": 2}).json()

        assert len(page["items"]) == 2
        assert page["total"] == 3

    def test_one_users_list_is_not_anothers(
        self,
        client: TestClient,
        filled: Seeded,
        sign_in: SignIn,
    ) -> None:
        client.cookies.clear()
        sign_in(AuthProvider.X)

        assert client.get("/api/v1/me/items").json()["total"] == 0


class TestAccountDeletion:
    def test_takes_the_user_their_sessions_and_their_lists(
        self,
        client: TestClient,
        headers: dict[str, str],
        catalog: Seeded,
        session_factory: sessionmaker[Session],
    ) -> None:
        client.put(f"/api/v1/me/items/{catalog.fauda}", json={"watched": True}, headers=headers)

        assert client.delete("/api/v1/me", headers=headers).status_code == 204

        with session_factory() as session:
            assert session.scalars(select(User)).all() == []
            assert session.scalars(select(UserSession)).all() == []
            assert session.scalars(select(UserItem)).all() == []

    def test_the_session_stops_working_immediately(
        self, client: TestClient, headers: dict[str, str]
    ) -> None:
        client.delete("/api/v1/me", headers=headers)

        assert client.get("/api/v1/me").status_code == 401

    def test_leaves_the_catalog_alone(
        self,
        client: TestClient,
        headers: dict[str, str],
        catalog: Seeded,
    ) -> None:
        """Deleting an account deletes a person's data, not the films."""
        client.put(f"/api/v1/me/items/{catalog.fauda}", json={"rating": 9}, headers=headers)

        client.delete("/api/v1/me", headers=headers)

        assert client.get(f"/api/v1/titles/{catalog.fauda}").status_code == 200

    def test_requires_the_csrf_token(self, client: TestClient, csrf: str) -> None:
        assert client.delete("/api/v1/me").status_code == 403
