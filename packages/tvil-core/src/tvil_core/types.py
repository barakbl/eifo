"""Custom SQLAlchemy column types."""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import DateTime, Dialect
from sqlalchemy.types import TypeDecorator


def utcnow() -> dt.datetime:
    """Current time as a timezone-aware UTC datetime."""
    return dt.datetime.now(dt.UTC)


class UtcDateTime(TypeDecorator[dt.datetime]):
    """A datetime column that is always timezone-aware UTC in Python.

    SQLite has no timezone support, so naive values would silently leak out of
    the database and compare wrongly against aware ones. This normalises on the
    way in (converting any aware value to UTC, rejecting naive ones) and
    re-attaches UTC on the way out.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: dt.datetime | None, dialect: Dialect) -> Any:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime rejected; TVIL stores UTC-aware datetimes only")
        return value.astimezone(dt.UTC)

    def process_result_value(self, value: Any, dialect: Dialect) -> dt.datetime | None:
        if value is None:
            return None
        assert isinstance(value, dt.datetime)
        if value.tzinfo is None:
            return value.replace(tzinfo=dt.UTC)
        return value.astimezone(dt.UTC)
