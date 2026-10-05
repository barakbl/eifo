"""Adding a film watched somewhere no tracked service covers."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from helpers import MakeAdmin, SignIn
from seed import Seeded
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from eifo_api.security import CSRF_HEADER
from eifo_api.tmdb import SEARCHES_PER_MINUTE, TmdbLookup
from eifo_core.additions import DAILY_LIMIT
from eifo_core.enums import OfferType, TitleKind
from eifo_core.models import Availability, Title, User, UserItem
from eifo_core.types import utcnow

TMDB = "https://api.themoviedb.org/3"
KEY = "tmdb-test-key"

MATRIX_EN: dict[str, Any] = {
    "id": 603,
    "title": "The Matrix",
    "original_title": "The Matrix",
    "overview": "A hacker learns the truth.",
    "release_date": "1999-03-30",
    "poster_path": "/matrix.jpg",
    "imdb_id": "tt0133093",
    "runtime": 136,
    "original_language": "en",
}
MATRIX_HE: dict[str, Any] = MATRIX_EN | {"title": "מטריקס", "overview": "האקר מגלה את האמת."}


@pytest.fixture
def tmdb(app: FastAPI) -> Iterator[respx.MockRouter]:
    """An instance with a TMDB key, and TMDB itself answering from fixtures."""
    app.state.tmdb = TmdbLookup(KEY)
    with respx.mock(base_url=TMDB, assert_all_called=False) as mock:
        mock.get("/movie/603", params={"language": "en-US"}).respond(json=MATRIX_EN)
        mock.get("/movie/603", params={"language": "he-IL"}).respond(json=MATRIX_HE)
        mock.get("/movie/404").respond(404, json={"status_code": 34})
        mock.get("/search/movie").respond(
            json={
                "results": [
                    {
                        "id": 603,
                        "title": "מטריקס",
                        "original_title": "The Matrix",
                        "release_date": "1999-03-30",
                        "poster_path": "/matrix.jpg",
                    },
                    {"id": 604, "title": "The Matrix Reloaded", "release_date": "2003-05-15"},
                    {"id": 666, "title": "Adult", "adult": True},
                    {"id": "junk"},
                ]
            }
        )
        yield mock


@pytest.fixture
def headers(sign_in: SignIn) -> dict[str, str]:
    return {CSRF_HEADER: sign_in()}


def add(client: TestClient, headers: dict[str, str], **body: Any) -> httpx.Response:
    return client.post("/api/v1/me/additions", json={"tmdb_id": 603} | body, headers=headers)


class TestOffered:
    def test_off_without_a_tmdb_key(self, client: TestClient, headers: dict[str, str]) -> None:
        assert client.get("/api/v1/meta").json()["can_add_titles"] is False
        response = client.get("/api/v1/me/additions/search", params={"q": "matrix"})
        assert response.status_code == 503
        assert "EIFO_TMDB_API_KEY" in response.json()["detail"]

    def test_on_with_one(
        self, client: TestClient, headers: dict[str, str], tmdb: respx.MockRouter
    ) -> None:
        assert client.get("/api/v1/meta").json()["can_add_titles"] is True

    def test_signed_out_gets_nothing(self, client: TestClient, tmdb: respx.MockRouter) -> None:
        assert client.get("/api/v1/me/additions/search", params={"q": "matrix"}).status_code == 401
        assert client.post("/api/v1/me/additions", json={"tmdb_id": 603}).status_code == 401
        assert not tmdb.calls


class TestSearching:
    def test_finds_films_and_drops_what_it_should_not_show(
        self, client: TestClient, headers: dict[str, str], tmdb: respx.MockRouter
    ) -> None:
        found = client.get("/api/v1/me/additions/search", params={"q": "matrix"}).json()

        assert [movie["tmdb_id"] for movie in found] == [603, 604]
        assert found[0] == {
            "tmdb_id": 603,
            "name": "מטריקס",
            "original_name": "The Matrix",
            "year": 1999,
            "thumbnail_url": "https://image.tmdb.org/t/p/w92/matrix.jpg",
            "title_id": None,
        }

    def test_says_which_the_catalog_already_has(
        self,
        client: TestClient,
        headers: dict[str, str],
        tmdb: respx.MockRouter,
        session_factory: sessionmaker[Session],
    ) -> None:
        with session_factory() as session:
            held = Title(type=TitleKind.MOVIE, tmdb_id=604, name_en="The Matrix Reloaded")
            # A series with the film's number is a different work.
            session.add_all([held, Title(type=TitleKind.SERIES, tmdb_id=603, name_en="Other")])
            session.commit()
            held_id = held.id

        found = client.get("/api/v1/me/additions/search", params={"q": "matrix"}).json()

        assert [movie["title_id"] for movie in found] == [None, held_id]

    def test_asks_tmdb_in_hebrew_and_never_shows_the_key(
        self, client: TestClient, headers: dict[str, str], tmdb: respx.MockRouter
    ) -> None:
        response = client.get("/api/v1/me/additions/search", params={"q": "matrix"})

        request = tmdb.calls.last.request
        assert request.url.params["language"] == "he-IL"
        assert request.url.params["include_adult"] == "false"
        assert KEY not in response.text
        assert response.headers["Cache-Control"] == "no-store"

    def test_a_short_query_is_not_sent(
        self, client: TestClient, headers: dict[str, str], tmdb: respx.MockRouter
    ) -> None:
        assert client.get("/api/v1/me/additions/search", params={"q": " m "}).json() == []
        assert not tmdb.calls

    def test_the_same_search_is_asked_of_tmdb_once(
        self, client: TestClient, headers: dict[str, str], tmdb: respx.MockRouter
    ) -> None:
        client.get("/api/v1/me/additions/search", params={"q": "matrix"})
        client.get("/api/v1/me/additions/search", params={"q": "  Matrix "})

        assert tmdb.calls.call_count == 1

    def test_a_flood_is_turned_away(
        self, client: TestClient, headers: dict[str, str], tmdb: respx.MockRouter
    ) -> None:
        for number in range(SEARCHES_PER_MINUTE):
            response = client.get("/api/v1/me/additions/search", params={"q": f"film {number}"})
            assert response.status_code == 200

        refused = client.get("/api/v1/me/additions/search", params={"q": "one more"})

        assert refused.status_code == 429
        assert refused.headers["Retry-After"] == "60"

    def test_tmdb_down_is_a_503_not_a_500(
        self, client: TestClient, headers: dict[str, str], tmdb: respx.MockRouter
    ) -> None:
        tmdb.get("/search/movie").mock(side_effect=httpx.ConnectTimeout("slow"))

        response = client.get("/api/v1/me/additions/search", params={"q": "matrix"})

        assert response.status_code == 503

    def test_tmdb_refusing_is_a_503(
        self, client: TestClient, headers: dict[str, str], tmdb: respx.MockRouter
    ) -> None:
        tmdb.get("/search/movie").respond(401, json={"status_message": f"bad key {KEY}"})

        response = client.get("/api/v1/me/additions/search", params={"q": "matrix"})

        assert response.status_code == 503
        assert KEY not in response.text


class TestAdding:
    def test_adds_the_film_from_tmdbs_own_answer(
        self,
        client: TestClient,
        headers: dict[str, str],
        tmdb: respx.MockRouter,
        session_factory: sessionmaker[Session],
    ) -> None:
        response = add(client, headers, rating=8)

        assert response.status_code == 201
        body = response.json()
        assert body["created"] is True
        assert body["item"]["watched"] is True
        assert body["item"]["rating"] == 8
        with session_factory() as session:
            title = session.get(Title, body["title_id"])
            assert title is not None
            assert (title.name_he, title.name_en, title.year) == ("מטריקס", "The Matrix", 1999)
            assert title.imdb_id == "tt0133093"
            assert title.added_at is not None

    def test_onto_the_want_to_watch_list(
        self, client: TestClient, headers: dict[str, str], tmdb: respx.MockRouter
    ) -> None:
        response = add(client, headers, status="want_to_watch")

        assert response.status_code == 201
        item = response.json()["item"]
        assert (item["want_to_watch"], item["watched"]) == (True, False)

    def test_an_unknown_list_is_refused(
        self, client: TestClient, headers: dict[str, str], tmdb: respx.MockRouter
    ) -> None:
        assert add(client, headers, status="favourites").status_code == 422
        assert not tmdb.calls

    def test_the_body_cannot_name_the_film(
        self, client: TestClient, headers: dict[str, str], tmdb: respx.MockRouter
    ) -> None:
        """Only an id: a name or a poster from a browser is refused, not ignored."""
        for extra in (
            {"name_en": "Hacked"},
            {"poster_source_url": "http://169.254.169.254/"},
        ):
            assert add(client, headers, **extra).status_code == 422
        assert not tmdb.calls

    @pytest.mark.parametrize("tmdb_id", [0, -1, 2**31, "603; DROP"])
    def test_the_id_must_be_one(
        self, client: TestClient, headers: dict[str, str], tmdb: respx.MockRouter, tmdb_id: Any
    ) -> None:
        assert add(client, headers, tmdb_id=tmdb_id).status_code == 422

    @pytest.mark.parametrize("rating", [0, 11, 7.5])
    def test_the_rating_must_be_one(
        self, client: TestClient, headers: dict[str, str], tmdb: respx.MockRouter, rating: Any
    ) -> None:
        assert add(client, headers, rating=rating).status_code == 422

    def test_needs_the_csrf_token(
        self, client: TestClient, headers: dict[str, str], tmdb: respx.MockRouter
    ) -> None:
        assert client.post("/api/v1/me/additions", json={"tmdb_id": 603}).status_code == 403

    def test_a_film_tmdb_does_not_have(
        self, client: TestClient, headers: dict[str, str], tmdb: respx.MockRouter
    ) -> None:
        assert add(client, headers, tmdb_id=404).status_code == 404

    def test_an_adult_film_is_not_added(
        self, client: TestClient, headers: dict[str, str], tmdb: respx.MockRouter
    ) -> None:
        tmdb.get("/movie/777").respond(json=MATRIX_EN | {"id": 777, "adult": True})

        assert add(client, headers, tmdb_id=777).status_code == 404

    def test_a_film_already_here_is_only_marked_watched(
        self,
        client: TestClient,
        headers: dict[str, str],
        tmdb: respx.MockRouter,
        catalog: Seeded,
        session_factory: sessionmaker[Session],
    ) -> None:
        with session_factory() as session:
            foxtrot = session.get(Title, catalog.foxtrot)
            assert foxtrot is not None
            foxtrot.tmdb_id = 603
            session.commit()

        response = add(client, headers)

        assert response.status_code == 200
        assert response.json() | {"item": None} == {
            "title_id": catalog.foxtrot,
            "created": False,
            "item": None,
        }

    def test_twice_is_once(
        self,
        client: TestClient,
        headers: dict[str, str],
        tmdb: respx.MockRouter,
        session_factory: sessionmaker[Session],
    ) -> None:
        first = add(client, headers).json()
        second = add(client, headers).json()

        assert second["title_id"] == first["title_id"]
        assert second["created"] is False
        with session_factory() as session:
            assert len(session.scalars(select(Title).where(Title.tmdb_id == 603)).all()) == 1

    def test_tmdb_down_adds_nothing(
        self,
        client: TestClient,
        headers: dict[str, str],
        tmdb: respx.MockRouter,
        session_factory: sessionmaker[Session],
    ) -> None:
        tmdb.get("/movie/603", params={"language": "he-IL"}).mock(
            side_effect=httpx.ConnectError("down")
        )

        assert add(client, headers).status_code == 503
        with session_factory() as session:
            assert session.scalar(select(Title.id)) is None

    def test_the_daily_limit(
        self,
        client: TestClient,
        headers: dict[str, str],
        tmdb: respx.MockRouter,
        session_factory: sessionmaker[Session],
    ) -> None:
        with session_factory() as session:
            user_id = session.scalar(select(User.id))
            for number in range(DAILY_LIMIT):
                session.add(
                    Title(
                        type=TitleKind.MOVIE,
                        tmdb_id=1000 + number,
                        name_en=f"Film {number}",
                        added_at=utcnow(),
                        added_by_user_id=user_id,
                    )
                )
            session.commit()

        response = add(client, headers)

        assert response.status_code == 429
        assert str(DAILY_LIMIT) in response.json()["detail"]


class TestOtherServices:
    @pytest.fixture
    def added(
        self, client: TestClient, headers: dict[str, str], tmdb: respx.MockRouter, catalog: Seeded
    ) -> int:
        title_id: int = add(client, headers).json()["title_id"]
        return title_id

    def _ids(self, client: TestClient, **params: str) -> set[int]:
        page = client.get("/api/v1/titles", params=params).json()
        return {card["id"] for card in page["items"]}

    def test_left_out_of_the_catalog_by_default(self, client: TestClient, added: int) -> None:
        assert added not in self._ids(client)
        assert added not in self._ids(client, available="any")
        assert added not in self._ids(client, sources="netflix_il")

    def test_shown_when_asked_for(self, client: TestClient, added: int, catalog: Seeded) -> None:
        assert self._ids(client, sources="other") == {added}
        assert client.get("/api/v1/meta").json()["user_added_count"] == 1

    def test_alongside_real_services(self, client: TestClient, added: int, catalog: Seeded) -> None:
        both = self._ids(client, sources="netflix_il,other")

        assert added in both
        assert both - {added} == self._ids(client, sources="netflix_il")

    def test_never_counted_as_gone(self, client: TestClient, added: int) -> None:
        assert added not in self._ids(client, sources="other", available="gone")

    def test_the_seeds_orphan_is_not_an_addition(
        self, client: TestClient, added: int, catalog: Seeded
    ) -> None:
        """A title on nothing that no member added is not "other services"."""
        assert catalog.orphan not in self._ids(client, sources="other")

    def test_marked_on_the_card_and_shows_its_poster_at_once(
        self, client: TestClient, added: int
    ) -> None:
        card = client.get("/api/v1/titles", params={"sources": "other"}).json()["items"][0]
        detail = client.get(f"/api/v1/titles/{added}").json()

        assert card["user_added"] is True
        assert detail["user_added"] is True
        assert card["poster_url"] == "https://image.tmdb.org/t/p/w500/matrix.jpg"

    def test_suggestions_follow_the_filter(self, client: TestClient, added: int) -> None:
        def suggested(**params: str) -> list[int]:
            answer = client.get("/api/v1/suggest", params={"q": "matrix", **params}).json()
            return [title["id"] for title in answer["titles"]]

        assert suggested() == []
        assert suggested(sources="other") == [added]

    def test_a_listing_makes_it_an_ordinary_title(
        self,
        client: TestClient,
        added: int,
        catalog: Seeded,
        session_factory: sessionmaker[Session],
    ) -> None:
        with session_factory() as session:
            session.add(
                Availability(title_id=added, source_id=catalog.netflix, offer_type=OfferType.STREAM)
            )
            session.commit()

        assert added not in self._ids(client, sources="other")
        assert added in self._ids(client, sources="netflix_il")
        assert client.get(f"/api/v1/titles/{added}").json()["user_added"] is False


class TestAdministering:
    @pytest.fixture
    def admin_headers(self, headers: dict[str, str], make_admin: MakeAdmin) -> dict[str, str]:
        make_admin()
        return headers

    def test_lists_additions_with_who_added_them(
        self,
        client: TestClient,
        admin_headers: dict[str, str],
        tmdb: respx.MockRouter,
    ) -> None:
        title_id = add(client, admin_headers).json()["title_id"]

        page = client.get("/api/v1/admin/additions").json()

        assert page["total"] == 1
        entry = page["items"][0]
        assert entry["title"]["id"] == title_id
        assert entry["added_by"]
        assert entry["members"] == 1

    def test_removes_one(
        self,
        client: TestClient,
        admin_headers: dict[str, str],
        tmdb: respx.MockRouter,
        session_factory: sessionmaker[Session],
    ) -> None:
        title_id = add(client, admin_headers, rating=3).json()["title_id"]

        response = client.delete(f"/api/v1/admin/additions/{title_id}", headers=admin_headers)

        assert response.status_code == 204
        with session_factory() as session:
            assert session.get(Title, title_id) is None
            assert session.scalar(select(UserItem.id)) is None

    def test_will_not_remove_a_title_a_service_lists(
        self,
        client: TestClient,
        admin_headers: dict[str, str],
        tmdb: respx.MockRouter,
        catalog: Seeded,
        session_factory: sessionmaker[Session],
    ) -> None:
        title_id = add(client, admin_headers).json()["title_id"]
        with session_factory() as session:
            session.add(
                Availability(
                    title_id=title_id,
                    source_id=catalog.netflix,
                    offer_type=OfferType.STREAM,
                    is_current=False,
                )
            )
            session.commit()

        response = client.delete(f"/api/v1/admin/additions/{title_id}", headers=admin_headers)

        assert response.status_code == 409

    def test_is_not_a_way_to_delete_any_title(
        self, client: TestClient, admin_headers: dict[str, str], catalog: Seeded
    ) -> None:
        response = client.delete(f"/api/v1/admin/additions/{catalog.orphan}", headers=admin_headers)

        assert response.status_code == 404

    def test_members_cannot(
        self, client: TestClient, headers: dict[str, str], tmdb: respx.MockRouter
    ) -> None:
        title_id = add(client, headers).json()["title_id"]

        assert client.get("/api/v1/admin/additions").status_code in (403, 404)
        assert client.delete(
            f"/api/v1/admin/additions/{title_id}", headers=headers
        ).status_code in (403, 404)


class TestTheMemberLeaves:
    def test_their_additions_stay(
        self,
        client: TestClient,
        headers: dict[str, str],
        tmdb: respx.MockRouter,
        session_factory: sessionmaker[Session],
    ) -> None:
        title_id = add(client, headers).json()["title_id"]

        assert client.delete("/api/v1/me", headers=headers).status_code == 204

        with session_factory() as session:
            title = session.get(Title, title_id)
            assert title is not None
            assert title.added_by_user_id is None
            assert title.added_at is not None
