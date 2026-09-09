"""Writing down what an enricher found: the queue, the patch rules, the scores.

An enricher is a pure reader - it is handed a snapshot and returns what a
ratings site says - and everything about what that means for the catalog is
here. It is called from the API on every ordinary run, so these are direct
tests of the deciding rather than of the fetching that leads to it.

Two rules run through all of it and are the reason most of these exist:

* **Metadata fills gaps only.** A scraped guess must not displace TMDB's
  canonical answer. The one exception is a name in the wrong script, which is
  not a name we have but one we mislabelled.
* **The queue has to advance.** A title is recorded as attempted even when
  nobody could rate it, or a run comes back to exactly the same titles tomorrow
  and the catalog never moves.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from factories import make_source, make_title
from sqlalchemy import select
from sqlalchemy.orm import Session

from eifo_core.enriching import (
    apply_offer_facts,
    apply_patch,
    mislabelled_names,
    outcome_of,
    recompute,
    recompute_all_aggregates,
    record_attempt,
    store_ratings,
    titles_due,
    view_of,
)
from eifo_core.enums import EnrichOutcome, OfferType, RatingProvider, TitleKind
from eifo_core.findings import EnrichResult, OfferFact, Rating, TitleView
from eifo_core.models import (
    AggregateScore,
    Availability,
    EnrichAttempt,
    ExternalRating,
    Genre,
    Source,
    Title,
)
from eifo_core.settings import Settings
from eifo_core.types import utcnow


def refuse(_message: str, _exc: Exception) -> None:
    """A rejection sink that keeps nothing; most tests only care that it stored."""


class Refusals:
    """A rejection sink that remembers, for the tests that are about refusing."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    def __call__(self, message: str, exc: Exception) -> None:
        self.seen.append(f"{message}: {exc}")


def add_title(session: Session, **overrides: Any) -> Title:
    title = make_title(**overrides)
    session.add(title)
    session.commit()
    return title


def add_rating(session: Session, title: Title, **overrides: Any) -> ExternalRating:
    values: dict[str, Any] = {
        "title_id": title.id,
        "provider": RatingProvider.IMDB,
        "score_raw": 8.0,
        "score_normalized": 80,
    }
    values.update(overrides)
    rating = ExternalRating(**values)
    session.add(rating)
    session.commit()
    return rating


def attempted(session: Session, title: Title, **overrides: Any) -> EnrichAttempt:
    values: dict[str, Any] = {
        "title_id": title.id,
        "attempted_at": utcnow(),
        "outcome": EnrichOutcome.OK,
        "fruitless": 0,
        "due_at": utcnow() + dt.timedelta(days=14),
    }
    values.update(overrides)
    attempt = EnrichAttempt(**values)
    session.add(attempt)
    session.commit()
    return attempt


class TestTheQueue:
    """Least recently attempted first, so a run advances instead of re-reading
    the head of the catalog."""

    def test_a_title_nobody_has_tried_is_due(self, session: Session, settings: Settings) -> None:
        add_title(session)

        assert len(titles_due(session, settings)) == 1

    def test_one_that_is_not_due_yet_is_left_alone(
        self, session: Session, settings: Settings
    ) -> None:
        attempted(session, add_title(session))

        assert titles_due(session, settings) == []

    def test_one_whose_wait_has_passed_comes_back(
        self, session: Session, settings: Settings
    ) -> None:
        attempted(session, add_title(session), due_at=utcnow() - dt.timedelta(minutes=1))

        assert len(titles_due(session, settings)) == 1

    def test_never_attempted_sorts_ahead_of_long_ago(
        self, session: Session, settings: Settings
    ) -> None:
        """Spelled out in the query rather than left to the dialect, which may
        sort NULLs either way."""
        old = add_title(session, name_he="ישן")
        attempted(
            session,
            old,
            attempted_at=utcnow() - dt.timedelta(days=90),
            due_at=utcnow() - dt.timedelta(days=1),
        )
        add_title(session, name_he="חדש")

        assert [title.name_he for title in titles_due(session, settings)] == ["חדש", "ישן"]

    def test_the_longest_unattempted_comes_first(
        self, session: Session, settings: Settings
    ) -> None:
        recent = add_title(session, name_he="לאחרונה")
        older = add_title(session, name_he="מזמן")
        attempted(
            session,
            recent,
            attempted_at=utcnow() - dt.timedelta(days=1),
            due_at=utcnow() - dt.timedelta(minutes=1),
        )
        attempted(
            session,
            older,
            attempted_at=utcnow() - dt.timedelta(days=30),
            due_at=utcnow() - dt.timedelta(minutes=1),
        )

        assert [title.name_he for title in titles_due(session, settings)] == ["מזמן", "לאחרונה"]

    def test_the_batch_is_bounded(self, session: Session, settings: Settings) -> None:
        for index in range(5):
            add_title(session, name_he=f"סרט {index}")

        assert len(titles_due(session, settings, limit=2)) == 2

    def test_force_ignores_the_schedule_entirely(
        self, session: Session, settings: Settings
    ) -> None:
        """How a change to what an enricher extracts reaches titles not due for weeks."""
        attempted(session, add_title(session))

        assert len(titles_due(session, settings, force=True)) == 1


class TestSchedulingTheNextAttempt:
    def _scheduled(self, session: Session, settings: Settings, title: Title) -> EnrichAttempt:
        session.commit()
        return session.scalars(select(EnrichAttempt)).one()

    def test_a_rated_title_comes_back_on_the_refresh_schedule(
        self, session: Session, settings: Settings
    ) -> None:
        title = add_title(session)

        record_attempt(session, title, settings, outcome=EnrichOutcome.OK)

        attempt = self._scheduled(session, settings, title)
        expected = utcnow() + dt.timedelta(days=settings.enrich.refresh_days)
        assert abs((attempt.due_at - expected).total_seconds()) < 60

    def test_one_a_service_currently_carries_is_kept_fresher(
        self, session: Session, settings: Settings
    ) -> None:
        """It is the one somebody may be looking at tonight."""
        title = add_title(session)
        source = make_source()
        session.add(source)
        session.flush()
        session.add(
            Availability(
                title_id=title.id,
                source_id=source.id,
                offer_type=OfferType.STREAM,
                is_current=True,
                first_seen=utcnow(),
                last_seen=utcnow(),
            )
        )
        session.commit()

        record_attempt(session, title, settings, outcome=EnrichOutcome.OK)

        attempt = self._scheduled(session, settings, title)
        expected = utcnow() + dt.timedelta(days=settings.enrich.hot_refresh_days)
        assert abs((attempt.due_at - expected).total_seconds()) < 60

    def test_an_empty_handed_attempt_backs_off(self, session: Session, settings: Settings) -> None:
        title = add_title(session)

        record_attempt(session, title, settings, outcome=EnrichOutcome.NO_MATCH)

        attempt = self._scheduled(session, settings, title)
        expected = utcnow() + dt.timedelta(days=settings.enrich.retry_days)
        assert attempt.fruitless == 1
        assert abs((attempt.due_at - expected).total_seconds()) < 60

    def test_the_wait_doubles_each_consecutive_time(
        self, session: Session, settings: Settings
    ) -> None:
        """Most of a catalog this local will never carry a score, and asking
        every month costs the whole batch."""
        title = add_title(session)

        for _ in range(3):
            record_attempt(session, title, settings, outcome=EnrichOutcome.NO_MATCH)
            session.commit()

        attempt = self._scheduled(session, settings, title)
        expected = utcnow() + dt.timedelta(days=settings.enrich.retry_days * 4)
        assert attempt.fruitless == 3
        assert abs((attempt.due_at - expected).total_seconds()) < 60

    def test_the_doubling_has_a_ceiling(self, session: Session, settings: Settings) -> None:
        """So nothing is written off for good."""
        title = add_title(session)
        attempted(session, title, outcome=EnrichOutcome.NO_MATCH, fruitless=20)

        record_attempt(session, title, settings, outcome=EnrichOutcome.NO_MATCH)

        attempt = self._scheduled(session, settings, title)
        ceiling = utcnow() + dt.timedelta(days=settings.enrich.retry_max_days)
        assert abs((attempt.due_at - ceiling).total_seconds()) < 60

    def test_a_provider_failure_is_retried_far_sooner(
        self, session: Session, settings: Settings
    ) -> None:
        """That is the provider's problem and it usually passes."""
        title = add_title(session)

        record_attempt(session, title, settings, outcome=EnrichOutcome.ERROR)

        attempt = self._scheduled(session, settings, title)
        expected = utcnow() + dt.timedelta(days=settings.enrich.retry_error_days)
        assert abs((attempt.due_at - expected).total_seconds()) < 60

    def test_a_success_resets_the_count(self, session: Session, settings: Settings) -> None:
        title = add_title(session)
        attempted(session, title, outcome=EnrichOutcome.NO_MATCH, fruitless=4)

        record_attempt(session, title, settings, outcome=EnrichOutcome.OK)

        assert self._scheduled(session, settings, title).fruitless == 0


class TestReadingTheOutcome:
    """The order matters: a rating written is a success whatever else went wrong."""

    def test_a_rating_written_is_a_success(self, session: Session) -> None:
        title = add_title(session)

        assert outcome_of(title, written=1, errored=True) is EnrichOutcome.OK

    def test_a_provider_failure_outranks_the_empty_handed_verdicts(self, session: Session) -> None:
        """It says nothing about whether the title is rateable."""
        title = add_title(session)

        assert outcome_of(title, written=0, errored=True) is EnrichOutcome.ERROR

    def test_nothing_found_is_an_ordinary_outcome(self, session: Session) -> None:
        title = add_title(session)

        assert outcome_of(title, written=0, errored=False) is not EnrichOutcome.OK


class TestStoringRatings:
    def test_a_new_rating_is_written_and_normalised(self, session: Session) -> None:
        title = add_title(session)

        written = store_ratings(
            session,
            title,
            [Rating(provider=RatingProvider.IMDB, score_raw=8.3, vote_count=45_123)],
            refuse,
        )
        session.commit()

        stored = session.scalars(select(ExternalRating)).one()
        assert written == 1
        assert stored.score_raw == 8.3
        assert stored.score_normalized == 83
        assert stored.vote_count == 45_123

    def test_the_same_provider_again_replaces_rather_than_duplicates(
        self, session: Session
    ) -> None:
        title = add_title(session)
        add_rating(session, title, score_raw=7.0, score_normalized=70)

        store_ratings(session, title, [Rating(provider=RatingProvider.IMDB, score_raw=8.3)], refuse)
        session.commit()

        assert session.scalars(select(ExternalRating)).one().score_raw == 8.3

    def test_a_url_it_no_longer_sends_is_kept(self, session: Session) -> None:
        """A score is never shown without a link back to whoever gave it."""
        title = add_title(session)
        add_rating(session, title, url="https://www.imdb.com/title/tt1/")

        store_ratings(session, title, [Rating(provider=RatingProvider.IMDB, score_raw=8.3)], refuse)
        session.commit()

        assert session.scalars(select(ExternalRating)).one().url == (
            "https://www.imdb.com/title/tt1/"
        )

    def test_an_out_of_scale_score_is_refused_not_stored(self, session: Session) -> None:
        """A percentage read as a score out of ten would quietly skew the aggregate."""
        title = add_title(session)
        refusals = Refusals()

        written = store_ratings(
            session,
            title,
            [Rating(provider=RatingProvider.IMDB, score_raw=89.0)],
            refusals,
        )
        session.commit()

        assert written == 0
        assert session.scalars(select(ExternalRating)).all() == []
        assert any("outside its" in message for message in refusals.seen)

    def test_one_bad_score_does_not_cost_the_others(self, session: Session) -> None:
        title = add_title(session)

        written = store_ratings(
            session,
            title,
            [
                Rating(provider=RatingProvider.IMDB, score_raw=89.0),
                Rating(provider=RatingProvider.TMDB, score_raw=7.5),
            ],
            refuse,
        )
        session.commit()

        assert written == 1
        assert session.scalars(select(ExternalRating)).one().provider is RatingProvider.TMDB


class TestPatchingMetadata:
    """Fill gaps only, so a scraped guess cannot displace a canonical answer."""

    def _patch(self, session: Session, title: Title, **fields: Any) -> bool:
        changed = apply_patch(session, title, EnrichResult(metadata_patch=fields), source="tmdb")
        session.commit()
        return changed

    def test_an_empty_field_is_filled(self, session: Session) -> None:
        title = add_title(session, name_en=None)

        assert self._patch(session, title, name_en="Fauda") is True
        assert title.name_en == "Fauda"

    def test_a_field_that_already_says_something_is_left_alone(self, session: Session) -> None:
        title = add_title(session, name_en="Fauda")

        assert self._patch(session, title, name_en="Something Else") is False
        assert title.name_en == "Fauda"

    def test_a_field_nobody_may_patch_is_ignored(self, session: Session) -> None:
        """A provider cannot quietly write to columns it has no business setting."""
        title = add_title(session)

        assert self._patch(session, title, id=999) is False
        assert title.id != 999

    def test_an_empty_value_is_not_an_answer(self, session: Session) -> None:
        title = add_title(session, name_en=None)

        assert self._patch(session, title, name_en="") is False
        assert title.name_en is None

    def test_a_placeholder_year_is_not_a_year(self, session: Session) -> None:
        """Catalogs use that field to mean "unknown" and "not scheduled" too."""
        title = add_title(session, year=None)

        assert self._patch(session, title, year=2999) is False
        assert title.year is None

    def test_a_real_year_is_kept(self, session: Session) -> None:
        title = add_title(session, year=None)

        assert self._patch(session, title, year=2015) is True
        assert title.year == 2015

    def test_a_wrong_script_english_name_is_replaced(self, session: Session) -> None:
        """Not a name we have - one we mislabelled. Every pass fetched the right
        answer and threw it away because the column already held something."""
        title = add_title(session, name_en="千と千尋の神隠し")

        assert self._patch(session, title, name_en="Spirited Away") is True
        assert title.name_en == "Spirited Away"

    def test_but_only_by_one_that_is_actually_in_the_right_script(self, session: Session) -> None:
        """So this can only ever improve a name, never trade one wrong for another."""
        title = add_title(session, name_en="千と千尋の神隠し")

        assert self._patch(session, title, name_en="千と千尋") is False
        assert title.name_en == "千と千尋の神隠し"

    def test_a_unique_id_another_title_holds_is_declined(self, session: Session) -> None:
        """Both id columns are unique, so writing it would take the run down."""
        add_title(session, name_he="חטופות", tmdb_id=479040)
        other = add_title(session, name_he="פאודה", tmdb_id=None)

        assert self._patch(session, other, tmdb_id=479040) is False
        assert other.tmdb_id is None

    def test_a_free_id_is_still_written(self, session: Session) -> None:
        title = add_title(session, tmdb_id=None)

        assert self._patch(session, title, tmdb_id=12345) is True
        assert title.tmdb_id == 12345

    def test_genres_arrive_as_rows(self, session: Session) -> None:
        title = add_title(session)

        changed = self._patch(
            session,
            title,
            genres=[{"tmdb_id": 18, "name_en": "Drama"}, {"tmdb_id": 35, "name_en": "Comedy"}],
        )

        assert changed is True
        # Re-read: the rows go into the join table directly, so the relationship
        # this session loaded as empty is not a record of what is there now.
        session.refresh(title)
        assert sorted(genre.name_en for genre in title.genres) == ["Comedy", "Drama"]

    def test_the_same_genre_on_two_titles_is_one_row(self, session: Session) -> None:
        first = add_title(session, name_he="פאודה")
        second = add_title(session, name_he="שטיסל")

        self._patch(session, first, genres=[{"tmdb_id": 18, "name_en": "Drama"}])
        self._patch(session, second, genres=[{"tmdb_id": 18, "name_en": "Drama"}])

        assert len(session.scalars(select(Genre)).all()) == 1


class TestAttachingPricesToOffers:
    """What a service says about an offer somebody else found.

    The harvester learns *that* a title is on Apple from JustWatch, which
    carries no price and no link; Apple publishes both. These are the rules for
    letting the second reach the first without letting it invent anything.
    """

    def _offer(self, session: Session, title: Title, **overrides: Any) -> Availability:
        source = session.scalars(select(Source).where(Source.key == "apple_tv_store")).first()
        if source is None:
            source = make_source(key="apple_tv_store", name="Apple TV Store")
            session.add(source)
            session.flush()
        values: dict[str, Any] = {
            "title_id": title.id,
            "source_id": source.id,
            "offer_type": OfferType.RENT,
            "first_seen": utcnow(),
            "last_seen": utcnow(),
            "is_current": True,
            "miss_count": 0,
        }
        values.update(overrides)
        row = Availability(**values)
        session.add(row)
        session.flush()
        return row

    def _attach(self, session: Session, title: Title, *facts: OfferFact) -> int:
        changed = apply_offer_facts(session, title, EnrichResult(offers=list(facts)))
        session.commit()
        return changed

    def test_a_price_and_a_link_reach_the_offer(self, session: Session) -> None:
        title = add_title(session)
        row = self._offer(session, title)

        changed = self._attach(
            session,
            title,
            OfferFact("apple_tv_store", OfferType.RENT, 1690, "ILS", "https://tv.apple.com/il/x"),
        )

        assert changed == 1
        assert (row.price_minor, row.price_currency) == (1690, "ILS")
        assert row.deep_link_url == "https://tv.apple.com/il/x"

    def test_it_never_creates_an_offer(self, session: Session) -> None:
        """An enricher knows what a service charges, not what it carries.

        A price for something nobody is offering is a matching mistake, and
        writing it would put a title on a service on one provider's say-so.
        """
        title = add_title(session)

        changed = self._attach(
            session,
            title,
            OfferFact("apple_tv_store", OfferType.BUY, 3490, "ILS", None),
        )

        assert changed == 0
        assert session.scalars(select(Availability)).all() == []

    def test_a_price_already_known_is_not_overwritten(self, session: Session) -> None:
        """A source that scrapes its own storefront knows better than a search."""
        title = add_title(session)
        row = self._offer(session, title, price_minor=1234, price_currency="ILS")

        self._attach(
            session,
            title,
            OfferFact("apple_tv_store", OfferType.RENT, 9900, "ILS", None),
        )

        assert row.price_minor == 1234

    def test_a_link_already_known_is_not_overwritten(self, session: Session) -> None:
        title = add_title(session)
        row = self._offer(session, title, deep_link_url="https://tv.apple.com/il/real")

        self._attach(
            session,
            title,
            OfferFact(
                "apple_tv_store", OfferType.RENT, None, None, "https://tv.apple.com/il/guess"
            ),
        )

        assert row.deep_link_url == "https://tv.apple.com/il/real"

    def test_the_kind_of_deal_has_to_match(self, session: Session) -> None:
        """A rental price is not what the same shop charges to sell it."""
        title = add_title(session)
        row = self._offer(session, title, offer_type=OfferType.RENT)

        changed = self._attach(
            session,
            title,
            OfferFact("apple_tv_store", OfferType.BUY, 3490, "ILS", None),
        )

        assert changed == 0
        assert row.price_minor is None

    def test_an_offer_that_has_gone_is_left_alone(self, session: Session) -> None:
        """Pricing something the service stopped carrying would revive it in
        every way that shows on the page."""
        title = add_title(session)
        row = self._offer(session, title, is_current=False)

        changed = self._attach(
            session,
            title,
            OfferFact("apple_tv_store", OfferType.RENT, 1690, "ILS", None),
        )

        assert changed == 0
        assert row.price_minor is None

    def test_a_fact_for_a_different_service_is_ignored(self, session: Session) -> None:
        title = add_title(session)
        row = self._offer(session, title)

        changed = self._attach(
            session,
            title,
            OfferFact("netflix_il", OfferType.RENT, 1690, "ILS", None),
        )

        assert changed == 0
        assert row.price_minor is None


class TestFindingTheMislabelled:
    """A repair, not a schedule: titles a source filed a Hebrew name under
    ``name_en``, which an en-US request can still fix."""

    def test_a_name_in_the_wrong_script_is_found(self, session: Session) -> None:
        add_title(session, name_en="千と千尋の神隠し", tmdb_id=129)

        assert len(mislabelled_names(session)) == 1

    def test_a_latin_name_is_not(self, session: Session) -> None:
        add_title(session, name_en="Spirited Away", tmdb_id=129)

        assert mislabelled_names(session) == []

    def test_one_with_nobody_to_ask_is_left_out(self, session: Session) -> None:
        """A title with no TMDB id has no source for a better answer."""
        add_title(session, name_en="千と千尋の神隠し", tmdb_id=None)

        assert mislabelled_names(session) == []

    def test_the_batch_is_bounded(self, session: Session) -> None:
        for index in range(3):
            add_title(session, name_en=f"千と千尋{index}", tmdb_id=100 + index)

        assert len(mislabelled_names(session, limit=2)) == 2


class TestAggregation:
    def test_two_providers_make_an_aggregate(self, session: Session, settings: Settings) -> None:
        title = add_title(session)
        add_rating(session, title, provider=RatingProvider.IMDB, score_normalized=80)
        add_rating(session, title, provider=RatingProvider.TMDB, score_normalized=60)

        assert recompute(session, title, settings) is True
        session.commit()
        assert session.scalars(select(AggregateScore)).one().score == 75

    def test_a_title_with_no_ratings_gets_no_row(
        self, session: Session, settings: Settings
    ) -> None:
        title = add_title(session)

        assert recompute(session, title, settings) is False
        assert session.scalars(select(AggregateScore)).all() == []

    def test_recomputing_replaces_rather_than_duplicates(
        self, session: Session, settings: Settings
    ) -> None:
        title = add_title(session)
        rating = add_rating(session, title, score_normalized=80)
        add_rating(session, title, provider=RatingProvider.TMDB, score_normalized=60)
        recompute(session, title, settings)
        session.commit()

        rating.score_normalized = 100
        recompute(session, title, settings)
        session.commit()

        # (100*3.0 + 60*1.0) / 4.0, IMDb being weighted three times TMDB.
        assert session.scalars(select(AggregateScore)).one().score == 90

    def test_recompute_all_rescores_every_rated_title(
        self, session: Session, settings: Settings
    ) -> None:
        """The IMDb bulk pass writes ratings directly, so scores need refreshing."""
        title = add_title(session)
        add_rating(session, title, provider=RatingProvider.IMDB, score_normalized=90)
        add_rating(session, title, provider=RatingProvider.TMDB, score_normalized=70)

        assert recompute_all_aggregates(session, settings) == 1
        assert session.scalars(select(AggregateScore)).one().score == 85

    def test_it_leaves_unrated_titles_alone(self, session: Session, settings: Settings) -> None:
        add_title(session, name_he="בלי ציון")
        rated = add_title(session, name_he="עם ציון")
        add_rating(session, rated, provider=RatingProvider.IMDB, score_normalized=90)
        add_rating(session, rated, provider=RatingProvider.TMDB, score_normalized=70)

        assert recompute_all_aggregates(session, settings) == 1

    def test_it_says_how_many_it_is_about_to_do(
        self, session: Session, settings: Settings, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The last minutes of an hour-long phase are exactly when somebody is
        wondering whether to kill it."""
        title = add_title(session)
        add_rating(session, title)

        with caplog.at_level("INFO", logger="eifo.enriching"):
            recompute_all_aggregates(session, settings)

        assert "rescoring 1 rated title(s)" in caplog.text


class TestTheViewAnEnricherIsGiven:
    """A snapshot rather than the ORM object, so an enricher cannot acquire a
    write path to the database by accident."""

    def test_it_carries_what_an_enricher_resolves_by(self, session: Session) -> None:
        title = add_title(session, tmdb_id=1234, imdb_id="tt4565380")

        view = view_of(title)

        assert (view.id, view.kind, view.year) == (title.id, TitleKind.SERIES, 2015)
        assert (view.tmdb_id, view.imdb_id) == (1234, "tt4565380")

    def test_it_names_the_title_the_way_a_person_would(self, session: Session) -> None:
        assert view_of(add_title(session)).display_name == "פאודה"

    def test_a_title_with_no_name_at_all_still_has_one(self) -> None:
        """Built by hand: the schema will not store a title with no name, and
        this is about the view being usable whatever it was handed."""
        view = TitleView(
            id=7,
            kind=TitleKind.MOVIE,
            name_he=None,
            name_en=None,
            year=None,
            tmdb_id=None,
            imdb_id=None,
        )

        assert view.display_name == "title#7"

    def test_it_describes_itself_for_a_log_in_english(self, session: Session) -> None:
        """The log is in English, unlike the page."""
        assert view_of(add_title(session)).describe() == "'Fauda' (id 1)"

    def test_an_untitled_one_still_says_which(self) -> None:
        view = TitleView(
            id=7,
            kind=TitleKind.MOVIE,
            name_he=None,
            name_en=None,
            year=None,
            tmdb_id=None,
            imdb_id=None,
        )

        assert view.describe() == "'untitled' (id 7)"
