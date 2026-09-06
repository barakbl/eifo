"""The enrichment pipeline: refresh policy, persistence, aggregation.

Enrichers report what they found; everything that touches the database happens
here, for the same reason source plugins never write: it keeps each provider
small enough to test from a recorded fixture.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from eifo_core.enriching import (
    _describe,
    apply_patch,
    outcome_of,
    recompute,
    record_attempt,
    store_ratings,
    titles_due,
    view_of,
)
from eifo_core.enums import FetchPhase, FetchStatus
from eifo_core.models import (
    ExternalRating,
    Title,
)
from eifo_core.settings import Settings
from eifo_core.types import utcnow
from eifo_fetcher.enrichers.base import Enricher, TitleView
from eifo_fetcher.progress import ProgressTicker, position, remaining
from eifo_fetcher.progress import tally as tally_of
from eifo_fetcher.runs import capture_log, close_run, open_run
from eifo_fetcher.sources.base import FetchContext, TooManyErrorsError

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


def apply_rate_limits(
    enrichers: list[Enricher],
    ctx: FetchContext,
    settings: Settings,
) -> None:
    """Set each scraped provider's pace before any of them is asked anything.

    Here rather than inside the enrichers because that is where it actually
    happens: an enricher calling ``ctx.apply_rate_limit`` resolved its rate
    from ``[sources.enrich]``, a section that exists nowhere, so the call set
    nothing and every scraped provider ran at the client-wide default.
    """
    for enricher in enrichers:
        if enricher.host is None:
            continue
        rps = settings.enrich.rate_limit_for(enricher.key, enricher.default_rate_limit_rps)
        if rps is None:
            continue
        ctx.http.rate_limiter.set_host_rate(enricher.host, rps)
        logger.info("%s reads %s at %.2f requests/second", enricher.key, enricher.host, rps)


def enrich_titles(
    session: Session,
    enrichers: list[Enricher],
    ctx: FetchContext,
    settings: Settings,
    *,
    force: bool = False,
    limit: int | None = None,
    titles: list[Title] | None = None,
) -> EnrichResultTally:
    """Run every enricher over the titles that are due, then recompute scores.

    Args:
        titles: enrich exactly these, instead of whatever is due. For repairs
            that know which titles they are about, and for the tests.
    """
    started_at = utcnow()
    tally = EnrichResultTally()
    status = FetchStatus.OK
    run = open_run(session, phase=FetchPhase.ENRICH, started_at=started_at)
    fatal: str | None = None

    apply_rate_limits(enrichers, ctx, settings)

    with capture_log() as captured:
        try:
            due = (
                titles
                if titles is not None
                else titles_due(session, settings, force=force, limit=limit)
            )
            # Said before the first title, because it is the number that decides
            # whether to wait for this or go to bed: a run with nine titles due
            # and a run with five thousand look identical until it is over.
            logger.info("%d title(s) due for enrichment", len(due))
            ticker = ProgressTicker()
            began = time.monotonic()

            for index, title in enumerate(due, 1):
                tally.titles_seen += 1
                view = view_of(title)
                # Every title by name, for anybody who wants to watch it work or
                # to find which one a provider choked on. At DEBUG because a
                # batch of five thousand would otherwise bury the progress lines
                # and spend the run row's whole log budget on a list of titles.
                logger.debug("enriching %s", _describe(title))
                written = 0
                errored = False
                for enricher in enrichers:
                    found = _run_one(session, enricher, title, view, ctx, tally)
                    if found is None:
                        errored = True
                    else:
                        written += found
                if recompute(session, title, settings):
                    tally.aggregates_computed += 1
                # Before the flush, so a title that yielded nothing still says so:
                # the queue has to learn from the attempts that found nothing, or
                # it spends every run on the same titles.
                record_attempt(
                    session,
                    title,
                    settings,
                    outcome=outcome_of(title, written=written, errored=errored),
                )
                session.flush()
                if index % COMMIT_EVERY == 0:
                    session.commit()

                logger.debug("%s: %d rating(s) written", _describe(title), written)
                if ticker.due(index):
                    logger.info("%s", _progress(tally, index, len(due), began))
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

    close_run(session, run, status=status, stats=tally.as_stats(), log=captured.text())
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
    written = store_ratings(
        session,
        title,
        result.ratings,
        lambda message, exc: ctx.record_error(message, exc=exc),
    )
    tally.ratings_written += written
    tally.by_enricher[enricher.key] = tally.by_enricher.get(enricher.key, 0) + written

    if apply_patch(session, title, result, source=enricher.key):
        tally.metadata_updated += 1
    return written


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
    # Cheap per row and there are tens of thousands of them, so this is minutes
    # of a phase that has already run for an hour - and the last minutes of a
    # long run are exactly when somebody is wondering whether to kill it.
    logger.info("rescoring %d rated title(s)", len(rated_ids))
    ticker = ProgressTicker()
    began = time.monotonic()

    for index, title_id in enumerate(rated_ids, 1):
        title = session.get(Title, title_id)
        if title is not None and recompute(session, title, settings):
            tally.aggregates_computed += 1
        # Thousands of titles in one transaction is the same write-lock problem
        # as the enrich loop, just after the IMDb pass rather than during it.
        if index % AGGREGATE_COMMIT_EVERY == 0:
            session.commit()
        if ticker.due(index):
            logger.info(
                "rescoring: %s%s",
                position(index, len(rated_ids)),
                _tail(remaining(index, len(rated_ids), time.monotonic() - began)),
            )

    session.commit()
    return tally.aggregates_computed


def _progress(tally: EnrichResultTally, done: int, total: int, began: float) -> str:
    """One line saying how far into the batch this is, and to what effect.

    The position answers "should I wait for this"; the tally answers "is it
    doing anything" - a run that is a third of the way through and has written
    no ratings at all is a run worth interrupting.
    """
    return "enrich: {}{} - {}".format(
        position(done, total),
        _tail(remaining(done, total, time.monotonic() - began)),
        tally_of(
            ratings=tally.ratings_written,
            metadata_updated=tally.metadata_updated,
            errors=len(tally.errors),
        ),
    )


def _tail(estimate: str | None) -> str:
    """An estimate as a clause, or nothing at all when there is none to give."""
    return f", {estimate}" if estimate else ""
