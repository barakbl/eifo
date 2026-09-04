"""Response models.

Pydantic models are the API contract; ORM objects never leave a router.
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from eifo_core.enums import (
    AuthProvider,
    CreditRole,
    FetchPhase,
    FetchStatus,
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

#: Cap on one bulk ruling. Big enough for "dismiss every Sing Along on this
#: page", small enough that a mistake is reviewable and one request is one
#: transaction that finishes.
BULK_RULING_MAX = 200


class BulkDecision(StrEnum):
    """The rulings that can sensibly be made about a set of listings at once."""

    DISMISS = "dismiss"
    CREATE = "create"


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


class PersonRef(BaseModel):
    """A person, as a title page needs to name and link to them."""

    id: int
    name_he: str | None = None
    name_en: str | None = None
    profile_url: str | None = None


class CreditOut(BaseModel):
    """One person's contribution to one title."""

    role: CreditRole
    person: PersonRef
    character: str | None = None


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


class Arrival(BaseModel):
    """A title as it turned up on one service.

    The unit is the offer, not the title: a film that has been on HBO Max for
    years and landed on Netflix last night is news about Netflix, and belongs
    to Netflix alone. Asking what is new on HBO Max must not answer with it.
    """

    #: When the title first appeared on this service, as far as Eifo saw.
    added_at: dt.datetime
    source_key: str
    source_name: str
    title: TitleCard


class TitleDetail(TitleCard):
    """A single title in full."""

    overview_he: str | None = None
    overview_en: str | None = None
    runtime_minutes: int | None = None
    seasons: int | None = None
    status: str | None = None
    backdrop_url: str | None = None
    #: ISO 639-1, and ISO 3166-1 alpha-2 codes. Codes rather than names: the
    #: client renders them in whichever language the reader chose.
    original_language: str | None = None
    origin_countries: list[str] = Field(default_factory=list)
    #: Director, cinematographer and billed cast, in that order.
    credits: list[CreditOut] = Field(default_factory=list)
    ratings: list[RatingOut] = Field(default_factory=list)
    aggregate: AggregateOut = Field(default_factory=AggregateOut)


class PersonCredit(BaseModel):
    """One title in a person's body of work."""

    role: CreditRole
    character: str | None = None
    title: TitleCard


class TitleSuggestion(BaseModel):
    """A title, reduced to what a dropdown row shows."""

    id: int
    type: TitleKind
    name_he: str | None = None
    name_en: str | None = None
    year: int | None = None
    poster_url: str | None = None


class PersonSuggestion(BaseModel):
    """A person, reduced to what a dropdown row shows.

    ``credit_count`` is not decoration: a hundred-odd names in the catalog
    belong to more than one person, and how much a catalogue credits somebody is
    the only thing on hand to tell them apart with.
    """

    id: int
    name_he: str | None = None
    name_en: str | None = None
    credit_count: int = 0


class Suggestions(BaseModel):
    """What to offer somebody mid-word.

    ``query`` comes back so a client can drop an answer to a question it has
    stopped asking - keystrokes outrun round trips, and an out-of-order reply
    would otherwise replace a newer one.
    """

    query: str
    titles: list[TitleSuggestion] = Field(default_factory=list)
    people: list[PersonSuggestion] = Field(default_factory=list)


class PersonDetail(BaseModel):
    """A person and everything the catalog credits them with.

    One object per person, not per role: someone who directs and acts is one
    human. Each credit carries its own ``role``, so the client can group them.
    """

    id: int
    name_he: str | None = None
    name_en: str | None = None
    profile_url: str | None = None
    tmdb_id: int | None = None
    credits: list[PersonCredit] = Field(default_factory=list)


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
    #: Whether this account may open the Manage tab. The client asks so it can
    #: leave the link out rather than offer one that 404s; the server never
    #: trusts the answer coming back.
    is_admin: bool = False


class UserItemOut(BaseModel):
    """One title in a user's lists, from that user's point of view."""

    title_id: int
    #: Two lists, not two halves of one. Both true is a title somebody has seen
    #: and means to see again; both false is one they only rated or noted.
    want_to_watch: bool = False
    watched: bool = False
    rating: int | None = None
    #: Private always, even on a public profile.
    note: str | None = None
    updated_at: dt.datetime
    #: Populated when the list is being browsed, absent on a write's echo.
    title: TitleCard | None = None


class ListService(BaseModel):
    """One service, and how much of a user's list it carries."""

    key: str
    name: str
    #: Titles on the list this service currently offers. Distinct titles, not
    #: offers: a film that can be rented or bought is one thing to watch.
    title_count: int


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

    #: Each list is set on its own. Sending one does not disturb the other,
    #: which is the whole point of their being two.
    want_to_watch: bool | None = None
    watched: bool | None = None
    rating: int | None = Field(default=None, ge=RATING_MIN, le=RATING_MAX)
    note: str | None = Field(default=None, max_length=NOTE_MAX_LENGTH)


# -- operator surfaces ------------------------------------------------------


class AdminSource(BaseModel):
    """A tracked service as an operator needs to see it.

    Everything on one row that answers "is this source alright": whether it is
    switched on, how much of the catalog it accounts for, when it last worked,
    and how much of its output is sitting in the review queue instead.
    """

    key: str
    name: str
    kind: SourceKind
    website_url: str
    active: bool
    #: The operator's override, or None when the config file decides.
    enabled: bool | None = None
    #: What the source is actually doing right now, config and override folded
    #: together - which is the thing an operator is asking about.
    effective_enabled: bool
    title_count: int = 0
    #: How much of what this source offers the catalog has actually filled in.
    #: Counts rather than percentages: the denominator is ``title_count`` and
    #: the client is the one deciding how to round and colour them.
    titles_with_poster: int = 0
    titles_with_score: int = 0
    titles_enriched: int = 0
    #: Set while an operator's request for a full pull is still outstanding, so
    #: the tab can say "queued" rather than look like the switch did nothing.
    backfill_requested_at: dt.datetime | None = None
    pending_reviews: int = 0
    last_sync_at: dt.datetime | None = None
    last_sync_status: FetchStatus | None = None
    stale: bool = False


class SourceToggle(BaseModel):
    """Switch a source on or off, or hand it back to the config file."""

    #: Null returns the source to whatever ``[sources]`` says, which is not the
    #: same as switching it on.
    enabled: bool | None = None


class RunOut(BaseModel):
    """One fetcher run, without its log."""

    id: int
    source_key: str | None = None
    phase: FetchPhase
    status: FetchStatus
    started_at: dt.datetime
    finished_at: dt.datetime | None = None
    duration_seconds: float | None = None
    stats: dict[str, Any] = Field(default_factory=dict)
    #: Whether there is a log to fetch, so the client offers the control only
    #: when pressing it would show something.
    has_log: bool = False


class RunDetail(RunOut):
    """One fetcher run, with whatever it said while it ran."""

    log: str | None = None


class ScoringProvider(BaseModel):
    """One rating provider's part in the catalog's aggregate scores.

    Two different questions, deliberately side by side: how much a provider is
    *meant* to count, which is a line in the configuration file, and how much it
    *actually* counted, which depends on how much of the catalog it has managed
    to rate. A provider weighted heaviest and reaching a tenth of the titles is
    not the one deciding the scores, and only the second number says so.
    """

    provider: RatingProvider
    #: How the provider is credited in the UI, the same string a title page uses.
    provider_name: str
    #: Its weight from ``[scores.weights]``. Not a percentage of anything: the
    #: weights are relative to each other and need not add up to anything.
    weight: float
    #: The share of the weight actually behind the catalog's scores, 0-100.
    #:
    #: Each provider's weight counted once per scored title it has rated, over
    #: the same total across every provider - so a heavy weight that rated
    #: little lands where it belongs. Null when nothing has been scored yet,
    #: which is not a zero: it is a catalog with no scores in it.
    share: float | None = None
    #: Titles this provider has rated, whether or not they ended up scored.
    titles_rated: int = 0
    #: Whether it feeds the separate Israeli aggregate.
    is_israeli: bool = False


class AdminStats(BaseModel):
    """The numbers an operator checks first."""

    title_count: int
    titles_with_score: int
    titles_missing_poster: int
    people_count: int
    #: Distinct titles somebody could watch right now, which is what "available"
    #: means to a reader. Not the same as the number of offers: a title on two
    #: services, or rentable and buyable at one shop, is one title and several
    #: offers.
    titles_available: int
    current_offers: int
    pending_reviews: int
    #: Every listing ever parked, ruled on or not. With ``pending_reviews`` it
    #: says how much of the queue has been worked through, which is the figure
    #: that answers "is anybody keeping up".
    reviews_total: int
    sources_total: int
    sources_stale: int
    #: When the newest finished run of any kind finished. None on an instance
    #: that has never run the fetcher, which is its own kind of answer.
    last_run_at: dt.datetime | None = None
    #: Hours after which a source counts as stale, so the client bands the
    #: freshness figures the same way the server does.
    stale_after_hours: int
    #: Every rating provider, heaviest contributor first. Here rather than on
    #: its own endpoint because it answers the same question the rest of this
    #: does - is the catalog alright - and the panel that shows it is already
    #: waiting on this call.
    scoring: list[ScoringProvider] = Field(default_factory=list)


class ReviewCandidate(BaseModel):
    """The title the matcher thought a parked listing might be."""

    title_id: int
    name_he: str | None = None
    name_en: str | None = None
    year: int | None = None
    similarity: float | None = None
    poster_url: str | None = None


class ReviewOut(BaseModel):
    """One parked listing, with everything needed to rule on it.

    Both sides of the question in one object: what the source is offering, and
    what the matcher suspected it already had. A reviewer comparing them should
    not have to fetch the second one.
    """

    id: int
    source_key: str
    source_name: str | None = None
    created_at: dt.datetime
    name: str
    name_alt: str | None = None
    year: int | None = None
    kind: TitleKind
    poster_url: str | None = None
    deep_link_url: str | None = None
    closest: ReviewCandidate | None = None


class ReviewCounts(BaseModel):
    """How much is waiting, in total and per source."""

    total: int
    by_source: dict[str, int] = Field(default_factory=dict)


class ReviewRuling(BaseModel):
    """What a reviewer decided about a parked listing."""

    #: Required for ``attach``; ignored otherwise.
    title_id: int | None = None


class BulkRuling(BaseModel):
    """The same ruling applied to several listings at once.

    Only the two rulings that need no per-item judgement: "these are all junk"
    and "these are all real titles nobody holds". Attaching is per-item by
    definition - it names a different title each time.
    """

    ids: list[int] = Field(min_length=1, max_length=BULK_RULING_MAX)
    decision: BulkDecision


class BulkResult(BaseModel):
    """What a bulk ruling did, and to what it could not be applied."""

    applied: int
    skipped: list[int] = Field(default_factory=list)
