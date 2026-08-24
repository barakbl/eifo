"""Working through the items the matcher could not place.

A parked item is not in the catalog: no title, no availability, nothing to
search for. So a queue that grows is a catalog that shrinks, and one that nobody
can drain is content quietly going missing - 545 items, 511 distinct listings,
by the time anybody counted.

Two things were wrong beyond the size. A ruling did not take effect until the
source next synced, so every decision anybody had made was still waiting: 78 of
them, on a source that had not run for a fortnight. And the band that parks
items was set where almost everything in it turned out to be a different title
anyway - of 78 rulings, 77 said "not that one" - so the queue filled with work
whose answer was nearly always the same.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from eifo_core.enums import MatchDecision, OfferType, TitleKind
from eifo_core.models import MatchReview, Source, Title
from eifo_core.types import utcnow
from eifo_fetcher.match import (
    AMBIGUOUS_THRESHOLD,
    TitleMatcher,
    similarity,
    title_kind_from,
)
from eifo_fetcher.pipeline import upsert_availability
from eifo_fetcher.sources.base import RawItem

logger = logging.getLogger("eifo.fetch.review")

#: Below this, a near-miss is not worth anybody's attention.
#:
#: Deliberately the same number as the band that parks items in the first place:
#: an item today's rules would not have parked should not still be sitting in
#: the queue because yesterday's rules did. The backlog was collected under a
#: threshold of 75, where 77 of the first 78 rulings said the suggestion was
#: wrong - "אודטה" against "פאודה" scores 80 and they are unrelated - and every
#: one of those left parked is a real listing kept out of the catalog.
AUTO_CREATE_BELOW = AMBIGUOUS_THRESHOLD

#: Names that are not titles. Trailers, promos, sing-alongs, daily recaps -
#: content a catalog of films and series has no row for, which sources feed into
#: the same pipeline as everything else.
NOT_A_TITLE = re.compile(
    r"""
    music \s* video | sing[\s-]?along | making \s+ of | assembled | featurette
    | behind \s+ the \s+ scenes | trailer | teaser
    | פרומו | קדימון | סיכום \s+ יומי | סיכום \s+ שבועי | מאחורי \s+ הקלעים
    | קליפ | האודישן | באולפן
    """,
    re.IGNORECASE | re.VERBOSE,
)


@dataclass(slots=True)
class ReviewTally:
    """What an automatic pass through the queue did."""

    expired: int = 0
    dismissed: int = 0
    created: int = 0
    left: int = 0
    errors: list[str] = field(default_factory=list)

    def as_stats(self) -> dict[str, Any]:
        return {
            "expired": self.expired,
            "dismissed": self.dismissed,
            "created": self.created,
            "left": self.left,
            "errors": self.errors,
        }


def pending(session: Session, *, source_key: str | None = None) -> list[MatchReview]:
    """Everything still waiting, oldest first."""
    statement = select(MatchReview).where(MatchReview.resolved_at.is_(None))
    if source_key:
        statement = statement.where(MatchReview.source_key == source_key)
    return list(session.scalars(statement.order_by(MatchReview.created_at, MatchReview.id)).all())


def item_from(review: MatchReview) -> RawItem:
    """Rebuild the listing a review was parked from.

    Enough of it to record where the title can be watched, which is the part a
    ruling has to be able to act on.
    """
    payload = review.raw_payload
    offer = payload.get("offer_type")
    return RawItem(
        source_key=review.source_key,
        kind=title_kind_from(str(payload.get("kind") or TitleKind.MOVIE.value)),
        name=str(payload.get("name") or ""),
        name_alt=payload.get("name_alt"),
        year=payload.get("year"),
        offer_type=OfferType(offer) if offer else OfferType.STREAM,
        tmdb_id=payload.get("tmdb_id"),
        imdb_id=payload.get("imdb_id"),
        deep_link_url=payload.get("deep_link_url"),
        poster_url=payload.get("poster_url"),
        extra=payload.get("extra") or {},
    )


# -- rulings ----------------------------------------------------------------


def attach(session: Session, review: MatchReview, title: Title) -> None:
    """The suggestion was right: give the offer to that title, now.

    Now, rather than whenever the source next runs. Waiting is why every ruling
    anybody had made was still sitting there unapplied.
    """
    review.resolved_title_id = title.id
    review.resolved_at = utcnow()
    review.decision = MatchDecision.ATTACHED
    _record_offer(session, review, title)


def create(session: Session, review: MatchReview) -> Title | None:
    """Not the suggestion, but a real title. Give it one of its own."""
    matcher = TitleMatcher(session)
    title = matcher.create_title(item_from(review))
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


def _record_offer(session: Session, review: MatchReview, title: Title) -> None:
    """Write the availability the parked item was carrying."""
    source = session.scalar(select(Source).where(Source.key == review.source_key))
    if source is None:
        logger.warning(
            "review %s is for source %r, which is not in the catalog; "
            "the title is recorded but not where to watch it",
            review.id,
            review.source_key,
        )
        return
    upsert_availability(
        session,
        title=title,
        source=source,
        item=item_from(review),
        seen_at=utcnow(),
    )


# -- the automatic pass -----------------------------------------------------


def auto_resolve(session: Session, *, apply: bool) -> ReviewTally:
    """Clear the part of the queue whose answer is not in doubt.

    Three rules, in order of how little judgement they need:

    * an item the source has stopped listing - the park is refreshed on every
      sync, so one older than the source's last successful run is gone;
    * a name that is not a title - a trailer, a promo, a daily recap;
    * a near-miss too weak to be the suggested title, where leaving it parked
      keeps a real listing out of the catalog for nothing.

    Everything else is left, because everything else is a judgement call.
    """
    tally = ReviewTally()
    for review in pending(session):
        try:
            _decide(session, review, tally, apply=apply)
        except Exception as exc:
            logger.exception("could not resolve review %s", review.id)
            tally.errors.append(f"review {review.id}: {type(exc).__name__}: {exc}")
    if apply:
        session.commit()
    return tally


def _decide(session: Session, review: MatchReview, tally: ReviewTally, *, apply: bool) -> None:
    name = str(review.raw_payload.get("name") or "")

    if _no_longer_listed(session, review):
        tally.expired += 1
        if apply:
            session.delete(review)
        return

    if NOT_A_TITLE.search(name):
        tally.dismissed += 1
        if apply:
            dismiss(session, review)
        return

    if _too_weak_to_be_the_suggestion(review):
        tally.created += 1
        if apply:
            create(session, review)
        return

    tally.left += 1


def _no_longer_listed(session: Session, review: MatchReview) -> bool:
    """Whether the source has stopped offering this item.

    A park is deleted and rewritten every time a sync sees the item again, so a
    row older than that source's last successful sync is one the source no
    longer lists. Nobody should be asked about a listing that is gone.
    """
    from eifo_core.enums import FetchPhase, FetchStatus
    from eifo_core.models import FetchRun

    last = session.scalar(
        select(FetchRun.started_at)
        .where(
            FetchRun.source_key == review.source_key,
            FetchRun.phase == FetchPhase.SYNC,
            FetchRun.status == FetchStatus.OK,
        )
        .order_by(FetchRun.started_at.desc())
        .limit(1)
    )
    return last is not None and review.created_at < last


def _too_weak_to_be_the_suggestion(review: MatchReview) -> bool:
    """Whether the near-miss that parked this is too weak to be worth asking about."""
    closest = review.candidates.get("closest") if review.candidates else None
    if not isinstance(closest, dict):
        return True

    score = float(closest.get("similarity") or 0.0)
    if score >= AUTO_CREATE_BELOW:
        return False

    # Agreeing years turn a weak name match into something worth a look.
    left, right = review.raw_payload.get("year"), closest.get("year")
    return not (left and right and left == right)


def describe(review: MatchReview) -> str:
    """One line about a parked item, for the CLI listing."""
    closest = review.candidates.get("closest") if review.candidates else None
    name = str(review.raw_payload.get("name") or "?")
    year = review.raw_payload.get("year") or "-"
    if isinstance(closest, dict):
        near = closest.get("name_he") or closest.get("name_en") or "?"
        match = f"#{closest.get('title_id')} {near} ({closest.get('similarity')}%)"
    else:
        match = "-"
    return f"{review.id}\t{review.source_key}\t{name} ({year})\t{match}"


def looks_like(review: MatchReview, name: str) -> float:
    """How close a parked item's name is to another - for a reviewer's search."""
    return similarity(str(review.raw_payload.get("name") or ""), name)
