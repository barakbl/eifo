"""Recording that a run happened - including the runs that do not finish.

A ``fetch_runs`` row used to be written when a phase completed, which meant the
runs most worth knowing about left no trace at all. A fetcher stopped by an OOM,
a power cut or a closed laptop lid produced exactly the same database as a night
when nothing was scheduled: no row, nothing to explain, nothing to alert on.

So the row is opened when the phase starts and closed when it ends. Anything
still open when the next fetcher comes along did not finish, and can be marked
as such - the single-flight lock is what makes that safe to assert, since a
fetcher holding it is the only one there is, so an open row it did not open
itself belongs to a process that is gone.

The opening write is committed on its own. That is the point: a row nobody can
see until the phase ends would be no better than what it replaces.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from eifo_core.enums import FetchPhase, FetchStatus
from eifo_core.models import FetchRun
from eifo_core.types import utcnow

logger = logging.getLogger("eifo.fetch.runs")


def open_run(
    session: Session,
    *,
    phase: FetchPhase,
    source_key: str | None = None,
    started_at: dt.datetime | None = None,
) -> FetchRun:
    """Record that a phase has begun, and commit so others can see it."""
    run = FetchRun(
        source_key=source_key,
        phase=phase,
        started_at=started_at or utcnow(),
        finished_at=None,
        status=FetchStatus.RUNNING,
        stats={},
    )
    session.add(run)
    session.commit()
    return run


def close_run(
    session: Session,
    run: FetchRun,
    *,
    status: FetchStatus,
    stats: dict[str, Any],
) -> None:
    """Record how a phase ended."""
    run.status = status
    run.stats = stats
    run.finished_at = utcnow()
    session.commit()


def close_abandoned_runs(session: Session) -> int:
    """Mark runs left open by a process that is no longer here.

    Safe to assert because the caller holds the fetcher lock: it is the only
    fetcher there is, and it has not opened anything yet, so every row still
    RUNNING belongs to a run that ended without being able to say so.

    Returns:
        How many were marked, which is normally zero.
    """
    abandoned = list(
        session.scalars(select(FetchRun).where(FetchRun.status == FetchStatus.RUNNING)).all()
    )
    if not abandoned:
        return 0

    now = utcnow()
    for run in abandoned:
        run.status = FetchStatus.CRASHED
        run.finished_at = now
        run.stats = {**(run.stats or {}), "errors": ["run ended without recording an outcome"]}
        logger.warning(
            "%s run for %s started %s never finished; marking it crashed",
            run.phase,
            run.source_key or "-",
            run.started_at,
        )
    session.commit()
    return len(abandoned)
