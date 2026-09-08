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
from eifo_core import logs
from eifo_core.settings import get_settings

#: Overridable, because a container's log shipper may want DEBUG and a laptop
#: does not. Anything Python's logging accepts by name.
LOG_LEVEL = os.environ.get("EIFO_LOG_LEVEL", "INFO").upper()

logs.configure_console(LOG_LEVEL)

#: What the file is called when ``log_dir`` is configured.
PROGRAM = "eifo-api"

_settings = get_settings()
_log_file = logs.add_file(_settings.log_dir, PROGRAM, LOG_LEVEL)
if _log_file is not None:
    # Uvicorn's own loggers do not propagate to the root, so the access log -
    # which is the record of every request and the status it got - would be the
    # one thing missing from the file. Attached rather than reconfigured: the
    # console output stays exactly as uvicorn arranged it.
    logs.attach_to(logging.getLogger(), "uvicorn", "uvicorn.access", "uvicorn.error")

app = create_app(_settings)

__all__ = ["app"]
