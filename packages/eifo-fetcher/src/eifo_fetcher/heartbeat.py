"""Telling a watchdog the nightly run happened.

The catalog went seventeen days without a sync and nothing said so. Every part
of the system was working as designed - the daemon was simply not running, and
a daemon that is not running has no way to report that. Only something outside
the box can notice an absence, which is what a dead man's switch is: the run
pings on its way past, and the watchdog raises the alarm when a ping does not
arrive.

Any service that takes a plain GET works (healthchecks.io, Uptime Kuma push
monitors). The URL is pinged bare on success, with ``/start`` when the run
begins and ``/fail`` when it does not finish - the convention healthchecks.io
uses and the others tolerate.
"""

from __future__ import annotations

import logging

import httpx

from eifo_core.settings import Settings

logger = logging.getLogger("eifo.fetch.heartbeat")

#: Short: a watchdog that is slow or down must not hold up the catalog.
TIMEOUT_SECONDS = 10.0


def ping(settings: Settings, event: str = "") -> None:
    """Tell the watchdog where the run got to, if one is configured.

    Best effort in the strongest sense: every failure is swallowed at debug
    level. Monitoring exists to report on the run, and a run that fell over
    because its monitoring was unreachable would be reporting on itself.

    Args:
        event: ``"start"``, ``"fail"``, or empty for success.
    """
    configured = settings.healthcheck_url
    if configured is None:
        return

    url = configured.get_secret_value().rstrip("/")
    if event:
        url = f"{url}/{event}"

    try:
        httpx.get(url, timeout=TIMEOUT_SECONDS, follow_redirects=True)
    except Exception as exc:
        # Never the URL itself: it carries the token that identifies the check.
        logger.debug("heartbeat ping failed: %s", type(exc).__name__)
