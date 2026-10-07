"""Narrow tokens: what an AI assistant's token may and may not reach."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from helpers import MakeAdmin, SignIn
from seed import Seeded
from sqlalchemy.orm import Session, sessionmaker

from eifo_api.scopes import allows
from eifo_api.security import CSRF_HEADER
from eifo_core.enums import TokenScope
from eifo_core.models import UserItem


@pytest.fixture
def csrf(sign_in: SignIn, make_admin: MakeAdmin) -> str:
    """An administrator: the case where a full token would be most dangerous."""
    token = sign_in()
    make_admin()
    return token


def issue(client: TestClient, csrf: str, scope: str | None) -> str:
    body = {"name": f"assistant-{scope}"} | ({"scope": scope} if scope else {})
    response = client.post("/api/v1/me/tokens", json=body, headers={CSRF_HEADER: csrf})
    assert response.status_code == 201, response.text
    assert response.json()["scope"] == (scope or "full")
    token: str = response.json()["token"]
    return token


def as_token(client: TestClient, token: str) -> TestClient:
    """The same app, with no cookie: a script holding only the token."""
    bare = TestClient(client.app, base_url="https://testserver")
    bare.headers["Authorization"] = f"Bearer {token}"
    return bare


class TestIssuing:
    def test_a_token_is_full_unless_asked_otherwise(self, client: TestClient, csrf: str) -> None:
        """What every token was before scopes, and what the fetcher needs."""
        issue(client, csrf, None)

        listed = client.get("/api/v1/me/tokens").json()
        assert [token["scope"] for token in listed] == ["full"]

    def test_an_unknown_scope_is_refused(self, client: TestClient, csrf: str) -> None:
        response = client.post(
            "/api/v1/me/tokens",
            json={"name": "x", "scope": "admin"},
            headers={CSRF_HEADER: csrf},
        )
        assert response.status_code == 422

    def test_me_says_what_the_token_may_do(self, client: TestClient, csrf: str) -> None:
        reader = as_token(client, issue(client, csrf, "read"))

        me = reader.get("/api/v1/me").json()

        assert me["token_scope"] == "read"
        assert me["is_admin"] is False
        assert client.get("/api/v1/me").json()["token_scope"] is None


class TestReading:
    @pytest.mark.parametrize(
        "path",
        [
            "/api/v1/meta",
            "/api/v1/titles?q=fauda",
            "/api/v1/sources",
            "/api/v1/genres",
            "/api/v1/suggest?q=fa",
            "/api/v1/whats-new",
            "/api/v1/me",
            "/api/v1/me/items",
            "/api/v1/me/items/services",
        ],
    )
    def test_reads_the_catalog_and_its_own_lists(
        self, client: TestClient, csrf: str, catalog: Seeded, path: str
    ) -> None:
        reader = as_token(client, issue(client, csrf, "read"))

        assert reader.get(path).status_code == 200

    def test_reads_one_title(self, client: TestClient, csrf: str, catalog: Seeded) -> None:
        reader = as_token(client, issue(client, csrf, "read"))

        assert reader.get(f"/api/v1/titles/{catalog.fauda}").status_code == 200

    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("GET", "/api/v1/admin/sources"),
            ("GET", "/api/v1/admin/members"),
            ("GET", "/api/v1/reviews"),
            ("GET", "/api/v1/ingest/posters/pending"),
            ("POST", "/api/v1/ingest/runs"),
            ("GET", "/api/v1/me/tokens"),
            ("POST", "/api/v1/me/tokens"),
            ("PATCH", "/api/v1/me"),
            ("DELETE", "/api/v1/me"),
            ("PUT", "/api/v1/me/items/1"),
            ("DELETE", "/api/v1/me/items/1"),
            ("POST", "/api/v1/me/additions"),
            ("GET", "/api/v1/me/additions/search?q=x"),
        ],
    )
    def test_nothing_else_even_for_an_administrator(
        self, client: TestClient, csrf: str, method: str, path: str
    ) -> None:
        reader = as_token(client, issue(client, csrf, "read"))

        response = reader.request(method, path, json={})

        assert response.status_code == 403
        assert "limited to 'read'" in response.json()["detail"]

    def test_the_same_owners_full_token_still_can(self, client: TestClient, csrf: str) -> None:
        """Proves the 403s above are the scope, not the owner."""
        full = as_token(client, issue(client, csrf, None))

        assert full.get("/api/v1/admin/sources").status_code == 200


class TestKeepingLists:
    def test_may_change_its_owners_lists(
        self,
        client: TestClient,
        csrf: str,
        catalog: Seeded,
        session_factory: sessionmaker[Session],
    ) -> None:
        keeper = as_token(client, issue(client, csrf, "lists"))

        put = keeper.put(f"/api/v1/me/items/{catalog.fauda}", json={"want_to_watch": True})

        assert put.status_code == 200
        with session_factory() as session:
            item = session.query(UserItem).one()
            assert item.want_to_watch

        assert keeper.delete(f"/api/v1/me/items/{catalog.fauda}").status_code == 204

    def test_but_not_the_profile_or_anything_else(self, client: TestClient, csrf: str) -> None:
        keeper = as_token(client, issue(client, csrf, "lists"))

        assert keeper.patch("/api/v1/me", json={"is_public": True}).status_code == 403
        assert keeper.get("/api/v1/admin/sources").status_code == 403


class TestTheAllowList:
    """The rule itself, without the app around it."""

    @pytest.mark.parametrize(
        "path",
        [
            "/api/v1/titles-admin",
            "/api/v1/titles/1/../../admin/sources",
            "/api/v1/titles/abc",
            "/api/v1/me/items/1/extra",
            "/titles",
            "/api/v2/titles",
        ],
    )
    def test_matches_whole_paths_only(self, path: str) -> None:
        assert not allows(TokenScope.READ, "GET", path)

    def test_head_reads_like_get(self) -> None:
        assert allows(TokenScope.READ, "HEAD", "/api/v1/titles")

    def test_a_trailing_slash_is_the_same_path(self) -> None:
        assert allows(TokenScope.READ, "GET", "/api/v1/titles/")

    def test_full_allows_everything(self) -> None:
        assert allows(TokenScope.FULL, "DELETE", "/api/v1/admin/additions/1")


class TestLeavingOutWhatYouHaveSeen:
    @pytest.fixture
    def seen(
        self,
        client: TestClient,
        csrf: str,
        catalog: Seeded,
    ) -> Seeded:
        headers = {CSRF_HEADER: csrf}
        client.put(f"/api/v1/me/items/{catalog.fauda}", json={"watched": True}, headers=headers)
        client.put(
            f"/api/v1/me/items/{catalog.foxtrot}", json={"want_to_watch": True}, headers=headers
        )
        return catalog

    def _ids(self, client: TestClient, **params: str) -> set[int]:
        page = client.get("/api/v1/titles", params={"available": "any", **params}).json()
        return {card["id"] for card in page["items"]}

    def test_watched(self, client: TestClient, seen: Seeded) -> None:
        everything = self._ids(client)
        unseen = self._ids(client, exclude="watched")

        assert everything - unseen == {seen.fauda}

    def test_listed_leaves_out_the_watchlist_too(self, client: TestClient, seen: Seeded) -> None:
        unlisted = self._ids(client, exclude="listed")

        assert seen.fauda not in unlisted
        assert seen.foxtrot not in unlisted

    def test_the_total_is_this_members(self, client: TestClient, seen: Seeded) -> None:
        """The remembered total is keyed per member, not shared."""
        everything = client.get("/api/v1/titles", params={"available": "any"}).json()["total"]
        unseen = client.get(
            "/api/v1/titles", params={"available": "any", "exclude": "watched"}
        ).json()["total"]

        assert unseen == everything - 1

    def test_never_cached_for_anybody_else(self, client: TestClient, seen: Seeded) -> None:
        personal = client.get("/api/v1/titles", params={"exclude": "watched"})
        public = client.get("/api/v1/titles")

        assert personal.headers["Cache-Control"] == "no-store"
        assert "ETag" not in personal.headers
        assert public.headers["Cache-Control"].startswith("public")

    def test_needs_somebody_to_ask(self, client: TestClient) -> None:
        response = client.get("/api/v1/titles", params={"exclude": "watched"})

        assert response.status_code == 401

    def test_a_read_token_can_use_it(self, client: TestClient, csrf: str, seen: Seeded) -> None:
        reader = as_token(client, issue(client, csrf, "read"))

        page = reader.get("/api/v1/titles", params={"available": "any", "exclude": "watched"})

        assert seen.fauda not in {card["id"] for card in page.json()["items"]}
