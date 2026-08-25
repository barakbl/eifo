"""Recording that a title is offered somewhere.

The write the nightly sync makes thousands of times and the review queue makes
one at a time. What matters about it is that seeing an offer again is not a new
offer: it revives a retired row, clears its strikes, and leaves the date it was
first seen alone.
"""

from __future__ import annotations

import datetime as dt

from factories import make_source, make_title
from sqlalchemy import select
from sqlalchemy.orm import Session

from eifo_core.enums import OfferType
from eifo_core.models import Availability
from eifo_core.offers import Offer, record_offer

MONDAY = dt.datetime(2026, 8, 3, 3, 0, tzinfo=dt.UTC)
TUESDAY = dt.datetime(2026, 8, 4, 3, 0, tzinfo=dt.UTC)


def _pair(session: Session) -> tuple[object, object]:
    title, source = make_title(), make_source()
    session.add_all([title, source])
    session.flush()
    return title, source


def _stored(session: Session) -> Availability:
    return session.scalars(select(Availability)).one()


class TestFirstSighting:
    def test_it_creates_the_row_and_says_so(self, session: Session) -> None:
        title, source = _pair(session)

        created = record_offer(session, title=title, source=source, offer=Offer(), seen_at=MONDAY)
        session.flush()

        assert created is True
        stored = _stored(session)
        assert stored.is_current is True
        assert stored.first_seen == MONDAY
        assert stored.last_seen == MONDAY

    def test_it_keeps_the_price_and_the_link(self, session: Session) -> None:
        title, source = _pair(session)

        record_offer(
            session,
            title=title,
            source=source,
            offer=Offer(
                offer_type=OfferType.RENT,
                deep_link_url="https://example.test/watch",
                price_minor=1990,
                price_currency="ILS",
            ),
            seen_at=MONDAY,
        )
        session.flush()

        stored = _stored(session)
        assert stored.offer_type is OfferType.RENT
        assert (stored.price_minor, stored.price_currency) == (1990, "ILS")
        assert stored.deep_link_url == "https://example.test/watch"


class TestSeeingItAgain:
    def test_it_updates_rather_than_duplicates(self, session: Session) -> None:
        title, source = _pair(session)
        record_offer(session, title=title, source=source, offer=Offer(), seen_at=MONDAY)
        session.flush()

        created = record_offer(session, title=title, source=source, offer=Offer(), seen_at=TUESDAY)
        session.flush()

        assert created is False
        stored = _stored(session)
        assert stored.last_seen == TUESDAY
        # The day it appeared does not move because it was seen again.
        assert stored.first_seen == MONDAY

    def test_it_revives_a_row_that_had_been_retired(self, session: Session) -> None:
        """A title that comes back is the same offer returning, not a new one."""
        title, source = _pair(session)
        record_offer(session, title=title, source=source, offer=Offer(), seen_at=MONDAY)
        session.flush()
        stored = _stored(session)
        stored.is_current = False
        stored.miss_count = 2
        stored.gone_since = MONDAY

        record_offer(session, title=title, source=source, offer=Offer(), seen_at=TUESDAY)
        session.flush()

        stored = _stored(session)
        assert stored.is_current is True
        assert stored.miss_count == 0
        assert stored.gone_since is None

    def test_a_source_that_stopped_quoting_a_price_keeps_the_last_one(
        self, session: Session
    ) -> None:
        """Better a stale figure than showing a paid title as free."""
        title, source = _pair(session)
        record_offer(
            session,
            title=title,
            source=source,
            offer=Offer(price_minor=1990, price_currency="ILS"),
            seen_at=MONDAY,
        )
        session.flush()

        record_offer(session, title=title, source=source, offer=Offer(), seen_at=TUESDAY)
        session.flush()

        assert _stored(session).price_minor == 1990

    def test_a_price_that_moved_is_news(self, session: Session) -> None:
        title, source = _pair(session)
        record_offer(
            session,
            title=title,
            source=source,
            offer=Offer(price_minor=1990, price_currency="ILS"),
            seen_at=MONDAY,
        )
        session.flush()

        record_offer(
            session,
            title=title,
            source=source,
            offer=Offer(price_minor=2490, price_currency="ILS"),
            seen_at=TUESDAY,
        )
        session.flush()

        assert _stored(session).price_minor == 2490


class TestTheWriteCache:
    def test_a_source_listing_the_same_title_twice_writes_one_row(self, session: Session) -> None:
        """A pending insert is invisible to a SELECT, so the cache is what stops it."""
        title, source = _pair(session)
        written: dict[tuple[int, int, OfferType], Availability] = {}

        first = record_offer(
            session, title=title, source=source, offer=Offer(), seen_at=MONDAY, written=written
        )
        second = record_offer(
            session, title=title, source=source, offer=Offer(), seen_at=MONDAY, written=written
        )
        session.flush()

        assert (first, second) == (True, False)
        assert len(session.scalars(select(Availability)).all()) == 1

    def test_different_offer_types_are_different_offers(self, session: Session) -> None:
        """Renting and streaming the same title are two things a viewer can do."""
        title, source = _pair(session)

        record_offer(
            session,
            title=title,
            source=source,
            offer=Offer(offer_type=OfferType.STREAM),
            seen_at=MONDAY,
        )
        record_offer(
            session,
            title=title,
            source=source,
            offer=Offer(offer_type=OfferType.RENT),
            seen_at=MONDAY,
        )
        session.flush()

        assert len(session.scalars(select(Availability)).all()) == 2
