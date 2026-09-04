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
import logging
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import Select, case, func, select
from sqlalchemy.orm import Session

from eifo_api.converters import ProviderRegistry
from eifo_api.deps import AdminDep, CsrfDep, SessionDep, SettingsDep
from eifo_api.schemas import (
    AdminSource,
    AdminStats,
    Page,
    RunDetail,
    RunOut,
    ScoringProvider,
    SourceToggle,
)
from eifo_core.enums import EnrichOutcome, FetchPhase, FetchStatus, RatingProvider
from eifo_core.models import (
    AggregateScore,
    Availability,
    EnrichAttempt,
    ExternalRating,
    FetchRun,
    MatchReview,
    Person,
    Source,
    Title,
)
from eifo_core.settings import Settings
from eifo_core.types import utcnow

logger = logging.getLogger("eifo.api.admin")

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

    coverage = _coverage_by_source(session)
    reviews = _pending_review_counts(session)
    last_sync = _last_sync_by_source(session)

    return [
        _to_admin_source(
            source,
            settings=settings,
            coverage=coverage.get(source.id, _Coverage()),
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

    Switching one on also asks for its catalog. Permission and intent are the
    same gesture here: nobody turns a service on to see an empty row until the
    small hours, so the ask is recorded and the fetcher acts on it within the
    minute. Switching off is only a decision about tonight - it withdraws a
    pending ask, and nothing already collected is touched.

    A change to the switch itself takes effect on the next run rather than the
    current one: the daemon opens a fresh session per phase, so nothing has to
    be restarted, and a sync already in flight is left to finish rather than
    half-obeying a new answer.
    """
    source = session.scalar(select(Source).where(Source.key == key))
    if source is None:
        raise HTTPException(status_code=404, detail=f"No source with key {key!r}")

    was_on = _effective(source, settings)
    source.enabled = body.enabled
    now_on = _effective(source, settings)

    if now_on and not was_on:
        source.backfill_requested_at = utcnow()
    elif not now_on:
        # Withdrawn rather than left queued: a source switched off should not
        # be dragged back through a full sync by an ask nobody stands behind.
        source.backfill_requested_at = None

    session.commit()

    stale_before = utcnow() - dt.timedelta(hours=settings.stale_after_hours)
    return _to_admin_source(
        source,
        settings=settings,
        coverage=_coverage_by_source(session).get(source.id, _Coverage()),
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
        titles_available=session.scalar(
            select(func.count(func.distinct(Availability.title_id))).where(
                Availability.is_current.is_(True)
            )
        )
        or 0,
        current_offers=_count(
            session, select(Availability).where(Availability.is_current.is_(True))
        ),
        pending_reviews=_count(
            session, select(MatchReview).where(MatchReview.resolved_at.is_(None))
        ),
        reviews_total=_count(session, select(MatchReview)),
        sources_total=len(active),
        sources_stale=stale,
        last_run_at=session.scalar(select(func.max(FetchRun.finished_at))),
        stale_after_hours=settings.stale_after_hours,
        scoring=_scoring_mix(session, settings),
    )


# -- helpers ----------------------------------------------------------------


def _configured_floors(settings: Settings) -> list[tuple[RatingProvider, int]]:
    """``[scores.min_votes]`` as providers, ignoring names that are not ones.

    The setting is a free-form mapping, so a typo there must not take the whole
    Manage tab down with a ValueError.
    """
    floors = []
    for name, floor in settings.scores.min_votes.items():
        try:
            floors.append((RatingProvider(name), floor))
        except ValueError:
            logger.warning("[scores.min_votes] names %r, which is not a rating provider", name)
    return floors


def _scoring_mix(session: Session, settings: Settings) -> list[ScoringProvider]:
    """Every rating provider's part in the catalog's scores, heaviest first.

    The weights in the configuration file say what each provider is *meant* to
    count for, and on their own they are misleading: a provider weighted
    heaviest that has rated a tenth of the catalog is not the one deciding its
    scores. So the share here is the weight that actually went in - each
    provider's weight counted once per scored title it has rated, over the same
    total across every provider.

    Halved for a thinly-voted rating and dropped entirely for one below its
    ``[scores.min_votes]`` floor, exactly as :mod:`eifo_fetcher.scores` does it,
    so the number here is the one the aggregates were built from rather than a
    second opinion about them.

    Every provider appears, including the ones that have rated nothing. A
    provider contributing zero is the single most useful row on the table:
    it means an enricher is off, or blocked, or quietly failing.
    """
    thin = settings.scores.low_vote_threshold
    # A rating with no vote count is not a thin one - it is a provider that does
    # not publish counts, and treating "unknown" as "hardly anybody" would halve
    # every score Rotten Tomatoes ever contributed.
    #
    # The floors come first: a rating below its provider's min_votes contributed
    # nothing at all to the aggregate, so crediting it half here would make this
    # table disagree with the scores it claims to describe.
    excluded = [
        (
            (ExternalRating.provider == provider)
            & ExternalRating.vote_count.is_not(None)
            & (ExternalRating.vote_count <= floor),
            0.0,
        )
        for provider, floor in _configured_floors(settings)
    ]
    units = case(
        *excluded,
        (ExternalRating.vote_count.is_not(None) & (ExternalRating.vote_count < thin), 0.5),
        else_=1.0,
    )
    # Two different denominators, on purpose: the share is about the titles that
    # ended up with a score, and the count is about the catalog. Left join so a
    # rating on a title nothing could score still counts as a rating.
    scored = AggregateScore.score.is_not(None)
    rows = session.execute(
        select(
            ExternalRating.provider,
            func.count().label("rated"),
            func.coalesce(func.sum(case((scored, units), else_=0.0)), 0.0).label("units"),
        )
        .outerjoin(AggregateScore, AggregateScore.title_id == ExternalRating.title_id)
        .group_by(ExternalRating.provider)
    ).all()
    counted = {RatingProvider(provider): (rated, units) for provider, rated, units in rows}

    registry = ProviderRegistry.load(session)
    weights = settings.scores.weights
    weighted = {
        provider: counted.get(provider, (0, 0.0))[1] * getattr(weights, provider.value, 0.0)
        for provider in RatingProvider
    }
    total = sum(weighted.values())

    mix = [
        ScoringProvider(
            provider=provider,
            provider_name=registry.label(provider),
            weight=getattr(weights, provider.value, 0.0),
            # None rather than 0 on a catalog with nothing scored yet: there is
            # no whole to take a share of, and a column of zeroes would read as
            # "every provider contributed nothing" to a catalog that has simply
            # not been enriched.
            share=(weighted[provider] / total) * 100 if total > 0 else None,
            titles_rated=counted.get(provider, (0, 0.0))[0],
            is_israeli=provider.is_israeli,
        )
        for provider in RatingProvider
    ]
    # Heaviest contributor first, then by weight, so a table of nothing-yet rows
    # still reads in the order the configuration intends.
    mix.sort(key=lambda row: (row.share or 0.0, row.weight), reverse=True)
    return mix


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
    coverage: _Coverage,
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
        effective_enabled=_effective(source, settings),
        title_count=coverage.titles,
        titles_with_poster=coverage.with_poster,
        titles_with_score=coverage.with_score,
        titles_enriched=coverage.enriched,
        backfill_requested_at=source.backfill_requested_at,
        pending_reviews=pending_reviews,
        last_sync_at=last_at,
        last_sync_status=last_status,
        stale=source.active and _is_stale(last, stale_before),
    )


def _effective(source: Source, settings: Settings) -> bool:
    """Whether this source is actually being collected.

    The same three answers the fetcher consults, in the same order: an
    operator's switch, then the config file, then what the plugin declares.
    The last one reaches here through the database because the API cannot ask
    a plugin anything - and while it could not, the tab reported a source as on
    for want of a line in a config file that was never going to mention it.
    """
    if source.enabled is not None:
        return source.enabled
    configured = settings.sources.get(source.key)
    return configured.enabled if configured is not None else source.default_enabled


def _is_stale(
    last: tuple[dt.datetime | None, FetchStatus | None] | None,
    stale_before: dt.datetime,
) -> bool:
    """A source is stale when its last *successful* sync is too old, or absent."""
    if last is None:
        return True
    last_at, _status = last
    return last_at is None or last_at < stale_before


#: Enrichment outcomes that count as done with. ``OK`` found ratings; ``NO_DATA``
#: established there are none to find, which is true of most of a catalog this
#: local and is as complete as that title is ever going to get. The other two are
#: unfinished business - ``NO_MATCH`` cannot be asked about until matching
#: improves, ``ERROR`` is a provider that was down - and counting them as
#: enriched would paint a column green precisely when every attempt had failed.
_SETTLED = (EnrichOutcome.OK, EnrichOutcome.NO_DATA)


@dataclass(frozen=True, slots=True)
class _Coverage:
    """How complete one source's titles are, counted rather than divided."""

    titles: int = 0
    with_poster: int = 0
    with_score: int = 0
    enriched: int = 0


def _coverage_by_source(session: Session) -> dict[int, _Coverage]:
    """What each source offers, and how much of it the catalog has filled in.

    One query with conditional counts rather than four: the joins are the
    expensive part, and a source list that costs four table scans per column
    is a page an operator stops opening.
    """
    distinct = func.count(func.distinct(Availability.title_id))
    rows = session.execute(
        select(
            Availability.source_id,
            distinct,
            func.count(
                func.distinct(case((Title.poster_path.is_not(None), Availability.title_id)))
            ),
            func.count(
                func.distinct(case((AggregateScore.title_id.is_not(None), Availability.title_id)))
            ),
            func.count(
                func.distinct(case((EnrichAttempt.outcome.in_(_SETTLED), Availability.title_id)))
            ),
        )
        .join(Title, Title.id == Availability.title_id)
        .outerjoin(AggregateScore, AggregateScore.title_id == Title.id)
        .outerjoin(EnrichAttempt, EnrichAttempt.title_id == Title.id)
        .where(Availability.is_current.is_(True))
        .group_by(Availability.source_id)
    ).all()
    return {
        source_id: _Coverage(titles, poster, score, enriched)
        for source_id, titles, poster, score, enriched in rows
    }


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
