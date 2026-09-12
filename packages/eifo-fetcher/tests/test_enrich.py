"""The enrichment pipeline: refresh policy, persistence, gap-filling."""

from __future__ import annotations

import datetime as dt
import logging
import time
from typing import Any

import pytest
from live import LiveApi
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from eifo_core import ingest as wire
from eifo_core.enriching import (
    mislabelled_names,
    titles_due,
)
from eifo_core.enums import (
    EnrichOutcome,
    FetchPhase,
    FetchStatus,
    OfferType,
    RatingProvider,
    SourceKind,
    TitleKind,
)
from eifo_core.findings import OfferFact
from eifo_core.models import (
    AggregateScore,
    Availability,
    EnrichAttempt,
    ExternalRating,
    FetchRun,
    Genre,
    Source,
    Title,
)
from eifo_core.settings import Settings
from eifo_core.types import utcnow
from eifo_fetcher.enrich import apply_rate_limits, enrich_titles
from eifo_fetcher.enrichers import discover_enrichers
from eifo_fetcher.enrichers.base import Enricher, EnrichResult, Rating, TitleView
from eifo_fetcher.enrichers.rt import HOST as RT_HOST
from eifo_fetcher.enrichers.seret import HOST as SERET_HOST
from eifo_fetcher.http import HttpClient, RateLimiter
from eifo_fetcher.ingest import IngestClient
from eifo_fetcher.sources.base import FetchContext, TooManyErrorsError


@pytest.fixture
def session_factory(live_api: LiveApi) -> sessionmaker[Session]:
    """The catalog the API writes to, which is the only one an enrich touches.

    Overriding the package fixture rather than adding a second name: every
    assertion below is about what an enrich left in the catalog, and there is
    now exactly one process that puts it there.
    """
    return live_api.session_factory


def run_enrich(session: Session, *args: Any, **kwargs: Any) -> Any:
    """Enrich, then let this session see what the application wrote.

    The catalog is written by the API now, in its own session and its own
    transaction. A test holding a ``Title`` it loaded beforehand would go on
    seeing the values it was loaded with, however many rows the run changed
    underneath it - so the identity map is emptied here, once, rather than in
    thirty assertions that would each have to remember to.
    """
    try:
        return enrich_titles(*args, **kwargs)
    finally:
        session.expire_all()


class FakeEnricher(Enricher):
    """Returns a canned result, or raises."""

    providers = (RatingProvider.SERET_VIEWERS,)

    def __init__(
        self,
        result: EnrichResult | None = None,
        *,
        error: Exception | None = None,
        key: str = "fake",
    ) -> None:
        self._result = result
        self._error = error
        self._key = key
        self.seen: list[TitleView] = []

    @property
    def key(self) -> str:
        return self._key

    def enrich(self, title: TitleView, ctx: FetchContext) -> EnrichResult | None:
        self.seen.append(title)
        if self._error is not None:
            raise self._error
        return self._result


def add_title(session: Session, **overrides: Any) -> Title:
    values: dict[str, Any] = {
        "type": TitleKind.SERIES,
        "name_he": "פאודה",
        "year": 2015,
    }
    values.update(overrides)
    title = Title(**values)
    session.add(title)
    session.commit()
    return title


def add_attempt(session: Session, title: Title, **overrides: Any) -> EnrichAttempt:
    values: dict[str, Any] = {
        "title_id": title.id,
        "attempted_at": utcnow(),
        "outcome": EnrichOutcome.OK,
        "fruitless": 0,
        "due_at": utcnow() + dt.timedelta(days=3),
    }
    values.update(overrides)
    attempt = EnrichAttempt(**values)
    session.add(attempt)
    session.commit()
    return attempt


def _attempt(session: Session, title: Title) -> EnrichAttempt:
    attempt = session.get(EnrichAttempt, title.id)
    assert attempt is not None, f"title {title.id} was not recorded as attempted"
    return attempt


def _attempt_now(session: Session, title: Title) -> None:
    """Bring a title forward so the next run picks it up again."""
    attempt = session.get(EnrichAttempt, title.id)
    if attempt is not None:
        attempt.due_at = utcnow()
        session.commit()


def _days_until(moment: dt.datetime) -> int:
    """Whole days from now, rounded to the nearest, so a test reads as its policy."""
    return round((moment - utcnow()).total_seconds() / 86400)


def add_rating(session: Session, title: Title, **overrides: Any) -> ExternalRating:
    values: dict[str, Any] = {
        "title_id": title.id,
        "provider": RatingProvider.IMDB,
        "score_raw": 8.4,
        "score_normalized": 84,
        "vote_count": 5000,
        "fetched_at": utcnow(),
    }
    values.update(overrides)
    rating = ExternalRating(**values)
    session.add(rating)
    session.commit()
    return rating


class TestTitlesDue:
    def test_a_title_never_attempted_is_due(self, session: Session, settings: Settings) -> None:
        add_title(session)

        assert len(titles_due(session, settings)) == 1

    def test_a_title_inside_its_cooldown_is_not_due(
        self, session: Session, settings: Settings
    ) -> None:
        add_attempt(session, add_title(session), due_at=utcnow() + dt.timedelta(days=3))

        assert titles_due(session, settings) == []

    def test_a_title_whose_cooldown_has_passed_is_due(
        self, session: Session, settings: Settings
    ) -> None:
        add_attempt(session, add_title(session), due_at=utcnow() - dt.timedelta(minutes=1))

        assert len(titles_due(session, settings)) == 1

    def test_a_rating_alone_does_not_make_a_title_look_attempted(
        self, session: Session, settings: Settings
    ) -> None:
        """Ratings are what an attempt produced, not the record that it happened.

        The IMDb bulk pass writes ratings for titles it never visited one by
        one, and the migration backfills the rest; neither should be mistaken
        for a per-title attempt that fixed a schedule.
        """
        add_rating(session, add_title(session))

        assert len(titles_due(session, settings)) == 1

    def test_titles_never_attempted_go_first(self, session: Session, settings: Settings) -> None:
        """Otherwise a refresh backlog can starve first-time enrichment indefinitely."""
        waiting = add_title(session, name_he="ותיק")
        add_attempt(session, waiting, due_at=utcnow() - dt.timedelta(days=1))
        fresh = add_title(session, name_he="חדש")

        assert [title.id for title in titles_due(session, settings)] == [fresh.id, waiting.id]

    def test_the_least_recently_attempted_goes_first(
        self, session: Session, settings: Settings
    ) -> None:
        recent = add_title(session, name_he="לאחרונה")
        long_ago = add_title(session, name_he="מזמן")
        overdue = utcnow() - dt.timedelta(days=1)
        add_attempt(session, recent, attempted_at=utcnow() - dt.timedelta(days=2), due_at=overdue)
        add_attempt(
            session, long_ago, attempted_at=utcnow() - dt.timedelta(days=90), due_at=overdue
        )

        assert [title.id for title in titles_due(session, settings)] == [long_ago.id, recent.id]

    def test_force_ignores_the_schedule(self, session: Session, settings: Settings) -> None:
        add_attempt(session, add_title(session), due_at=utcnow() + dt.timedelta(days=365))

        assert len(titles_due(session, settings, force=True)) == 1

    def test_the_batch_size_bounds_a_run(self, session: Session, settings: Settings) -> None:
        for index in range(5):
            add_title(session, name_he=f"תוכנית {index}")

        assert len(titles_due(session, settings, limit=2)) == 2


class TestSchedulingTheNextAttempt:
    """Every title leaves a run with a date on it; these are how it is chosen."""

    def _ctx(self, http: Any, settings: Settings) -> FetchContext:
        return FetchContext(source_key="enrich", http=http, settings=settings)

    def _run(
        self,
        api: IngestClient,
        session: Session,
        settings: Settings,
        http: Any,
        enricher: FakeEnricher,
        **kwargs: Any,
    ) -> None:
        run_enrich(session, api, [enricher], self._ctx(http, settings), settings, **kwargs)

    def test_a_rated_title_comes_back_on_the_refresh_schedule(
        self, api: IngestClient, session: Session, settings: Settings, http: Any
    ) -> None:
        title = add_title(session)
        rated = FakeEnricher(EnrichResult(ratings=[Rating(RatingProvider.SERET_VIEWERS, 8.0)]))

        self._run(api, session, settings, http, rated)

        attempt = session.get(EnrichAttempt, title.id)
        assert attempt is not None
        assert attempt.outcome is EnrichOutcome.OK
        assert attempt.fruitless == 0
        # Nothing carries it, so the slower of the two refresh rates applies.
        assert _days_until(attempt.due_at) == settings.enrich.refresh_days

    def test_available_titles_are_refreshed_sooner(
        self, api: IngestClient, session: Session, settings: Settings, http: Any
    ) -> None:
        """What people can actually watch is worth keeping fresher."""
        source = Source(
            key="mako",
            name="Mako",
            kind=SourceKind.FREE,
            website_url="https://example.com",
        )
        session.add(source)
        hot = add_title(session, name_he="זמין")
        cold = add_title(session, name_he="לא זמין")
        session.flush()
        session.add(Availability(title_id=hot.id, source_id=source.id, offer_type=OfferType.FREE))
        session.commit()
        rated = FakeEnricher(EnrichResult(ratings=[Rating(RatingProvider.SERET_VIEWERS, 8.0)]))

        self._run(api, session, settings, http, rated)

        assert _days_until(_attempt(session, hot).due_at) == settings.enrich.hot_refresh_days
        assert _days_until(_attempt(session, cold).due_at) == settings.enrich.refresh_days

    def test_a_title_nobody_rates_is_recorded_as_no_data(
        self, api: IngestClient, session: Session, settings: Settings, http: Any
    ) -> None:
        """Known exactly, and simply not carried by any provider - most of this catalog."""
        title = add_title(session, tmdb_id=1234)

        self._run(api, session, settings, http, FakeEnricher(None))

        attempt = _attempt(session, title)
        assert attempt.outcome is EnrichOutcome.NO_DATA
        assert attempt.fruitless == 1
        assert _days_until(attempt.due_at) == settings.enrich.retry_days

    def test_a_title_with_no_external_id_is_recorded_as_no_match(
        self, api: IngestClient, session: Session, settings: Settings, http: Any
    ) -> None:
        """Nothing to look it up by; matching has to improve before asking again."""
        title = add_title(session, tmdb_id=None, imdb_id=None)

        self._run(api, session, settings, http, FakeEnricher(None))

        assert _attempt(session, title).outcome is EnrichOutcome.NO_MATCH

    def test_a_provider_failure_is_retried_sooner_than_an_empty_answer(
        self, api: IngestClient, session: Session, settings: Settings, http: Any
    ) -> None:
        """A provider being down says nothing about whether the title is rateable."""
        title = add_title(session, tmdb_id=1234)

        self._run(api, session, settings, http, FakeEnricher(error=RuntimeError("boom")))

        attempt = _attempt(session, title)
        assert attempt.outcome is EnrichOutcome.ERROR
        assert _days_until(attempt.due_at) == settings.enrich.retry_error_days

    def test_each_fruitless_attempt_waits_longer(
        self, api: IngestClient, session: Session, settings: Settings, http: Any
    ) -> None:
        title = add_title(session, tmdb_id=1234)
        nothing = FakeEnricher(None)

        waits = []
        for _ in range(3):
            _attempt_now(session, title)
            self._run(api, session, settings, http, nothing)
            waits.append(_days_until(_attempt(session, title).due_at))

        base = settings.enrich.retry_days
        assert waits == [base, base * 2, base * 4]
        assert _attempt(session, title).fruitless == 3

    def test_the_wait_stops_doubling_at_the_ceiling(
        self, api: IngestClient, session: Session, settings: Settings, http: Any
    ) -> None:
        """Nothing is written off for good: a title TMDB has not rated yet may be rated later."""
        title = add_title(session, tmdb_id=1234)
        add_attempt(session, title, outcome=EnrichOutcome.NO_DATA, fruitless=40, due_at=utcnow())

        self._run(api, session, settings, http, FakeEnricher(None))

        assert _days_until(_attempt(session, title).due_at) == settings.enrich.retry_max_days

    def test_a_rating_clears_the_backoff(
        self, api: IngestClient, session: Session, settings: Settings, http: Any
    ) -> None:
        title = add_title(session, tmdb_id=1234)
        add_attempt(session, title, outcome=EnrichOutcome.NO_DATA, fruitless=5, due_at=utcnow())
        rated = FakeEnricher(EnrichResult(ratings=[Rating(RatingProvider.SERET_VIEWERS, 8.0)]))

        self._run(api, session, settings, http, rated)

        attempt = _attempt(session, title)
        assert attempt.outcome is EnrichOutcome.OK
        assert attempt.fruitless == 0

    def test_a_run_moves_past_the_titles_the_last_one_could_not_rate(
        self, api: IngestClient, session: Session, settings: Settings, http: Any
    ) -> None:
        """The wedge itself.

        Ten consecutive runs on the deployed catalog read the same 500 titles
        and wrote nothing, because a title with no rating was due again the
        moment the run that failed to rate it finished.
        """
        for index in range(4):
            add_title(session, name_he=f"סרט {index}", tmdb_id=1000 + index)
        nothing = FakeEnricher(None)

        self._run(api, session, settings, http, nothing, limit=2)
        first = [view.id for view in nothing.seen]
        nothing.seen.clear()
        self._run(api, session, settings, http, nothing, limit=2)
        second = [view.id for view in nothing.seen]

        assert len(first) == 2
        assert len(second) == 2
        assert set(first).isdisjoint(second)


class TestRateLimits:
    """How hard each scraped provider leans on its host.

    This used to be each enricher's own business, via ``ctx.apply_rate_limit``
    - which resolved the rate from ``[sources.enrich]``, a section that exists
    in no config file and no documentation. So the call set nothing, every
    scraped provider ran at the client-wide default, and there was no way to
    change it. Now the providers declare a host and a pace and the pipeline
    applies them, which is where the rest of this project's politeness lives.
    """

    def _limiter(self, **enrich: Any) -> RateLimiter:
        limiter = RateLimiter(default_rps=0)
        with HttpClient(rate_limiter=limiter, sleep=lambda _seconds: None) as http:
            settings = Settings(_env_file=None, enrich=enrich)
            ctx = FetchContext(source_key="enrich", http=http, settings=settings)
            apply_rate_limits(discover_enrichers(settings), ctx, settings)
        return limiter

    def _spacing(self, limiter: RateLimiter, host: str) -> float:
        later = time.monotonic() + 10_000.0
        limiter.wait(host, sleep=lambda _seconds: None, now=lambda: later)
        return limiter.wait(host, sleep=lambda _seconds: None, now=lambda: later)

    def test_each_scraped_provider_gets_its_own_default(self) -> None:
        limiter = self._limiter()

        assert self._spacing(limiter, RT_HOST) == pytest.approx(1.0)
        assert self._spacing(limiter, SERET_HOST) == pytest.approx(2.0)

    def test_configuration_overrides_a_default(self) -> None:
        limiter = self._limiter(rate_limits={"rt": 0.25, "seret": 4.0})

        assert self._spacing(limiter, RT_HOST) == pytest.approx(4.0)
        assert self._spacing(limiter, SERET_HOST) == pytest.approx(0.25)

    def test_one_provider_can_be_retuned_without_touching_the_others(self) -> None:
        limiter = self._limiter(rate_limits={"rt": 0.5})

        assert self._spacing(limiter, RT_HOST) == pytest.approx(2.0)
        assert self._spacing(limiter, SERET_HOST) == pytest.approx(2.0)

    def test_a_provider_with_no_host_of_its_own_is_left_alone(self) -> None:
        """TMDB is an API with its own [tmdb] pace, not somebody's website."""
        tmdb = next(e for e in discover_enrichers(Settings(_env_file=None)) if e.key == "tmdb")

        assert tmdb.host is None
        assert tmdb.default_rate_limit_rps is None


class TestEnrichTitles:
    def _ctx(self, http: Any, settings: Settings) -> FetchContext:
        return FetchContext(source_key="enrich", http=http, settings=settings)

    def test_stores_a_returned_rating(
        self, api: IngestClient, session: Session, settings: Settings, http: Any
    ) -> None:
        add_title(session)
        enricher = FakeEnricher(
            EnrichResult(
                ratings=[
                    Rating(
                        provider=RatingProvider.SERET_VIEWERS,
                        score_raw=8.9,
                        vote_count=120,
                        url="https://www.seret.co.il/movies/1",
                    )
                ]
            )
        )

        tally = run_enrich(session, api, [enricher], self._ctx(http, settings), settings)

        rating = session.scalars(select(ExternalRating)).one()
        assert rating.provider is RatingProvider.SERET_VIEWERS
        assert rating.score_raw == 8.9
        assert rating.score_normalized == 89
        assert rating.url == "https://www.seret.co.il/movies/1"
        assert tally.ratings_written == 1

    def test_re_enriching_updates_rather_than_duplicates(
        self, api: IngestClient, session: Session, settings: Settings, http: Any
    ) -> None:
        add_title(session)
        first = FakeEnricher(EnrichResult(ratings=[Rating(RatingProvider.SERET_VIEWERS, 7.0)]))
        run_enrich(session, api, [first], self._ctx(http, settings), settings)

        second = FakeEnricher(EnrichResult(ratings=[Rating(RatingProvider.SERET_VIEWERS, 9.0)]))
        run_enrich(session, api, [second], self._ctx(http, settings), settings, force=True)

        rating = session.scalars(select(ExternalRating)).one()
        assert rating.score_raw == 9.0

    def test_an_out_of_scale_score_is_rejected_not_stored(
        self, api: IngestClient, session: Session, settings: Settings, http: Any
    ) -> None:
        """A percentage read as a /10 score would silently skew the aggregate."""
        add_title(session)
        enricher = FakeEnricher(EnrichResult(ratings=[Rating(RatingProvider.SERET_VIEWERS, 89.0)]))
        ctx = self._ctx(http, settings)

        tally = run_enrich(session, api, [enricher], ctx, settings)

        assert session.scalars(select(ExternalRating)).all() == []
        # Refused where the catalog is, and reported back rather than counted
        # here: this side has no idea what a score is allowed to be, and a
        # refusal that never crossed back would be a parser bug nobody sees.
        assert any("outside its 0-10 scale" in entry for entry in tally.errors)
        assert any("title 1" in entry for entry in tally.errors), "which title, not which index"

    def test_a_provider_with_nothing_is_not_an_error(
        self, api: IngestClient, session: Session, settings: Settings, http: Any
    ) -> None:
        """Plenty of Israeli titles simply do not exist on foreign sites."""
        add_title(session)
        ctx = self._ctx(http, settings)

        run_enrich(session, api, [FakeEnricher(None)], ctx, settings)

        assert ctx.error_count == 0
        assert session.scalars(select(ExternalRating)).all() == []

    def test_one_failing_provider_does_not_stop_the_others(
        self, api: IngestClient, session: Session, settings: Settings, http: Any
    ) -> None:
        add_title(session)
        broken = FakeEnricher(error=RuntimeError("site down"), key="broken")
        working = FakeEnricher(
            EnrichResult(ratings=[Rating(RatingProvider.SERET_VIEWERS, 8.0)]), key="working"
        )
        ctx = self._ctx(http, settings)

        run_enrich(session, api, [broken, working], ctx, settings)

        assert len(session.scalars(select(ExternalRating)).all()) == 1
        assert ctx.error_count == 1

    def test_records_a_fetch_run(
        self, api: IngestClient, session: Session, settings: Settings, http: Any
    ) -> None:
        add_title(session)
        enricher = FakeEnricher(EnrichResult(ratings=[Rating(RatingProvider.SERET_VIEWERS, 8.0)]))

        run_enrich(session, api, [enricher], self._ctx(http, settings), settings)

        run = session.scalars(select(FetchRun).where(FetchRun.phase == FetchPhase.ENRICH)).one()
        assert run.status is FetchStatus.OK
        assert run.stats["titles_seen"] == 1
        assert run.stats["by_enricher"] == {"fake": 1}


class TestMetadataPatch:
    def _ctx(self, http: Any, settings: Settings) -> FetchContext:
        return FetchContext(source_key="enrich", http=http, settings=settings)

    def test_fills_an_empty_field(
        self, api: IngestClient, session: Session, settings: Settings, http: Any
    ) -> None:
        add_title(session, name_en=None)
        enricher = FakeEnricher(EnrichResult(metadata_patch={"name_en": "Fauda"}))

        run_enrich(session, api, [enricher], self._ctx(http, settings), settings)

        assert session.scalars(select(Title)).one().name_en == "Fauda"

    def test_never_overwrites_a_known_value(
        self, api: IngestClient, session: Session, settings: Settings, http: Any
    ) -> None:
        """A provider's guess must not displace an answer we already trust."""
        add_title(session, name_en="Fauda")
        enricher = FakeEnricher(EnrichResult(metadata_patch={"name_en": "Something Else"}))

        run_enrich(session, api, [enricher], self._ctx(http, settings), settings)

        assert session.scalars(select(Title)).one().name_en == "Fauda"

    def test_ignores_fields_outside_the_allowed_set(
        self, api: IngestClient, session: Session, settings: Settings, http: Any
    ) -> None:
        """An enricher has no business writing to bookkeeping columns."""
        title = add_title(session)
        original = title.created_at
        enricher = FakeEnricher(
            EnrichResult(metadata_patch={"created_at": "1999-01-01", "poster_path": "x/y.jpg"})
        )

        run_enrich(session, api, [enricher], self._ctx(http, settings), settings)

        stored = session.scalars(select(Title)).one()
        assert stored.created_at == original
        assert stored.poster_path is None

    def test_creates_and_attaches_genres(
        self, api: IngestClient, session: Session, settings: Settings, http: Any
    ) -> None:
        add_title(session)
        enricher = FakeEnricher(
            EnrichResult(
                metadata_patch={"genres": [{"tmdb_id": 18, "name_en": "Drama", "name_he": "דרמה"}]}
            )
        )

        run_enrich(session, api, [enricher], self._ctx(http, settings), settings)

        genre = session.scalars(select(Genre)).one()
        assert genre.name_en == "Drama"
        assert genre.name_he == "דרמה"
        assert session.scalars(select(Title)).one().genres == [genre]

    def test_a_genre_listed_twice_is_attached_once(
        self, api: IngestClient, session: Session, settings: Settings, http: Any
    ) -> None:
        """TMDB repeats one now and then, and the join table will not have it.

        The insert failed, the flush raised, and the whole enrich run ended on
        one bad payload - twenty-two titles into a backlog of thirty thousand.
        """
        add_title(session)
        enricher = FakeEnricher(
            EnrichResult(
                metadata_patch={
                    "genres": [
                        {"tmdb_id": 18, "name_en": "Drama"},
                        {"tmdb_id": 35, "name_en": "Comedy"},
                        {"tmdb_id": 18, "name_en": "Drama"},
                    ]
                }
            )
        )

        run_enrich(session, api, [enricher], self._ctx(http, settings), settings)

        stored = session.scalars(select(Title)).one()
        assert sorted(genre.name_en for genre in stored.genres) == ["Comedy", "Drama"]

    def test_the_run_survives_it(
        self, api: IngestClient, session: Session, settings: Settings, http: Any
    ) -> None:
        """The failure that mattered was not the duplicate; it was the run dying."""
        add_title(session)
        enricher = FakeEnricher(
            EnrichResult(
                metadata_patch={
                    "genres": [{"tmdb_id": 18, "name_en": "Drama"}] * 3,
                    "runtime_minutes": 100,
                }
            )
        )

        tally = run_enrich(session, api, [enricher], self._ctx(http, settings), settings)

        assert tally.errors == []
        assert session.scalars(select(Title)).one().runtime_minutes == 100

    def test_reuses_an_existing_genre(
        self, api: IngestClient, session: Session, settings: Settings, http: Any
    ) -> None:
        session.add(Genre(tmdb_id=18, name_en="Drama"))
        add_title(session)
        enricher = FakeEnricher(
            EnrichResult(metadata_patch={"genres": [{"tmdb_id": 18, "name_en": "Drama"}]})
        )

        run_enrich(session, api, [enricher], self._ctx(http, settings), settings)

        assert len(session.scalars(select(Genre)).all()) == 1


class TestAggregation:
    def _ctx(self, http: Any, settings: Settings) -> FetchContext:
        return FetchContext(source_key="enrich", http=http, settings=settings)

    def test_computes_an_aggregate_from_two_providers(
        self, api: IngestClient, session: Session, settings: Settings, http: Any
    ) -> None:
        title = add_title(session)
        add_rating(session, title, provider=RatingProvider.IMDB, score_normalized=80)
        enricher = FakeEnricher(EnrichResult(ratings=[Rating(RatingProvider.TMDB, 6.0)]))

        run_enrich(session, api, [enricher], self._ctx(http, settings), settings, force=True)

        aggregate = session.scalars(select(AggregateScore)).one()
        assert aggregate.score == 75
        assert set(aggregate.components) == {"imdb", "tmdb"}

    def test_a_title_with_no_ratings_gets_no_aggregate_row(
        self, api: IngestClient, session: Session, settings: Settings, http: Any
    ) -> None:
        add_title(session)

        run_enrich(session, api, [FakeEnricher(None)], self._ctx(http, settings), settings)

        assert session.scalars(select(AggregateScore)).all() == []


class TestReportingAsItGoes:
    """Findings are sent in batches, and that is what bounds what a crash loses.

    SQLite allows one writer, so a whole run in one transaction held the write
    lock for up to twenty-nine minutes against a thirty-second busy timeout, and
    anything else touching the database waited and then failed. The transaction
    is on the far side now and the same rule governs it: each batch of findings
    is one request and one transaction, so a run that dies keeps everything it
    had already reported.
    """

    def _ctx(self, http: Any, settings: Settings) -> FetchContext:
        return FetchContext(source_key="enrich", http=http, settings=settings)

    def test_a_long_batch_is_reported_in_several_pieces_not_one(
        self, live_api: LiveApi, session: Session, settings: Settings, http: Any
    ) -> None:
        for index in range(wire.ENRICH_CHUNK_SIZE * 2 + 5):
            add_title(session, name_he=f"סרט {index}", tmdb_id=2000 + index)
        sent: list[int] = []
        original = live_api.api.report

        def counting_report(run_id: int, entries: list[Any]) -> Any:
            sent.append(len(entries))
            return original(run_id, entries)

        live_api.api.report = counting_report  # type: ignore[method-assign]

        run_enrich(session, live_api.api, [FakeEnricher(None)], self._ctx(http, settings), settings)

        assert sent == [wire.ENRICH_CHUNK_SIZE, wire.ENRICH_CHUNK_SIZE, 5]

    def test_work_already_done_survives_a_crash_mid_batch(
        self, api: IngestClient, session: Session, settings: Settings, http: Any
    ) -> None:
        """A crash used to discard every rating the run had fetched."""
        for index in range(wire.ENRICH_CHUNK_SIZE + 2):
            add_title(session, name_he=f"סרט {index}", tmdb_id=3000 + index)

        seen = 0

        class FailsLate(FakeEnricher):
            def enrich(self, title: TitleView, ctx: FetchContext) -> EnrichResult | None:
                nonlocal seen
                seen += 1
                if seen > wire.ENRICH_CHUNK_SIZE:
                    raise RuntimeError("provider fell over")
                return None

        run_enrich(session, api, [FailsLate()], self._ctx(http, settings), settings)

        # The titles from before the failure kept their attempt rows, so the
        # next run starts where this one stopped rather than repeating it.
        recorded = session.scalars(select(EnrichAttempt)).all()
        assert len(recorded) >= wire.ENRICH_CHUNK_SIZE

    def test_a_failed_run_records_what_went_wrong(
        self, api: IngestClient, session: Session, settings: Settings, http: Any
    ) -> None:
        """A run that says only that it failed leaves the obvious question unanswered."""
        add_title(session)
        exhausted = FakeEnricher(error=TooManyErrorsError("enrich", 25))

        run_enrich(session, api, [exhausted], self._ctx(http, settings), settings)

        run = session.scalars(select(FetchRun).where(FetchRun.phase == FetchPhase.ENRICH)).one()
        assert run.status is FetchStatus.FAILED
        assert any("TooManyErrorsError" in entry for entry in run.stats["errors"])

    def test_a_run_row_exists_while_the_phase_is_still_going(
        self, api: IngestClient, session: Session, settings: Settings, http: Any
    ) -> None:
        """So a fetcher killed mid-enrich leaves a trace rather than nothing at all."""
        add_title(session)
        seen: list[FetchStatus] = []

        class LooksAtTheRun(FakeEnricher):
            def enrich(self, title: TitleView, ctx: FetchContext) -> EnrichResult | None:
                seen.extend(session.scalars(select(FetchRun.status)).all())
                return None

        run_enrich(session, api, [LooksAtTheRun()], self._ctx(http, settings), settings)

        assert seen == [FetchStatus.RUNNING]


class TestExternalIdCollisions:
    """Both id columns are unique, so a second writer used to take the run down."""

    def _ctx(self, http: Any, settings: Settings) -> FetchContext:
        return FetchContext(source_key="enrich", http=http, settings=settings)

    def test_an_id_another_title_owns_is_not_written(
        self, api: IngestClient, session: Session, settings: Settings, http: Any
    ) -> None:
        owner = add_title(session, name_he="חטופות", tmdb_id=479040)
        other = add_title(session, name_he="חטופות", tmdb_id=None)
        enricher = FakeEnricher(EnrichResult(metadata_patch={"tmdb_id": 479040}))

        run_enrich(session, api, [enricher], self._ctx(http, settings), settings)

        assert other.tmdb_id is None
        assert owner.tmdb_id == 479040

    def test_the_run_carries_on(
        self, api: IngestClient, session: Session, settings: Settings, http: Any
    ) -> None:
        """It used to raise on the next flush and lose the rest of the batch."""
        add_title(session, name_he="חטופות", tmdb_id=479040)
        add_title(session, name_he="אחר", tmdb_id=None)
        enricher = FakeEnricher(EnrichResult(metadata_patch={"tmdb_id": 479040}))

        tally = run_enrich(session, api, [enricher], self._ctx(http, settings), settings)

        assert tally.titles_seen == 2

    def test_a_free_id_is_still_written(
        self, api: IngestClient, session: Session, settings: Settings, http: Any
    ) -> None:
        title = add_title(session, tmdb_id=None)
        enricher = FakeEnricher(EnrichResult(metadata_patch={"tmdb_id": 12345}))

        run_enrich(session, api, [enricher], self._ctx(http, settings), settings)

        assert title.tmdb_id == 12345

    def test_a_series_holding_the_number_does_not_take_a_films_id(
        self, api: IngestClient, session: Session, settings: Settings, http: Any
    ) -> None:
        """TMDB numbers films and series separately, and the schema keys them
        as (type, tmdb_id) - so movie 105 is free while series 105 is taken.

        Compared without the kind, Back to the Future was refused its own id
        because Sex and the City is series 105. Fourteen films in one run.
        """
        add_title(session, type=TitleKind.SERIES, name_en="Sex and the City", tmdb_id=105)
        film = add_title(
            session, type=TitleKind.MOVIE, name_en="Back to the Future", year=1985, tmdb_id=None
        )
        enricher = FakeEnricher(EnrichResult(metadata_patch={"tmdb_id": 105}))

        run_enrich(session, api, [enricher], self._ctx(http, settings), settings)

        assert film.tmdb_id == 105

    def test_the_same_kind_holding_it_still_blocks(
        self, api: IngestClient, session: Session, settings: Settings, http: Any
    ) -> None:
        """The guard narrows; it does not go away. Two films cannot share one."""
        owner = add_title(session, type=TitleKind.MOVIE, name_en="Held", tmdb_id=105)
        other = add_title(session, type=TitleKind.MOVIE, name_en="Other", tmdb_id=None)
        enricher = FakeEnricher(EnrichResult(metadata_patch={"tmdb_id": 105}))

        run_enrich(session, api, [enricher], self._ctx(http, settings), settings)

        assert other.tmdb_id is None
        assert owner.tmdb_id == 105

    def test_an_imdb_id_is_global_whatever_the_kind(
        self, api: IngestClient, session: Session, settings: Settings, http: Any
    ) -> None:
        """IMDb numbers everything once, so the kind must not narrow that one."""
        add_title(session, type=TitleKind.SERIES, name_en="Held", imdb_id="tt0088763")
        film = add_title(session, type=TitleKind.MOVIE, name_en="Other", imdb_id=None)
        enricher = FakeEnricher(EnrichResult(metadata_patch={"imdb_id": "tt0088763"}))

        run_enrich(session, api, [enricher], self._ctx(http, settings), settings)

        assert film.imdb_id is None


class TestCorrectingAMislabelledName:
    """A name in the wrong script is not a name we have; it is one we mislabelled."""

    def _ctx(self, http: Any, settings: Settings) -> FetchContext:
        return FetchContext(source_key="enrich", http=http, settings=settings)

    def test_a_wrong_script_english_name_is_replaced(
        self, api: IngestClient, session: Session, settings: Settings, http: Any
    ) -> None:
        """Every pass fetched "Spirited Away" and threw it away: the column was not empty."""
        title = add_title(session, name_he=None, name_en="千と千尋の神隠し", tmdb_id=129)
        enricher = FakeEnricher(EnrichResult(metadata_patch={"name_en": "Spirited Away"}))

        run_enrich(session, api, [enricher], self._ctx(http, settings), settings)

        assert title.name_en == "Spirited Away"

    def test_a_real_english_name_is_still_never_overwritten(
        self, api: IngestClient, session: Session, settings: Settings, http: Any
    ) -> None:
        title = add_title(session, name_en="Fauda")
        enricher = FakeEnricher(EnrichResult(metadata_patch={"name_en": "Something Else"}))

        run_enrich(session, api, [enricher], self._ctx(http, settings), settings)

        assert title.name_en == "Fauda"

    def test_one_wrong_answer_is_not_traded_for_another(
        self, api: IngestClient, session: Session, settings: Settings, http: Any
    ) -> None:
        """The replacement has to be in the right script itself."""
        title = add_title(session, name_en="千と千尋の神隠し", tmdb_id=129)
        enricher = FakeEnricher(EnrichResult(metadata_patch={"name_en": "スピリット"}))

        run_enrich(session, api, [enricher], self._ctx(http, settings), settings)

        assert title.name_en == "千と千尋の神隠し"

    def test_hebrew_names_get_the_same_treatment(
        self, api: IngestClient, session: Session, settings: Settings, http: Any
    ) -> None:
        title = add_title(session, name_he="Fauda", name_en="Fauda")
        enricher = FakeEnricher(EnrichResult(metadata_patch={"name_he": "פאודה"}))

        run_enrich(session, api, [enricher], self._ctx(http, settings), settings)

        assert title.name_he == "פאודה"


class TestFindingTheMislabelled:
    def test_only_names_written_in_another_script(self, session: Session) -> None:
        japanese = add_title(session, name_en="千と千尋の神隠し", tmdb_id=129)
        add_title(session, name_en="Fauda", tmdb_id=2)
        add_title(session, name_en="Amélie", tmdb_id=3)

        assert [title.id for title in mislabelled_names(session)] == [japanese.id]

    def test_a_title_with_nobody_to_ask_is_left_out(self, session: Session) -> None:
        """An en-US request needs a TMDB id; without one there is no repair to make."""
        add_title(session, name_en="千と千尋の神隠し", tmdb_id=None)

        assert mislabelled_names(session) == []

    def test_the_batch_can_be_bounded(self, session: Session) -> None:
        for index in range(4):
            add_title(session, name_en=f"千と千尋{index}", tmdb_id=100 + index)

        assert len(mislabelled_names(session, limit=2)) == 2


class TestAPricePass:
    """A run made only of price enrichers walks offers, not the ratings queue.

    Against the real application, because the whole bug was in the seam: the
    fetcher asked the queue, the queue answered honestly - nothing is due - and
    the run finished in two seconds having priced nothing, on a catalog with
    seventeen thousand unpriced Apple offers in it.
    """

    def _ctx(self, http: Any, settings: Settings) -> FetchContext:
        return FetchContext(source_key="enrich", http=http, settings=settings)

    def _offer(self, session: Session, title: Title, **overrides: Any) -> Availability:
        source = session.scalars(select(Source).where(Source.key == "apple_tv_store")).first()
        if source is None:
            source = Source(
                key="apple_tv_store",
                name="Apple TV Store",
                kind=SourceKind.RENT_BUY,
                website_url="https://tv.apple.com/il",
            )
            session.add(source)
            session.flush()
        values: dict[str, Any] = {
            "title_id": title.id,
            "source_id": source.id,
            "offer_type": OfferType.RENT,
        }
        values.update(overrides)
        row = Availability(**values)
        session.add(row)
        session.commit()
        return row

    def _pricer(self, price: int = 1690) -> FakeEnricher:
        enricher = FakeEnricher(
            EnrichResult(
                offers=[
                    OfferFact(
                        source_key="apple_tv_store",
                        offer_type=OfferType.RENT,
                        price_minor=price,
                        price_currency="ILS",
                        deep_link_url="https://tv.apple.com/il/movie/x",
                    )
                ]
            ),
            key="apple_prices",
        )
        enricher.prices_for = "apple_tv_store"
        return enricher

    def test_it_prices_a_title_the_queue_would_not_hand_over(
        self, api: IngestClient, session: Session, settings: Settings, http: Any
    ) -> None:
        title = add_title(session)
        offer = self._offer(session, title)
        add_attempt(
            session,
            title,
            outcome=EnrichOutcome.NO_DATA,
            fruitless=10,
            due_at=utcnow() + dt.timedelta(days=365),
        )
        pricer = self._pricer()

        run_enrich(session, api, [pricer], self._ctx(http, settings), settings)

        assert [view.id for view in pricer.seen] == [title.id]
        session.refresh(offer)
        assert offer.price_minor == 1690

    def test_a_price_leaves_the_title_recorded_as_a_success(
        self, api: IngestClient, session: Session, settings: Settings, http: Any
    ) -> None:
        """Or the pass punishes itself: every title it priced backed off further
        than the last, until the whole catalog was a year from due."""
        title = add_title(session)
        self._offer(session, title)

        run_enrich(session, api, [self._pricer()], self._ctx(http, settings), settings)

        attempt = _attempt(session, title)
        assert attempt.outcome is EnrichOutcome.OK
        assert attempt.fruitless == 0

    def test_a_priced_title_is_not_asked_about_twice(
        self, api: IngestClient, session: Session, settings: Settings, http: Any
    ) -> None:
        """What makes the pass finite: the worklist is what is missing, and the
        first run takes it off the list."""
        self._offer(session, add_title(session))
        run_enrich(session, api, [self._pricer()], self._ctx(http, settings), settings)

        second = self._pricer()
        run_enrich(session, api, [second], self._ctx(http, settings), settings)

        assert second.seen == []

    def test_a_run_with_a_rating_provider_in_it_still_uses_the_queue(
        self, api: IngestClient, session: Session, settings: Settings, http: Any
    ) -> None:
        """Two enrichers, two questions, and only the queue covers both."""
        title = add_title(session)
        self._offer(session, title)
        add_attempt(session, title, due_at=utcnow() + dt.timedelta(days=365))
        due_now = add_title(session, name_he="שטיסל")
        rater = FakeEnricher(EnrichResult(ratings=[Rating(RatingProvider.SERET_VIEWERS, 8.0)]))

        run_enrich(session, api, [rater, self._pricer()], self._ctx(http, settings), settings)

        # The queue's answer, not the worklist's: the unpriced title is backed
        # off, and the one nobody has attempted is the only thing due.
        assert [view.id for view in rater.seen] == [due_now.id]


class TestSayingHowFarThroughTheBatchItIs:
    """An enrich run is the longest thing the fetcher does and used to say
    nothing at all between "enriching with: tmdb, rt" and the tally at the end.
    Unlike a sync it knows how much it was given, so it can say what is left."""

    def _ctx(self, http: Any, settings: Settings) -> FetchContext:
        return FetchContext(source_key="enrich", http=http, settings=settings)

    def _log(self, session: Session) -> str:
        run = session.scalars(
            select(FetchRun).where(FetchRun.phase == FetchPhase.ENRICH).order_by(FetchRun.id.desc())
        ).first()
        assert run is not None
        return run.log or ""

    def test_it_says_how_much_there_is_before_it_starts(
        self,
        api: IngestClient,
        session: Session,
        settings: Settings,
        http: Any,
        fetcher_logs_at_info: None,
    ) -> None:
        """The number that decides whether to wait for this or go to bed."""
        for index in range(12):
            add_title(session, name_he=f"סדרה {index}")

        run_enrich(
            session, api, [FakeEnricher(EnrichResult())], self._ctx(http, settings), settings
        )

        assert "12 title(s) due for enrichment" in self._log(session)

    def test_it_reports_its_position_in_the_batch(
        self,
        api: IngestClient,
        session: Session,
        settings: Settings,
        http: Any,
        fetcher_logs_at_info: None,
    ) -> None:
        for index in range(12):
            add_title(session, name_he=f"סדרה {index}")

        run_enrich(
            session, api, [FakeEnricher(EnrichResult())], self._ctx(http, settings), settings
        )

        # Of twelve, not just "10" - the share is the part that says whether to
        # wait, and a bare count never does.
        assert "enrich: 10 of 12 (83%)" in self._log(session)

    def test_it_says_what_it_is_finding_as_well_as_where_it_is(
        self,
        api: IngestClient,
        session: Session,
        settings: Settings,
        http: Any,
        fetcher_logs_at_info: None,
    ) -> None:
        """A run a third of the way through that has written nothing is a run
        worth interrupting, and the position alone would not say so."""
        for index in range(12):
            add_title(session, name_he=f"סדרה {index}")
        enricher = FakeEnricher(
            EnrichResult(
                ratings=[
                    Rating(provider=RatingProvider.SERET_VIEWERS, score_raw=8.0, vote_count=900)
                ]
            )
        )

        run_enrich(session, api, [enricher], self._ctx(http, settings), settings)

        assert "10 ratings" in self._log(session)

    def test_a_batch_too_short_to_report_on_still_works(
        self,
        api: IngestClient,
        session: Session,
        settings: Settings,
        http: Any,
        fetcher_logs_at_info: None,
    ) -> None:
        """Nothing to say is not the same as something going wrong."""
        add_title(session)

        tally = run_enrich(
            session, api, [FakeEnricher(EnrichResult())], self._ctx(http, settings), settings
        )

        assert tally.titles_seen == 1
        assert "1 title(s) due for enrichment" in self._log(session)

    def test_every_title_is_named_for_anybody_watching_it_work(
        self, api: IngestClient, session: Session, settings: Settings, http: Any, caplog: Any
    ) -> None:
        """At DEBUG - `eifo-fetch -v`. A batch of five thousand at INFO would
        bury the progress lines and spend the run row's whole log budget on a
        list of titles."""
        add_title(session, name_en="Fauda")
        enricher = FakeEnricher(EnrichResult(ratings=[Rating(RatingProvider.SERET_VIEWERS, 8.0)]))

        with caplog.at_level(logging.DEBUG, logger="eifo.fetch.enrich"):
            run_enrich(session, api, [enricher], self._ctx(http, settings), settings)

        said = caplog.text
        assert "enriching 'Fauda' (id 1)" in said
        assert "finding(s) offered" in said
