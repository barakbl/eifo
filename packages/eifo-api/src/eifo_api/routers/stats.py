"""Catalog statistics: how each service has grown, and what it holds now.

Two questions, one endpoint each. ``/stats/growth`` is history - what every sync
added, read back off the tally the run already wrote. ``/stats/services`` is the
present - titles, offers and price coverage per service, counted from the
catalog itself.

Open to every member, like the rest of the catalog. Nothing here is operator
material: no logs, no errors, no review queue - only counts a curious viewer
could have worked out by paging through the grid.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any

from fastapi import APIRouter, Query
from sqlalchemy import case, distinct, func, select

from eifo_api.deps import SessionDep
from eifo_api.schemas import CatalogSnapshot, PriceCoverage, ServiceSnapshot, SyncGrowth
from eifo_core.enums import FetchPhase, FetchStatus, OfferType, TitleKind
from eifo_core.models import Availability, FetchRun, Source, Title

router = APIRouter(prefix="/stats", tags=["stats"])

#: How far back growth reaches when nobody says. A year of nightly syncs across
#: every service is a few thousand small rows, which is still one request.
DEFAULT_GROWTH_DAYS = 365
MAX_GROWTH_DAYS = 3650

#: When a sync counts as loading a catalog rather than growing one.
#:
#: Ordinary nights add a sliver of what a service carries - under 5% on every
#: service on record. The syncs that found an existing catalog added most of
#: it: Apple's store 94% of what it saw, Disney+ 93%, Netflix's first
#: uncapped sweep 40%. "The first sync" is not a usable test, because the early
#: ones were capped (80 titles, then 2,000) and the real load landed days
#: later. A quarter of the run's own listings is well clear of both.
CATALOG_LOAD_SHARE = 0.25
#: And never for a handful: five new titles on a service showing twelve is a
#: busy night, not a load.
CATALOG_LOAD_MIN = 50


@router.get("/growth", response_model=list[SyncGrowth], summary="What each sync added")
def growth(
    session: SessionDep,
    days: Annotated[int, Query(ge=1, le=MAX_GROWTH_DAYS)] = DEFAULT_GROWTH_DAYS,
) -> list[SyncGrowth]:
    """Every sync of every service in the window, oldest first.

    Read from the tally each run already keeps rather than reconstructed from
    ``first_seen``: an offer that arrived and left again is gone from the
    catalog's present but was still added that night, and only the run
    remembers it.

    A run with no tally - one that crashed before it counted anything - is left
    out rather than reported as a night that added nothing.
    """
    since = dt.datetime.now(dt.UTC) - dt.timedelta(days=days)
    names = dict(session.execute(select(Source.key, Source.name)).tuples().all())

    runs = session.scalars(
        select(FetchRun)
        .where(
            FetchRun.phase == FetchPhase.SYNC,
            FetchRun.source_key.is_not(None),
            FetchRun.started_at >= since,
        )
        .order_by(FetchRun.started_at, FetchRun.id)
    ).all()

    return [
        SyncGrowth(
            run_id=run.id,
            source_key=run.source_key,
            source_name=names.get(run.source_key, run.source_key),
            status=run.status,
            started_at=run.started_at,
            finished_at=run.finished_at,
            items_seen=_count(run.stats, "items_seen"),
            offers_added=_count(run.stats, "availability_created"),
            titles_created=_count(run.stats, "titles_created"),
            offers_retired=_count(run.stats, "retired"),
            catalog_load=_is_catalog_load(run.stats),
        )
        for run in runs
        if run.source_key is not None and _has_tally(run.stats)
    ]


@router.get("/services", response_model=CatalogSnapshot, summary="What each service holds now")
def services(session: SessionDep) -> CatalogSnapshot:
    """Titles, offers and price coverage per service, as the catalog stands.

    Current offers only, on every source including retired ones - a retired
    source keeps its row so the table can say it is retired, rather than the
    service quietly vanishing from the list.
    """
    rent, buy = OfferType.RENT, OfferType.BUY
    priced = Availability.price_minor.is_not(None)

    def tally(condition: Any) -> Any:
        return func.coalesce(func.sum(case((condition, 1), else_=0)), 0)

    def titles_where(condition: Any) -> Any:
        return func.count(distinct(case((condition, Availability.title_id), else_=None)))

    rows = session.execute(
        select(
            Availability.source_id,
            func.count(distinct(Availability.title_id)).label("titles"),
            titles_where(Title.type == TitleKind.MOVIE).label("movies"),
            titles_where(Title.type == TitleKind.SERIES).label("series"),
            func.count().label("offers"),
            *[
                tally(Availability.offer_type == kind).label(f"n_{kind.value}")
                for kind in OfferType
            ],
            tally((Availability.offer_type == rent) & priced).label("rent_priced"),
            tally((Availability.offer_type == buy) & priced).label("buy_priced"),
            titles_where((Availability.offer_type == rent) & priced).label("rent_titles"),
            titles_where((Availability.offer_type == buy) & priced).label("buy_titles"),
        )
        .join(Title, Title.id == Availability.title_id)
        .where(Availability.is_current.is_(True))
        .group_by(Availability.source_id)
    ).all()
    by_source = {row.source_id: row for row in rows}
    synced = _last_sync(session)

    snapshots = []
    for source in session.scalars(select(Source).order_by(Source.name)).all():
        row = by_source.get(source.id)
        if row is None:
            snapshots.append(
                ServiceSnapshot(
                    key=source.key,
                    name=source.name,
                    kind=source.kind,
                    active=source.active,
                    last_synced_at=synced.get(source.key),
                )
            )
            continue
        by_type = {kind: int(getattr(row, f"n_{kind.value}")) for kind in OfferType}
        snapshots.append(
            ServiceSnapshot(
                key=source.key,
                name=source.name,
                kind=source.kind,
                active=source.active,
                titles=row.titles,
                movies=row.movies,
                series=row.series,
                offers=row.offers,
                offers_by_type={kind: n for kind, n in by_type.items() if n},
                rent=_coverage(by_type[rent], int(row.rent_priced), row.rent_titles),
                buy=_coverage(by_type[buy], int(row.buy_priced), row.buy_titles),
                last_synced_at=synced.get(source.key),
            )
        )

    # The catalog's own totals, because a title on three services is one title.
    totals = session.execute(
        select(
            func.count(distinct(Availability.title_id)),
            func.count(distinct(case((Title.type == TitleKind.MOVIE, Title.id), else_=None))),
            func.count(distinct(case((Title.type == TitleKind.SERIES, Title.id), else_=None))),
            func.count(),
        )
        .join(Title, Title.id == Availability.title_id)
        .where(Availability.is_current.is_(True))
    ).one()

    return CatalogSnapshot(
        generated_at=dt.datetime.now(dt.UTC),
        titles=totals[0],
        movies=totals[1],
        series=totals[2],
        offers=totals[3],
        services=snapshots,
    )


def _coverage(offers: int, priced: int, titles_priced: int) -> PriceCoverage:
    return PriceCoverage(
        offers=offers, priced=priced, unpriced=offers - priced, titles_priced=titles_priced
    )


def _is_catalog_load(stats: dict[str, Any] | None) -> bool:
    """Whether a sync found a catalog that was already there."""
    added = _count(stats, "availability_created")
    seen = _count(stats, "items_seen")
    return added >= CATALOG_LOAD_MIN and added >= CATALOG_LOAD_SHARE * seen


def _last_sync(session: SessionDep) -> dict[str, dt.datetime]:
    rows = session.execute(
        select(FetchRun.source_key, func.max(FetchRun.finished_at))
        .where(
            FetchRun.phase == FetchPhase.SYNC,
            FetchRun.status == FetchStatus.OK,
            FetchRun.source_key.is_not(None),
        )
        .group_by(FetchRun.source_key)
    ).tuples()
    return {key: when for key, when in rows if key is not None and when is not None}


def _has_tally(stats: dict[str, Any] | None) -> bool:
    """Whether a run got far enough to count anything."""
    tally = stats or {}
    return any(key in tally for key in ("items_seen", "availability_created", "titles_created"))


def _count(stats: dict[str, Any] | None, key: str) -> int:
    """One figure from a tally, as a number whatever the tally held."""
    value = (stats or {}).get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0
