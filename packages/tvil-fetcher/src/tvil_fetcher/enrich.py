"""The enrichment pipeline: refresh policy, persistence, aggregation.

Enrichers report what they found; everything that touches the database happens
here, for the same reason source plugins never write: it keeps each provider
small enough to test from a recorded fixture.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tvil_core.enums import FetchPhase, FetchStatus, RatingProvider
from tvil_core.models import (
    AggregateScore,
    Availability,
    ExternalRating,
    FetchRun,
    Genre,
    Title,
    TitleGenre,
)
from tvil_core.settings import Settings
from tvil_core.types import utcnow
from tvil_fetcher.enrichers.base import Enricher, EnrichResult, Rating, TitleView
from tvil_fetcher.scores import RatingInput, aggregate, normalise
from tvil_fetcher.sources.base import FetchContext, TooManyErrorsError

logger = logging.getLogger("tvil.fetch.enrich")

#: Metadata fields an enricher may fill. Anything else in a patch is ignored,
#: so a provider cannot quietly write to columns it has no business setting.
PATCHABLE_FIELDS = frozenset(
    {
        "tmdb_id",
        "imdb_id",
        "name_he",
        "name_en",
        "overview_he",
        "overview_en",
        "year",
        "runtime_minutes",
        "seasons",
        "status",
        "poster_source_url",
    }
)


@dataclass(slots=True)
class EnrichResultTally:
    """What one enrichment run did."""

    titles_seen: int = 0
    ratings_written: int = 0
    metadata_updated: int = 0
    aggregates_computed: int = 0
    by_enricher: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def as_stats(self) -> dict[str, Any]:
        return {
            "titles_seen": self.titles_seen,
            "ratings_written": self.ratings_written,
            "metadata_updated": self.metadata_updated,
            "aggregates_computed": self.aggregates_computed,
            "by_enricher": self.by_enricher,
            "errors": self.errors,
            "error_count": len(self.errors),
        }


def titles_due(
    session: Session,
    settings: Settings,
    *,
    force: bool = False,
    limit: int | None = None,
) -> list[Title]:
    """Titles whose ratings are missing or stale.

    Titles currently available on some service are refreshed more often than
    ones nothing carries: those are the ones people are actually looking at.
    """
    batch = limit if limit is not None else settings.enrich.batch_size

    if force:
        return list(session.scalars(select(Title).order_by(Title.id).limit(batch)).all())

    now = utcnow()
    cold_cutoff = now - dt.timedelta(days=settings.enrich.refresh_days)
    hot_cutoff = now - dt.timedelta(days=settings.enrich.hot_refresh_days)

    hot_ids = {
        title_id
        for (title_id,) in session.execute(
            select(Availability.title_id).where(Availability.is_current.is_(True)).distinct()
        ).all()
    }
    freshest = _freshest_rating_by_title(session)

    due: list[Title] = []
    for title in session.scalars(select(Title).order_by(Title.id)).all():
        fetched_at = freshest.get(title.id)
        cutoff = hot_cutoff if title.id in hot_ids else cold_cutoff
        if fetched_at is None or fetched_at < cutoff:
            due.append(title)
        if len(due) >= batch:
            break
    return due


def _freshest_rating_by_title(session: Session) -> dict[int, dt.datetime]:
    """When each title was last enriched, by its most recent rating."""
    return {
        title_id: fetched_at
        for title_id, fetched_at in session.execute(
            select(ExternalRating.title_id, func.max(ExternalRating.fetched_at)).group_by(
                ExternalRating.title_id
            )
        ).all()
        if fetched_at is not None
    }


def enrich_titles(
    session: Session,
    enrichers: list[Enricher],
    ctx: FetchContext,
    settings: Settings,
    *,
    force: bool = False,
    limit: int | None = None,
) -> EnrichResultTally:
    """Run every enricher over the titles that are due, then recompute scores."""
    started_at = utcnow()
    tally = EnrichResultTally()
    status = FetchStatus.OK

    try:
        for title in titles_due(session, settings, force=force, limit=limit):
            tally.titles_seen += 1
            view = _view_of(title)
            for enricher in enrichers:
                _run_one(session, enricher, title, view, ctx, tally)
            _recompute(session, title, settings, tally)
            session.flush()
    except TooManyErrorsError as exc:
        logger.error("%s", exc)
        status = FetchStatus.FAILED
    except Exception:
        logger.exception("enrichment failed")
        status = FetchStatus.FAILED

    tally.errors = list(ctx.errors)
    session.add(
        FetchRun(
            phase=FetchPhase.ENRICH,
            started_at=started_at,
            finished_at=utcnow(),
            status=status,
            stats=tally.as_stats(),
        )
    )
    session.commit()
    return tally


def _run_one(
    session: Session,
    enricher: Enricher,
    title: Title,
    view: TitleView,
    ctx: FetchContext,
    tally: EnrichResultTally,
) -> None:
    """Apply one enricher to one title, tolerating provider failures."""
    try:
        result = enricher.enrich(view, ctx)
    except TooManyErrorsError:
        raise
    except Exception as exc:
        ctx.record_error(f"{enricher.key} failed for title {title.id}", exc=exc)
        return

    if result is None or result.is_empty:
        # A provider having nothing on a title is ordinary, not a failure.
        return

    ctx.record_success()
    written = _store_ratings(session, title, result.ratings, ctx)
    tally.ratings_written += written
    tally.by_enricher[enricher.key] = tally.by_enricher.get(enricher.key, 0) + written

    if _apply_patch(session, title, result):
        tally.metadata_updated += 1


def _store_ratings(
    session: Session,
    title: Title,
    ratings: list[Rating],
    ctx: FetchContext,
) -> int:
    """Upsert one row per (title, provider)."""
    stored = {rating.provider: rating for rating in title.ratings}
    written = 0
    now = utcnow()

    for rating in ratings:
        try:
            normalized = normalise(rating.provider, rating.score_raw)
        except ValueError as exc:
            # An out-of-scale score means the parser is wrong; storing it would
            # quietly skew the aggregate.
            ctx.record_error(f"rejected {rating.provider} score for title {title.id}", exc=exc)
            continue

        existing = stored.get(rating.provider)
        if existing is None:
            session.add(
                ExternalRating(
                    title_id=title.id,
                    provider=rating.provider,
                    score_raw=rating.score_raw,
                    score_normalized=normalized,
                    vote_count=rating.vote_count,
                    url=rating.url,
                    fetched_at=now,
                )
            )
        else:
            existing.score_raw = rating.score_raw
            existing.score_normalized = normalized
            existing.vote_count = rating.vote_count
            existing.url = rating.url or existing.url
            existing.fetched_at = now
        written += 1

    return written


def _apply_patch(session: Session, title: Title, result: EnrichResult) -> bool:
    """Fill empty fields only; never overwrite what is already known."""
    changed = False

    for field_name, value in result.metadata_patch.items():
        if field_name == "genres":
            changed |= _apply_genres(session, title, value)
            continue
        if field_name not in PATCHABLE_FIELDS or value in (None, ""):
            continue
        if getattr(title, field_name, None) in (None, ""):
            setattr(title, field_name, value)
            changed = True

    return changed


def _apply_genres(session: Session, title: Title, genres: Any) -> bool:
    """Attach genres, creating any the catalog has not seen before."""
    if not isinstance(genres, list) or title.genres:
        return False

    changed = False
    for entry in genres:
        if not isinstance(entry, dict) or not entry.get("tmdb_id"):
            continue
        genre = _get_or_create_genre(session, entry)
        if genre is not None:
            session.add(TitleGenre(title_id=title.id, genre_id=genre.id))
            changed = True
    return changed


def _get_or_create_genre(session: Session, entry: dict[str, Any]) -> Genre | None:
    tmdb_id = int(entry["tmdb_id"])
    genre = session.scalar(select(Genre).where(Genre.tmdb_id == tmdb_id))
    if genre is not None:
        if not genre.name_he and entry.get("name_he"):
            genre.name_he = entry["name_he"]
        return genre

    name_en = entry.get("name_en")
    if not name_en:
        return None

    genre = Genre(tmdb_id=tmdb_id, name_en=name_en, name_he=entry.get("name_he"))
    session.add(genre)
    session.flush()
    return genre


def _recompute(
    session: Session,
    title: Title,
    settings: Settings,
    tally: EnrichResultTally,
) -> None:
    """Recompute a title's aggregate from whatever ratings it now has."""
    session.flush()
    ratings = session.scalars(
        select(ExternalRating).where(ExternalRating.title_id == title.id)
    ).all()
    if not ratings:
        return

    computed = aggregate(
        [
            RatingInput(
                provider=RatingProvider(rating.provider),
                score_normalized=rating.score_normalized,
                vote_count=rating.vote_count,
            )
            for rating in ratings
        ],
        settings.scores,
    )

    stored = session.get(AggregateScore, title.id)
    if stored is None:
        session.add(
            AggregateScore(
                title_id=title.id,
                score=computed.score,
                score_israeli=computed.score_israeli,
                components=computed.components,
            )
        )
    else:
        stored.score = computed.score
        stored.score_israeli = computed.score_israeli
        stored.components = computed.components
        stored.computed_at = utcnow()

    tally.aggregates_computed += 1


def recompute_all_aggregates(session: Session, settings: Settings) -> int:
    """Rescore every title that has ratings.

    Needed after the IMDb bulk pass, which writes ratings without going through
    the per-title path that would otherwise rescore as it goes.
    """
    tally = EnrichResultTally()
    rated_ids = [
        title_id
        for (title_id,) in session.execute(select(ExternalRating.title_id).distinct()).all()
    ]

    for title_id in rated_ids:
        title = session.get(Title, title_id)
        if title is not None:
            _recompute(session, title, settings, tally)

    session.commit()
    return tally.aggregates_computed


def _view_of(title: Title) -> TitleView:
    return TitleView(
        id=title.id,
        kind=title.type,
        name_he=title.name_he,
        name_en=title.name_en,
        year=title.year,
        tmdb_id=title.tmdb_id,
        imdb_id=title.imdb_id,
    )
