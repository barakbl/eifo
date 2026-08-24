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

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from eifo_core.enums import EnrichOutcome, FetchPhase, FetchStatus, RatingProvider
from eifo_core.models import (
    AggregateScore,
    Availability,
    EnrichAttempt,
    ExternalRating,
    Genre,
    Title,
    TitleGenre,
)
from eifo_core.settings import Settings
from eifo_core.types import utcnow
from eifo_fetcher.enrichers.base import Enricher, EnrichResult, Rating, TitleView
from eifo_fetcher.people import apply_credits
from eifo_fetcher.runs import close_run, open_run
from eifo_fetcher.scores import RatingInput, aggregate, normalise
from eifo_fetcher.sources.base import FetchContext, TooManyErrorsError, plausible_year

logger = logging.getLogger("eifo.fetch.enrich")

#: Titles enriched between commits.
#:
#: SQLite allows one writer at a time, and every title here means network calls.
#: One transaction for the batch held the write lock for the whole run - up to
#: twenty-nine minutes, against a thirty-second busy timeout - so anything else
#: touching the database during a nightly enrich waited half a minute and then
#: failed. Committing as we go bounds that, and bounds what a crash loses.
COMMIT_EVERY = 25

#: Aggregates recomputed between commits. Cheaper per row than enrichment -
#: no network - so a larger batch still keeps each lock short.
AGGREGATE_COMMIT_EVERY = 200

#: Patchable fields the schema keeps unique, so a second writer collides.
_UNIQUE_FIELDS = frozenset({"tmdb_id", "imdb_id"})

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
        "original_language",
        "origin_countries",
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
    """Titles it is worth putting through the enrichers now.

    Least recently attempted first, with never attempted counting as infinitely
    long ago, so a run always advances instead of re-reading the head of the
    catalog. When a title next falls due is decided at the end of its last
    attempt: a rated one comes back on the refresh schedule, and one nobody
    could rate waits progressively longer.
    """
    batch = limit if limit is not None else settings.enrich.batch_size

    if force:
        return list(session.scalars(select(Title).order_by(Title.id).limit(batch)).all())

    statement = (
        select(Title)
        .outerjoin(EnrichAttempt, EnrichAttempt.title_id == Title.id)
        .where(or_(EnrichAttempt.title_id.is_(None), EnrichAttempt.due_at <= utcnow()))
        # Never attempted sorts first: is_(None) is true there, and true is the
        # high value, so descending puts it at the front. Spelled out rather
        # than left to the dialect, which may sort NULLs either way.
        .order_by(
            EnrichAttempt.attempted_at.is_(None).desc(),
            EnrichAttempt.attempted_at,
            Title.id,
        )
        .limit(batch)
    )
    return list(session.scalars(statement).all())


def _record_attempt(
    session: Session,
    title: Title,
    settings: Settings,
    *,
    outcome: EnrichOutcome,
) -> None:
    """Write down that this title was tried, and when it is worth trying again."""
    now = utcnow()
    attempt = session.get(EnrichAttempt, title.id)
    previous = attempt.fruitless if attempt is not None else 0
    fruitless = 0 if outcome is EnrichOutcome.OK else previous + 1
    due_at = now + _wait_after(session, title, settings, outcome=outcome, fruitless=fruitless)

    if attempt is None:
        session.add(
            EnrichAttempt(
                title_id=title.id,
                attempted_at=now,
                outcome=outcome,
                fruitless=fruitless,
                due_at=due_at,
            )
        )
        return

    attempt.attempted_at = now
    attempt.outcome = outcome
    attempt.fruitless = fruitless
    attempt.due_at = due_at


def _wait_after(
    session: Session,
    title: Title,
    settings: Settings,
    *,
    outcome: EnrichOutcome,
    fruitless: int,
) -> dt.timedelta:
    """How long to leave a title alone after this outcome.

    A title that was rated comes back on the ordinary refresh schedule, sooner
    if some service currently carries it, since that is the one somebody may be
    looking at tonight. Everything else backs off, doubling with each
    consecutive empty-handed attempt, so the titles no provider covers cannot
    crowd out the ones worth asking about.
    """
    enrich = settings.enrich
    if outcome is EnrichOutcome.OK:
        fresher = _is_available(session, title.id)
        return dt.timedelta(days=enrich.hot_refresh_days if fresher else enrich.refresh_days)

    base = enrich.retry_error_days if outcome is EnrichOutcome.ERROR else enrich.retry_days
    return dt.timedelta(days=min(base * 2 ** max(fruitless - 1, 0), enrich.retry_max_days))


def _is_available(session: Session, title_id: int) -> bool:
    """Whether any service currently carries this title."""
    found = session.scalar(
        select(Availability.title_id)
        .where(Availability.title_id == title_id, Availability.is_current.is_(True))
        .limit(1)
    )
    return found is not None


def _outcome_of(title: Title, *, written: int, errored: bool) -> EnrichOutcome:
    """Read the outcome off what one title's pass through the enrichers produced.

    The order matters: a rating written is a success whatever else went wrong,
    and a provider failure says nothing about whether the title is rateable, so
    it outranks the two empty-handed verdicts below it.
    """
    if written:
        return EnrichOutcome.OK
    if errored:
        return EnrichOutcome.ERROR
    if title.tmdb_id is not None or title.imdb_id is not None:
        return EnrichOutcome.NO_DATA
    return EnrichOutcome.NO_MATCH


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
    run = open_run(session, phase=FetchPhase.ENRICH, started_at=started_at)
    fatal: str | None = None

    try:
        for index, title in enumerate(titles_due(session, settings, force=force, limit=limit), 1):
            tally.titles_seen += 1
            view = _view_of(title)
            written = 0
            errored = False
            for enricher in enrichers:
                found = _run_one(session, enricher, title, view, ctx, tally)
                if found is None:
                    errored = True
                else:
                    written += found
            _recompute(session, title, settings, tally)
            # Before the flush, so a title that yielded nothing still says so:
            # the queue has to learn from the attempts that found nothing, or
            # it spends every run on the same titles.
            _record_attempt(
                session,
                title,
                settings,
                outcome=_outcome_of(title, written=written, errored=errored),
            )
            session.flush()
            if index % COMMIT_EVERY == 0:
                session.commit()
    except TooManyErrorsError as exc:
        logger.error("%s", exc)
        status = FetchStatus.FAILED
        fatal = f"{type(exc).__name__}: {exc}"
        session.rollback()
    except Exception as exc:
        logger.exception("enrichment failed")
        status = FetchStatus.FAILED
        fatal = f"{type(exc).__name__}: {exc}"
        # Without this the session is left needing one, and recording the
        # failure would itself raise - losing the row that explains the run.
        session.rollback()

    tally.errors = list(ctx.errors)
    if fatal is not None:
        tally.errors.append(f"fatal: {fatal}")
    close_run(session, run, status=status, stats=tally.as_stats())
    return tally


def _run_one(
    session: Session,
    enricher: Enricher,
    title: Title,
    view: TitleView,
    ctx: FetchContext,
    tally: EnrichResultTally,
) -> int | None:
    """Apply one enricher to one title, tolerating provider failures.

    Returns:
        How many ratings it wrote, or None if the provider itself failed - a
        distinction the caller needs, because "nobody rates this title" and
        "this provider is down" deserve different waits before trying again.
    """
    try:
        result = enricher.enrich(view, ctx)
    except TooManyErrorsError:
        raise
    except Exception as exc:
        ctx.record_error(f"{enricher.key} failed for title {title.id}", exc=exc)
        return None

    if result is None or result.is_empty:
        # A provider having nothing on a title is ordinary, not a failure.
        return 0

    ctx.record_success()
    written = _store_ratings(session, title, result.ratings, ctx)
    tally.ratings_written += written
    tally.by_enricher[enricher.key] = tally.by_enricher.get(enricher.key, 0) + written

    if _apply_patch(session, title, result, source=enricher.key):
        tally.metadata_updated += 1
    return written


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


def _apply_patch(session: Session, title: Title, result: EnrichResult, *, source: str) -> bool:
    """Fill empty fields only; never overwrite what is already known."""
    changed = False

    for field_name, value in result.metadata_patch.items():
        if field_name == "genres":
            changed |= _apply_genres(session, title, value)
            continue
        if field_name == "credits":
            changed |= _apply_credits(session, title, value, source=source)
            continue
        if field_name == "year":
            value = plausible_year(value)
        if field_name not in PATCHABLE_FIELDS or value in (None, ""):
            continue
        if field_name in _UNIQUE_FIELDS and _already_taken(session, field_name, value, title):
            continue
        if getattr(title, field_name, None) in (None, ""):
            setattr(title, field_name, value)
            changed = True

    return changed


def _already_taken(session: Session, field_name: str, value: Any, title: Title) -> bool:
    """Whether another title already holds this external id.

    Both id columns are unique, so writing one another title owns raises on the
    next flush and takes the whole run's remaining work with it. It also means
    something worth knowing: two titles the enricher believes are the same work.
    Recording that is the dedupe tool's job, so this only declines to write and
    says so.
    """
    column = getattr(Title, field_name)
    owner = session.scalar(select(Title.id).where(column == value, Title.id != title.id))
    if owner is None:
        return False

    logger.warning(
        "title %s and title %s both look like %s=%r; leaving it on %s",
        title.id,
        owner,
        field_name,
        value,
        owner,
    )
    return True


def _apply_credits(session: Session, title: Title, entries: Any, *, source: str) -> bool:
    """Attach who made this, crediting whoever said so."""
    if not isinstance(entries, list):
        return False
    return apply_credits(session, title, entries, source=source) > 0


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

    for index, title_id in enumerate(rated_ids, 1):
        title = session.get(Title, title_id)
        if title is not None:
            _recompute(session, title, settings, tally)
        # Thousands of titles in one transaction is the same write-lock problem
        # as the enrich loop, just after the IMDb pass rather than during it.
        if index % AGGREGATE_COMMIT_EVERY == 0:
            session.commit()

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
