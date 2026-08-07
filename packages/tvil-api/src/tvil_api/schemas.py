"""Response models.

Pydantic models are the API contract; ORM objects never leave a router.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from pydantic import BaseModel, Field

from tvil_core.enums import (
    FetchStatus,
    OfferType,
    RatingProvider,
    SourceKind,
    TitleKind,
)


class Page[T](BaseModel):
    """A page of results, with enough context to render a pager."""

    items: list[T]
    page: int
    page_size: int
    total: int

    @property
    def pages(self) -> int:
        return max(1, -(-self.total // self.page_size))


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


class SourceOut(BaseModel):
    """A tracked service, including ones no longer tracked."""

    id: int
    key: str
    name: str
    kind: SourceKind
    website_url: str
    logo_url: str | None = None
    active: bool
    deactivated_at: dt.datetime | None = None
    title_count: int = 0
    last_synced_at: dt.datetime | None = None


class GenreOut(BaseModel):
    id: int
    name_en: str
    name_he: str | None = None


class AvailabilityOut(BaseModel):
    """Where a title can be watched — or where it used to be.

    ``is_current`` and ``source_active`` drive two different badges: content
    that went away, and a source TVIL no longer tracks at all.
    """

    source_key: str
    source_name: str
    source_kind: SourceKind
    source_active: bool
    offer_type: OfferType
    is_current: bool
    deep_link_url: str | None = None
    last_seen: dt.datetime
    gone_since: dt.datetime | None = None


class RatingOut(BaseModel):
    """One provider's score, always with a link back to its origin."""

    provider: RatingProvider
    provider_name: str
    score_raw: float
    #: Formatted the way the provider itself shows it ("8.4", "92%").
    score_display: str
    score_normalized: int
    vote_count: int | None = None
    url: str | None = None


class AggregateOut(BaseModel):
    """The combined score and the working behind it."""

    score: int | None = None
    score_israeli: int | None = None
    components: dict[str, Any] = Field(default_factory=dict)


class TitleCard(BaseModel):
    """Everything the results grid needs, in one object.

    Availability is embedded rather than fetched per card: a grid of 24 titles
    should cost one round trip, not twenty-five.
    """

    id: int
    type: TitleKind
    name_he: str | None = None
    name_en: str | None = None
    year: int | None = None
    poster_url: str | None = None
    score: int | None = None
    score_israeli: int | None = None
    genres: list[GenreOut] = Field(default_factory=list)
    availability: list[AvailabilityOut] = Field(default_factory=list)


class TitleDetail(TitleCard):
    """A single title in full."""

    overview_he: str | None = None
    overview_en: str | None = None
    runtime_minutes: int | None = None
    seasons: int | None = None
    status: str | None = None
    backdrop_url: str | None = None
    ratings: list[RatingOut] = Field(default_factory=list)
    aggregate: AggregateOut = Field(default_factory=AggregateOut)
