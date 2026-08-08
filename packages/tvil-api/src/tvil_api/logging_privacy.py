"""Keep user data out of the log file.

The access log records the path a request asked for, query string included. On
the catalog that is useful — it is what somebody searched for in public. On a
user route it is a personal detail written to disk on every request, so those
lines lose their query string (docs.internal/09-auth-privacy.md).
"""

from __future__ import annotations

import logging

from tvil_api.caching import is_private_path

#: uvicorn logs each request through this one.
ACCESS_LOGGER = "uvicorn.access"

REDACTION = "?<redacted>"


def redact_private_query(value: str) -> str:
    """Drop the query string from a user-route path, leaving a marker."""
    path, separator, _query = value.partition("?")
    if not separator or not is_private_path(path):
        return value
    return f"{path}{REDACTION}"


class PrivateQueryFilter(logging.Filter):
    """Strip query strings from log records that name a user route.

    A filter rather than a formatter: it has to apply to whatever handler the
    deployment configured, and it must run before anything is written.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(
                redact_private_query(argument) if isinstance(argument, str) else argument
                for argument in record.args
            )
        return True


def install_log_filters() -> None:
    """Attach the filter to the access log. Safe to call more than once."""
    logger = logging.getLogger(ACCESS_LOGGER)
    if not any(isinstance(existing, PrivateQueryFilter) for existing in logger.filters):
        logger.addFilter(PrivateQueryFilter())
