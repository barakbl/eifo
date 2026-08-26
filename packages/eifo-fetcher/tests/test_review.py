"""Working through the items the matcher could not place.

A parked item is not in the catalog at all, so these are mostly about a ruling
putting it there - and about not asking anybody a question whose answer is
already obvious.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from eifo_core.enums import (
    FetchPhase,
    FetchStatus,
    MatchDecision,
    OfferType,
    SourceKind,
    TitleKind,
)
from eifo_core.models import Availability, FetchRun, MatchReview, Source, Title
from eifo_core.types import utcnow
from eifo_fetcher import review
from eifo_fetcher.match import MatchMethod, TitleMatcher
from eifo_fetcher.pipeline import expire_reviews
from eifo_fetcher.sources.base import RawItem


def add_source(session: Session, key: str = "mako") -> Source:
    source = Source(
        key=key, name=key.title(), kind=SourceKind.FREE, website_url=f"https://{key}.example"
    )
    session.add(source)
    session.commit()
    return source


def add_review(session: Session, **payload: Any) -> MatchReview:
    body: dict[str, Any] = {
        "name": "אודטה",
        "year": 2015,
        "kind": TitleKind.SERIES.value,
        "offer_type": OfferType.FREE.value,
        "deep_link_url": "https://mako.example/odeta",
    }
    body.update(payload)
    candidates = body.pop("_candidates", {})
    item = MatchReview(source_key="mako", raw_payload=body, candidates=candidates)
    session.add(item)
    session.commit()
    return item


def add_title(session: Session, **overrides: Any) -> Title:
    values: dict[str, Any] = {"type": TitleKind.SERIES, "name_he": "פאודה", "year": 2015}
    values.update(overrides)
    title = Title(**values)
    session.add(title)
    session.commit()
    return title


class TestARulingTakesEffectNow:
    """Not on the source's next sync, which for one source was never."""

    def test_attaching_puts_the_offer_in_the_catalog(self, session: Session) -> None:
        source = add_source(session)
        title = add_title(session)
        item = add_review(session)

        review.attach(session, item, title)
        session.commit()

        offer = session.scalars(select(Availability)).one()
        assert offer.title_id == title.id
        assert offer.source_id == source.id
        assert offer.deep_link_url == "https://mako.example/odeta"

    def test_attaching_records_what_was_decided(self, session: Session) -> None:
        title = add_title(session)
        item = add_review(session)

        review.attach(session, item, title)

        assert item.decision is MatchDecision.ATTACHED
        assert item.resolved_title_id == title.id
        assert item.resolved_at is not None

    def test_creating_makes_the_title_and_its_offer(self, session: Session) -> None:
        add_source(session)
        item = add_review(session)

        created = review.create(session, item)
        session.commit()

        assert created is not None
        assert created.name_he == "אודטה"
        assert session.scalars(select(Availability)).one().title_id == created.id

    def test_the_offer_type_the_item_carried_is_kept(self, session: Session) -> None:
        """A rental is not a subscription; guessing would misprice the catalog."""
        add_source(session)
        item = add_review(session, offer_type=OfferType.RENT.value)

        review.create(session, item)
        session.commit()

        assert session.scalars(select(Availability)).one().offer_type is OfferType.RENT

    def test_dismissing_creates_nothing(self, session: Session) -> None:
        add_source(session)
        item = add_review(session)

        review.dismiss(session, item)
        session.commit()

        assert item.decision is MatchDecision.DISMISSED
        assert session.scalars(select(Title)).all() == []
        assert session.scalars(select(Availability)).all() == []

    def test_a_ruling_survives_the_source_being_unknown(self, session: Session) -> None:
        """The title still exists; only where to watch it is missing."""
        item = add_review(session)

        created = review.create(session, item)
        session.commit()

        assert created is not None
        assert session.scalars(select(Availability)).all() == []


class TestARulingIsHonouredLater:
    def _item(self) -> RawItem:
        return RawItem(source_key="mako", kind=TitleKind.SERIES, name="אודטה", year=2015)

    def test_a_dismissed_item_never_becomes_a_title(self, session: Session) -> None:
        """The whole point of having the word: a trailer stays out of the catalog."""
        item = add_review(session)
        review.dismiss(session, item)
        session.commit()

        result = TitleMatcher(session).match(self._item())

        assert result.title is None
        assert result.method is MatchMethod.DISMISSED
        assert session.scalars(select(Title)).all() == []

    def test_an_attached_item_resolves_to_its_title(self, session: Session) -> None:
        title = add_title(session, name_he="אודטה")
        item = add_review(session)
        review.attach(session, item, title)
        session.commit()

        result = TitleMatcher(session).match(self._item())

        assert result.title is title


class TestTheAutomaticPass:
    def test_a_listing_the_source_dropped_is_not_worth_asking_about(self, session: Session) -> None:
        add_review(session)
        session.add(
            FetchRun(
                source_key="mako",
                phase=FetchPhase.SYNC,
                started_at=utcnow() + dt.timedelta(minutes=1),
                finished_at=utcnow() + dt.timedelta(minutes=2),
                status=FetchStatus.OK,
                stats={},
            )
        )
        session.commit()

        tally = review.auto_resolve(session, apply=True)

        assert tally.expired == 1
        assert session.scalars(select(MatchReview)).all() == []

    @pytest.mark.parametrize(
        "name",
        [
            "Mufasa The Lion King Sing Along",
            "Assembled The Making Of Doctor Strange",
            "אולימפיאדה סיכום יומי 27.7",
            "פרומו לעונה החדשה",
            "Official Trailer",
        ],
    )
    def test_something_that_is_not_a_title_is_dismissed(self, session: Session, name: str) -> None:
        add_review(session, name=name)

        tally = review.auto_resolve(session, apply=True)

        assert tally.dismissed == 1
        assert session.scalars(select(Title)).all() == []

    def test_a_weak_near_miss_becomes_its_own_title(self, session: Session) -> None:
        """אודטה against פאודה scores 80 and they are unrelated.

        Leaving it parked keeps a real listing out of the catalog for nothing.
        """
        add_source(session)
        add_review(session, _candidates={"closest": {"title_id": 1, "similarity": 80.0}})

        tally = review.auto_resolve(session, apply=True)

        assert tally.created == 1
        assert session.scalars(select(Title)).one().name_he == "אודטה"

    def test_a_strong_near_miss_is_left_for_a_human(self, session: Session) -> None:
        add_review(session, _candidates={"closest": {"title_id": 1, "similarity": 88.0}})

        tally = review.auto_resolve(session, apply=True)

        assert tally.left == 1
        assert tally.created == 0

    def test_agreeing_years_make_a_weak_match_worth_a_look(self, session: Session) -> None:
        add_review(
            session,
            year=2015,
            _candidates={"closest": {"title_id": 1, "similarity": 78.0, "year": 2015}},
        )

        tally = review.auto_resolve(session, apply=True)

        assert tally.left == 1

    def test_nothing_is_written_without_being_asked(self, session: Session) -> None:
        add_review(session, name="Official Trailer")

        tally = review.auto_resolve(session, apply=False)

        assert tally.dismissed == 1
        assert session.scalars(select(MatchReview)).one().resolved_at is None


class TestExpiry:
    def test_a_sync_drops_parks_it_did_not_refresh(self, session: Session) -> None:
        add_review(session)

        removed = expire_reviews(session, "mako", before=utcnow() + dt.timedelta(minutes=1))
        session.commit()

        assert removed == 1
        assert session.scalars(select(MatchReview)).all() == []

    def test_a_park_the_sync_just_wrote_is_kept(self, session: Session) -> None:
        add_review(session)

        removed = expire_reviews(session, "mako", before=utcnow() - dt.timedelta(minutes=1))

        assert removed == 0

    def test_a_ruling_somebody_made_is_never_dropped(self, session: Session) -> None:
        """Their answer has to survive; it is the one thing here nobody can regenerate."""
        item = add_review(session)
        review.dismiss(session, item)
        session.commit()

        removed = expire_reviews(session, "mako", before=utcnow() + dt.timedelta(minutes=1))

        assert removed == 0
        assert session.scalars(select(MatchReview)).one().decision is MatchDecision.DISMISSED


class TestWhatIsNotATitle:
    """The queue's junk filter, which dismisses without asking anybody.

    That makes a false positive expensive: `review auto --apply` records the
    ruling as "not a title, never offer it again", and the listing leaves the
    catalog for good. So the bar is what a word actually tells you.
    """

    @pytest.mark.parametrize(
        "name",
        [
            "Who Said Hannah Montana Music Video",
            "Rotten To The Core Sing Along Descendants",
            "One Last Adventure: The Making of Stranger Things 5",
            "Behind the Scenes of Dune",
            "Fauda Featurette",
        ],
    )
    def test_a_phrase_that_is_only_ever_bonus_content(self, name: str) -> None:
        assert review.not_a_title(name)

    @pytest.mark.parametrize(
        "name",
        [
            "Trailer",
            "The Batman Trailer",
            "Trailer: The Batman",
            "Teaser - Dune",
            "Teaser \u2013 Dune",  # an en dash, which titles use and a linter mistrusts
        ],
    )
    def test_an_ordinary_word_counts_where_it_sits(self, name: str) -> None:
        """The whole name, the end of it, or the start followed by a separator."""
        assert review.not_a_title(name)

    @pytest.mark.parametrize(
        "name",
        [
            "Trailer Park Boys",
            "Young Farts Trailer Parts",
            "Trailer Horn",
            "LEGO Marvel Super Heroes: Avengers Reassembled!",
            "The Heat Is Back On: The Remaking of Miss Saigon",
        ],
    )
    def test_and_not_where_it_is_merely_present(self, name: str) -> None:
        """Every one of these is a real title the previous rule threw away.

        Trailer Park Boys ran for twelve seasons. "Reassembled" is not
        "assembled", and "Remaking" is not "making of" - the old pattern had no
        word boundaries at all.
        """
        assert not review.not_a_title(name)

    @pytest.mark.parametrize(
        "name",
        ["קדימון לעונה 2", "סיכום יומי", "קליפ חנוכה - הילה הכל יכולה", "פסטיגל- הקליפ"],
    )
    def test_hebrew_markers_do_not_wait_for_punctuation(self, name: str) -> None:
        """Hebrew does not separate a label from a name the way English does,
        and writes the definite article as part of the word - הקליפ."""
        assert review.not_a_title(name)

    @pytest.mark.parametrize("name", ["פאודה", "שוברים שורה", "Foxtrot", "", "   "])
    def test_a_real_name_is_left_alone(self, name: str) -> None:
        assert not review.not_a_title(name)
