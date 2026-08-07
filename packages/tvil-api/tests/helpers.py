"""Typed helpers shared by the API test suite."""

from __future__ import annotations

import datetime as dt
from typing import Protocol

from tvil_core.enums import FetchStatus


class SeedSource(Protocol):
    """Signature of the ``seed_source`` fixture."""

    def __call__(
        self,
        key: str = ...,
        *,
        name: str = ...,
        active: bool = ...,
        synced_at: dt.datetime | None = ...,
        status: FetchStatus = ...,
    ) -> None: ...
