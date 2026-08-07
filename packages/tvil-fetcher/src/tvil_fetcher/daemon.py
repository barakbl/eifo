"""Scheduled phases for installations not using system cron.

Cron remains the documented default (docs.internal/11-ops-install.md); this
exists so the Docker deployment needs no cron daemon inside the container.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from tvil_core.db import create_engine_from_settings, make_session_factory, require_schema
from tvil_core.settings import Settings
from tvil_fetcher.http import HttpClient
from tvil_fetcher.runner import fetch_images, sync_all

logger = logging.getLogger("tvil.fetch.daemon")


def _parse_time(value: str) -> tuple[int, int]:
    """Parse ``HH:MM`` from the schedule configuration."""
    hour, _, minute = value.partition(":")
    try:
        return int(hour), int(minute)
    except ValueError as exc:
        raise ValueError(f"invalid schedule time {value!r}; expected HH:MM") from exc


def _phases(settings: Settings) -> list[tuple[str, str, Callable[[], None]]]:
    """The scheduled phases, each as (name, HH:MM, callable)."""

    def sync() -> None:
        _run_phase(settings, "sync")

    def images() -> None:
        _run_phase(settings, "images")

    return [
        ("sync", settings.schedule.sync, sync),
        ("images", settings.schedule.images, images),
    ]


def _run_phase(settings: Settings, phase: str) -> None:
    """Run one phase with its own engine, so a failure cannot poison the next."""
    engine = create_engine_from_settings(settings)
    try:
        require_schema(engine, settings.db_url)
        session_factory = make_session_factory(engine)
        with HttpClient() as http:
            if phase == "sync":
                sync_all(session_factory, settings, http=http)
            else:
                fetch_images(session_factory, settings, http=http)
    except Exception:
        # A scheduled run must never take the daemon down with it.
        logger.exception("scheduled %s failed", phase)
    finally:
        engine.dispose()


def run_once(settings: Settings) -> int:
    """Run every scheduled phase immediately, then return."""
    for name, _at, job in _phases(settings):
        logger.info("running %s", name)
        job()
    return 0


def run_daemon(settings: Settings) -> int:
    """Block, running each phase at its configured time."""
    scheduler = BlockingScheduler(timezone="UTC")

    for name, at, job in _phases(settings):
        hour, minute = _parse_time(at)
        scheduler.add_job(
            job,
            CronTrigger(hour=hour, minute=minute),
            id=name,
            name=f"tvil {name}",
            max_instances=1,
            coalesce=True,
        )
        logger.info("scheduled %s daily at %02d:%02d UTC", name, hour, minute)

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("daemon stopped")
    return 0
