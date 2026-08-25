"""``/api/v1/admin`` - the operator's view of the catalog.

Everything here answers one of three questions: is the catalog alright, is a
particular source alright, and what happened on the night it stopped being
alright. Nothing here writes catalog data. The single write is a source's
on/off override, which the fetcher reads next time it runs.

The whole router is behind :func:`~eifo_api.deps.require_admin`, which 404s
rather than 403s: a signed-in stranger is not owed the knowledge that this
surface exists.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from eifo_api.deps import AdminDep, CsrfDep, SessionDep, SettingsDep
from eifo_api.schemas import (
    AdminSource,
    AdminStats,
    Page,
    RunDetail,
    RunOut,
    SourceToggle,
)
from eifo_core.enums import FetchPhase, FetchStatus
from eifo_core.models import (
    AggregateScore,
    Availability,
    FetchRun,
    MatchReview,
    Person,
    Source,
    Title,
)
from eifo_core.settings import Settings
from eifo_core.types import utcnow

router = APIRouter(prefix="/admin", tags=["admin"])

MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 25


@router.get("/sources", response_model=list[AdminSource], summary="Every source, with its health")
def list_sources(
    _admin: AdminDep,
    session: SessionDep,
    settings: SettingsDep,
) -> list[AdminSource]:
    """One row per source: switched on, coverage, last run, queue depth."""
    stale_before = utcnow() - dt.timedelta(hours=settings.stale_after_hours)

    titles = _current_offer_counts(session)
    reviews = _pending_review_counts(session)
    last_sync = _last_sync_by_source(session)

    return [
        _to_admin_source(
            source,
            settings=settings,
            title_count=titles.get(source.id, 0),
            pending_reviews=reviews.get(source.key, 0),
            last=last_sync.get(source.key),
            stale_before=stale_before,
        )
        for source in session.scalars(select(Source).order_by(Source.name)).all()
    ]


@router.patch(
    "/sources/{key}",
    response_model=AdminSource,
    summary="Switch a source on or off",
)
def toggle_source(
    key: str,
    body: SourceToggle,
    _admin: AdminDep,
    _csrf: CsrfDep,
    session: SessionDep,
    settings: SettingsDep,
) -> AdminSource:
    """Set - or clear - the operator's override of the configured switch.

    Takes effect on the next run rather than the current one: the daemon opens
    a fresh session per phase, so nothing has to be restarted, and a sync
    already in flight is left to finish rather than half-obeying a new answer.
    """
    source = session.scalar(select(Source).where(Source.key == key))
    if source is None:
        raise HTTPException(status_code=404, detail=f"No source with key {key!r}")

    source.enabled = body.enabled
    session.commit()

    stale_before = utcnow() - dt.timedelta(hours=settings.stale_after_hours)
    return _to_admin_source(
        source,
        settings=settings,
        title_count=_current_offer_counts(session).get(source.id, 0),
        pending_reviews=_pending_review_counts(session).get(source.key, 0),
        last=_last_sync_by_source(session).get(source.key),
        stale_before=stale_before,
    )


@router.get("/runs", response_model=Page[RunOut], summary="Recent fetcher runs")
def list_runs(
    _admin: AdminDep,
    session: SessionDep,
    source: Annotated[str | None, Query(description="Limit to one source key")] = None,
    phase: Annotated[FetchPhase | None, Query()] = None,
    status: Annotated[FetchStatus | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> Page[RunOut]:
    """Newest first, without the logs - those are fetched one at a time."""
    filtered = _runs_query(source=source, phase=phase, status=status)
    total = session.scalar(select(func.count()).select_from(filtered.subquery())) or 0

    runs = session.scalars(
        filtered.order_by(FetchRun.started_at.desc(), FetchRun.id.desc())
        .limit(page_size)
        .offset((page - 1) * page_size)
    ).all()

    return Page(
        items=[_to_run(run) for run in runs],
        page=page,
        page_size=page_size,
        total=total,
    )


@router.get("/runs/{run_id}", response_model=RunDetail, summary="One run, with its log")
def get_run(run_id: int, _admin: AdminDep, session: SessionDep) -> RunDetail:
    """What the run counted, and what it said while it was counting."""
    run = session.get(FetchRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"No run with id {run_id}")

    return RunDetail(**_to_run(run).model_dump(), log=run.log)


@router.get("/stats", response_model=AdminStats, summary="Catalog health at a glance")
def get_stats(_admin: AdminDep, session: SessionDep, settings: SettingsDep) -> AdminStats:
    """The numbers worth looking at before anything else."""
    stale_before = utcnow() - dt.timedelta(hours=settings.stale_after_hours)
    last_sync = _last_sync_by_source(session)

    active = session.scalars(select(Source).where(Source.active.is_(True))).all()
    stale = sum(1 for source in active if _is_stale(last_sync.get(source.key), stale_before))

    return AdminStats(
        title_count=_count(session, select(Title)),
        titles_with_score=_count(session, select(AggregateScore)),
        titles_missing_poster=_count(session, select(Title).where(Title.poster_path.is_(None))),
        people_count=_count(session, select(Person)),
        current_offers=_count(
            session, select(Availability).where(Availability.is_current.is_(True))
        ),
        pending_reviews=_count(
            session, select(MatchReview).where(MatchReview.resolved_at.is_(None))
        ),
        sources_total=len(active),
        sources_stale=stale,
        last_run_at=session.scalar(select(func.max(FetchRun.finished_at))),
        stale_after_hours=settings.stale_after_hours,
    )


# -- helpers ----------------------------------------------------------------


def _runs_query(
    *,
    source: str | None,
    phase: FetchPhase | None,
    status: FetchStatus | None,
) -> Select[tuple[FetchRun]]:
    statement = select(FetchRun)
    if source:
        statement = statement.where(FetchRun.source_key == source)
    if phase is not None:
        statement = statement.where(FetchRun.phase == phase)
    if status is not None:
        statement = statement.where(FetchRun.status == status)
    return statement


def _count(session: Session, statement: Select[Any]) -> int:
    return session.scalar(select(func.count()).select_from(statement.subquery())) or 0


def _to_run(run: FetchRun) -> RunOut:
    finished = run.finished_at
    return RunOut(
        id=run.id,
        source_key=run.source_key,
        phase=run.phase,
        status=run.status,
        started_at=run.started_at,
        finished_at=finished,
        duration_seconds=(finished - run.started_at).total_seconds() if finished else None,
        stats=run.stats or {},
        has_log=bool(run.log),
    )


def _to_admin_source(
    source: Source,
    *,
    settings: Settings,
    title_count: int,
    pending_reviews: int,
    last: tuple[dt.datetime | None, FetchStatus | None] | None,
    stale_before: dt.datetime,
) -> AdminSource:
    last_at, last_status = last or (None, None)
    return AdminSource(
        key=source.key,
        name=source.name,
        kind=source.kind,
        website_url=source.website_url,
        active=source.active,
        enabled=source.enabled,
        effective_enabled=(
            source.enabled
            if source.enabled is not None
            else settings.source_config(source.key).enabled
        ),
        title_count=title_count,
        pending_reviews=pending_reviews,
        last_sync_at=last_at,
        last_sync_status=last_status,
        stale=source.active and _is_stale(last, stale_before),
    )


def _is_stale(
    last: tuple[dt.datetime | None, FetchStatus | None] | None,
    stale_before: dt.datetime,
) -> bool:
    """A source is stale when its last *successful* sync is too old, or absent."""
    if last is None:
        return True
    last_at, _status = last
    return last_at is None or last_at < stale_before


def _current_offer_counts(session: Session) -> dict[int, int]:
    """Titles each source is currently offering."""
    rows = session.execute(
        select(Availability.source_id, func.count(func.distinct(Availability.title_id)))
        .where(Availability.is_current.is_(True))
        .group_by(Availability.source_id)
    ).all()
    return {source_id: count for source_id, count in rows}


def _pending_review_counts(session: Session) -> dict[str, int]:
    rows = session.execute(
        select(MatchReview.source_key, func.count())
        .where(MatchReview.resolved_at.is_(None))
        .group_by(MatchReview.source_key)
    ).all()
    return {key: count for key, count in rows if key}


def _last_sync_by_source(
    session: Session,
) -> dict[str, tuple[dt.datetime | None, FetchStatus | None]]:
    """Each source's last successful sync, paired with its latest outcome.

    Two different questions that read as one: "when was this last known good"
    and "is it working now". A source that succeeded yesterday and has failed
    every hour since is neither fresh nor broken-and-empty, and an operator
    needs to see it as exactly that.
    """
    last_ok = {
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
        if key is not None
    }

    newest = (
        select(FetchRun.source_key, func.max(FetchRun.started_at).label("started_at"))
        .where(FetchRun.phase == FetchPhase.SYNC, FetchRun.source_key.is_not(None))
        .group_by(FetchRun.source_key)
        .subquery()
    )
    latest_status = {
        key: status
        for key, status in session.execute(
            select(FetchRun.source_key, FetchRun.status).join(
                newest,
                (FetchRun.source_key == newest.c.source_key)
                & (FetchRun.started_at == newest.c.started_at),
            )
        ).all()
        if key is not None
    }

    return {
        key: (last_ok.get(key), latest_status.get(key)) for key in set(last_ok) | set(latest_status)
    }
