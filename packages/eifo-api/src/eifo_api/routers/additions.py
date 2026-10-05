"""Films a member watched somewhere no tracked service covers.

Two routes: find the film on TMDB, and add it. The rules about what adding
means - dedup, the daily limit, the race - are :mod:`eifo_core.additions`; this
is the part that talks to TMDB and to a browser.

Scoped to ``principal.user`` throughout, like the rest of ``/me``.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from eifo_api.converters import to_user_item
from eifo_api.deps import CsrfDep, PrincipalDep, SessionDep
from eifo_api.schemas import AdditionCreate, AdditionOut, FoundMovieOut
from eifo_api.tmdb import (
    SEARCHES_PER_MINUTE,
    RateLimit,
    TmdbLookup,
    TmdbUnavailableError,
    get_tmdb,
)
from eifo_core.additions import DAILY_LIMIT, DailyLimitReachedError, add_to_list
from eifo_core.enums import TitleKind
from eifo_core.models import Title, TmdbAlias

router = APIRouter(tags=["user"])

TmdbDep = Annotated[TmdbLookup, Depends(get_tmdb)]

#: One letter matches half of TMDB; two is where a guess becomes useful.
MIN_QUERY_LENGTH = 2
MAX_QUERY_LENGTH = 100

#: Adding is a TMDB round trip or two. Ten a minute is far past anybody
#: recording what they watched and well short of a loop.
ADDS_PER_MINUTE = 10

_UNAVAILABLE = "TMDB is not answering right now. Try again in a minute."


def _limit(request: Request, name: str, limit: int) -> RateLimit:
    """The app's limiter of this name, made on first use."""
    limits: dict[str, RateLimit] = request.app.state.rate_limits
    if name not in limits:
        limits[name] = RateLimit(limit, 60.0)
    return limits[name]


def _within_limit(request: Request, user_id: int, name: str, limit: int) -> None:
    if not _limit(request, name, limit).allow(user_id):
        raise HTTPException(
            status_code=429,
            detail="That is a lot of requests in a minute. Give it a moment.",
            headers={"Retry-After": "60"},
        )


@router.get(
    "/me/additions/search",
    response_model=list[FoundMovieOut],
    summary="Find a film to add",
)
def search_films(
    request: Request,
    principal: PrincipalDep,
    session: SessionDep,
    tmdb: TmdbDep,
    q: Annotated[str, Query(max_length=MAX_QUERY_LENGTH)],
) -> list[FoundMovieOut]:
    """Films on TMDB matching a name, each saying whether we already hold it.

    Films only, for now. A film the catalog has comes back with its
    ``title_id``, so the client can open it instead of offering to add a copy.
    """
    query = " ".join(q.split())
    if len(query) < MIN_QUERY_LENGTH:
        return []
    _within_limit(request, principal.user.id, "search", SEARCHES_PER_MINUTE)

    try:
        found = tmdb.search(query)
    except TmdbUnavailableError as cause:
        raise HTTPException(status_code=503, detail=_UNAVAILABLE) from cause

    held = _held(session, [movie.tmdb_id for movie in found])
    return [
        FoundMovieOut(
            tmdb_id=movie.tmdb_id,
            name=movie.name,
            original_name=movie.original_name,
            year=movie.year,
            thumbnail_url=movie.thumbnail_url,
            title_id=held.get(movie.tmdb_id),
        )
        for movie in found
    ]


@router.post(
    "/me/additions",
    response_model=AdditionOut,
    summary="Add a film you watched, or mean to",
    responses={201: {"description": "The film was new to the catalog"}},
)
def add_film(
    body: AdditionCreate,
    request: Request,
    response: Response,
    principal: PrincipalDep,
    _csrf: CsrfDep,
    session: SessionDep,
    tmdb: TmdbDep,
) -> AdditionOut:
    """Put a film on a list - watched by default - adding it if it is new.

    The body names a TMDB id and, optionally, a rating. Everything about the
    film itself is read from TMDB here, never taken from the request.
    """
    _within_limit(request, principal.user.id, "add", ADDS_PER_MINUTE)

    try:
        record = tmdb.movie(body.tmdb_id)
    except TmdbUnavailableError as cause:
        raise HTTPException(status_code=503, detail=_UNAVAILABLE) from cause
    if record is None:
        raise HTTPException(status_code=404, detail=f"TMDB has no film {body.tmdb_id}.")

    try:
        added = add_to_list(session, principal.user, record, onto=body.status, rating=body.rating)
    except DailyLimitReachedError as cause:
        raise HTTPException(
            status_code=429,
            detail=f"You have added {DAILY_LIMIT} films today. The rest can wait for tomorrow.",
        ) from cause

    if added.created:
        response.status_code = 201
    return AdditionOut(
        title_id=added.title.id,
        created=added.created,
        item=to_user_item(added.item),
    )


def _held(session: Session, tmdb_ids: list[int]) -> dict[int, int]:
    """Which of these TMDB films the catalog holds, as ``tmdb_id -> title_id``.

    Aliases included: a film held under TMDB's other record of it is held.
    """
    if not tmdb_ids:
        return {}
    held = dict(
        session.execute(
            select(Title.tmdb_id, Title.id).where(
                Title.type == TitleKind.MOVIE, Title.tmdb_id.in_(tmdb_ids)
            )
        )
        .tuples()
        .all()
    )
    for tmdb_id, title_id in session.execute(
        select(TmdbAlias.tmdb_id, TmdbAlias.title_id).where(
            TmdbAlias.type == TitleKind.MOVIE, TmdbAlias.tmdb_id.in_(tmdb_ids)
        )
    ).tuples():
        held.setdefault(tmdb_id, title_id)
    return {tmdb_id: title_id for tmdb_id, title_id in held.items() if tmdb_id is not None}
