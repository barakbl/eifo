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

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from eifo_core.enums import FetchPhase, FetchStatus, OfferType
from eifo_core.models import Availability, FetchRun, MatchReview, Source, Title
from eifo_core.offers import Offer, WriteCache, record_offer
from eifo_core.types import utcnow
from eifo_fetcher.match import MatchStats, TitleMatcher
from eifo_fetcher.people import apply_credits
from eifo_fetcher.runs import capture_log, close_run, open_run
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
) -> SyncResult:
    """Sync one source end to end and record a ``fetch_runs`` row.

    Args:
        tmdb: enables the matcher's TMDB lookup; without it the matcher falls
            back to external ids and local fuzzy comparison only.
        items: pre-fetched items, used by the tests; normally the plugin is
            asked to fetch them.
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
    with capture_log() as captured:
        try:
            stream = items if items is not None else plugin.fetch(ctx)
            _ingest(session, stream, source, matcher, result, started_at)
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

        if result.status is FetchStatus.OK and _looks_truncated(
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
    run_started_at: dt.datetime,
) -> None:
    titles_before = _title_count(session)

    # Rows added but not yet flushed are invisible to a SELECT, so a title seen
    # twice in one stream would be inserted twice and break the unique
    # constraint. Sources repeat themselves routinely - paginated APIs return a
    # title again when the underlying result set shifts between pages, and two
    # listings can resolve to the same canonical title.
    written: dict[tuple[int, int, OfferType], Availability] = {}

    for item in items:
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

        # Release the write lock regularly. A partially ingested source is safe:
        # the run is recorded as failed, so nothing sweeps, and the next run
        # upserts the rest.
        if result.items_seen % COMMIT_EVERY == 0:
            session.commit()

    session.flush()
    result.titles_created = _title_count(session) - titles_before


def upsert_source(session: Session, info: SourceInfo) -> Source:
    """Create or refresh the source row, reactivating it if it had been retired."""
    source = session.scalar(select(Source).where(Source.key == info.key))
    if source is None:
        source = Source(
            key=info.key,
            name=info.name,
            kind=info.kind,
            website_url=info.website_url,
            logo_path=info.logo_path,
        )
        session.add(source)
        return source

    source.name = info.name
    source.kind = info.kind
    source.website_url = info.website_url
    if info.logo_path:
        source.logo_path = info.logo_path
    if not source.active:
        source.active = True
        source.deactivated_at = None
    return source


def upsert_availability(
    session: Session,
    *,
    title: Title,
    source: Source,
    item: RawItem,
    seen_at: dt.datetime,
    written: WriteCache | None = None,
) -> bool:
    """Record that a title is offered right now. Returns True if newly created.

    A thin translation from a source listing to the offer it implies; the write
    itself is :func:`eifo_core.offers.record_offer`, which the API also calls
    when a reviewer attaches a parked listing to a title.
    """
    return record_offer(
        session,
        title=title,
        source=source,
        offer=Offer(
            offer_type=item.offer_type,
            deep_link_url=item.deep_link_url,
            price_minor=item.price_minor,
            price_currency=item.price_currency,
        ),
        seen_at=seen_at,
        written=written,
    )


def sweep_source(session: Session, source: Source, *, run_started_at: dt.datetime) -> int:
    """Strike, then retire, availability this run did not see.

    Returns the number of rows retired by this sweep.
    """
    stale = session.scalars(
        select(Availability).where(
            Availability.source_id == source.id,
            Availability.is_current.is_(True),
            Availability.last_seen < run_started_at,
        )
    ).all()

    retired = 0
    now = utcnow()
    for availability in stale:
        availability.miss_count += 1
        if availability.miss_count >= MISS_LIMIT:
            availability.is_current = False
            availability.gone_since = now
            retired += 1

    session.flush()
    return retired


def expire_reviews(session: Session, source_key: str, *, before: dt.datetime) -> int:
    """Drop parked items a source has stopped listing.

    A park is deleted and rewritten every time a sync sees the item again, so
    one older than this run is an item the source no longer carries. Asking
    somebody about a listing that is gone wastes the only scarce thing in the
    review queue, which is their attention. If it comes back, so does the park.
    """
    stale = session.scalars(
        select(MatchReview).where(
            MatchReview.source_key == source_key,
            MatchReview.resolved_at.is_(None),
            MatchReview.created_at < before,
        )
    ).all()
    for review in stale:
        session.delete(review)
    removed = len(stale)
    if removed:
        logger.info("%s: %d parked item(s) are no longer listed", source_key, removed)
    return removed


def _looks_truncated(session: Session, source_key: str, items_seen: int) -> bool:
    """Whether this run is too small to believe, given the previous one."""
    previous = session.scalar(
        select(FetchRun)
        .where(
            FetchRun.source_key == source_key,
            FetchRun.phase == FetchPhase.SYNC,
            FetchRun.status == FetchStatus.OK,
        )
        .order_by(FetchRun.started_at.desc())
        .limit(1)
    )
    if previous is None:
        return False

    baseline = int(previous.stats.get("items_seen", 0))
    if baseline < VOLUME_GUARD_MIN_ITEMS:
        return False
    return items_seen < baseline * VOLUME_GUARD_RATIO


def _title_count(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(Title)) or 0


def deactivate_missing_sources(session: Session, active_keys: Iterable[str]) -> list[str]:
    """Retire sources that configuration or plugins no longer provide.

    Their titles and availability history stay untouched - the UI badges them as
    "no longer tracked" rather than pretending they never existed.
    """
    keep = set(active_keys)
    retired: list[str] = []
    now = utcnow()
    for source in session.scalars(select(Source).where(Source.active.is_(True))).all():
        if source.key not in keep:
            source.active = False
            source.deactivated_at = now
            retired.append(source.key)
    session.flush()
    return retired


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
