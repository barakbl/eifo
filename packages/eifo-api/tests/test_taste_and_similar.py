"""/me/taste, /titles/{id}/similar, and the catalog's new filters."""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient
from helpers import SignIn
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from eifo_api.security import CSRF_HEADER
from eifo_core.enums import CreditRole, OfferType, SourceKind, TitleKind, TokenScope
from eifo_core.models import (
    ApiToken,
    Availability,
    Credit,
    Genre,
    Person,
    Source,
    Title,
    User,
)
from eifo_core.tokens import hash_token, new_api_token


@dataclass(frozen=True)
class Films:
    seed: int
    same_director: int
    same_genre: int
    elsewhere: int
    israeli: int
    director: int
    netflix: str = "netflix_il"


@pytest.fixture
def films(session_factory: sessionmaker[Session]) -> Films:
    with session_factory() as session:
        drama = Genre(tmdb_id=18, name_en="Drama", name_he="דרמה")
        crime = Genre(tmdb_id=80, name_en="Crime")
        netflix = Source(
            key="netflix_il", name="Netflix", kind=SourceKind.SUBSCRIPTION, website_url="https://n"
        )
        yes = Source(key="yes", name="yes", kind=SourceKind.SUBSCRIPTION, website_url="https://y")
        auteur = Person(name_en="An Auteur")
        session.add_all([drama, crime, netflix, yes, auteur])
        session.flush()

        def film(name: str, source: Source, genres: list[Genre], **extra: object) -> Title:
            title = Title(type=TitleKind.MOVIE, name_en=name, year=2000, genres=genres, **extra)
            session.add(title)
            session.flush()
            session.add(
                Availability(title_id=title.id, source_id=source.id, offer_type=OfferType.STREAM)
            )
            return title

        seed = film("Seed", netflix, [drama, crime])
        same_director = film("Same Director", netflix, [drama])
        same_genre = film("Same Genres", netflix, [drama, crime])
        elsewhere = film("Elsewhere", yes, [drama, crime])
        israeli = film("Israeli", yes, [drama], origin_countries="FR,IL", original_language="he")
        for title in (seed, same_director):
            session.add(
                Credit(
                    title_id=title.id, person_id=auteur.id, role=CreditRole.DIRECTOR, source="tmdb"
                )
            )
        session.commit()
        return Films(
            seed=seed.id,
            same_director=same_director.id,
            same_genre=same_genre.id,
            elsewhere=elsewhere.id,
            israeli=israeli.id,
            director=auteur.id,
        )


@pytest.fixture
def csrf(sign_in: SignIn) -> str:
    return sign_in()


def ids(page: dict[str, object]) -> list[int]:
    return [card["id"] for card in page["items"]]  # type: ignore[index,union-attr]


class TestSimilar:
    def test_most_alike_first_with_reasons(self, client: TestClient, films: Films) -> None:
        page = client.get(f"/api/v1/titles/{films.seed}/similar").json()

        assert ids(page)[:2] == [films.same_director, films.same_genre]
        first = page["items"][0]
        assert first["because"]["people"][0]["name_en"] == "An Auteur"
        assert first["because"]["genres"] == ["Drama"]
        assert page["items"][1]["because"]["genres"] == ["Crime", "Drama"]
        # Hebrew where the catalog has it, English where it does not.
        assert page["items"][1]["because"]["genres_he"] == ["Crime", "דרמה"]
        assert first["similarity"] > page["items"][1]["similarity"]
        assert films.seed not in ids(page)

    def test_filtered_like_the_catalog(self, client: TestClient, films: Films) -> None:
        page = client.get(f"/api/v1/titles/{films.seed}/similar", params={"sources": "yes"}).json()

        assert set(ids(page)) == {films.elsewhere, films.israeli}

    def test_leaves_out_what_you_have_seen(
        self, client: TestClient, csrf: str, films: Films
    ) -> None:
        client.put(
            f"/api/v1/me/items/{films.same_director}",
            json={"watched": True},
            headers={CSRF_HEADER: csrf},
        )

        response = client.get(f"/api/v1/titles/{films.seed}/similar", params={"exclude": "watched"})

        assert films.same_director not in ids(response.json())
        assert response.headers["Cache-Control"] == "no-store"

    def test_exclude_needs_somebody(self, client: TestClient, films: Films) -> None:
        response = client.get(f"/api/v1/titles/{films.seed}/similar", params={"exclude": "watched"})
        assert response.status_code == 401

    def test_an_unknown_title(self, client: TestClient, films: Films) -> None:
        assert client.get("/api/v1/titles/999999/similar").status_code == 404

    def test_the_page_size_is_capped(self, client: TestClient, films: Films) -> None:
        response = client.get(f"/api/v1/titles/{films.seed}/similar", params={"page_size": 51})
        assert response.status_code == 422


class TestNewFilters:
    def test_by_person(self, client: TestClient, films: Films) -> None:
        page = client.get("/api/v1/titles", params={"person": films.director}).json()

        assert set(ids(page)) == {films.seed, films.same_director}

    def test_by_person_in_one_role(self, client: TestClient, films: Films) -> None:
        directed = client.get(
            "/api/v1/titles", params={"person": films.director, "role": "director"}
        ).json()
        acted = client.get(
            "/api/v1/titles", params={"person": films.director, "role": "cast"}
        ).json()

        assert set(ids(directed)) == {films.seed, films.same_director}
        assert acted["total"] == 0

    @pytest.mark.parametrize("countries", ["IL", "il", "IL,US", "FR"])
    def test_by_country(self, client: TestClient, films: Films, countries: str) -> None:
        page = client.get("/api/v1/titles", params={"countries": countries}).json()

        assert ids(page) == [films.israeli]

    @pytest.mark.parametrize("nonsense", ["R", "L", "R,I", "ILX", ",,"])
    def test_a_country_that_is_not_a_code_matches_nothing(
        self, client: TestClient, films: Films, nonsense: str
    ) -> None:
        """Not "everywhere": a filter that cannot be read narrows to nothing.

        "R" is inside "FR", too - the padding is what keeps it from matching.
        """
        page = client.get("/api/v1/titles", params={"countries": nonsense}).json()

        assert page["total"] == 0

    def test_by_language(self, client: TestClient, films: Films) -> None:
        page = client.get("/api/v1/titles", params={"language": "HE"}).json()

        assert ids(page) == [films.israeli]

    def test_by_ids(self, client: TestClient, films: Films) -> None:
        page = client.get(
            "/api/v1/titles", params={"ids": f"{films.seed},{films.israeli},abc,99999"}
        ).json()

        assert set(ids(page)) == {films.seed, films.israeli}

    def test_ids_with_nothing_usable_is_nothing(self, client: TestClient, films: Films) -> None:
        assert client.get("/api/v1/titles", params={"ids": "abc"}).json()["total"] == 0

    def test_filters_combine(self, client: TestClient, films: Films) -> None:
        page = client.get(
            "/api/v1/titles", params={"person": films.director, "sources": "netflix_il"}
        ).json()

        assert set(ids(page)) == {films.seed, films.same_director}


class TestTaste:
    def test_needs_somebody(self, client: TestClient) -> None:
        assert client.get("/api/v1/me/taste").status_code == 401

    def test_read_from_your_ratings(self, client: TestClient, csrf: str, films: Films) -> None:
        headers = {CSRF_HEADER: csrf}
        for title_id, rating in (
            (films.seed, 10),
            (films.same_director, 9),
            (films.same_genre, 6),
            (films.elsewhere, 3),
        ):
            client.put(f"/api/v1/me/items/{title_id}", json={"rating": rating}, headers=headers)

        taste = client.get("/api/v1/me/taste").json()

        assert (taste["rated"], taste["average"]) == (4, 7.0)
        assert [entry["name"] for entry in taste["favourites"]] == ["Seed", "Same Director"]
        assert [entry["name"] for entry in taste["dislikes"]] == ["Elsewhere"]
        assert taste["liked"]["directors"][0]["id"] == films.director
        assert taste["liked"]["directors"][0]["titles"] == 2
        assert client.get("/api/v1/me/taste").headers["Cache-Control"] == "no-store"

    def test_a_read_token_may_ask(
        self,
        client: TestClient,
        csrf: str,
        films: Films,
        session_factory: sessionmaker[Session],
    ) -> None:
        token = new_api_token()
        with session_factory() as session:
            user = session.scalars(select(User)).one()
            session.add(
                ApiToken(
                    token_hash=hash_token(token),
                    user_id=user.id,
                    name="assistant",
                    scope=TokenScope.READ,
                )
            )
            session.commit()

        reader = TestClient(client.app, base_url="https://testserver")
        reader.headers["Authorization"] = f"Bearer {token}"

        assert reader.get("/api/v1/me/taste").status_code == 200
        assert reader.get("/api/v1/me/for-you").status_code == 200
        assert reader.get(f"/api/v1/titles/{films.seed}/similar").status_code == 200


class TestForYou:
    def rate(self, client: TestClient, csrf: str, title_id: int, **body: object) -> None:
        response = client.put(
            f"/api/v1/me/items/{title_id}", json=body, headers={CSRF_HEADER: csrf}
        )
        assert response.status_code == 200, response.text

    def test_needs_somebody(self, client: TestClient) -> None:
        assert client.get("/api/v1/me/for-you").status_code == 401

    def test_nothing_until_something_is_loved(
        self, client: TestClient, csrf: str, films: Films
    ) -> None:
        self.rate(client, csrf, films.seed, rating=7)

        assert client.get("/api/v1/me/for-you").json() == []

    def test_picks_with_the_favourite_and_the_reason(
        self, client: TestClient, csrf: str, films: Films
    ) -> None:
        self.rate(client, csrf, films.seed, rating=10)

        picks = client.get("/api/v1/me/for-you").json()

        assert picks[0]["id"] == films.same_director
        assert picks[0]["seed"] == {
            "title_id": films.seed,
            "name": "Seed",
            "name_he": None,
            "year": 2000,
            "rating": 10,
        }
        assert picks[0]["because"]["people"][0]["name_en"] == "An Auteur"
        assert films.seed not in {pick["id"] for pick in picks}

    def test_on_the_services_asked_about(self, client: TestClient, csrf: str, films: Films) -> None:
        self.rate(client, csrf, films.seed, rating=10)

        picks = client.get("/api/v1/me/for-you", params={"sources": "yes"}).json()

        assert {pick["id"] for pick in picks} == {films.elsewhere, films.israeli}

    def test_never_what_is_on_a_list(self, client: TestClient, csrf: str, films: Films) -> None:
        self.rate(client, csrf, films.seed, rating=10)
        self.rate(client, csrf, films.same_director, want_to_watch=True)

        picks = client.get("/api/v1/me/for-you").json()

        assert films.same_director not in {pick["id"] for pick in picks}

    def test_a_new_rating_is_seen_at_once(
        self, client: TestClient, csrf: str, films: Films
    ) -> None:
        """The answer is remembered, but never past a change to the lists."""
        self.rate(client, csrf, films.seed, rating=10)
        before = {pick["id"] for pick in client.get("/api/v1/me/for-you").json()}
        assert films.same_genre in before

        self.rate(client, csrf, films.same_genre, watched=True)
        after = {pick["id"] for pick in client.get("/api/v1/me/for-you").json()}

        assert films.same_genre not in after

    def test_the_limit(self, client: TestClient, csrf: str, films: Films) -> None:
        self.rate(client, csrf, films.seed, rating=10)

        assert len(client.get("/api/v1/me/for-you", params={"limit": 1}).json()) == 1
        assert client.get("/api/v1/me/for-you", params={"limit": 31}).status_code == 422

    def test_never_cached_for_anybody_else(
        self, client: TestClient, csrf: str, films: Films
    ) -> None:
        assert client.get("/api/v1/me/for-you").headers["Cache-Control"] == "no-store"
