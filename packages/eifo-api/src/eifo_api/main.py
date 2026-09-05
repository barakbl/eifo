"""ASGI entry point: ``uvicorn eifo_api.main:app``.

This module is where the API stops being a library and becomes a program, so it
is where logging is configured. Uvicorn sets up its own loggers and leaves the
root alone, which meant every line this application logged about itself went
nowhere - including "database migrated to 0025", the one line that says an
upgrade just rewrote the schema under a running service. The only messages that
reached a terminal were warnings, and those only because Python's last-resort
handler catches them.

Not in ``create_app``: the test suite builds dozens of apps, and a function that
reconfigures process-wide logging every time it is called is a poor neighbour.
"""

import logging
import os

from eifo_api.app import create_app

#: Overridable, because a container's log shipper may want DEBUG and a laptop
#: does not. Anything Python's logging accepts by name.
LOG_LEVEL = os.environ.get("EIFO_LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
)
# It logs the full request URL, and OAuth exchanges carry secrets in those.
logging.getLogger("httpx").setLevel(logging.WARNING)

app = create_app()

__all__ = ["app"]
