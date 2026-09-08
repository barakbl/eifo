"""The nightly run, and the schedule that triggers it.

Cron remains the documented default (docs.internal/11-ops-install.md); this
exists so the Docker deployment needs no cron daemon inside the container. Both
paths run the same thing: ``run_nightly`` is what ``eifo-fetch all`` runs too,
so a catalog updated by cron and one updated by the daemon are updated
identically.

The phases are a chain rather than three jobs at three times. They always were
in effect - enrichment needs the titles sync creates, artwork needs the URLs
enrichment fills in - but each had its own hour, which only worked while every
phase finished inside its slot. A full sync stopped doing that: at two hours it
runs past the enrich trigger, and the two then competed for the same database.
"""

from __future__ import annotations

import logging

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from eifo_core.enums import FetchPhase
from eifo_core.settings import Settings
from eifo_fetcher.credentials import api_client
from eifo_fetcher.heartbeat import ping
from eifo_fetcher.http import HttpClient
from eifo_fetcher.lock import AlreadyRunningError, single_flight
from eifo_fetcher.runner import enrich_all, fetch_images, phase_client, sync_all

logger = logging.getLogger("eifo.fetch.daemon")

#: A machine asleep at 03:00 should run the catalog on waking rather than skip
#: the night. APScheduler's default grace is one second, which treats every
#: suspended laptop and every busy Pi as a missed night.
MISFIRE_GRACE_SECONDS = 3600

#: The phases, in dependency order: enrichment needs the titles sync creates,
#: artwork needs the URLs enrichment fills in.
PHASES = (FetchPhase.SYNC, FetchPhase.ENRICH, FetchPhase.IMAGES)

#: How often to look for a source somebody has just switched on.
#:
#: Short, because this is somebody sitting in front of the Manage tab having
#: just flipped a switch, and cheap, because with nothing pending it is one
#: request answered by an indexed read of a table with a dozen rows in it.
BACKFILL_POLL_SECONDS = 30


def _parse_time(value: str) -> tuple[int, int]:
    """Parse ``HH:MM`` from the schedule configuration."""
    hour, _, minute = value.partition(":")
    try:
        return int(hour), int(minute)
    except ValueError as exc:
        raise ValueError(f"invalid schedule time {value!r}; expected HH:MM") from exc


def _run_phase(settings: Settings, phase: FetchPhase) -> bool:
    """Run one phase with its own client, so a failure cannot poison the next.

    It opens no database. Every phase writes through the API now, which is what
    lets the daemon run on a machine the catalog is not on - the case the whole
    of this refactor is for, and one that used to be impossible here because
    this function reached for an engine before it did anything else.

    Returns:
        Whether it got through without raising. A phase that fails does not stop
        the chain: enrichment still has yesterday's titles to work with, and
        artwork still has yesterday's URLs, so there is more to gain from
        carrying on than from standing still.
    """
    try:
        with HttpClient() as http, phase_client(settings, phase) as api:
            if phase is FetchPhase.SYNC:
                sync_all(settings, http=http, api=api)
            elif phase is FetchPhase.ENRICH:
                enrich_all(settings, http=http, api=api)
            else:
                fetch_images(settings, http=http, api=api)
        return True
    except Exception:
        # A scheduled run must never take the daemon down with it.
        logger.exception("scheduled %s failed", phase.value)
        return False


def run_backfills(settings: Settings) -> bool:
    """Pull the catalogue of any source an operator has just switched on.

    Which sources those are is asked of the API rather than read from a table:
    this process has no database, and the ask was made in the Manage tab on the
    machine that does.

    Sync only. Enrichment and artwork are the nightly chain's business and cost
    far more than the titles do - what the operator asked to see is the service
    appearing in the catalog, and that is what sync produces.

    Returns:
        Whether anything ran without error. Nothing pending is a success: there
        was nothing to get wrong.
    """
    try:
        # A bare client, not phase_client: this runs every thirty seconds and
        # nearly always finds nothing, and the per-phase housekeeping - posting
        # the previous attempt, re-declaring what credits every score - is work
        # for a run that is about to happen rather than for a question.
        with api_client(settings) as api:
            wanted = api.requested_backfills()
            if not wanted:
                return True

            # The nightly chain and this share one lock: a backfill must not
            # run beside a full sync, and if the nightly run has the lock it
            # will pick these up itself in a few hours anyway - the ask keeps
            # until then.
            try:
                with (
                    single_flight(settings),
                    phase_client(settings, FetchPhase.SYNC, api=api),
                    HttpClient() as http,
                ):
                    logger.info("backfilling on request: %s", ", ".join(wanted))
                    # sync_all clears the asks it answered, so a fetcher killed
                    # partway leaves them standing and the next tick retries.
                    sync_all(settings, http=http, api=api, only=wanted)
            except AlreadyRunningError:
                logger.info(
                    "backfill deferred, another fetcher holds the lock: %s", ", ".join(wanted)
                )
        return True
    except Exception:
        # Same rule as a scheduled phase: never take the daemon down.
        logger.exception("requested backfill failed")
        return False


def run_nightly(settings: Settings) -> bool:
    """Sync, enrich and fetch artwork, in that order, holding the fetcher lock.

    Returns:
        Whether every phase succeeded. A run that could not take the lock counts
        as a success: another fetcher is doing the work, which is the outcome
        that was wanted.
    """
    try:
        with single_flight(settings):
            ping(settings, "start")
            ok = True
            for phase in PHASES:
                logger.info("running %s", phase.value)
                ok &= _run_phase(settings, phase)
            ping(settings, "" if ok else "fail")
            return ok
    except AlreadyRunningError as exc:
        logger.warning("%s", exc)
        return True


def run_once(settings: Settings) -> bool:
    """Run the whole chain immediately, then return whether it all worked."""
    return run_nightly(settings)


def run_daemon(settings: Settings) -> int:
    """Block, running the nightly chain at its configured time."""
    scheduler = BlockingScheduler(timezone="UTC")
    hour, minute = _parse_time(settings.schedule.nightly)

    scheduler.add_job(
        lambda: run_nightly(settings),
        CronTrigger(hour=hour, minute=minute),
        id="nightly",
        name="eifo nightly",
        # Between them: one run at a time, a backlog collapsed into a single
        # run rather than a queue of them, and an hour's tolerance for a
        # machine that was not awake at the appointed minute.
        max_instances=1,
        coalesce=True,
        misfire_grace_time=MISFIRE_GRACE_SECONDS,
    )
    scheduler.add_job(
        lambda: run_backfills(settings),
        IntervalTrigger(seconds=BACKFILL_POLL_SECONDS),
        id="backfill",
        name="eifo requested backfills",
        max_instances=1,
        coalesce=True,
    )
    logger.info(
        "scheduled %s nightly at %02d:%02d UTC, requested backfills every %ds",
        " -> ".join(phase.value for phase in PHASES),
        hour,
        minute,
        BACKFILL_POLL_SECONDS,
    )

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("daemon stopped")
    return 0
