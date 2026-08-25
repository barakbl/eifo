"""Recording that a title is offered somewhere right now.

The fetcher writes these by the thousand on a nightly sync; the API writes one
at a time, when a reviewer says a parked listing belongs to a title. Both are
the same write, and having two of them would eventually mean two behaviours -
one that revives a retired row and one that quietly leaves it retired.

It lives in ``eifo-core`` because that is the only thing the fetcher and the API
share: they never call each other, the database is the whole contract between
them (docs.internal/02-architecture.md).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from eifo_core.enums import OfferType
from eifo_core.models import Availability, Source, Title


@dataclass(frozen=True, slots=True)
class Offer:
    """What a source is offering, stripped of everything else about the listing.

    A deliberately small shape: the fetcher's ``RawItem`` carries names, credits
    and countries that have nothing to do with whether a title can be watched,
    and the API has no ``RawItem`` at all.
    """

    offer_type: OfferType = OfferType.STREAM
    deep_link_url: str | None = None
    price_minor: int | None = None
    price_currency: str | None = None


#: Rows created earlier in the same run, keyed by the unique triple.
WriteCache = dict[tuple[int, int, OfferType], Availability]


def record_offer(
    session: Session,
    *,
    title: Title,
    source: Source,
    offer: Offer,
    seen_at: dt.datetime,
    written: WriteCache | None = None,
) -> bool:
    """Record that a title is offered right now. Returns True if newly created.

    Seeing an offer again clears any strikes against it and revives a row that
    had been retired, which is what makes re-runs idempotent.

    Args:
        written: rows already created in this run. Pending inserts are invisible
            to a SELECT, so without this a source that lists the same title
            twice would insert it twice.
    """
    key = (title.id, source.id, offer.offer_type)

    availability = written.get(key) if written is not None else None
    if availability is None:
        availability = session.scalar(
            select(Availability).where(
                Availability.title_id == title.id,
                Availability.source_id == source.id,
                Availability.offer_type == offer.offer_type,
            )
        )

    if availability is None:
        availability = Availability(
            title_id=title.id,
            source_id=source.id,
            offer_type=offer.offer_type,
            deep_link_url=offer.deep_link_url,
            price_minor=offer.price_minor,
            price_currency=offer.price_currency,
            first_seen=seen_at,
            last_seen=seen_at,
            is_current=True,
            miss_count=0,
        )
        session.add(availability)
        if written is not None:
            written[key] = availability
        return True

    availability.last_seen = seen_at
    availability.miss_count = 0
    availability.is_current = True
    availability.gone_since = None
    if offer.deep_link_url:
        availability.deep_link_url = offer.deep_link_url
    if offer.price_minor is not None:
        # A price that moved is news; a source that stopped quoting one keeps
        # the last figure rather than silently showing an offer as free.
        availability.price_minor = offer.price_minor
        availability.price_currency = offer.price_currency
    return False
