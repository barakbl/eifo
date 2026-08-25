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
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from eifo_core.enums import FetchPhase, FetchStatus
from eifo_core.models import FetchRun
from eifo_core.types import utcnow

logger = logging.getLogger("eifo.fetch.runs")

#: Where the fetcher's own log records come from. Capturing this rather than the
#: root logger keeps somebody else's library chatter out of the row.
FETCHER_LOGGER = "eifo"

#: How much of a run's output to keep, in bytes of formatted text.
#:
#: The tail, not the whole thing: a run that failed explains itself in its last
#: few lines, and a full sync of a large source writes tens of thousands. These
#: rows are never deleted, so the budget is per run forever - 64KB is around
#: seven hundred lines, which is more than anybody reads and small enough that a
#: year of nightly runs is megabytes rather than gigabytes.
MAX_LOG_BYTES = 64_000

_LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s %(message)s"


class RunLogCapture(logging.Handler):
    """Keeps the tail of what a run said, for its ``fetch_runs`` row.

    A ring buffer rather than a growing list: a sync of a source with 5,000
    listings can log a line per listing, and the interesting part is always the
    end. Old lines fall off the front once the budget is spent.
    """

    def __init__(self, max_bytes: int = MAX_LOG_BYTES) -> None:
        super().__init__()
        self.setFormatter(logging.Formatter(_LOG_FORMAT))
        self._max_bytes = max_bytes
        self._lines: deque[str] = deque()
        self._size = 0
        #: Set when the budget has pushed at least one line out, so the reader
        #: is told the log is a tail rather than the whole run.
        self.truncated = False

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
        except Exception:  # pragma: no cover - a broken format string
            self.handleError(record)
            return

        self._lines.append(line)
        self._size += len(line) + 1
        while self._size > self._max_bytes and len(self._lines) > 1:
            self._size -= len(self._lines.popleft()) + 1
            self.truncated = True

    def text(self) -> str | None:
        """What the run said, or None if it said nothing worth storing."""
        if not self._lines:
            return None
        body = "\n".join(self._lines)
        if self.truncated:
            return f"[earlier lines dropped; showing the last {self._size // 1000}KB]\n{body}"
        return body


@contextmanager
def capture_log(level: int = logging.INFO) -> Iterator[RunLogCapture]:
    """Collect the fetcher's log records for the duration of a run.

    Attached to the ``eifo`` logger rather than the root, so somebody else's
    library chatter stays out of the row, and removed again on the way out
    however the block ends - a handler left behind would go on collecting into
    a run that had already been written.

    Records the process is configured to emit, and no others: this adds a
    handler and does not touch anybody's level. ``eifo-fetch`` configures INFO
    (DEBUG with ``-v``), so a normal run records everything it says; a caller
    who has deliberately quietened the fetcher gets a row as quiet as the
    console they asked for, which is the honest reading of "what the run said".
    """
    handler = RunLogCapture()
    handler.setLevel(level)
    target = logging.getLogger(FETCHER_LOGGER)
    target.addHandler(handler)
    try:
        yield handler
    finally:
        target.removeHandler(handler)
        handler.close()


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
    log: str | None = None,
) -> None:
    """Record how a phase ended, and what it said while it ran."""
    run.status = status
    run.stats = stats
    run.finished_at = utcnow()
    if log:
        run.log = log
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
