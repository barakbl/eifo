"""Merging titles the catalog holds twice.

A merge repoints rows across half the schema and then deletes one of the two,
so the tests are mostly about what must survive it.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from eifo_core.enums import (
    AuthProvider,
    CreditRole,
    ItemStatus,
    OfferType,
    RatingProvider,
    SourceKind,
    TitleKind,
)
from eifo_core.models import (
    AggregateScore,
    Availability,
    Credit,
    ExternalRating,
    MatchReview,
    Person,
    Source,
    Title,
    TmdbAlias,
    User,
    UserItem,
)
from eifo_core.types import utcnow
from eifo_fetcher import dedupe as dedupe_module
from eifo_fetcher.dedupe import (
    apply_merges,
    dangling_references,
    needs_a_human,
    pick_winner,
    plan_merges,
)


def add_title(session: Session, **overrides: Any) -> Title:
    values: dict[str, Any] = {"type": TitleKind.SERIES, "name_he": "פאודה", "year": 2015}
    values.update(overrides)
    title = Title(**values)
    session.add(title)
    session.commit()
    return title


def add_source(session: Session, key: str = "mako") -> Source:
    source = Source(
        key=key, name=key.title(), kind=SourceKind.FREE, website_url=f"https://{key}.example"
    )
    session.add(source)
    session.commit()
    return source


def add_offer(session: Session, title: Title, source: Source, **overrides: Any) -> Availability:
    values: dict[str, Any] = {
        "title_id": title.id,
        "source_id": source.id,
        "offer_type": OfferType.FREE,
    }
    values.update(overrides)
    offer = Availability(**values)
    session.add(offer)
    session.commit()
    return offer


def add_rating(session: Session, title: Title, **overrides: Any) -> ExternalRating:
    values: dict[str, Any] = {
        "title_id": title.id,
        "provider": RatingProvider.IMDB,
        "score_raw": 8.0,
        "score_normalized": 80,
        "fetched_at": utcnow(),
    }
    values.update(overrides)
    rating = ExternalRating(**values)
    session.add(rating)
    session.commit()
    return rating


def add_user(session: Session, subject: str = "1") -> User:
    user = User(
        auth_provider=AuthProvider.GOOGLE,
        auth_subject=subject,
        email=f"{subject}@e.com",
        display_name="צופה",
    )
    session.add(user)
    session.commit()
    return user


def merge_all(session: Session) -> Any:
    return apply_merges(session, plan_merges(session))


def titles(session: Session) -> list[Title]:
    return list(session.scalars(select(Title).order_by(Title.id)).all())


class TestFindingThem:
    def test_two_rows_with_the_same_name_and_year_are_one_title(self, session: Session) -> None:
        add_title(session)
        add_title(session)

        assert len(plan_merges(session)) == 1

    def test_normalisation_decides_sameness(self, session: Session) -> None:
        """The two catalogs punctuate differently and mean the same thing."""
        add_title(session, name_he=None, name_en="Marvel's Daredevil")
        add_title(session, name_he=None, name_en="Marvels Daredevil")

        assert len(plan_merges(session)) == 1

    def test_the_same_name_in_a_different_year_is_a_remake(self, session: Session) -> None:
        add_title(session, name_he=None, name_en="The Office", year=2001)
        add_title(session, name_he=None, name_en="The Office", year=2005)

        assert plan_merges(session) == []

    def test_a_film_and_a_series_are_not_merged_by_a_script(self, session: Session) -> None:
        """Often a real duplicate, often not - which is why somebody has to look."""
        add_title(session, type=TitleKind.MOVIE, name_he="זוהי סדום")
        add_title(session, type=TitleKind.SERIES, name_he="זוהי סדום")

        assert plan_merges(session) == []
        assert needs_a_human(session)["cross_kind"] == 1

    def test_a_year_gap_is_reported_rather_than_merged(self, session: Session) -> None:
        add_title(session, name_he="חטופים", year=2010)
        add_title(session, name_he="חטופים", year=2016)

        assert plan_merges(session) == []
        assert needs_a_human(session)["year_gap"] == 1

    def test_a_title_belongs_to_one_merge_only(self, session: Session) -> None:
        """Three rows of one title are one group, not three overlapping pairs."""
        for _ in range(3):
            add_title(session)

        plans = plan_merges(session)

        assert len(plans) == 1
        assert len(plans[0].losers) == 2


class TestWhichRowSurvives:
    def test_an_imdb_id_wins(self, session: Session) -> None:
        """It is what the ratings pass joins on, and the hardest to get back."""
        plain = add_title(session)
        identified = add_title(session, imdb_id="tt1234567")

        assert pick_winner([plain, identified]) is identified

    def test_then_the_row_that_knows_more(self, session: Session) -> None:
        sparse = add_title(session, tmdb_id=1)
        rich = add_title(session, tmdb_id=2)
        add_rating(session, rich)

        assert pick_winner([sparse, rich]) is rich

    def test_then_the_older_one(self, session: Session) -> None:
        first = add_title(session)
        second = add_title(session)

        assert pick_winner([second, first]) is first


class TestWhatMustSurviveAMerge:
    def test_the_loser_is_gone_and_the_winner_remains(self, session: Session) -> None:
        keeper = add_title(session, imdb_id="tt1")
        add_title(session)

        merge_all(session)

        assert [title.id for title in titles(session)] == [keeper.id]

    def test_offers_from_both_rows_are_kept(self, session: Session) -> None:
        """A split catalog is the whole harm: neither row shows everything."""
        keeper = add_title(session, imdb_id="tt1")
        loser = add_title(session)
        add_offer(session, keeper, add_source(session, "mako"))
        add_offer(session, loser, add_source(session, "kan"))

        merge_all(session)

        offers = session.scalars(select(Availability)).all()
        assert len(offers) == 2
        assert {offer.title_id for offer in offers} == {keeper.id}

    def test_one_offer_from_two_rows_is_folded_into_the_longest_history(
        self, session: Session
    ) -> None:
        keeper = add_title(session, imdb_id="tt1")
        loser = add_title(session)
        source = add_source(session)
        old = utcnow() - dt.timedelta(days=30)
        add_offer(session, keeper, source, first_seen=utcnow(), is_current=False, miss_count=2)
        add_offer(session, loser, source, first_seen=old, is_current=True, miss_count=0)

        merge_all(session)

        offer = session.scalars(select(Availability)).one()
        assert offer.first_seen == old
        assert offer.is_current is True
        assert offer.miss_count == 0

    def test_the_freshest_score_from_each_provider_is_kept(self, session: Session) -> None:
        keeper = add_title(session, imdb_id="tt1")
        loser = add_title(session)
        add_rating(session, keeper, score_raw=7.0, fetched_at=utcnow() - dt.timedelta(days=10))
        add_rating(session, loser, score_raw=9.0, fetched_at=utcnow())

        merge_all(session)

        rating = session.scalars(select(ExternalRating)).one()
        assert rating.score_raw == 9.0
        assert rating.title_id == keeper.id

    def test_somebody_s_list_entry_moves_with_the_title(self, session: Session) -> None:
        """A rating and a private note are the only things here nobody can regenerate."""
        keeper = add_title(session, imdb_id="tt1")
        loser = add_title(session)
        user = add_user(session)
        session.add(
            UserItem(user_id=user.id, title_id=loser.id, status=ItemStatus.WATCHED, rating=9)
        )
        session.commit()

        merge_all(session)

        entry = session.scalars(select(UserItem)).one()
        assert entry.title_id == keeper.id
        assert entry.rating == 9

    def test_the_entry_on_the_surviving_title_wins_when_both_exist(self, session: Session) -> None:
        keeper = add_title(session, imdb_id="tt1")
        loser = add_title(session)
        user = add_user(session)
        session.add(UserItem(user_id=user.id, title_id=keeper.id, rating=10))
        session.add(UserItem(user_id=user.id, title_id=loser.id, rating=3))
        session.commit()

        merge_all(session)

        assert session.scalars(select(UserItem)).one().rating == 10

    def test_credits_are_moved_without_duplicating_them(self, session: Session) -> None:
        keeper = add_title(session, imdb_id="tt1")
        loser = add_title(session)
        person = Person(name_en="Lior Raz")
        session.add(person)
        session.commit()
        session.add(
            Credit(
                title_id=keeper.id,
                person_id=person.id,
                role=CreditRole.CAST,
                character="Doron",
                source="tmdb",
            )
        )
        session.add(
            Credit(
                title_id=loser.id,
                person_id=person.id,
                role=CreditRole.CAST,
                character="Doron",
                source="tmdb",
            )
        )
        session.add(
            Credit(
                title_id=loser.id,
                person_id=person.id,
                role=CreditRole.DIRECTOR,
                character=None,
                source="tmdb",
            )
        )
        session.commit()

        merge_all(session)

        credits = session.scalars(select(Credit)).all()
        assert len(credits) == 2
        assert {credit.title_id for credit in credits} == {keeper.id}

    def test_what_the_winner_did_not_know_it_learns(self, session: Session) -> None:
        add_title(session, imdb_id="tt1", name_en=None, year=None)
        add_title(session, name_en="Fauda", year=2015, runtime_minutes=40)

        merge_all(session)

        survivor = titles(session)[0]
        assert survivor.name_en == "Fauda"
        assert survivor.runtime_minutes == 40

    def test_a_ruling_somebody_made_still_points_somewhere(self, session: Session) -> None:
        keeper = add_title(session, imdb_id="tt1")
        loser = add_title(session)
        session.add(
            MatchReview(
                source_key="mako",
                raw_payload={"name": "פאודה", "kind": "series"},
                candidates={},
                resolved_at=utcnow(),
                resolved_title_id=loser.id,
            )
        )
        session.commit()

        merge_all(session)

        assert session.scalars(select(MatchReview)).one().resolved_title_id == keeper.id

    def test_the_stale_aggregate_is_dropped_rather_than_left_wrong(self, session: Session) -> None:
        """It described a title that has since gained the other row's ratings."""
        keeper = add_title(session, imdb_id="tt1")
        add_title(session)
        session.add(AggregateScore(title_id=keeper.id, score=50, score_israeli=None, components={}))
        session.commit()

        merge_all(session)

        assert session.scalars(select(AggregateScore)).all() == []

    def test_nothing_is_left_pointing_at_the_deleted_row(self, session: Session) -> None:
        add_title(session, imdb_id="tt1")
        loser = add_title(session)
        add_offer(session, loser, add_source(session))
        add_rating(session, loser)

        merge_all(session)

        assert dangling_references(session) == []


class TestTheMergeStaying:
    def test_the_losing_tmdb_id_is_remembered(self, session: Session) -> None:
        """Or the feed offers it again tomorrow and the duplicate comes back."""
        keeper = add_title(session, imdb_id="tt1", tmdb_id=16997)
        add_title(session, tmdb_id=327352)

        merge_all(session)

        alias = session.get(TmdbAlias, 327352)
        assert alias is not None and alias.title_id == keeper.id

    def test_aliases_the_loser_had_come_along_too(self, session: Session) -> None:
        keeper = add_title(session, imdb_id="tt1", tmdb_id=1)
        loser = add_title(session, tmdb_id=2)
        session.add(TmdbAlias(tmdb_id=999, title_id=loser.id))
        session.commit()

        merge_all(session)

        assert session.get(TmdbAlias, 999).title_id == keeper.id


class TestFailureIsPerGroup:
    def test_one_bad_group_does_not_undo_the_others(
        self, session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        add_title(session, name_he="ראשון")
        add_title(session, name_he="ראשון")
        add_title(session, name_he="שני")
        add_title(session, name_he="שני")
        plans = plan_merges(session)
        assert len(plans) == 2

        calls = {"n": 0}
        original = dedupe_module._fill_gaps

        def explode_on_the_second(winner: Title, loser: Title) -> None:
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("something went wrong")
            original(winner, loser)

        monkeypatch.setattr(dedupe_module, "_fill_gaps", explode_on_the_second)

        tally = apply_merges(session, plans)

        assert tally.groups == 1
        assert len(tally.errors) == 1
        assert len(titles(session)) == 3
