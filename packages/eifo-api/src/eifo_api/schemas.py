"""Response models.

Pydantic models are the API contract; ORM objects never leave a router.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from eifo_core.enums import (
    AuthProvider,
    FetchStatus,
    ItemStatus,
    OfferType,
    RatingProvider,
    SourceKind,
    TitleKind,
)
from eifo_core.models import (
    DISPLAY_NAME_MAX_LENGTH,
    HANDLE_MAX_LENGTH,
    NOTE_MAX_LENGTH,
    RATING_MAX,
    RATING_MIN,
)

#: Handles appear in a public URL, so they are restricted to what reads
#: unambiguously in one: no case, no punctuation, nothing to homoglyph with.
HANDLE_PATTERN = r"^[a-z0-9_]+$"
HANDLE_MIN_LENGTH = 3


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

    Doubles as the health endpoint - ``stale`` per source is the signal that a
    fetcher has stopped working (docs.internal/11-ops-install.md).
    """

    version: str
    generated_at: dt.datetime
    title_count: int
    sources: list[SourceFreshness]
    attribution: list[Attribution]
    #: Sign-in providers this deployment is configured for; the client renders a
    #: button per entry, and none at all on a deployment without accounts.
    login_providers: list[AuthProvider] = Field(default_factory=list)


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
    """Where a title can be watched - or where it used to be.

    ``is_current`` and ``source_active`` drive two different badges: content
    that went away, and a source Eifo no longer tracks at all.
    """

    source_key: str
    source_name: str
    source_kind: SourceKind
    source_active: bool
    offer_type: OfferType
    is_current: bool
    deep_link_url: str | None = None
    #: What it costs, in the currency's minor unit (1990 = 19.90 ILS), with its
    #: ISO-4217 code. Both are null unless the source charges per title.
    price_minor: int | None = None
    price_currency: str | None = None
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


class UserOut(BaseModel):
    """A user, as they are allowed to be seen.

    The omissions are the point: no ``email``, no ``auth_provider``, no
    ``auth_subject``. This model is the only way a user reaches a response, so
    the identity we were handed at login cannot leak by accident - asserted by
    the privacy suite against the full response body.
    """

    id: int
    display_name: str
    handle: str | None = None
    avatar_url: str | None = None
    is_public: bool
    my_source_ids: list[int] = Field(default_factory=list)
    created_at: dt.datetime


class MeResponse(BaseModel):
    """The signed-in user plus the CSRF token for their session.

    Bundled because every client needs both at boot, and a token that arrives
    with the user it belongs to cannot be paired with the wrong session.
    """

    user: UserOut
    csrf_token: str


class UserItemOut(BaseModel):
    """One title in a user's lists, from that user's point of view."""

    title_id: int
    status: ItemStatus | None = None
    rating: int | None = None
    #: Private always, even on a public profile.
    note: str | None = None
    updated_at: dt.datetime
    #: Populated when the list is being browsed, absent on a write's echo.
    title: TitleCard | None = None


class ProfilePatch(BaseModel):
    """A partial profile update; absent fields are left alone."""

    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, min_length=1, max_length=DISPLAY_NAME_MAX_LENGTH)
    handle: str | None = Field(
        default=None,
        min_length=HANDLE_MIN_LENGTH,
        max_length=HANDLE_MAX_LENGTH,
        pattern=HANDLE_PATTERN,
    )
    is_public: bool | None = None
    my_source_ids: list[int] | None = None


class ItemUpsert(BaseModel):
    """A partial update to one list entry; an explicit null clears a field."""

    model_config = ConfigDict(extra="forbid")

    status: ItemStatus | None = None
    rating: int | None = Field(default=None, ge=RATING_MIN, le=RATING_MAX)
    note: str | None = Field(default=None, max_length=NOTE_MAX_LENGTH)
