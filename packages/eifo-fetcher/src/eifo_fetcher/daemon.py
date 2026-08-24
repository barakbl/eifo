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

from eifo_core.db import create_engine_from_settings, make_session_factory, require_schema
from eifo_core.fts import ensure_search_triggers
from eifo_core.settings import Settings
from eifo_fetcher.heartbeat import ping
from eifo_fetcher.http import HttpClient
from eifo_fetcher.lock import AlreadyRunningError, single_flight
from eifo_fetcher.runner import enrich_all, fetch_images, sync_all

logger = logging.getLogger("eifo.fetch.daemon")

#: A machine asleep at 03:00 should run the catalog on waking rather than skip
#: the night. APScheduler's default grace is one second, which treats every
#: suspended laptop and every busy Pi as a missed night.
MISFIRE_GRACE_SECONDS = 3600

#: Phase names in dependency order, each with the function that runs it.
PHASES = ("sync", "enrich", "images")


def _parse_time(value: str) -> tuple[int, int]:
    """Parse ``HH:MM`` from the schedule configuration."""
    hour, _, minute = value.partition(":")
    try:
        return int(hour), int(minute)
    except ValueError as exc:
        raise ValueError(f"invalid schedule time {value!r}; expected HH:MM") from exc


def _run_phase(settings: Settings, phase: str) -> bool:
    """Run one phase with its own engine, so a failure cannot poison the next.

    Returns:
        Whether it got through without raising. A phase that fails does not stop
        the chain: enrichment still has yesterday's titles to work with, and
        artwork still has yesterday's URLs, so there is more to gain from
        carrying on than from standing still.
    """
    engine = create_engine_from_settings(settings)
    try:
        require_schema(engine, settings.db_url)
        ensure_search_triggers(engine)
        session_factory = make_session_factory(engine)
        with HttpClient() as http:
            if phase == "sync":
                sync_all(session_factory, settings, http=http)
            elif phase == "enrich":
                enrich_all(session_factory, settings, http=http)
            else:
                fetch_images(session_factory, settings, http=http)
        return True
    except Exception:
        # A scheduled run must never take the daemon down with it.
        logger.exception("scheduled %s failed", phase)
        return False
    finally:
        engine.dispose()


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
                logger.info("running %s", phase)
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
    logger.info(
        "scheduled %s nightly at %02d:%02d UTC",
        " -> ".join(PHASES),
        hour,
        minute,
    )

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("daemon stopped")
    return 0
