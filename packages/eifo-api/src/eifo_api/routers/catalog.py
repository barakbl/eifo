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
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import Select, func, or_, select, text
from sqlalchemy.orm import Session, selectinload

from eifo_api.converters import (
    ProviderRegistry,
    hydrate_titles,
    image_url,
    to_card,
    to_detail,
    to_genre,
    to_person_detail,
    to_source,
)
from eifo_api.deps import SessionDep
from eifo_api.schemas import (
    Arrival,
    GenreOut,
    Page,
    PersonDetail,
    PersonSuggestion,
    SourceOut,
    Suggestions,
    TitleCard,
    TitleDetail,
    TitleSuggestion,
)
from eifo_api.search import apply_relevance, apply_text_search, name_match
from eifo_core.enums import FetchPhase, FetchStatus, TitleKind
from eifo_core.fts import PEOPLE, TITLES
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
    #: How well a title matches the text searched for. Only means anything
    #: alongside ``q``; without one there is nothing to rank against.
    RELEVANCE = "relevance"
    SCORE = "score"
    SCORE_ISRAELI = "score_israeli"
    YEAR = "year"
    NAME = "name"
    RECENTLY_ADDED = "recently_added"


class SortOrder(StrEnum):
    ASC = "asc"
    DESC = "desc"


#: Which way round each sort reads when nobody says. Best first for anything
#: scored or dated, A to Z for a name - and leaving ``order`` unset keeps every
#: URL written before it existed answering exactly as it did.
NATURAL_ORDER = {
    # Ascending, because the match ladder counts down: rung zero is the exact
    # title (eifo_api.search).
    Sort.RELEVANCE: SortOrder.ASC,
    Sort.SCORE: SortOrder.DESC,
    Sort.SCORE_ISRAELI: SortOrder.DESC,
    Sort.YEAR: SortOrder.DESC,
    Sort.NAME: SortOrder.ASC,
    Sort.RECENTLY_ADDED: SortOrder.DESC,
}


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
    runtime_max: Annotated[
        int | None,
        Query(ge=1, le=1000, description="Longest film, in minutes; films only"),
    ] = None,
    sort: Annotated[
        Sort | None,
        Query(description="Ordering; best match when searching, best rated otherwise"),
    ] = None,
    order: Annotated[
        SortOrder | None,
        Query(description="Sort direction; the field's own default when unset"),
    ] = None,
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
        runtime_max=runtime_max,
    )

    total = session.scalar(select(func.count()).select_from(filtered.subquery())) or 0
    ordered = (
        _apply_sort(filtered, sort or _default_sort(q), order, query=q)
        .limit(page_size)
        .offset((page - 1) * page_size)
    )
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
    # One small query for the seven-odd rows that say how each score is
    # credited, rather than a per-rating lazy load of the same table.
    return to_detail(titles[0], ProviderRegistry.load(session))


@router.get("/whats-new", response_model=Page[Arrival], summary="Recent arrivals, per service")
def list_whats_new(
    session: SessionDep,
    sources: Annotated[str | None, Query(description="Comma-separated source keys")] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> Page[Arrival]:
    """What has turned up on each service lately, newest first.

    An arrival belongs to one service. Kung Fu Panda spent years on HBO Max and
    landed on Netflix last night: that is news about Netflix, and asking what is
    new on HBO Max must not answer with it. So the row here is the offer rather
    than the title, and a title that arrived on two services in the same week is
    two arrivals - which is what it was.

    Only what can be watched now, on a service still tracked: something that
    arrived and left again is not news anybody can act on.
    """
    arrivals = _arrivals(_csv(sources))

    total = session.scalar(select(func.count()).select_from(arrivals.subquery())) or 0
    rows = session.execute(
        arrivals.order_by(_ADDED_AT.desc(), Availability.title_id.desc(), Availability.source_id)
        .limit(page_size)
        .offset((page - 1) * page_size)
    ).all()

    cards = {
        title.id: to_card(title)
        for title in hydrate_titles(session, [row.title_id for row in rows])
    }
    return Page(
        items=[
            Arrival(
                added_at=row.added_at,
                source_key=row.source_key,
                source_name=row.source_name,
                title=cards[row.title_id],
            )
            for row in rows
            if row.title_id in cards
        ],
        page=page,
        page_size=page_size,
        total=total,
    )


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


#: How many suggestions of each kind, when the caller does not say. Titles get
#: the larger share: most searches are for one, and a person is the answer to a
#: rarer question.
SUGGEST_TITLES = 7
SUGGEST_PEOPLE = 3

#: How many ranked ids to read before narrowing to the filtered catalog, as a
#: multiple of the number wanted, and the ceiling on that. Enough that a filter
#: as narrow as one service still fills the list, small enough that the read
#: stays a dropdown's worth of work.
SUGGEST_OVERREAD = 40
SUGGEST_MAX_SCANNED = 400


@router.get("/suggest", response_model=Suggestions, summary="Search-as-you-type")
def suggest(
    session: SessionDep,
    q: Annotated[str, Query(min_length=1, max_length=100)],
    sources: Annotated[str | None, Query(description="Comma-separated source keys")] = None,
    available: Annotated[AvailabilityFilter, Query()] = AvailabilityFilter.CURRENT,
    type: Annotated[TitleKind | None, Query()] = None,
    genres: Annotated[str | None, Query(description="Comma-separated genre ids")] = None,
    year_min: Annotated[int | None, Query(ge=1880, le=2200)] = None,
    year_max: Annotated[int | None, Query(ge=1880, le=2200)] = None,
    score_min: Annotated[int | None, Query(ge=0, le=100)] = None,
    runtime_max: Annotated[
        int | None,
        Query(ge=1, le=1000, description="Longest film, in minutes; films only"),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=20)] = SUGGEST_TITLES + SUGGEST_PEOPLE,
) -> Suggestions:
    """Titles and people whose names start with what has been typed.

    People are here because there was no other way to reach them: thirty
    thousand of them, every one findable only by already being on a title they
    worked on. "What else has she been in" was answerable; "find her" was not.

    Titles take the same filters the grid does, because a suggestion is a
    preview of a result. Offered without them, the list advertised titles the
    grid would then say do not exist - search "batman" with one service
    selected and seven of them appeared above an empty catalog. People are not
    filtered: a director is not on a streaming service, and narrowing them by
    one would be answering a different question from the one being asked.
    """
    titles = max(1, round(limit * SUGGEST_TITLES / (SUGGEST_TITLES + SUGGEST_PEOPLE)))
    return Suggestions(
        query=q,
        titles=_suggest_titles(
            session,
            q,
            limit=titles,
            within=_filtered_ids(
                session,
                q=None,
                source_keys=_csv(sources),
                available=available,
                kind=type,
                genre_ids=_int_csv(genres),
                year_min=year_min,
                year_max=year_max,
                score_min=score_min,
                runtime_max=runtime_max,
            ),
        ),
        people=_suggest_people(session, q, limit=max(1, limit - titles)),
    )


def _suggest_titles(
    session: Session,
    query: str,
    *,
    limit: int,
    within: Select[tuple[int]] | None = None,
) -> list[TitleSuggestion]:
    match = name_match(TITLES.columns[:2], query)
    if match is None:
        return []

    # Ranked wider than asked for, then narrowed to what the grid would show.
    # Both halves are bounded, and the bound earns its keep: joining the whole
    # filtered catalog into the ranking, so that one statement could do all of
    # it, made a two-letter query take a third of a second - which is not a
    # speed anybody types at.
    matching = text(f"SELECT rowid FROM {TITLES.name} WHERE {TITLES.name} MATCH :fts").bindparams(
        fts=match
    )
    depth = limit if within is None else min(limit * SUGGEST_OVERREAD, SUGGEST_MAX_SCANNED)
    ordered = apply_relevance(select(Title.id).where(Title.id.in_(matching)), query)
    if ordered is None:
        return []
    ids = list(session.scalars(ordered.limit(depth)).all())

    if within is not None and ids:
        allowed = set(session.scalars(within.where(Title.id.in_(ids))).all())
        ids = [title_id for title_id in ids if title_id in allowed][:limit]

    if not ids:
        return []

    # One narrow read: a dropdown row needs no availability and no genres. The
    # score comes along because the row shows it, and an outer join keeps a
    # title nobody has rated in the list rather than dropping it.
    found = {
        row.Title.id: row
        for row in session.execute(
            select(Title, AggregateScore.score)
            .outerjoin(AggregateScore, AggregateScore.title_id == Title.id)
            .where(Title.id.in_(ids))
        ).all()
    }
    return [
        TitleSuggestion(
            id=row.Title.id,
            type=row.Title.type,
            name_he=row.Title.name_he,
            name_en=row.Title.name_en,
            year=row.Title.year,
            poster_url=image_url(row.Title.poster_path),
            score=row.score,
        )
        for title_id in ids
        if (row := found.get(title_id)) is not None
    ]


def _suggest_people(session: Session, query: str, *, limit: int) -> list[PersonSuggestion]:
    match = name_match(PEOPLE.columns, query)
    if match is None:
        return []

    # Ranked by how much the catalog credits them, not by bm25 alone: a hundred
    # names belong to more than one person, and the one somebody means is
    # overwhelmingly the one with the work.
    ranked = text(
        f"SELECT p.id, COUNT(c.id) AS credits FROM {PEOPLE.name} f "
        f"JOIN people p ON p.id = f.rowid "
        "LEFT JOIN credits c ON c.person_id = p.id "
        f"WHERE {PEOPLE.name} MATCH :fts "
        "GROUP BY p.id ORDER BY credits DESC, p.id LIMIT :limit"
    ).bindparams(fts=match, limit=limit)
    rows = session.execute(ranked).all()
    if not rows:
        return []

    people = {
        person.id: person
        for person in session.scalars(
            select(Person).where(Person.id.in_([row[0] for row in rows]))
        ).all()
    }
    return [
        PersonSuggestion(
            id=person.id,
            name_he=person.name_he,
            name_en=person.name_en,
            credit_count=count,
        )
        for person_id, count in rows
        if (person := people.get(person_id)) is not None
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
    runtime_max: int | None,
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

    # An evening is only so long, and a film is the only thing whose length we
    # can answer for: what we hold for a series is one episode, so letting a
    # forty-minute drama through "under two hours" would answer a question
    # nobody asked. A film whose runtime we do not know is left out for the same
    # reason the year filter leaves out an unknown year - a maybe is not a match
    # to somebody asking what fits before bed.
    if runtime_max is not None:
        statement = statement.where(
            Title.type == TitleKind.MOVIE,
            Title.runtime_minutes.is_not(None),
            Title.runtime_minutes <= runtime_max,
        )

    for genre_id in genre_ids:
        statement = statement.where(
            Title.id.in_(select(TitleGenre.title_id).where(TitleGenre.genre_id == genre_id))
        )

    statement = statement.where(Title.id.in_(_availability_ids(source_keys, available)))
    return statement


#: When a title arrived on a service: the earliest offer of any kind it has
#: there. A film that turned up to rent on Monday and to buy on Friday arrived
#: on Monday, and saying so twice would be reporting the price list, not news.
_ADDED_AT = func.min(Availability.first_seen).label("added_at")


def _arrivals(source_keys: list[str]) -> Select[tuple[int, int, str, str, dt.datetime]]:
    """One row per title and service, dated when the title turned up there.

    What a service was already carrying the first time Eifo swept it is left
    out. Those titles did not arrive; they were found - the whole catalog of a
    newly tracked service landing in one night would bury every real arrival
    under it, and would be saying something untrue besides. A source with no
    completed sync on record is taken at face value rather than silenced, which
    is what a fixture or a hand-seeded database looks like.
    """
    watching_since = (
        select(FetchRun.source_key, func.min(FetchRun.finished_at).label("since"))
        .where(
            FetchRun.phase == FetchPhase.SYNC,
            FetchRun.status == FetchStatus.OK,
            FetchRun.finished_at.is_not(None),
        )
        .group_by(FetchRun.source_key)
        .subquery()
    )

    statement = (
        select(
            Availability.title_id,
            Availability.source_id,
            Source.key.label("source_key"),
            Source.name.label("source_name"),
            _ADDED_AT,
        )
        .join(Source, Source.id == Availability.source_id)
        .outerjoin(watching_since, watching_since.c.source_key == Source.key)
        .where(
            Availability.is_current.is_(True),
            Source.active.is_(True),
            or_(
                watching_since.c.since.is_(None),
                Availability.first_seen > watching_since.c.since,
            ),
        )
        .group_by(Availability.title_id, Availability.source_id, Source.key, Source.name)
    )

    if source_keys:
        statement = statement.where(Source.key.in_(source_keys))

    return statement


def _availability_ids(source_keys: list[str], available: AvailabilityFilter) -> Select[tuple[int]]:
    """Title ids whose availability matches the requested state.

    "current" means available now on a source still being tracked: a title left
    behind on a retired source is history, not something to send a viewer to.

    "gone" means gone from everything being asked about, not gone from one
    thing. Kung Fu Panda left Netflix and went on streaming on Disney+; listing
    it as no longer available said something plainly untrue about a film
    anybody could watch that evening. A title that left one service while
    playing on another has moved, not gone. The exclusion is scoped to the same
    services as the question, so somebody who narrowed to what they pay for is
    still told what left their own services.
    """
    statement = select(Availability.title_id).join(Source, Source.id == Availability.source_id)

    if source_keys:
        statement = statement.where(Source.key.in_(source_keys))

    if available is AvailabilityFilter.CURRENT:
        statement = statement.where(Availability.is_current.is_(True), Source.active.is_(True))
    elif available is AvailabilityFilter.GONE:
        statement = statement.where(
            Availability.is_current.is_(False),
            Availability.title_id.not_in(
                _availability_ids(source_keys, AvailabilityFilter.CURRENT)
            ),
        )

    return statement


def _default_sort(query: str | None) -> Sort:
    """What to order by when nobody said.

    Searching asks a question, and the best answer to it is the closest match -
    which is what the index computed and what the catalog then threw away,
    re-sorting by score so that an exactly-named title with no rating landed
    below fuzzier matches that happened to have one.
    """
    return Sort.RELEVANCE if query else Sort.SCORE


def _apply_sort(
    statement: Select[tuple[int]],
    sort: Sort,
    order: SortOrder | None = None,
    *,
    query: str | None = None,
) -> Select[tuple[int]]:
    """Order the id query, in whichever direction was asked for.

    What the catalog does not know sorts last either way. SQLite sorts NULL
    lowest, so descending put the gaps at the end by accident rather than on
    purpose - and asking for the oldest films would have answered with the 1,836
    whose year nobody knows, the worst possible response to that question. The
    leading ``is_(None)`` key says it deliberately, and says it in both
    directions.
    """
    # Deliberately loose: a mapped column, a coalesce and a scalar subquery are
    # all orderable and share no useful static type.
    column: Any
    if sort is Sort.RELEVANCE:
        # Relevance orders by several things at once - how well the name
        # matches, then how many people have heard of it - so it builds the
        # whole ordering rather than handing back one column to sort on.
        # Whether the caller wants the natural direction, not which way the
        # columns point: relevance is several columns pointing different ways.
        best_first = (order or NATURAL_ORDER[sort]) is NATURAL_ORDER[sort]
        ranked = apply_relevance(statement, query, best_first=best_first) if query else None
        if ranked is None:
            # Nothing to rank against; the ordinary default is the honest answer.
            return _apply_sort(statement, Sort.SCORE, order)
        return ranked
    if sort is Sort.SCORE or sort is Sort.SCORE_ISRAELI:
        column = AggregateScore.score if sort is Sort.SCORE else AggregateScore.score_israeli
        statement = statement.outerjoin(AggregateScore, AggregateScore.title_id == Title.id)
    elif sort is Sort.YEAR:
        column = Title.year
    elif sort is Sort.NAME:
        column = func.coalesce(Title.name_he, Title.name_en)
    else:
        # recently_added: when the title first appeared anywhere, not when we
        # created the row - a long-known film can arrive on a service today.
        column = (
            select(func.max(Availability.first_seen))
            .where(Availability.title_id == Title.id)
            .correlate(Title)
            .scalar_subquery()
        )

    descending = (order or NATURAL_ORDER[sort]) is SortOrder.DESC
    return statement.order_by(
        column.is_(None),
        column.desc() if descending else column.asc(),
        Title.id,
    )


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
