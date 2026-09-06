"""Writing a catalog down.

Everything here turns a listing into rows: the source it came from, the offer it
implies, the ones that have stopped being offered, and the bookkeeping around a
run that decides whether to believe any of it.

It moved out of the fetcher because the fetcher no longer does it. A sync ships
its listings to the API and the API writes them, so this is called from
``eifo-api`` on every ordinary run - and from the fetcher only where a command
still works against a database it can see (``review``, ``rematch``, ``dedupe``).
Core is where a thing both services need lives.

The naming is deliberate: these are the operations that change what the catalog
says. Deciding *which* title a listing means is :mod:`eifo_core.match`, and it
is a separate question with separate rules.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Iterable, Mapping

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from eifo_core.enums import FetchPhase, FetchStatus
from eifo_core.items import RawItem, SourceInfo
from eifo_core.models import Availability, FetchRun, MatchReview, Source, Title
from eifo_core.offers import Offer, WriteCache, record_offer
from eifo_core.types import utcnow

logger = logging.getLogger("eifo.catalog")

#: Consecutive sweeps an offer may be missing before it is retired. Two, so a
#: single failed read of a paginated catalog never retires a live listing.
MISS_LIMIT = 2

#: Below this many items in the previous run, a shrink says nothing: a source
#: with nine listings that returns four has not obviously broken.
VOLUME_GUARD_MIN_ITEMS = 50

#: A run returning less than this share of the previous one is not believed.
VOLUME_GUARD_RATIO = 0.5


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
            source_ref=item.source_ref,
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


def looks_truncated(session: Session, source_key: str, items_seen: int) -> bool:
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


def title_count(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(Title)) or 0


def register_declared_sources(
    session: Session,
    declared: Mapping[str, SourceInfo],
    *,
    enabled: Iterable[str],
) -> list[str]:
    """Give every plugin a row, whether or not it is switched on.

    Returns the keys newly written.

    A source used to exist only once it had synced, which made the operator's
    source list a list of sources that had already run. A service switched off -
    or one added in an upgrade - was invisible on the one screen whose job is
    showing services, so the toggle that would have switched it on was not there
    to press. The database is the only thing the fetcher and the API share, so
    what the fetcher knows about has to be written down for the API to see it.

    A row created for a source that is not switched on starts inactive, which is
    what it is: declared, known, not currently collected.
    """
    stored = {
        source.key: source
        for source in session.scalars(select(Source).where(Source.key.in_(list(declared)))).all()
    }
    switched_on = set(enabled)
    written: list[str] = []

    for key, info in declared.items():
        existing = stored.get(key)
        if existing is not None:
            # A plugin that changes its own default has to be able to say so:
            # the API reads this column and cannot ask the plugin directly.
            existing.default_enabled = info.default_enabled
            continue
        session.add(
            Source(
                key=key,
                name=info.name,
                kind=info.kind,
                website_url=info.website_url,
                logo_path=info.logo_path,
                default_enabled=info.default_enabled,
                active=key in switched_on,
                deactivated_at=None if key in switched_on else utcnow(),
            )
        )
        written.append(key)

    session.flush()
    return written


def requested_backfills(session: Session) -> list[str]:
    """Source keys an operator has asked to have pulled in full, oldest first."""
    return list(
        session.scalars(
            select(Source.key)
            .where(Source.backfill_requested_at.is_not(None))
            .order_by(Source.backfill_requested_at)
        ).all()
    )


def clear_backfill_requests(session: Session, keys: Iterable[str]) -> None:
    """Mark an operator's asks as answered.

    Cleared after the sync rather than before it, so a fetcher that dies partway
    leaves the ask standing and the next tick tries again. The cost of running
    one twice is a repeated sync; the cost of clearing one too early is a source
    that was asked for and never arrives.
    """
    for source in session.scalars(select(Source).where(Source.key.in_(list(keys)))).all():
        source.backfill_requested_at = None
    session.flush()


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
