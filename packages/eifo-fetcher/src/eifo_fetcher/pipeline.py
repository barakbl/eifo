"""The sync pipeline: fetch, match, upsert, sweep, record.

Two rules here exist to keep the catalog honest when a scraper misbehaves:

* **Two strikes** - availability is only retired after an item has been missing
  from two consecutive *successful* syncs, so one flaky run never expires a
  catalog.
* **The volume guard** - a sync returning far less than the previous successful
  run is treated as a broken parser, not as mass removal: the run is recorded as
  ``aborted_suspicious`` and no sweep happens.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from eifo_core.catalog import (
    expire_reviews,
    looks_truncated,
    sweep_source,
    title_count,
    upsert_availability,
    upsert_source,
)
from eifo_core.enums import FetchPhase, FetchStatus, OfferType
from eifo_core.match import MatchMethod, MatchStats, TitleMatcher
from eifo_core.models import Availability, Source
from eifo_core.people import apply_credits
from eifo_core.types import utcnow
from eifo_fetcher.progress import ProgressTicker, tally
from eifo_fetcher.runs import RunLogCapture, capturing, close_run, new_capture, open_run
from eifo_fetcher.sources.base import (
    FetchContext,
    RawItem,
    SourceInfo,
    SourcePlugin,
    TooManyErrorsError,
)
from eifo_fetcher.tmdb import TmdbClient

logger = logging.getLogger("eifo.fetch.pipeline")

#: Consecutive successful syncs an item may be missing from before retirement.
MISS_LIMIT = 2
#: A sync returning less than this share of the previous run is assumed broken.
VOLUME_GUARD_RATIO = 0.20
#: Below this many items the ratio is noise, so the guard stays out of the way.
VOLUME_GUARD_MIN_ITEMS = 50
#: Items ingested between commits.
#:
#: SQLite allows one writer at a time. Matching makes network calls, so holding
#: a single transaction for a whole source would keep the write lock for minutes
#: and lock out anything else touching the database - including the next phase
#: of the same run. Committing as we go keeps each lock short.
COMMIT_EVERY = 200


@dataclass(slots=True)
class SyncResult:
    """What one source's sync did."""

    source_key: str
    status: FetchStatus
    items_seen: int = 0
    availability_created: int = 0
    availability_updated: int = 0
    titles_created: int = 0
    retired: int = 0
    reviews_expired: int = 0
    errors: list[str] = field(default_factory=list)
    matched_by: dict[str, int] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status is FetchStatus.OK

    def as_stats(self) -> dict[str, Any]:
        return {
            "items_seen": self.items_seen,
            "reviews_expired": self.reviews_expired,
            "availability_created": self.availability_created,
            "availability_updated": self.availability_updated,
            "titles_created": self.titles_created,
            "retired": self.retired,
            "matched_by": self.matched_by,
            "errors": self.errors,
            "error_count": len(self.errors),
        }


def sync_source(
    session: Session,
    plugin: SourcePlugin,
    info: SourceInfo,
    ctx: FetchContext,
    *,
    tmdb: TmdbClient | None = None,
    items: Iterable[RawItem] | None = None,
    capture: RunLogCapture | None = None,
) -> SyncResult:
    """Sync one source end to end and record a ``fetch_runs`` row.

    Args:
        tmdb: enables the matcher's TMDB lookup; without it the matcher falls
            back to external ids and local fuzzy comparison only.
        items: pre-fetched items, from :class:`~eifo_fetcher.prefetch.Prefetcher`
            or from a test; normally the plugin is asked to fetch them here.
        capture: a log capture already collecting for this source. The
            prefetcher opens one before it starts reading, which is how lines
            logged during the fetch reach the row for the source that logged
            them rather than whichever row happened to be open at the time.
    """
    started_at = utcnow()
    source = upsert_source(session, info)
    session.flush()

    # Opened before any work, so a sync that dies mid-flight leaves a row
    # saying it started rather than no row at all.
    run = open_run(session, phase=FetchPhase.SYNC, source_key=info.key, started_at=started_at)
    result = SyncResult(source_key=info.key, status=FetchStatus.OK)
    matcher = TitleMatcher(session, tmdb=tmdb, stats=MatchStats())

    fatal: str | None = None
    # Everything this source says goes into its own row. "mako returned nothing"
    # used to be answerable only by running it again and watching.
    captured = capture if capture is not None else new_capture()
    with capturing(captured):
        try:
            stream = items if items is not None else plugin.fetch(ctx)
            _ingest(session, stream, source, matcher, result, ctx, started_at)
        except TooManyErrorsError as exc:
            logger.error("%s", exc)
            result.status = FetchStatus.FAILED
            fatal = f"{type(exc).__name__}: {exc}"
            session.rollback()
        except Exception as exc:
            logger.exception("source %r failed", info.key)
            result.status = FetchStatus.FAILED
            # Whatever ended the run goes into the row. Without this a failed
            # sync records errors: [] - it says that it failed and nothing
            # about why, which is the one question anyone reading it will have.
            fatal = f"{type(exc).__name__}: {exc}"
            # A failure mid-flush leaves the session needing a rollback; without
            # one even recording the failure would raise, turning a bad source
            # into a crashed run.
            session.rollback()

        result.errors = list(ctx.errors)
        if fatal is not None:
            result.errors.append(f"fatal: {fatal}")
        result.matched_by = matcher.stats.as_dict()

        if result.status is FetchStatus.OK and looks_truncated(
            session, info.key, result.items_seen
        ):
            result.status = FetchStatus.ABORTED_SUSPICIOUS
            logger.error(
                "%s returned %d items, far below its previous run; assuming a broken "
                "parser and skipping the sweep",
                info.key,
                result.items_seen,
            )

        # Only a run we believe swept: a failure would retire a live catalog.
        if result.status is FetchStatus.OK:
            result.retired = sweep_source(session, source, run_started_at=started_at)
            # A park is rewritten every time a sync sees the item again, so one
            # older than this run is an item the source has stopped listing.
            # Nobody should be asked about a listing that is gone.
            result.reviews_expired = expire_reviews(session, info.key, before=started_at)

    # Outside the capture: the log is what the run said, and this is the run
    # being written down.
    close_run(session, run, status=result.status, stats=result.as_stats(), log=captured.text())
    return result


def _ingest(
    session: Session,
    items: Iterable[RawItem],
    source: Source,
    matcher: TitleMatcher,
    result: SyncResult,
    ctx: FetchContext,
    run_started_at: dt.datetime,
) -> None:
    titles_before = title_count(session)
    ticker = ProgressTicker()

    # Rows added but not yet flushed are invisible to a SELECT, so a title seen
    # twice in one stream would be inserted twice and break the unique
    # constraint. Sources repeat themselves routinely - paginated APIs return a
    # title again when the underlying result set shifts between pages, and two
    # listings can resolve to the same canonical title.
    written: dict[tuple[int, int, OfferType], Availability] = {}

    for item in items:
        # Between items rather than after a successful match, and so counted
        # over what has been done rather than what is about to be. Both of these
        # used to sit at the bottom of the loop, past a `continue` that an
        # unmatched item takes - so a run working through a long stretch of
        # parked listings neither committed nor said anything. Parking a listing
        # is a write like any other, which made that a write lock held open for
        # as long as the stretch lasted, on a run that looked hung.
        if result.items_seen and result.items_seen % COMMIT_EVERY == 0:
            # Release the write lock regularly. A partially ingested source is
            # safe: the run is recorded as failed, so nothing sweeps, and the
            # next run upserts the rest.
            session.commit()
        if ticker.due(result.items_seen):
            # A catalog of twenty thousand listings is twenty minutes in which
            # the only thing telling this apart from a hang is that it keeps
            # saying where it has got to.
            logger.info("%s: %s", source.key, _progress(result, matcher, ctx))

        result.items_seen += 1
        match = matcher.match(item)
        if match.title is None:
            continue
        # Remember where artwork can be fetched from; the images phase downloads
        # it later so a slow CDN never holds up a catalog sync.
        if item.poster_url and not match.title.poster_source_url:
            match.title.poster_source_url = item.poster_url
        # A catalogue that knows who made a film is often the only thing that
        # does: TMDB carries little Israeli cinema. Filling gaps only, as
        # enrichment does, so a scrape never displaces a canonical answer.
        if item.origin_countries and not match.title.origin_countries:
            match.title.origin_countries = item.origin_countries
        if item.credits:
            apply_credits(session, match.title, item.credits, source=source.key)
        created = upsert_availability(
            session,
            title=match.title,
            source=source,
            item=item,
            seen_at=run_started_at,
            written=written,
        )
        if created:
            result.availability_created += 1
        else:
            result.availability_updated += 1

    session.flush()
    result.titles_created = title_count(session) - titles_before


def _progress(result: SyncResult, matcher: TitleMatcher, ctx: FetchContext) -> str:
    """One line saying how far into a source's catalog this run is, and to what.

    The counts are the ones somebody watching would ask for: is it finding
    anything new, is it finding anything at all, and is it going wrong. Zeroes
    are left out - on a settled catalog most of these are zero every night, and
    printing them crowds out the numbers that are not.
    """
    counts = matcher.stats.counts
    return "{} listings in - {}".format(
        f"{result.items_seen:,}",
        tally(
            new_titles=counts.get(MatchMethod.CREATED.value, 0),
            new_offers=result.availability_created,
            already_listed=result.availability_updated,
            parked_for_review=counts.get(MatchMethod.REVIEW.value, 0),
            errors=ctx.error_count,
        ),
    )


def iter_with_error_capture(
    items: Iterator[RawItem],
    ctx: FetchContext,
) -> Iterator[RawItem]:
    """Yield items, turning per-item parse failures into recorded errors."""
    while True:
        try:
            item = next(items)
        except StopIteration:
            return
        except TooManyErrorsError:
            raise
        except Exception as exc:
            ctx.record_error("failed to parse an item", exc=exc)
            continue
        ctx.record_success()
        yield item
