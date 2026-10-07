"""Taste from ratings, and what is like what."""

from __future__ import annotations

from typing import Any

import pytest
from factories import make_title, make_user
from sqlalchemy import select
from sqlalchemy.orm import Session

from eifo_core.enums import CreditRole, TitleKind
from eifo_core.models import AggregateScore, Credit, Genre, Person, Title, User, UserItem
from eifo_core.taste import PER_SEED_PICKS, for_you, seed_of, similar, taste


@pytest.fixture
def user(session: Session) -> User:
    user = make_user()
    session.add(user)
    session.commit()
    return user


class World:
    """A handful of films, genres and people, built per test."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.genres: dict[str, Genre] = {}
        self.people: dict[str, Person] = {}

    def genre(self, name: str) -> Genre:
        if name not in self.genres:
            self.genres[name] = Genre(name_en=name, tmdb_id=len(self.genres) + 1)
            self.session.add(self.genres[name])
        return self.genres[name]

    def person(self, name: str) -> Person:
        if name not in self.people:
            self.people[name] = Person(name_en=name)
            self.session.add(self.people[name])
            self.session.flush()
        return self.people[name]

    def film(
        self,
        name: str,
        *,
        year: int = 2000,
        genres: tuple[str, ...] = ("Drama",),
        director: str | None = None,
        cast: tuple[str, ...] = (),
        score: int | None = None,
        kind: TitleKind = TitleKind.MOVIE,
        **extra: Any,
    ) -> Title:
        title = make_title(
            type=kind,
            name_en=name,
            name_he=None,
            year=year,
            genres=[self.genre(g) for g in genres],
            **extra,
        )
        self.session.add(title)
        self.session.flush()
        if director:
            self.session.add(
                Credit(
                    title_id=title.id,
                    person_id=self.person(director).id,
                    role=CreditRole.DIRECTOR,
                    source="tmdb",
                )
            )
        for order, actor in enumerate(cast):
            self.session.add(
                Credit(
                    title_id=title.id,
                    person_id=self.person(actor).id,
                    role=CreditRole.CAST,
                    billing_order=order,
                    source="tmdb",
                )
            )
        if score is not None:
            self.session.add(AggregateScore(title_id=title.id, score=score, components={}))
        self.session.flush()
        return title

    def rate(self, user: User, title: Title, rating: int) -> None:
        self.session.add(UserItem(user_id=user.id, title_id=title.id, rating=rating, watched=True))
        self.session.flush()


@pytest.fixture
def world(session: Session) -> World:
    return World(session)


class TestTaste:
    def test_nothing_rated_is_an_empty_taste(self, session: Session, user: User) -> None:
        found = taste(session, user.id)

        assert found.rated == 0
        assert found.average is None
        assert found.favourites == []

    def test_counts_and_averages(self, session: Session, user: User, world: World) -> None:
        for name, rating in (("A", 9), ("B", 7), ("C", 2)):
            world.rate(user, world.film(name), rating)

        found = taste(session, user.id)

        assert (found.rated, found.average) == (3, 6.0)
        assert found.distribution == {2: 1, 7: 1, 9: 1}
        assert [entry.name for entry in found.favourites] == ["A"]
        assert [entry.name for entry in found.dislikes] == ["C"]

    def test_one_ten_does_not_make_a_favourite_director(
        self, session: Session, user: User, world: World
    ) -> None:
        """The whole point of the prior: two 9s beat a single 10."""
        world.rate(user, world.film("Once", director="One Hit"), 10)
        for name in ("Steady 1", "Steady 2", "Steady 3"):
            world.rate(user, world.film(name, director="Steady Hand"), 9)
        for name in ("Filler 1", "Filler 2", "Filler 3"):
            world.rate(user, world.film(name), 5)

        directors = [leaning.name for leaning in taste(session, user.id).liked["directors"]]

        assert directors == ["Steady Hand", "One Hit"]

    def test_disliked_genres(self, session: Session, user: User, world: World) -> None:
        for index in range(3):
            world.rate(user, world.film(f"Good {index}", genres=("Drama",)), 9)
            world.rate(user, world.film(f"Bad {index}", genres=("Horror",)), 2)

        found = taste(session, user.id)

        assert [leaning.name for leaning in found.liked["genres"]] == ["Drama"]
        assert [leaning.name for leaning in found.disliked["genres"]] == ["Horror"]
        horror = found.disliked["genres"][0]
        assert (horror.titles, horror.average) == (3, 2.0)
        assert horror.weighted > horror.average, "pulled toward the member's mean"

    def test_only_leads_count_as_cast(self, session: Session, user: User, world: World) -> None:
        extras = tuple(f"Extra {n}" for n in range(6))
        for name in ("X", "Y"):
            world.rate(user, world.film(name, cast=(*extras, "Crowd")), 9)
        world.rate(user, world.film("Z"), 3)

        names = {leaning.name for leaning in taste(session, user.id).liked["cast"]}

        assert "Extra 0" in names
        assert "Crowd" not in names

    def test_countries_decades_and_kinds(self, session: Session, user: User, world: World) -> None:
        world.rate(
            user,
            world.film("Israeli", year=1987, origin_countries="IL,FR", original_language="he"),
            10,
        )
        world.rate(user, world.film("Other", year=2015), 4)

        found = taste(session, user.id)

        assert {leaning.key for leaning in found.liked["countries"]} == {"IL", "FR"}
        assert [leaning.name for leaning in found.liked["decades"]] == ["1980s"]
        assert [leaning.key for leaning in found.liked["languages"]] == ["he"]

    def test_against_consensus(self, session: Session, user: User, world: World) -> None:
        world.rate(user, world.film("Kind", score=60), 9)
        world.rate(user, world.film("Harsh", score=80), 6)
        world.rate(user, world.film("Unscored"), 1)

        assert taste(session, user.id).against_consensus == 5.0

    def test_only_this_members_ratings(self, session: Session, user: User, world: World) -> None:
        other = make_user(auth_subject="2", email="other@example.com")
        session.add(other)
        session.flush()
        world.rate(other, world.film("Theirs"), 10)

        assert taste(session, user.id).rated == 0


class TestSimilar:
    def _names(self, session: Session, found: list[Any]) -> list[str]:
        names = dict(session.execute(select(Title.id, Title.name_en)).tuples().all())
        return [names[entry.title_id] for entry in found]

    def test_the_same_director_ranks_above_the_same_genre(
        self, session: Session, world: World
    ) -> None:
        seed = world.film("Seed", genres=("Drama", "Crime"), director="Auteur")
        world.film("Same director", genres=("Drama",), director="Auteur")
        world.film("Same genres", genres=("Drama", "Crime"))
        world.film("Nothing alike", genres=("Comedy",))

        found, total = similar(session, seed_of(session, seed))

        assert self._names(session, found) == ["Same director", "Same genres"]
        assert total == 2
        assert found[0].shared_people == (world.people["Auteur"].id,)

    def test_never_the_seed_itself(self, session: Session, world: World) -> None:
        seed = world.film("Seed")

        found, _ = similar(session, seed_of(session, seed))

        assert seed.id not in {entry.title_id for entry in found}

    def test_a_closer_era_and_a_better_score_break_ties(
        self, session: Session, world: World
    ) -> None:
        seed = world.film("Seed", year=1975)
        world.film("Far", year=2020, score=90)
        world.film("Near", year=1977, score=60)
        world.film("Near and better", year=1977, score=90)

        found, _ = similar(session, seed_of(session, seed))

        assert self._names(session, found) == ["Near and better", "Near", "Far"]

    def test_leads_count_and_the_crowd_does_not(self, session: Session, world: World) -> None:
        crowd = tuple(f"Crowd {n}" for n in range(8))
        seed = world.film("Seed", genres=("Western",), cast=("Star", *crowd))
        world.film("Sequel", genres=("Comedy",), cast=("Star",))
        world.film(
            "Same crowd", genres=("Horror",), cast=(*(f"Other {n}" for n in range(7)), "Crowd 7")
        )

        found, _ = similar(session, seed_of(session, seed))

        assert self._names(session, found) == ["Sequel"]

    def test_within_narrows_the_candidates(self, session: Session, world: World) -> None:
        seed = world.film("Seed")
        world.film("Allowed")
        blocked = world.film("Blocked")

        found, total = similar(
            session, seed_of(session, seed), within=select(Title.id).where(Title.id != blocked.id)
        )

        assert self._names(session, found) == ["Allowed"]
        assert total == 1

    def test_pages(self, session: Session, world: World) -> None:
        seed = world.film("Seed")
        for index in range(5):
            world.film(f"Like {index}", score=90 - index)

        first, total = similar(session, seed_of(session, seed), limit=2)
        second, _ = similar(session, seed_of(session, seed), limit=2, offset=2)

        assert total == 5
        assert self._names(session, first) == ["Like 0", "Like 1"]
        assert self._names(session, second) == ["Like 2", "Like 3"]

    def test_a_title_with_nothing_to_go_on(self, session: Session, world: World) -> None:
        seed = world.film("Bare", genres=())
        world.film("Anything")

        found, total = similar(session, seed_of(session, seed))

        assert (found, total) == ([], 0)


class TestForYou:
    def _names(self, session: Session, picks: list[Any]) -> list[str]:
        names = dict(session.execute(select(Title.id, Title.name_en)).tuples().all())
        return [names[pick.title_id] for pick in picks]

    def test_nothing_rated_highly_is_nothing_to_go_on(
        self, session: Session, user: User, world: World
    ) -> None:
        world.rate(user, world.film("Fine"), 7)
        world.film("Anything")

        assert for_you(session, user.id) == []

    def test_from_a_favourite_with_the_reason(
        self, session: Session, user: User, world: World
    ) -> None:
        loved = world.film("Loved", director="Auteur")
        world.rate(user, loved, 10)
        world.film("By the same hand", director="Auteur")
        world.film("Same genre only")

        picks = for_you(session, user.id)

        assert self._names(session, picks) == ["By the same hand", "Same genre only"]
        assert (picks[0].seed_id, picks[0].seed_rating) == (loved.id, 10)
        assert picks[0].shared_people == (world.people["Auteur"].id,)

    def test_a_loved_favourite_counts_for_more(
        self, session: Session, user: User, world: World
    ) -> None:
        world.rate(user, world.film("Ten", genres=("Western",)), 10)
        world.rate(user, world.film("Eight", genres=("Comedy",)), 8)
        world.film("Like the ten", genres=("Western",))
        world.film("Like the eight", genres=("Comedy",))

        assert self._names(session, for_you(session, user.id)) == ["Like the ten", "Like the eight"]

    def test_never_what_the_member_rated(self, session: Session, user: User, world: World) -> None:
        """Two favourites alike would otherwise recommend each other."""
        first, second = world.film("First"), world.film("Second")
        world.rate(user, first, 10)
        world.rate(user, second, 9)
        world.film("New")

        assert self._names(session, for_you(session, user.id)) == ["New"]

    def test_favourites_agreeing_lift_a_title(
        self, session: Session, user: User, world: World
    ) -> None:
        """Equally good matches for one favourite; only one is liked by both."""
        world.rate(user, world.film("Western love", genres=("Western",)), 9)
        world.rate(user, world.film("Comedy love", genres=("Comedy",)), 9)
        world.film("Western horror", genres=("Western", "Horror"))
        world.film("Western comedy", genres=("Western", "Comedy"))

        picks = for_you(session, user.id)

        assert self._names(session, picks) == ["Western comedy", "Western horror"]

    def test_one_favourite_cannot_fill_the_row(
        self, session: Session, user: User, world: World
    ) -> None:
        world.rate(user, world.film("Franchise", genres=("Action",)), 10)
        world.rate(user, world.film("Other love", genres=("Romance",)), 9)
        for number in range(6):
            world.film(f"Sequel {number}", genres=("Action",), score=90)
        world.film("Something else", genres=("Romance",), score=50)

        picks = for_you(session, user.id)

        sequels = [name for name in self._names(session, picks) if name.startswith("Sequel")]
        assert len(sequels) == PER_SEED_PICKS
        assert "Something else" in self._names(session, picks)

    def test_within_is_the_callers_to_decide(
        self, session: Session, user: User, world: World
    ) -> None:
        world.rate(user, world.film("Loved"), 10)
        world.film("Allowed")
        blocked = world.film("Blocked")

        picks = for_you(session, user.id, within=select(Title.id).where(Title.id != blocked.id))

        assert self._names(session, picks) == ["Allowed"]

    def test_the_limit(self, session: Session, user: User, world: World) -> None:
        for number in range(3):
            world.rate(user, world.film(f"Love {number}", genres=(f"G{number}",)), 10)
            for extra in range(3):
                world.film(f"Like {number}.{extra}", genres=(f"G{number}",))

        assert len(for_you(session, user.id, limit=4)) == 4
