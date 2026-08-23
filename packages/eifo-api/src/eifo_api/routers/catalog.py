"""Catalog endpoints: titles, sources and genres.

The titles list is the only performance-sensitive path in the API, so it runs
as two queries regardless of page size: one to select and count matching ids,
one to hydrate that page with its availability, genres and scores. Building
cards from lazily-loaded relationships would turn a 24-card grid into dozens of
round trips.
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session, selectinload

from eifo_api.converters import (
    hydrate_titles,
    to_card,
    to_detail,
    to_genre,
    to_person_detail,
    to_source,
)
from eifo_api.deps import SessionDep
from eifo_api.schemas import GenreOut, Page, PersonDetail, SourceOut, TitleCard, TitleDetail
from eifo_api.search import apply_text_search
from eifo_core.enums import FetchPhase, FetchStatus, TitleKind
from eifo_core.models import (
    AggregateScore,
    Availability,
    Credit,
    FetchRun,
    Genre,
    Person,
    Source,
    Title,
    TitleGenre,
)

router = APIRouter(tags=["catalog"])

MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 24


class AvailabilityFilter(StrEnum):
    """Which availability state a search is interested in."""

    CURRENT = "current"
    ANY = "any"
    GONE = "gone"


class Sort(StrEnum):
    SCORE = "score"
    SCORE_ISRAELI = "score_israeli"
    YEAR = "year"
    NAME = "name"
    RECENTLY_ADDED = "recently_added"


@router.get("/titles", response_model=Page[TitleCard], summary="Search and filter titles")
def list_titles(
    session: SessionDep,
    q: Annotated[str | None, Query(description="Text search, Hebrew or English")] = None,
    sources: Annotated[str | None, Query(description="Comma-separated source keys")] = None,
    available: Annotated[AvailabilityFilter, Query()] = AvailabilityFilter.CURRENT,
    type: Annotated[TitleKind | None, Query()] = None,
    genres: Annotated[str | None, Query(description="Comma-separated genre ids")] = None,
    year_min: Annotated[int | None, Query(ge=1880, le=2200)] = None,
    year_max: Annotated[int | None, Query(ge=1880, le=2200)] = None,
    score_min: Annotated[int | None, Query(ge=0, le=100)] = None,
    sort: Annotated[Sort, Query()] = Sort.SCORE,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> Page[TitleCard]:
    """Titles matching every supplied filter."""
    filtered = _filtered_ids(
        session,
        q=q,
        source_keys=_csv(sources),
        available=available,
        kind=type,
        genre_ids=_int_csv(genres),
        year_min=year_min,
        year_max=year_max,
        score_min=score_min,
    )

    total = session.scalar(select(func.count()).select_from(filtered.subquery())) or 0
    ordered = _apply_sort(filtered, sort).limit(page_size).offset((page - 1) * page_size)
    title_ids = list(session.scalars(ordered).all())

    return Page(
        items=[to_card(title) for title in hydrate_titles(session, title_ids)],
        page=page,
        page_size=page_size,
        total=total,
    )


@router.get("/titles/{title_id}", response_model=TitleDetail, summary="One title in full")
def get_title(title_id: int, session: SessionDep) -> TitleDetail:
    titles = hydrate_titles(session, [title_id])
    if not titles:
        raise HTTPException(status_code=404, detail=f"No title with id {title_id}")
    return to_detail(titles[0])


@router.get("/people/{person_id}", response_model=PersonDetail, summary="One person's work")
def get_person(person_id: int, session: SessionDep) -> PersonDetail:
    """A person and everything the catalog credits them with.

    One page per person rather than one per role: someone who directs and acts
    is one human, and two unconnected pages would say otherwise. Credits come
    back ordered by role - director, cinematographer, then cast - and newest
    first within each. A title with no year sorts last: unknown is not the same
    as ancient.
    """
    person = session.get(Person, person_id)
    if person is None:
        raise HTTPException(status_code=404, detail=f"No person with id {person_id}")

    credits = list(
        session.scalars(
            select(Credit)
            .where(Credit.person_id == person.id)
            .join(Credit.title)
            .order_by(Title.year.is_(None), Title.year.desc(), Title.id)
            .options(selectinload(Credit.title))
        ).all()
    )

    # One hydrate for every credited title, so a filmography costs a couple of
    # round trips rather than one per film.
    hydrated = {
        title.id: title
        for title in hydrate_titles(session, [credit.title_id for credit in credits])
    }
    for credit in credits:
        if credit.title_id in hydrated:
            credit.title = hydrated[credit.title_id]

    return to_person_detail(person, credits)


@router.get("/sources", response_model=list[SourceOut], summary="Every tracked service")
def list_sources(session: SessionDep) -> list[SourceOut]:
    """All sources, including retired ones so the client can badge them."""
    counts = _current_counts_by_source(session)
    synced = _last_sync_by_source(session)

    return [
        to_source(
            source,
            title_count=counts.get(source.id, 0),
            last_synced_at=synced.get(source.key),
        )
        for source in session.scalars(select(Source).order_by(Source.name)).all()
    ]


@router.get("/genres", response_model=list[GenreOut], summary="Genre taxonomy")
def list_genres(session: SessionDep) -> list[GenreOut]:
    return [to_genre(genre) for genre in session.scalars(select(Genre).order_by(Genre.name_en))]


# -- query construction ---------------------------------------------------


def _filtered_ids(
    session: Session,
    *,
    q: str | None,
    source_keys: list[str],
    available: AvailabilityFilter,
    kind: TitleKind | None,
    genre_ids: list[int],
    year_min: int | None,
    year_max: int | None,
    score_min: int | None,
) -> Select[tuple[int]]:
    """Ids of titles matching every filter, before ordering or paging."""
    statement = select(Title.id)

    if q:
        statement = apply_text_search(statement, q)
    if kind is not None:
        statement = statement.where(Title.type == kind)
    if year_min is not None:
        statement = statement.where(Title.year >= year_min)
    if year_max is not None:
        statement = statement.where(Title.year <= year_max)

    if score_min is not None:
        statement = statement.where(
            Title.id.in_(select(AggregateScore.title_id).where(AggregateScore.score >= score_min))
        )

    for genre_id in genre_ids:
        statement = statement.where(
            Title.id.in_(select(TitleGenre.title_id).where(TitleGenre.genre_id == genre_id))
        )

    statement = statement.where(Title.id.in_(_availability_ids(source_keys, available)))
    return statement


def _availability_ids(source_keys: list[str], available: AvailabilityFilter) -> Select[tuple[int]]:
    """Title ids whose availability matches the requested state.

    "current" means available now on a source still being tracked: a title left
    behind on a retired source is history, not something to send a viewer to.
    """
    statement = select(Availability.title_id).join(Source, Source.id == Availability.source_id)

    if source_keys:
        statement = statement.where(Source.key.in_(source_keys))

    if available is AvailabilityFilter.CURRENT:
        statement = statement.where(Availability.is_current.is_(True), Source.active.is_(True))
    elif available is AvailabilityFilter.GONE:
        statement = statement.where(Availability.is_current.is_(False))

    return statement


def _apply_sort(statement: Select[tuple[int]], sort: Sort) -> Select[tuple[int]]:
    """Order the id query. SQLite sorts NULL lowest, so DESC puts gaps last."""
    if sort is Sort.SCORE or sort is Sort.SCORE_ISRAELI:
        column = AggregateScore.score if sort is Sort.SCORE else AggregateScore.score_israeli
        return statement.outerjoin(AggregateScore, AggregateScore.title_id == Title.id).order_by(
            column.desc(), Title.id
        )

    if sort is Sort.YEAR:
        return statement.order_by(Title.year.desc(), Title.id)

    if sort is Sort.NAME:
        return statement.order_by(func.coalesce(Title.name_he, Title.name_en), Title.id)

    # recently_added: when the title first appeared anywhere, not when we
    # created the row - a long-known film can arrive on a service today.
    first_seen = (
        select(func.max(Availability.first_seen))
        .where(Availability.title_id == Title.id)
        .correlate(Title)
        .scalar_subquery()
    )
    return statement.order_by(first_seen.desc(), Title.id)


def _current_counts_by_source(session: Session) -> dict[int, int]:
    return {
        source_id: count
        for source_id, count in session.execute(
            select(Availability.source_id, func.count())
            .where(Availability.is_current.is_(True))
            .group_by(Availability.source_id)
        ).all()
    }


def _last_sync_by_source(session: Session) -> dict[str, dt.datetime]:
    return {
        key: finished
        for key, finished in session.execute(
            select(FetchRun.source_key, func.max(FetchRun.finished_at))
            .where(
                FetchRun.phase == FetchPhase.SYNC,
                FetchRun.status == FetchStatus.OK,
                FetchRun.source_key.is_not(None),
                FetchRun.finished_at.is_not(None),
            )
            .group_by(FetchRun.source_key)
        ).all()
        if key is not None and finished is not None
    }


def _csv(value: str | None) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()] if value else []


def _int_csv(value: str | None) -> list[int]:
    """Parse comma-separated ids, ignoring anything that is not a number."""
    return [int(part) for part in _csv(value) if part.isdigit()]
