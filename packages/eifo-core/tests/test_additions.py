"""Films members add by hand, and how they stop being only that."""

from __future__ import annotations

import datetime as dt
from typing import Any, ClassVar

import pytest
from factories import make_source, make_title, make_user
from sqlalchemy import select
from sqlalchemy.orm import Session

from eifo_core.additions import (
    DAILY_LIMIT,
    DailyLimitReachedError,
    MovieRecord,
    add_to_list,
    added_recently,
    find_existing,
    is_user_added,
    movie_from_tmdb,
    removable,
    user_added_ids,
)
from eifo_core.catalog import upsert_availability
from eifo_core.enums import ItemStatus, OfferType, TitleKind
from eifo_core.items import RawItem, TmdbTitle
from eifo_core.match import TitleMatcher
from eifo_core.models import Availability, Title, TmdbAlias, User, UserItem
from eifo_core.types import utcnow


def record(**overrides: Any) -> MovieRecord:
    values: dict[str, Any] = {
        "tmdb_id": 603,
        "imdb_id": "tt0133093",
        "name_he": "מטריקס",
        "name_en": "The Matrix",
        "year": 1999,
        "overview_he": None,
        "overview_en": "A hacker learns the truth.",
        "poster_path": "/matrix.jpg",
        "runtime_minutes": 136,
        "original_language": "en",
    }
    values.update(overrides)
    return MovieRecord(**values)


@pytest.fixture
def user(session: Session) -> User:
    user = make_user()
    session.add(user)
    session.commit()
    return user


class TestReadingTmdb:
    HEBREW: ClassVar[dict[str, Any]] = {
        "id": 603,
        "title": "מטריקס",
        "overview": "האקר מגלה את האמת.",
        "release_date": "1999-03-30",
        "poster_path": "/he.jpg",
    }
    ENGLISH: ClassVar[dict[str, Any]] = {
        "id": 603,
        "title": "The Matrix",
        "original_title": "The Matrix",
        "overview": "A hacker learns the truth.",
        "release_date": "1999-03-30",
        "poster_path": "/en.jpg",
        "imdb_id": "tt0133093",
        "runtime": 136,
        "original_language": "en",
    }

    def test_reads_both_languages(self) -> None:
        movie = movie_from_tmdb(self.HEBREW, self.ENGLISH)

        assert movie == MovieRecord(
            tmdb_id=603,
            imdb_id="tt0133093",
            name_he="מטריקס",
            name_en="The Matrix",
            year=1999,
            overview_he="האקר מגלה את האמת.",
            overview_en="A hacker learns the truth.",
            poster_path="/en.jpg",
            runtime_minutes=136,
            original_language="en",
        )
        assert movie.poster_source_url == "https://image.tmdb.org/t/p/w500/en.jpg"

    def test_an_untranslated_title_is_not_taken_for_hebrew(self) -> None:
        """Asked for Hebrew, TMDB answers in English when it has nothing else."""
        hebrew = self.HEBREW | {"title": "The Matrix", "overview": "A hacker learns the truth."}

        movie = movie_from_tmdb(hebrew, self.ENGLISH)

        assert movie is not None
        assert movie.name_he is None
        assert movie.overview_he is None
        assert movie.name_en == "The Matrix"

    def test_a_name_in_a_third_script_is_kept(self) -> None:
        english = self.ENGLISH | {"title": "千と千尋の神隠し", "original_title": "千と千尋の神隠し"}

        movie = movie_from_tmdb({"id": 129}, english | {"id": 129})

        assert movie is not None
        assert movie.name_en == "千と千尋の神隠し"

    @pytest.mark.parametrize(
        "poster",
        [
            "https://evil.example/x.jpg",
            "//evil.example/x.jpg",
            "/../../etc/passwd",
            "/x.jpg?u=http://evil",
            42,
        ],
    )
    def test_a_poster_must_be_a_tmdb_path(self, poster: object) -> None:
        """The image pipeline fetches what this becomes; it may only ever name TMDB."""
        movie = movie_from_tmdb({}, self.ENGLISH | {"poster_path": poster})

        assert movie is not None
        assert movie.poster_path is None
        assert movie.poster_source_url is None

    @pytest.mark.parametrize("imdb", ["nm0000206", "tt", "tt12ab", "x" * 30])
    def test_an_imdb_id_must_look_like_one(self, imdb: str) -> None:
        movie = movie_from_tmdb({}, self.ENGLISH | {"imdb_id": imdb})

        assert movie is not None
        assert movie.imdb_id is None

    def test_a_placeholder_year_is_dropped(self) -> None:
        movie = movie_from_tmdb({}, self.ENGLISH | {"release_date": "2999-01-01"})

        assert movie is not None
        assert movie.year is None

    @pytest.mark.parametrize(
        "english", [{}, {"id": "603", "title": "x"}, {"id": 0, "title": "x"}, {"id": 603}]
    )
    def test_refuses_what_is_not_a_film(self, english: dict[str, Any]) -> None:
        assert movie_from_tmdb({}, english) is None


class TestAdding:
    def test_creates_a_title_and_marks_it_watched(self, session: Session, user: User) -> None:
        added = add_to_list(session, user, record(), rating=8)

        assert added.created
        title = session.get(Title, added.title.id)
        assert title is not None
        assert (title.type, title.tmdb_id, title.imdb_id) == (TitleKind.MOVIE, 603, "tt0133093")
        assert title.added_by_user_id == user.id
        assert title.added_at is not None
        assert title.poster_source_url == "https://image.tmdb.org/t/p/w500/matrix.jpg"
        assert (added.item.watched, added.item.rating) == (True, 8)
        assert is_user_added(session, title.id)

    def test_can_go_on_the_want_to_watch_list_instead(self, session: Session, user: User) -> None:
        added = add_to_list(session, user, record(), onto=ItemStatus.WANT_TO_WATCH)

        assert added.created
        assert (added.item.want_to_watch, added.item.watched) == (True, False)
        assert is_user_added(session, added.title.id)

    def test_the_other_list_is_left_alone(self, session: Session, user: User) -> None:
        add_to_list(session, user, record(), onto=ItemStatus.WANT_TO_WATCH)
        item = add_to_list(session, user, record()).item

        assert (item.want_to_watch, item.watched) == (True, True)

    def test_a_film_already_in_the_catalog_is_only_marked_watched(
        self, session: Session, user: User
    ) -> None:
        existing = make_title(type=TitleKind.MOVIE, tmdb_id=603, name_en="The Matrix")
        session.add(existing)
        session.commit()

        added = add_to_list(session, user, record())

        assert not added.created
        assert added.title.id == existing.id
        assert existing.added_at is None
        assert session.scalar(select(Title.id).where(Title.id != existing.id)) is None

    def test_found_through_a_known_duplicate_tmdb_record(
        self, session: Session, user: User
    ) -> None:
        existing = make_title(type=TitleKind.MOVIE, tmdb_id=9999, name_en="The Matrix")
        session.add(existing)
        session.flush()
        session.add(TmdbAlias(type=TitleKind.MOVIE, tmdb_id=603, title_id=existing.id))
        session.commit()

        assert find_existing(session, 603, None) is existing
        assert not add_to_list(session, user, record()).created

    def test_found_by_imdb_id_when_the_catalog_has_no_tmdb_id(
        self, session: Session, user: User
    ) -> None:
        existing = make_title(type=TitleKind.MOVIE, imdb_id="tt0133093", name_en="Matrix")
        session.add(existing)
        session.commit()

        assert add_to_list(session, user, record()).title.id == existing.id

    def test_a_series_with_the_same_number_is_a_different_work(
        self, session: Session, user: User
    ) -> None:
        """TMDB numbers films and series separately."""
        session.add(make_title(type=TitleKind.SERIES, tmdb_id=603))
        session.commit()

        assert add_to_list(session, user, record(imdb_id=None)).created

    def test_adding_twice_is_harmless(self, session: Session, user: User) -> None:
        first = add_to_list(session, user, record(), rating=7)
        second = add_to_list(session, user, record())

        assert second.title.id == first.title.id
        assert not second.created
        # No rating given the second time is not a request to clear the first.
        assert second.item.rating == 7
        assert session.scalars(select(UserItem)).all() == [second.item]

    def test_keeps_what_the_member_already_said(self, session: Session, user: User) -> None:
        existing = make_title(type=TitleKind.MOVIE, tmdb_id=603)
        session.add(existing)
        session.flush()
        session.add(
            UserItem(user_id=user.id, title_id=existing.id, want_to_watch=True, note="ראיתי")
        )
        session.commit()

        item = add_to_list(session, user, record()).item

        assert (item.want_to_watch, item.watched, item.note) == (True, True, "ראיתי")

    def test_losing_the_race_takes_the_winners_title(
        self, session: Session, user: User, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Both requests looked, both saw nothing, both insert; one wins."""
        winner = make_title(type=TitleKind.MOVIE, tmdb_id=603, name_en="The Matrix")
        session.add(winner)
        session.commit()

        import eifo_core.additions as additions

        real = additions.find_existing
        calls = iter([None])
        monkeypatch.setattr(
            additions,
            "find_existing",
            lambda *args: next(calls, None) or real(*args),
        )

        added = add_to_list(session, user, record())

        assert not added.created
        assert added.title.id == winner.id
        assert session.scalar(select(UserItem.title_id)) == winner.id


class TestDailyLimit:
    def test_the_limit_refuses_a_new_title(self, session: Session, user: User) -> None:
        for number in range(DAILY_LIMIT):
            add_to_list(session, user, record(tmdb_id=1000 + number, imdb_id=None))

        with pytest.raises(DailyLimitReachedError):
            add_to_list(session, user, record(tmdb_id=5000, imdb_id=None))
        assert session.scalar(select(Title).where(Title.tmdb_id == 5000)) is None

    def test_the_limit_never_refuses_a_film_the_catalog_has(
        self, session: Session, user: User
    ) -> None:
        session.add(make_title(type=TitleKind.MOVIE, tmdb_id=603))
        for number in range(DAILY_LIMIT):
            add_to_list(session, user, record(tmdb_id=1000 + number, imdb_id=None))

        assert not add_to_list(session, user, record()).created

    def test_the_window_rolls(self, session: Session, user: User) -> None:
        add_to_list(session, user, record())

        assert added_recently(session, user) == 1
        assert added_recently(session, user, now=utcnow() + dt.timedelta(days=1, seconds=1)) == 0


class TestBecomingAnOrdinaryTitle:
    def test_a_sync_listing_finds_it_and_ends_its_addition(
        self, session: Session, user: User
    ) -> None:
        """The whole point: tonight's sync lands on this title, not beside it."""
        added = add_to_list(session, user, record(), rating=9)
        netflix = make_source(key="netflix_il", name="Netflix")
        session.add(netflix)
        session.flush()

        listing = RawItem(
            source_key="netflix_il",
            kind=TitleKind.MOVIE,
            name="The Matrix",
            year=1999,
            offer_type=OfferType.STREAM,
        )
        hit = TmdbTitle(
            tmdb_id=603,
            kind=TitleKind.MOVIE,
            name="The Matrix",
            original_name="The Matrix",
            year=1999,
            overview=None,
            poster_path=None,
        )

        class Tmdb:
            def search(self, *_: object, **__: object) -> list[TmdbTitle]:
                return [hit]

        result = TitleMatcher(session, tmdb=Tmdb()).match(listing)
        assert result.title is not None
        assert result.title.id == added.title.id

        upsert_availability(
            session, title=result.title, source=netflix, item=listing, seen_at=utcnow()
        )
        session.commit()

        assert not is_user_added(session, added.title.id)
        assert not removable(session, added.title)
        item = session.scalar(select(UserItem))
        assert item is not None
        assert item.rating == 9
        # The history stays: who added it, and when.
        assert added.title.added_by_user_id == user.id

    def test_a_listing_carrying_the_tmdb_id_finds_it(self, session: Session, user: User) -> None:
        added = add_to_list(session, user, record())

        listing = RawItem(source_key="netflix_il", kind=TitleKind.MOVIE, name="Matrix", tmdb_id=603)
        result = TitleMatcher(session).match(listing)

        assert result.title is not None
        assert result.title.id == added.title.id

    def test_a_lapsed_listing_still_counts(self, session: Session, user: User) -> None:
        """Left Netflix is "no longer available", not "only a member vouches for it"."""
        added = add_to_list(session, user, record())
        netflix = make_source()
        session.add(netflix)
        session.flush()
        session.add(
            Availability(
                title_id=added.title.id,
                source_id=netflix.id,
                offer_type=OfferType.STREAM,
                is_current=False,
            )
        )
        session.commit()

        assert not is_user_added(session, added.title.id)

    def test_a_title_no_member_added_is_never_user_added(self, session: Session) -> None:
        """The catalog has titles on nothing that nobody added: merges, reviews."""
        orphan = make_title(type=TitleKind.MOVIE)
        session.add(orphan)
        session.commit()

        assert not is_user_added(session, orphan.id)
        assert list(session.scalars(user_added_ids())) == []


class TestTheMemberLeaves:
    def test_the_film_and_its_mark_outlive_the_account(self, session: Session, user: User) -> None:
        added = add_to_list(session, user, record())
        title_id = added.title.id

        session.delete(user)
        session.commit()
        session.expire_all()

        title = session.get(Title, title_id)
        assert title is not None
        assert title.added_by_user_id is None
        assert is_user_added(session, title_id)
