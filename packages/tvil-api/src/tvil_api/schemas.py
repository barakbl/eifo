"""Response models.

Pydantic models are the API contract; ORM objects never leave a router.
"""

from __future__ import annotations

import datetime as dt

from pydantic import BaseModel

from tvil_core.enums import FetchStatus, SourceKind


class Attribution(BaseModel):
    """A data-licence credit the client is required to display."""

    text: str
    url: str | None = None


class SourceFreshness(BaseModel):
    """When a source's catalog was last confirmed."""

    key: str
    name: str
    kind: SourceKind
    active: bool
    last_sync_at: dt.datetime | None = None
    last_sync_status: FetchStatus | None = None
    stale: bool = False


class MetaResponse(BaseModel):
    """Service metadata: data freshness plus required attribution.

    Doubles as the health endpoint — ``stale`` per source is the signal that a
    fetcher has stopped working (docs.internal/11-ops-install.md).
    """

    version: str
    generated_at: dt.datetime
    title_count: int
    sources: list[SourceFreshness]
    attribution: list[Attribution]
