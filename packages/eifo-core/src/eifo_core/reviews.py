"""Ruling on a listing the matcher could not place.

A parked item is not in the catalog: no title, no availability, nothing to
search for. Three rulings are possible and each is a write to the schema:

* **attach** - it is that title after all; give it the offer.
* **create** - a real title, but not that one; give it one of its own.
* **dismiss** - not a title at all; never offer it again.

The rulings live here rather than in the fetcher because there are now two
things that make them: ``eifo-fetch review`` and a person working through the
queue in the Manage tab. The API cannot call the fetcher and never will - the
database is the entire contract between them (docs.internal/02-architecture.md)
- so the alternative to this module is the same three rulings implemented
twice, drifting.

Each takes effect immediately. A ruling that waited for the source's next sync
is how 78 of them came to be sitting unapplied against a source that had not
run for a fortnight.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from sqlalchemy import Float, Select, func, select
from sqlalchemy.orm import Session

from eifo_core.enums import MatchDecision, OfferType, TitleKind
from eifo_core.models import MatchReview, Source, Title
from eifo_core.naming import split_by_script
from eifo_core.offers import Offer, record_offer
from eifo_core.types import utcnow


class ReviewOrder(StrEnum):
    """How to work through the queue.

    Oldest first is the honest default - a listing parked in March is a listing
    missing from the catalog since March. Sorting by how close the suggestion
    was puts the easy yes/no rulings together, which is faster to burst through.
    """

    AGE = "age"
    SIMILARITY = "similarity"


class UnknownSourceError(RuntimeError):
    """The review names a source the catalog does not have.

    Only reachable for a review parked by a source that was later deleted
    outright rather than retired, which nothing in this project does.
    """

    def __init__(self, source_key: str) -> None:
        super().__init__(f"no source with key {source_key!r}")
        self.source_key = source_key


def pending_query(*, source_key: str | None = None) -> Select[tuple[MatchReview]]:
    """Everything still waiting, unordered."""
    statement = select(MatchReview).where(MatchReview.resolved_at.is_(None))
    if source_key:
        statement = statement.where(MatchReview.source_key == source_key)
    return statement


def pending(
    session: Session,
    *,
    source_key: str | None = None,
    order: ReviewOrder = ReviewOrder.AGE,
    limit: int | None = None,
    offset: int = 0,
) -> list[MatchReview]:
    """A page of the queue."""
    statement = pending_query(source_key=source_key)

    if order is ReviewOrder.SIMILARITY:
        # SQLite orders JSON text lexically, so "9" would beat "80"; casting is
        # what makes 92 the closest match rather than the last one alphabetically.
        closeness = func.cast(MatchReview.candidates["closest"]["similarity"].as_string(), Float())
        statement = statement.order_by(closeness.desc().nulls_last(), MatchReview.id)
    else:
        statement = statement.order_by(MatchReview.created_at, MatchReview.id)

    if limit is not None:
        statement = statement.limit(limit).offset(offset)
    return list(session.scalars(statement).all())


def pending_count(session: Session, *, source_key: str | None = None) -> int:
    """How many rulings are outstanding."""
    return (
        session.scalar(
            select(func.count()).select_from(pending_query(source_key=source_key).subquery())
        )
        or 0
    )


def pending_by_source(session: Session) -> dict[str, int]:
    """Outstanding rulings per source, so the queue can be worked one at a time."""
    rows = session.execute(
        select(MatchReview.source_key, func.count())
        .where(MatchReview.resolved_at.is_(None))
        .group_by(MatchReview.source_key)
    ).all()
    return {key: count for key, count in rows if key}


# -- rulings ----------------------------------------------------------------


def attach(session: Session, review: MatchReview, title: Title) -> None:
    """The suggestion was right: give the offer to that title, now.

    Raises:
        UnknownSourceError: the review names a source that is not in the catalog.
    """
    review.resolved_title_id = title.id
    review.resolved_at = utcnow()
    review.decision = MatchDecision.ATTACHED
    _record_offer(session, review, title)


def create(session: Session, review: MatchReview) -> Title:
    """Not the suggestion, but a real title. Give it one of its own.

    Raises:
        UnknownSourceError: the review names a source that is not in the catalog.
    """
    title = title_from(review)
    session.add(title)
    session.flush()

    review.resolved_title_id = title.id
    review.resolved_at = utcnow()
    review.decision = MatchDecision.CREATED
    _record_offer(session, review, title)
    return title


def dismiss(session: Session, review: MatchReview) -> None:
    """Not a title at all. Nothing to create, and never offer it again."""
    review.resolved_title_id = None
    review.resolved_at = utcnow()
    review.decision = MatchDecision.DISMISSED


# -- reading a parked payload -----------------------------------------------


def title_from(review: MatchReview) -> Title:
    """The title a parked listing describes, unsaved.

    The year is taken as stored: it passed the ingestion gate when the item was
    parked, which is the one place a placeholder year is stopped
    (``eifo_fetcher.sources.base.plausible_year``).
    """
    payload = review.raw_payload
    name = str(payload.get("name") or "")
    hebrew, english = split_by_script(name, payload.get("name_alt"))
    if hebrew is None and english is None:
        # A row must carry a name, so a title in a third script goes in the
        # English column for want of anywhere better. The metadata enricher
        # replaces it with a real English title on its first visit.
        english = name

    return Title(
        type=kind_of(review),
        tmdb_id=payload.get("tmdb_id"),
        imdb_id=payload.get("imdb_id") or None,
        name_he=hebrew,
        name_en=english,
        year=payload.get("year"),
        poster_source_url=payload.get("poster_url"),
    )


def kind_of(review: MatchReview) -> TitleKind:
    """Film or series, defaulting to film when the payload says something else."""
    try:
        return TitleKind(str(review.raw_payload.get("kind") or TitleKind.MOVIE.value))
    except ValueError:
        return TitleKind.MOVIE


def offer_of(review: MatchReview) -> Offer:
    """The offer a parked listing was carrying."""
    payload = review.raw_payload
    raw = payload.get("offer_type")
    try:
        offer_type = OfferType(raw) if raw else OfferType.STREAM
    except ValueError:
        offer_type = OfferType.STREAM

    return Offer(
        offer_type=offer_type,
        deep_link_url=payload.get("deep_link_url"),
        price_minor=payload.get("price_minor"),
        price_currency=payload.get("price_currency"),
    )


def closest_candidate(review: MatchReview) -> dict[str, Any] | None:
    """The title the matcher thought it might be, if it named one."""
    closest = review.candidates.get("closest") if review.candidates else None
    return closest if isinstance(closest, dict) else None


def _record_offer(session: Session, review: MatchReview, title: Title) -> None:
    source = session.scalar(select(Source).where(Source.key == review.source_key))
    if source is None:
        raise UnknownSourceError(review.source_key)
    record_offer(
        session,
        title=title,
        source=source,
        offer=offer_of(review),
        seen_at=utcnow(),
    )
