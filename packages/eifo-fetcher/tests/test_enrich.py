"""The enrichment pipeline: refresh policy, persistence, gap-filling."""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from eifo_core.enums import (
    FetchPhase,
    FetchStatus,
    OfferType,
    RatingProvider,
    SourceKind,
    TitleKind,
)
from eifo_core.models import (
    AggregateScore,
    Availability,
    ExternalRating,
    FetchRun,
    Genre,
    Source,
    Title,
)
from eifo_core.settings import Settings
from eifo_core.types import utcnow
from eifo_fetcher.enrich import enrich_titles, recompute_all_aggregates, titles_due
from eifo_fetcher.enrichers.base import Enricher, EnrichResult, Rating, TitleView
from eifo_fetcher.sources.base import FetchContext


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
    def test_a_title_with_no_ratings_is_due(self, session: Session, settings: Settings) -> None:
        add_title(session)

        assert len(titles_due(session, settings)) == 1

    def test_a_freshly_rated_title_is_not_due(self, session: Session, settings: Settings) -> None:
        add_rating(session, add_title(session))

        assert titles_due(session, settings) == []

    def test_a_stale_title_is_due(self, session: Session, settings: Settings) -> None:
        title = add_title(session)
        add_rating(session, title, fetched_at=utcnow() - dt.timedelta(days=30))

        assert len(titles_due(session, settings)) == 1

    def test_available_titles_are_refreshed_sooner(
        self, session: Session, settings: Settings
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
        # Older than the hot cutoff (3d) but newer than the cold one (14d).
        stamp = utcnow() - dt.timedelta(days=5)
        add_rating(session, hot, fetched_at=stamp)
        add_rating(session, cold, fetched_at=stamp)

        due = titles_due(session, settings)

        assert [title.id for title in due] == [hot.id]

    def test_force_ignores_freshness(self, session: Session, settings: Settings) -> None:
        add_rating(session, add_title(session))

        assert len(titles_due(session, settings, force=True)) == 1

    def test_the_batch_size_bounds_a_run(self, session: Session, settings: Settings) -> None:
        for index in range(5):
            add_title(session, name_he=f"תוכנית {index}")

        assert len(titles_due(session, settings, limit=2)) == 2


class TestEnrichTitles:
    def _ctx(self, http: Any, settings: Settings) -> FetchContext:
        return FetchContext(source_key="enrich", http=http, settings=settings)

    def test_stores_a_returned_rating(
        self, session: Session, settings: Settings, http: Any
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

        tally = enrich_titles(session, [enricher], self._ctx(http, settings), settings)

        rating = session.scalars(select(ExternalRating)).one()
        assert rating.provider is RatingProvider.SERET_VIEWERS
        assert rating.score_raw == 8.9
        assert rating.score_normalized == 89
        assert rating.url == "https://www.seret.co.il/movies/1"
        assert tally.ratings_written == 1

    def test_re_enriching_updates_rather_than_duplicates(
        self, session: Session, settings: Settings, http: Any
    ) -> None:
        add_title(session)
        first = FakeEnricher(EnrichResult(ratings=[Rating(RatingProvider.SERET_VIEWERS, 7.0)]))
        enrich_titles(session, [first], self._ctx(http, settings), settings)

        second = FakeEnricher(EnrichResult(ratings=[Rating(RatingProvider.SERET_VIEWERS, 9.0)]))
        enrich_titles(session, [second], self._ctx(http, settings), settings, force=True)

        rating = session.scalars(select(ExternalRating)).one()
        assert rating.score_raw == 9.0

    def test_an_out_of_scale_score_is_rejected_not_stored(
        self, session: Session, settings: Settings, http: Any
    ) -> None:
        """A percentage read as a /10 score would silently skew the aggregate."""
        add_title(session)
        enricher = FakeEnricher(EnrichResult(ratings=[Rating(RatingProvider.SERET_VIEWERS, 89.0)]))
        ctx = self._ctx(http, settings)

        enrich_titles(session, [enricher], ctx, settings)

        assert session.scalars(select(ExternalRating)).all() == []
        assert ctx.error_count == 1

    def test_a_provider_with_nothing_is_not_an_error(
        self, session: Session, settings: Settings, http: Any
    ) -> None:
        """Plenty of Israeli titles simply do not exist on foreign sites."""
        add_title(session)
        ctx = self._ctx(http, settings)

        enrich_titles(session, [FakeEnricher(None)], ctx, settings)

        assert ctx.error_count == 0
        assert session.scalars(select(ExternalRating)).all() == []

    def test_one_failing_provider_does_not_stop_the_others(
        self, session: Session, settings: Settings, http: Any
    ) -> None:
        add_title(session)
        broken = FakeEnricher(error=RuntimeError("site down"), key="broken")
        working = FakeEnricher(
            EnrichResult(ratings=[Rating(RatingProvider.SERET_VIEWERS, 8.0)]), key="working"
        )
        ctx = self._ctx(http, settings)

        enrich_titles(session, [broken, working], ctx, settings)

        assert len(session.scalars(select(ExternalRating)).all()) == 1
        assert ctx.error_count == 1

    def test_records_a_fetch_run(self, session: Session, settings: Settings, http: Any) -> None:
        add_title(session)
        enricher = FakeEnricher(EnrichResult(ratings=[Rating(RatingProvider.SERET_VIEWERS, 8.0)]))

        enrich_titles(session, [enricher], self._ctx(http, settings), settings)

        run = session.scalars(select(FetchRun).where(FetchRun.phase == FetchPhase.ENRICH)).one()
        assert run.status is FetchStatus.OK
        assert run.stats["titles_seen"] == 1
        assert run.stats["by_enricher"] == {"fake": 1}


class TestMetadataPatch:
    def _ctx(self, http: Any, settings: Settings) -> FetchContext:
        return FetchContext(source_key="enrich", http=http, settings=settings)

    def test_fills_an_empty_field(self, session: Session, settings: Settings, http: Any) -> None:
        add_title(session, name_en=None)
        enricher = FakeEnricher(EnrichResult(metadata_patch={"name_en": "Fauda"}))

        enrich_titles(session, [enricher], self._ctx(http, settings), settings)

        assert session.scalars(select(Title)).one().name_en == "Fauda"

    def test_never_overwrites_a_known_value(
        self, session: Session, settings: Settings, http: Any
    ) -> None:
        """A provider's guess must not displace an answer we already trust."""
        add_title(session, name_en="Fauda")
        enricher = FakeEnricher(EnrichResult(metadata_patch={"name_en": "Something Else"}))

        enrich_titles(session, [enricher], self._ctx(http, settings), settings)

        assert session.scalars(select(Title)).one().name_en == "Fauda"

    def test_ignores_fields_outside_the_allowed_set(
        self, session: Session, settings: Settings, http: Any
    ) -> None:
        """An enricher has no business writing to bookkeeping columns."""
        title = add_title(session)
        original = title.created_at
        enricher = FakeEnricher(
            EnrichResult(metadata_patch={"created_at": "1999-01-01", "poster_path": "x/y.jpg"})
        )

        enrich_titles(session, [enricher], self._ctx(http, settings), settings)

        stored = session.scalars(select(Title)).one()
        assert stored.created_at == original
        assert stored.poster_path is None

    def test_creates_and_attaches_genres(
        self, session: Session, settings: Settings, http: Any
    ) -> None:
        add_title(session)
        enricher = FakeEnricher(
            EnrichResult(
                metadata_patch={"genres": [{"tmdb_id": 18, "name_en": "Drama", "name_he": "דרמה"}]}
            )
        )

        enrich_titles(session, [enricher], self._ctx(http, settings), settings)

        genre = session.scalars(select(Genre)).one()
        assert genre.name_en == "Drama"
        assert genre.name_he == "דרמה"
        assert session.scalars(select(Title)).one().genres == [genre]

    def test_reuses_an_existing_genre(
        self, session: Session, settings: Settings, http: Any
    ) -> None:
        session.add(Genre(tmdb_id=18, name_en="Drama"))
        add_title(session)
        enricher = FakeEnricher(
            EnrichResult(metadata_patch={"genres": [{"tmdb_id": 18, "name_en": "Drama"}]})
        )

        enrich_titles(session, [enricher], self._ctx(http, settings), settings)

        assert len(session.scalars(select(Genre)).all()) == 1


class TestAggregation:
    def _ctx(self, http: Any, settings: Settings) -> FetchContext:
        return FetchContext(source_key="enrich", http=http, settings=settings)

    def test_computes_an_aggregate_from_two_providers(
        self, session: Session, settings: Settings, http: Any
    ) -> None:
        title = add_title(session)
        add_rating(session, title, provider=RatingProvider.IMDB, score_normalized=80)
        enricher = FakeEnricher(EnrichResult(ratings=[Rating(RatingProvider.TMDB, 6.0)]))

        enrich_titles(session, [enricher], self._ctx(http, settings), settings, force=True)

        aggregate = session.scalars(select(AggregateScore)).one()
        assert aggregate.score == 75
        assert set(aggregate.components) == {"imdb", "tmdb"}

    def test_recompute_all_rescores_every_rated_title(
        self, session: Session, settings: Settings
    ) -> None:
        """The IMDb bulk pass writes ratings directly, so scores need refreshing."""
        title = add_title(session)
        add_rating(session, title, provider=RatingProvider.IMDB, score_normalized=90)
        add_rating(session, title, provider=RatingProvider.TMDB, score_normalized=70)

        computed = recompute_all_aggregates(session, settings)

        assert computed == 1
        assert session.scalars(select(AggregateScore)).one().score == 85

    def test_a_title_with_no_ratings_gets_no_aggregate_row(
        self, session: Session, settings: Settings, http: Any
    ) -> None:
        add_title(session)

        enrich_titles(session, [FakeEnricher(None)], self._ctx(http, settings), settings)

        assert session.scalars(select(AggregateScore)).all() == []
