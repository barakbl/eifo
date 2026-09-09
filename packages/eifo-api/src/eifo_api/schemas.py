"""Response models.

Pydantic models are the API contract; ORM objects never leave a router.
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from eifo_core.enums import (
    AuthProvider,
    CreditRole,
    FetchPhase,
    FetchStatus,
    MemberRole,
    OfferType,
    RatingProvider,
    SourceKind,
    TitleKind,
)
from eifo_core.ingest import MAX_IMDB_WRITE_CHUNK, MAX_SERET_WRITE_CHUNK
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

#: Cap on a run log arriving over the wire. The sender keeps the tail of what a
#: run said and trims it to 64KB; this is that with room to spare, so a sender
#: that trims correctly is never refused and one that does not is.
MAX_RUN_LOG_CHARS = 200_000

#: A provider's mark, base64-encoded. These are small SVGs and PNGs; anything
#: near this is not a mark.
MAX_LOGO_CHARS = 400_000

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


class RatingGroupOut(BaseModel):
    """One service's chip: its mark, and every figure it reported.

    A service rather than a score, because two of them report two figures.
    Rotten Tomatoes measuring critics and the crowd separately is one site
    having measured two things - shown as two chips it read as two raters
    disagreeing, on a page whose whole business is telling raters apart.

    Everything here is what the fetcher recorded from the plugin that produced
    the scores (``rating_providers``). The client renders it and knows the name
    of no provider.
    """

    key: str
    name: str
    #: The service's mark, when its plugin ships one. The chip shows the name
    #: instead when it does not, which is what every chip did before marks.
    logo_url: str | None = None
    #: Where this title lives on that service, falling back to the service
    #: itself. One link for the chip, because the chip is one thing.
    url: str | None = None
    scores: list[RatingOut] = Field(default_factory=list)


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
    #: How many people the score rests on, so a card can say when that is few.
    #: None when no rater reports a count at all - which is not the same as few.
    score_votes: int | None = None
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
    #: The same ratings, gathered by the service that reported them. Both are
    #: sent: the chips read by service, and the aggregate's working reads by
    #: rater, because a weight is per rater and not per site.
    rating_groups: list[RatingGroupOut] = Field(default_factory=list)
    aggregate: AggregateOut = Field(default_factory=AggregateOut)


class PersonCredit(BaseModel):
    """One title in a person's body of work."""

    role: CreditRole
    character: str | None = None
    title: TitleCard


class TitleSuggestion(BaseModel):
    """A title, reduced to what a dropdown row shows.

    The score is part of that: a suggestion is a preview of a result, and the
    grid behind it leads with how good the thing is. Null means unrated, which
    the pill says out loud rather than passing off as a nought.
    """

    id: int
    type: TitleKind
    name_he: str | None = None
    name_en: str | None = None
    year: int | None = None
    poster_url: str | None = None
    score: int | None = None


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


class RunOpen(BaseModel):
    """A fetcher saying it has started a phase.

    ``started_at`` is the fetcher's clock rather than the server's, because the
    fetcher is where the work began and the two machines are no longer
    guaranteed to be the same one. Left out, the server uses its own - which is
    the honest answer when nobody said otherwise.
    """

    phase: FetchPhase
    source_key: str | None = Field(default=None, max_length=50)
    started_at: dt.datetime | None = None


class RunClose(BaseModel):
    """A fetcher saying how a phase ended."""

    status: FetchStatus
    finished_at: dt.datetime | None = None
    stats: dict[str, Any] = Field(default_factory=dict)
    #: The tail of what the run said. Capped here as well as at the sender,
    #: because a cap that only one side enforces is a cap on well-behaved
    #: senders.
    log: str | None = Field(default=None, max_length=MAX_RUN_LOG_CHARS)


class DeclaredSource(BaseModel):
    """One service a plugin says it can populate."""

    source_key: str = Field(max_length=50)
    name: str = Field(max_length=200)
    kind: SourceKind
    website_url: str = Field(max_length=1000)
    logo_path: str | None = Field(default=None, max_length=500)
    default_enabled: bool = True


class SourceDeclaration(BaseModel):
    """Everything this fetcher's plugins can do, and what it means to run."""

    sources: list[DeclaredSource] = Field(default_factory=list)
    #: Keys the fetcher's own configuration switches on. An administrator's
    #: override still wins, and comes back in the answer.
    enabled: list[str] = Field(default_factory=list)
    #: Retire rows for services no plugin declares any more. Off for a partial
    #: run: a fetcher syncing one source is not evidence the others are gone.
    retire_missing: bool = False


class SourceRegistry(BaseModel):
    """What changed, and which switches an administrator has thrown."""

    added: list[str] = Field(default_factory=list)
    retired: list[str] = Field(default_factory=list)
    #: Only the sources carrying an explicit answer. Absent means the
    #: configuration file still decides.
    overrides: dict[str, bool] = Field(default_factory=dict)


class BackfillsDone(BaseModel):
    keys: list[str] = Field(default_factory=list)


class SyncStart(BaseModel):
    """A fetcher announcing which source it is about to read.

    Carries the source's identity as well as its key, because the plugin is the
    thing that knows what a service is called and where it lives, and the API
    cannot ask it. A service added in an upgrade becomes visible to the operator
    the first time a sync mentions it.
    """

    source_key: str = Field(max_length=50)
    name: str = Field(max_length=200)
    kind: SourceKind
    website_url: str = Field(max_length=1000)
    logo_path: str | None = Field(default=None, max_length=500)
    default_enabled: bool = True
    #: The fetcher's clock. The work began on its machine, which is no longer
    #: guaranteed to be this one.
    started_at: dt.datetime | None = None


class SyncBegun(BaseModel):
    """Where to send the listings."""

    run_id: int
    source_id: int
    started_at: dt.datetime


class SyncChunkOut(BaseModel):
    """What one chunk achieved, and what it could not do alone."""

    stored: int
    #: Listings nothing local claimed and no TMDB hit arrived for. Handed back
    #: for the sender to look up and offer again - which is what keeps the
    #: TMDB key, and the calls, on the machine with the network.
    needs_tmdb: list[dict[str, Any]] = Field(default_factory=list)
    #: Per listing, by its index in the chunk, so the sender is told which one.
    rejected: list[dict[str, Any]] = Field(default_factory=list)
    matched_by: dict[str, int] = Field(default_factory=dict)


class SyncFinish(BaseModel):
    """A fetcher saying it has no more listings for this run."""

    status: FetchStatus = FetchStatus.OK
    errors: list[str] = Field(default_factory=list)
    #: What the fetcher said while reading. Appended to what the API said while
    #: writing, so one row holds the whole story in order.
    log: str | None = Field(default=None, max_length=MAX_RUN_LOG_CHARS)


class SyncOutcome(BaseModel):
    """The run, as it will appear in the Runs panel."""

    source_key: str
    status: FetchStatus
    items_seen: int = 0
    availability_created: int = 0
    availability_updated: int = 0
    titles_created: int = 0
    retired: int = 0
    reviews_expired: int = 0
    errors: list[str] = Field(default_factory=list)
    matched_by: dict[str, int] = Field(default_factory=dict)


class TitleDue(BaseModel):
    """A title an enricher should look up, as the enricher will see it."""

    id: int
    kind: TitleKind
    name_he: str | None = None
    name_en: str | None = None
    year: int | None = None
    tmdb_id: int | None = None
    imdb_id: str | None = None


class DeclaredRatingProvider(BaseModel):
    """One figure a provider reports, as its plugin declares it."""

    provider: RatingProvider
    label: str = Field(max_length=100)
    group_key: str = Field(max_length=50)
    group_name: str = Field(max_length=100)
    website_url: str | None = Field(default=None, max_length=1000)
    position: int = 0
    #: The mark, base64-encoded. The file ships with the plugin, which is not
    #: necessarily on this machine.
    logo: str | None = Field(default=None, max_length=MAX_LOGO_CHARS)
    logo_suffix: str = Field(default="", max_length=8)


class ProviderDeclaration(BaseModel):
    providers: list[DeclaredRatingProvider] = Field(default_factory=list)


class ProvidersOut(BaseModel):
    """Which providers' details actually changed - normally none."""

    changed: list[str] = Field(default_factory=list)


class SeretEntryOut(BaseModel):
    """One page of the Seret index, as the lookup needs it."""

    kind: TitleKind
    seret_id: int
    name_he: str | None = None
    name_en: str | None = None
    year: int | None = None
    imdb_id: str | None = None
    viewers_score: float | None = None
    #: How many people that score is an average of. The enricher reports it
    #: with the rating, so a score from eleven votes can be told from one from
    #: eleven thousand.
    viewers_votes: int | None = None
    critics_score: float | None = None
    url: str | None = None
    #: When this page was last read. The crawl uses it to decide what is stale,
    #: which it cannot work out from its own side.
    indexed_at: dt.datetime | None = None
    unreadable: bool = False


class SeretPage(SeretEntryOut):
    """One page a crawl read, readable or not."""


class SeretPages(BaseModel):
    pages: list[SeretPage] = Field(default_factory=list, max_length=MAX_SERET_WRITE_CHUNK)


class SeretIndexOut(BaseModel):
    created: int
    updated: int
    #: Pages that can score a title now and could not before this batch - ones
    #: never read, and ones whose film has been released and rated since we
    #: last looked. Only these are worth waking anything for.
    newly_scorable: int = 0
    #: Titles that had no Seret page and now have one, put back in the queue.
    woken: int = 0


class SeretStatus(BaseModel):
    """What the index holds, and whether it is worth crawling for.

    Two questions in one answer because they are asked together and neither can
    be answered from the fetcher's side. ``eifo-fetch seret status`` prints the
    counts; the nightly enrich reads ``catalog_titles`` to decide whether to
    crawl at all, which is the same guard the IMDb pass makes - a fresh install
    should not ask somebody's site for 8,900 pages in order to enrich nothing.
    """

    pages: int = 0
    movies: int = 0
    series: int = 0
    with_imdb_id: int = 0
    with_viewer_score: int = 0
    with_critic_score: int = 0
    unreadable: int = 0
    #: Titles in the catalog, which is what decides whether a crawl has a
    #: purpose. Not part of the index, and here anyway: it is the other half of
    #: the only question anybody asks this endpoint.
    catalog_titles: int = 0


class WantedImdb(BaseModel):
    """A title the IMDb bulk pass should watch for in the dataset."""

    title_id: int
    imdb_id: str


class ImdbRating(BaseModel):
    title_id: int
    score_raw: float
    vote_count: int | None = None
    #: Where the score can be read in full. Sent rather than built here: the
    #: fetcher is holding the ``imdb_id`` the row was matched by, and the API
    #: would have to look it up again to say the same thing.
    url: str | None = Field(default=None, max_length=1000)


class ImdbRatings(BaseModel):
    ratings: list[ImdbRating] = Field(default_factory=list, max_length=MAX_IMDB_WRITE_CHUNK)


class RescoreOut(BaseModel):
    """How many aggregates a rescore rebuilt."""

    aggregates_computed: int


class EnrichStart(BaseModel):
    started_at: dt.datetime | None = None


class EnrichBegun(BaseModel):
    run_id: int
    started_at: dt.datetime


class EnrichChunkOut(BaseModel):
    """What one batch of findings achieved."""

    titles_seen: int
    ratings_written: int
    #: Per title, by its index in the batch. A score outside its provider's
    #: scale is a parser bug, and storing it would quietly skew the aggregate.
    rejected: list[dict[str, Any]] = Field(default_factory=list)


class EnrichFinish(BaseModel):
    status: FetchStatus = FetchStatus.OK
    errors: list[str] = Field(default_factory=list)
    #: What only the sender knows about the run - which providers it asked, and
    #: how much each turned up. Merged into what this side counted rather than
    #: replacing it, and never allowed to overwrite it: the totals here are what
    #: was actually written, and a sender must not be able to say otherwise.
    stats: dict[str, Any] = Field(default_factory=dict)
    log: str | None = Field(default=None, max_length=MAX_RUN_LOG_CHARS)


class EnrichOutcomeOut(BaseModel):
    status: FetchStatus
    titles_seen: int = 0
    ratings_written: int = 0
    metadata_updated: int = 0
    aggregates_computed: int = 0
    errors: list[str] = Field(default_factory=list)


class PendingPoster(BaseModel):
    """One title that still needs its artwork downloaded."""

    title_id: int
    source_url: str


class RejectedPoster(BaseModel):
    """One title whose artwork was not stored, and why.

    Reported per title rather than failing the batch: one unreadable image
    among a hundred good ones should cost that one image, and the reason has
    to reach whoever can act on it - which is the sender, not this log.
    """

    title_id: int
    reason: str


class IngestResult(BaseModel):
    """What an upload achieved."""

    stored: int
    rejected: list[RejectedPoster] = Field(default_factory=list)


class AuthContext(BaseModel):
    """What a signed-out visitor needs in order to stop being one.

    Two facts and nothing else. This is the one thing a members-only instance
    answers to a stranger, so it must not carry a hint of what is inside.
    """

    members_only: bool
    login_providers: list[AuthProvider] = Field(default_factory=list)


class MemberOut(BaseModel):
    """One address on the allowlist."""

    email: str
    role: MemberRole
    #: True when the role comes from the configuration file rather than this
    #: row. Those cannot be edited from here, and the UI has to be able to say
    #: so rather than offering a button that will be refused.
    from_config: bool = False
    invited_by: str | None = None
    created_at: dt.datetime


class MemberInvite(BaseModel):
    """Let an address in."""

    email: str = Field(min_length=3, max_length=320)
    role: MemberRole = MemberRole.MEMBER

    @field_validator("email")
    @classmethod
    def _looks_like_an_address(cls, value: str) -> str:
        """A shape check, not a validation.

        Whether an address exists is not knowable from here and does not matter:
        the thing that decides is Google, at sign-in, and an address nobody owns
        simply never arrives. What this catches is the typo worth catching -
        a name with no domain, a stray space, two addresses pasted into one
        box - which would otherwise sit on the list looking like an invitation
        somebody had been given.

        No dependency for it. `pydantic[email]` brings a DNS library to answer
        a question that is not being asked.
        """
        address = value.strip()
        local, at, domain = address.partition("@")
        if not at or not local or "." not in domain or any(c.isspace() for c in address):
            raise ValueError("Enter one email address, like name@example.com")
        return address


class MemberRoleChange(BaseModel):
    """Promote or demote somebody already on the list."""

    role: MemberRole


class ApiTokenOut(BaseModel):
    """A token that exists, described without giving it away.

    There is deliberately no field for the token itself: it is shown once, when
    it is created, and never stored in a form anything could show again.
    """

    name: str
    #: The first characters, so a person can tell which of their tokens a
    #: script is holding without being able to reconstruct it.
    hint: str
    created_at: dt.datetime
    last_used_at: dt.datetime | None = None


class ApiTokenCreated(ApiTokenOut):
    """A token, the one time it is readable."""

    token: str


class ApiTokenCreate(BaseModel):
    """Ask for a token, and say what it is for."""

    name: str = Field(min_length=1, max_length=100)


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
