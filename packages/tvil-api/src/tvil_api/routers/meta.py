"""``GET /api/v1/meta`` — data freshness and attribution."""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tvil_api import __version__
from tvil_api.deps import SessionDep, SettingsDep
from tvil_api.schemas import Attribution, MetaResponse, SourceFreshness
from tvil_core.enums import FetchPhase, FetchStatus
from tvil_core.models import FetchRun, Source, Title
from tvil_core.types import utcnow

router = APIRouter(tags=["meta"])

#: Required by the data licences; the client renders these verbatim.
ATTRIBUTION = [
    Attribution(
        text="Streaming availability data by JustWatch",
        url="https://www.justwatch.com/",
    ),
    Attribution(
        text="Metadata and artwork by TMDB",
        url="https://www.themoviedb.org/",
    ),
    Attribution(
        text="Ratings by IMDb, Rotten Tomatoes and Seret",
        url=None,
    ),
]


@router.get("/meta", response_model=MetaResponse, summary="Data freshness and attribution")
def get_meta(session: SessionDep, settings: SettingsDep) -> MetaResponse:
    """Report per-source freshness, the catalog size and licence attribution."""
    now = utcnow()
    stale_before = now - dt.timedelta(hours=settings.stale_after_hours)

    last_success = _last_sync_at_by_source(session, successful_only=True)
    latest_status = _latest_sync_status_by_source(session)

    sources = [
        _freshness(
            source,
            last_sync_at=last_success.get(source.key),
            last_sync_status=latest_status.get(source.key),
            stale_before=stale_before,
        )
        for source in session.scalars(select(Source).order_by(Source.name)).all()
    ]

    title_count = session.scalar(select(func.count()).select_from(Title)) or 0

    return MetaResponse(
        version=__version__,
        generated_at=now,
        title_count=title_count,
        sources=sources,
        attribution=ATTRIBUTION,
    )


def _last_sync_at_by_source(
    session: Session,
    *,
    successful_only: bool,
) -> dict[str, dt.datetime]:
    """When each source's catalog was last confirmed."""
    query = (
        select(FetchRun.source_key, func.max(FetchRun.finished_at))
        .where(
            FetchRun.phase == FetchPhase.SYNC,
            FetchRun.source_key.is_not(None),
            FetchRun.finished_at.is_not(None),
        )
        .group_by(FetchRun.source_key)
    )
    if successful_only:
        query = query.where(FetchRun.status == FetchStatus.OK)

    return {
        key: finished
        for key, finished in session.execute(query).all()
        if key is not None and finished is not None
    }


def _latest_sync_status_by_source(session: Session) -> dict[str, FetchStatus]:
    """Outcome of each source's most recent sync attempt, successful or not.

    Reported alongside the last success so a source that succeeded yesterday but
    is failing now is visible as exactly that.
    """
    newest = (
        select(FetchRun.source_key, func.max(FetchRun.started_at).label("started_at"))
        .where(FetchRun.phase == FetchPhase.SYNC, FetchRun.source_key.is_not(None))
        .group_by(FetchRun.source_key)
        .subquery()
    )
    rows = session.execute(
        select(FetchRun.source_key, FetchRun.status).join(
            newest,
            (FetchRun.source_key == newest.c.source_key)
            & (FetchRun.started_at == newest.c.started_at),
        )
    ).all()
    return {key: status for key, status in rows if key is not None}


def _freshness(
    source: Source,
    *,
    last_sync_at: dt.datetime | None,
    last_sync_status: FetchStatus | None,
    stale_before: dt.datetime,
) -> SourceFreshness:
    # A retired source is not "stale" — it is intentionally no longer tracked.
    stale = source.active and (last_sync_at is None or last_sync_at < stale_before)
    return SourceFreshness(
        key=source.key,
        name=source.name,
        kind=source.kind,
        active=source.active,
        last_sync_at=last_sync_at,
        last_sync_status=last_sync_status,
        stale=stale,
    )
