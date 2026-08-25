"""``/api/v1/reviews`` - working through what the matcher could not place.

A parked listing is not in the catalog at all: no title, no availability,
nothing to search for. So this queue is not a tidy-up job, it is content
missing from the product, and until now the only way to drain it was a CLI that
showed a hundred unordered rows with the names cut to forty characters.

Three rulings, each taking effect on the spot rather than whenever the source
next syncs. The rulings themselves live in :mod:`eifo_core.reviews`, because
``eifo-fetch review`` makes exactly the same three and the API cannot call it
(docs.internal/02-architecture.md).

Administrators only. The ticket described this as session-authed, which it was
written before there was any notion of an administrator to be - but every one
of these writes catalog data that everybody else reads, and "create a title"
is not a thing to hand to whoever signs up.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from eifo_api.converters import image_url
from eifo_api.deps import AdminDep, CsrfDep, SessionDep
from eifo_api.schemas import (
    BulkDecision,
    BulkResult,
    BulkRuling,
    Page,
    ReviewCandidate,
    ReviewCounts,
    ReviewOut,
    ReviewRuling,
)
from eifo_core import reviews as core_reviews
from eifo_core.models import MatchReview, Source, Title
from eifo_core.reviews import ReviewOrder

router = APIRouter(prefix="/reviews", tags=["admin"])

MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 25


@router.get("", response_model=Page[ReviewOut], summary="The review queue")
def list_reviews(
    _admin: AdminDep,
    session: SessionDep,
    source: Annotated[str | None, Query(description="Limit to one source key")] = None,
    order: Annotated[ReviewOrder, Query()] = ReviewOrder.AGE,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> Page[ReviewOut]:
    """Parked listings, each with the title the matcher suspected it might be."""
    total = core_reviews.pending_count(session, source_key=source)
    waiting = core_reviews.pending(
        session,
        source_key=source,
        order=order,
        limit=page_size,
        offset=(page - 1) * page_size,
    )

    names = _source_names(session)
    candidates = _candidate_titles(session, waiting)

    return Page(
        items=[_to_review(review, names, candidates) for review in waiting],
        page=page,
        page_size=page_size,
        total=total,
    )


@router.get("/count", response_model=ReviewCounts, summary="How much is waiting")
def count_reviews(_admin: AdminDep, session: SessionDep) -> ReviewCounts:
    """Total and per-source, for the nav badge and the filter chips."""
    by_source = core_reviews.pending_by_source(session)
    return ReviewCounts(total=sum(by_source.values()), by_source=by_source)


@router.post("/{review_id}/attach", response_model=ReviewOut, summary="It is that title")
def attach_review(
    review_id: int,
    body: ReviewRuling,
    _admin: AdminDep,
    _csrf: CsrfDep,
    session: SessionDep,
) -> ReviewOut:
    """Give the parked offer to an existing title, now."""
    review = _unresolved(session, review_id)
    if body.title_id is None:
        raise HTTPException(status_code=422, detail="Attaching needs a title_id.")

    title = session.get(Title, body.title_id)
    if title is None:
        raise HTTPException(status_code=404, detail=f"No title with id {body.title_id}")

    _rule(session, lambda: core_reviews.attach(session, review, title))
    return _to_review(review, _source_names(session), {})


@router.post("/{review_id}/create", response_model=ReviewOut, summary="A title of its own")
def create_from_review(
    review_id: int,
    _admin: AdminDep,
    _csrf: CsrfDep,
    session: SessionDep,
) -> ReviewOut:
    """Not the suggestion, but a real title nobody holds. Create it."""
    review = _unresolved(session, review_id)
    _rule(session, lambda: core_reviews.create(session, review))
    return _to_review(review, _source_names(session), {})


@router.post("/{review_id}/dismiss", response_model=ReviewOut, summary="Not a title at all")
def dismiss_review(
    review_id: int,
    _admin: AdminDep,
    _csrf: CsrfDep,
    session: SessionDep,
) -> ReviewOut:
    """A trailer, a promo, a daily recap. Never offer it again."""
    review = _unresolved(session, review_id)
    core_reviews.dismiss(session, review)
    session.commit()
    return _to_review(review, _source_names(session), {})


@router.post("/bulk", response_model=BulkResult, summary="One ruling, many listings")
def rule_in_bulk(
    body: BulkRuling,
    _admin: AdminDep,
    _csrf: CsrfDep,
    session: SessionDep,
) -> BulkResult:
    """Apply the same ruling to a set of listings, skipping any already ruled on.

    Skipped rather than rejected: two people draining the same queue, or one
    person with two tabs, should not lose a whole selection because one row in
    it was answered a moment ago.
    """
    waiting = {
        review.id: review
        for review in session.scalars(
            select(MatchReview).where(
                MatchReview.id.in_(body.ids), MatchReview.resolved_at.is_(None)
            )
        ).all()
    }

    applied = 0
    for review_id in body.ids:
        review = waiting.get(review_id)
        if review is None:
            continue
        if body.decision is BulkDecision.DISMISS:
            core_reviews.dismiss(session, review)
        else:
            core_reviews.create(session, review)
        applied += 1

    session.commit()
    return BulkResult(
        applied=applied,
        skipped=[review_id for review_id in body.ids if review_id not in waiting],
    )


# -- helpers ----------------------------------------------------------------


def _unresolved(session: Session, review_id: int) -> MatchReview:
    review = session.get(MatchReview, review_id)
    if review is None:
        raise HTTPException(status_code=404, detail=f"No review with id {review_id}")
    if review.resolved_at is not None:
        raise HTTPException(status_code=409, detail=f"Review {review_id} has already been ruled on")
    return review


def _rule(session: Session, apply: Callable[[], object]) -> None:
    """Run a ruling, turning a source that has gone into a 409 rather than a 500."""
    try:
        apply()
    except core_reviews.UnknownSourceError as exc:
        session.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"Source {exc.source_key!r} is no longer in the catalog.",
        ) from exc
    session.commit()


def _source_names(session: Session) -> dict[str, str]:
    return {key: name for key, name in session.execute(select(Source.key, Source.name)).all()}


def _candidate_titles(session: Session, reviews: list[MatchReview]) -> dict[int, Title]:
    """The suggested titles, fetched once for the page rather than once per row."""
    wanted = {
        int(closest["title_id"])
        for review in reviews
        if (closest := core_reviews.closest_candidate(review)) and closest.get("title_id")
    }
    if not wanted:
        return {}
    return {
        title.id: title
        for title in session.scalars(select(Title).where(Title.id.in_(wanted))).all()
    }


def _to_review(
    review: MatchReview,
    source_names: dict[str, str],
    candidates: dict[int, Title],
) -> ReviewOut:
    payload = review.raw_payload
    return ReviewOut(
        id=review.id,
        source_key=review.source_key,
        source_name=source_names.get(review.source_key),
        created_at=review.created_at,
        name=str(payload.get("name") or ""),
        name_alt=payload.get("name_alt"),
        year=payload.get("year"),
        kind=core_reviews.kind_of(review),
        poster_url=payload.get("poster_url"),
        deep_link_url=payload.get("deep_link_url"),
        closest=_to_candidate(review, candidates),
    )


def _to_candidate(review: MatchReview, candidates: dict[int, Title]) -> ReviewCandidate | None:
    """The matcher's suggestion, with the stored poster if the title has one.

    The names come from the candidate blob rather than the title row: it is
    what the matcher was comparing against, and a reviewer judging the decision
    should see the same thing it saw.
    """
    closest = core_reviews.closest_candidate(review)
    if not closest or not closest.get("title_id"):
        return None

    title_id = int(closest["title_id"])
    title = candidates.get(title_id)
    return ReviewCandidate(
        title_id=title_id,
        name_he=closest.get("name_he"),
        name_en=closest.get("name_en"),
        year=closest.get("year"),
        similarity=closest.get("similarity"),
        poster_url=image_url(title.poster_path) if title else None,
    )
