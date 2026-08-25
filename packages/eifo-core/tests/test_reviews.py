"""Ruling on a listing the matcher could not place.

These live in ``eifo-core`` because two things now make these rulings - the
``eifo-fetch review`` CLI and a person in the Manage tab - and the API cannot
call the fetcher. So what is tested here is the ruling itself: what it writes,
and that it writes it now rather than leaving it for the source's next sync.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from factories import make_source, make_title
from sqlalchemy import select
from sqlalchemy.orm import Session

from eifo_core import reviews
from eifo_core.enums import MatchDecision, OfferType, TitleKind
from eifo_core.models import Availability, MatchReview, Title

MARCH = dt.datetime(2026, 3, 1, 9, 0, tzinfo=dt.UTC)
AUGUST = dt.datetime(2026, 8, 1, 9, 0, tzinfo=dt.UTC)


def park(
    session: Session,
    *,
    source_key: str = "cellcom_tv",
    name: str = "סרוגים 2",
    created_at: dt.datetime = MARCH,
    closest: dict[str, Any] | None = None,
    **payload: Any,
) -> MatchReview:
    """A listing sitting in the queue, as the matcher writes one."""
    review = MatchReview(
        source_key=source_key,
        raw_payload={"name": name, "kind": TitleKind.SERIES.value, "year": 2008, **payload},
        candidates={"closest": closest} if closest else {},
        created_at=created_at,
    )
    session.add(review)
    session.flush()
    return review


class TestReadingTheQueue:
    def test_only_what_is_still_waiting(self, session: Session) -> None:
        park(session)
        ruled = park(session, name="כבר טופל")
        reviews.dismiss(session, ruled)
        session.flush()

        assert [item.id for item in reviews.pending(session)] != []
        assert ruled.id not in {item.id for item in reviews.pending(session)}

    def test_oldest_first_by_default(self, session: Session) -> None:
        """A listing parked in March has been missing from the catalog since March."""
        newer = park(session, name="חדש", created_at=AUGUST)
        older = park(session, name="ישן", created_at=MARCH)

        assert [item.id for item in reviews.pending(session)] == [older.id, newer.id]

    def test_closest_first_when_asked(self, session: Session) -> None:
        """SQLite compares JSON as text, so 9 would beat 80 without the cast."""
        weak = park(session, name="חלש", closest={"title_id": 1, "similarity": 9})
        strong = park(session, name="חזק", closest={"title_id": 2, "similarity": 80})

        ordered = reviews.pending(session, order=reviews.ReviewOrder.SIMILARITY)

        assert [item.id for item in ordered] == [strong.id, weak.id]

    def test_a_listing_with_no_suggestion_sorts_last(self, session: Session) -> None:
        bare = park(session, name="בלי הצעה")
        scored = park(session, name="עם הצעה", closest={"title_id": 1, "similarity": 40})

        ordered = reviews.pending(session, order=reviews.ReviewOrder.SIMILARITY)

        assert [item.id for item in ordered] == [scored.id, bare.id]

    def test_it_can_be_narrowed_to_one_source(self, session: Session) -> None:
        park(session, source_key="mako")
        park(session, source_key="cellcom_tv")

        assert len(reviews.pending(session, source_key="mako")) == 1
        assert reviews.pending_count(session, source_key="mako") == 1

    def test_counts_per_source_drive_the_filter_chips(self, session: Session) -> None:
        park(session, source_key="mako")
        park(session, source_key="mako", name="עוד אחד")
        park(session, source_key="cellcom_tv")

        assert reviews.pending_by_source(session) == {"mako": 2, "cellcom_tv": 1}

    def test_a_page_of_the_queue(self, session: Session) -> None:
        for index in range(5):
            park(session, name=f"פריט {index}", created_at=MARCH + dt.timedelta(days=index))

        page = reviews.pending(session, limit=2, offset=2)

        assert [item.raw_payload["name"] for item in page] == ["פריט 2", "פריט 3"]


class TestAttach:
    def test_it_gives_the_offer_to_that_title_now(self, session: Session) -> None:
        """Now, not at the source's next sync - which is the whole point."""
        source = make_source()
        title = make_title()
        session.add_all([source, title])
        session.flush()
        review = park(session, deep_link_url="https://cellcom.test/watch/9")

        reviews.attach(session, review, title)
        session.flush()

        assert review.decision is MatchDecision.ATTACHED
        assert review.resolved_title_id == title.id
        assert review.resolved_at is not None

        offer = session.scalars(select(Availability)).one()
        assert offer.title_id == title.id
        assert offer.source_id == source.id
        assert offer.deep_link_url == "https://cellcom.test/watch/9"
        assert offer.is_current is True

    def test_the_offer_type_the_listing_carried_is_kept(self, session: Session) -> None:
        session.add_all([make_source(), make_title()])
        session.flush()
        title = session.scalars(select(Title)).one()
        review = park(session, offer_type=OfferType.RENT.value)

        reviews.attach(session, review, title)
        session.flush()

        assert session.scalars(select(Availability)).one().offer_type is OfferType.RENT

    def test_a_source_that_is_gone_is_reported_rather_than_guessed_at(
        self, session: Session
    ) -> None:
        session.add(make_title())
        session.flush()
        title = session.scalars(select(Title)).one()
        review = park(session, source_key="a_source_nobody_has")

        with pytest.raises(reviews.UnknownSourceError):
            reviews.attach(session, review, title)


class TestCreate:
    def test_it_makes_a_title_and_gives_it_the_offer(self, session: Session) -> None:
        session.add(make_source())
        session.flush()
        review = park(session, name="סרוגים 2", name_alt="Srugim 2")

        title = reviews.create(session, review)
        session.flush()

        assert title.name_he == "סרוגים 2"
        assert title.name_en == "Srugim 2"
        assert title.year == 2008
        assert title.type is TitleKind.SERIES
        assert review.decision is MatchDecision.CREATED
        assert review.resolved_title_id == title.id
        assert session.scalars(select(Availability)).one().title_id == title.id

    def test_names_go_in_the_column_their_script_says(self, session: Session) -> None:
        session.add(make_source())
        session.flush()
        review = park(session, name="Srugim 2", name_alt="סרוגים 2")

        title = reviews.create(session, review)

        assert (title.name_he, title.name_en) == ("סרוגים 2", "Srugim 2")

    def test_a_third_script_still_gets_a_name(self, session: Session) -> None:
        """A row must carry one, and the enricher replaces it on its first visit."""
        session.add(make_source())
        session.flush()
        review = park(session, name="千と千尋の神隠し")

        title = reviews.create(session, review)

        assert title.name_he is None
        assert title.name_en == "千と千尋の神隠し"

    def test_external_ids_come_across(self, session: Session) -> None:
        session.add(make_source())
        session.flush()
        review = park(session, tmdb_id=1396, imdb_id="tt0903747")

        title = reviews.create(session, review)

        assert (title.tmdb_id, title.imdb_id) == (1396, "tt0903747")

    def test_an_empty_imdb_id_is_stored_as_nothing(self, session: Session) -> None:
        """A unique index treats "" as a value, so two of them would collide."""
        session.add(make_source())
        session.flush()
        review = park(session, imdb_id="")

        assert reviews.create(session, review).imdb_id is None


class TestDismiss:
    def test_it_records_the_ruling_and_creates_nothing(self, session: Session) -> None:
        review = park(session, name="פרומו לעונה 2")

        reviews.dismiss(session, review)
        session.flush()

        assert review.decision is MatchDecision.DISMISSED
        assert review.resolved_title_id is None
        assert review.resolved_at is not None
        assert session.scalars(select(Title)).all() == []
        assert session.scalars(select(Availability)).all() == []

    def test_it_needs_no_source(self, session: Session) -> None:
        """Nothing is being offered, so there is nowhere for it to play."""
        review = park(session, source_key="a_source_nobody_has")

        reviews.dismiss(session, review)

        assert review.decision is MatchDecision.DISMISSED


class TestReadingAParkedPayload:
    def test_a_kind_it_does_not_recognise_is_treated_as_a_film(self, session: Session) -> None:
        review = park(session, kind="podcast")

        assert reviews.kind_of(review) is TitleKind.MOVIE

    def test_an_offer_type_it_does_not_recognise_is_treated_as_streaming(
        self, session: Session
    ) -> None:
        review = park(session, offer_type="barter")

        assert reviews.offer_of(review).offer_type is OfferType.STREAM

    def test_a_missing_offer_type_is_streaming(self, session: Session) -> None:
        assert reviews.offer_of(park(session)).offer_type is OfferType.STREAM

    def test_the_suggestion_is_read_when_there_is_one(self, session: Session) -> None:
        review = park(session, closest={"title_id": 7, "similarity": 82.5})

        assert reviews.closest_candidate(review) == {"title_id": 7, "similarity": 82.5}

    def test_and_is_nothing_when_there_is_not(self, session: Session) -> None:
        assert reviews.closest_candidate(park(session)) is None
