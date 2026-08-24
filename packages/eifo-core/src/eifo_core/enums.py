"""Enumerations shared by the schema, the fetcher plugins and the API."""

from enum import StrEnum


class TitleKind(StrEnum):
    """What a canonical title is."""

    MOVIE = "movie"
    SERIES = "series"


class SourceKind(StrEnum):
    """How a source charges for access - drives grouping in the UI."""

    SUBSCRIPTION = "subscription"
    FREE = "free"
    RENT_BUY = "rent_buy"


class OfferType(StrEnum):
    """How a specific title is offered by a source."""

    STREAM = "stream"
    RENT = "rent"
    BUY = "buy"
    FREE = "free"


class CreditRole(StrEnum):
    """How a person contributed to a title.

    Deliberately short: these are the credits a viewer scans before deciding
    what to watch, not a full crew list.
    """

    DIRECTOR = "director"
    CINEMATOGRAPHER = "cinematographer"
    CAST = "cast"


class RatingProvider(StrEnum):
    """Where a score came from.

    Adding a member is half of adding a ratings provider; the other half is a
    weight in ``[scores.weights]`` (docs.internal/06-enrichment.md).
    """

    IMDB = "imdb"
    TMDB = "tmdb"
    RT_CRITICS = "rt_critics"
    RT_AUDIENCE = "rt_audience"
    SERET_CRITICS = "seret_critics"
    SERET_VIEWERS = "seret_viewers"
    EDB = "edb"

    @property
    def is_israeli(self) -> bool:
        """Whether this provider feeds the separate Israeli aggregate."""
        return self in _ISRAELI_PROVIDERS


_ISRAELI_PROVIDERS = frozenset(
    {RatingProvider.SERET_CRITICS, RatingProvider.SERET_VIEWERS, RatingProvider.EDB}
)


class AuthProvider(StrEnum):
    """Where an account's identity comes from.

    Accounts are keyed on ``(provider, subject)`` and never linked across
    providers: signing in with Google and with X gives two separate accounts
    even for the same person (docs.internal/09-auth-privacy.md).
    """

    GOOGLE = "google"
    X = "x"


class ItemStatus(StrEnum):
    """Where a title sits in a user's lists.

    Null rather than a member of this enum is the third state: a title that was
    rated or noted without being filed under either list.
    """

    WATCHED = "watched"
    WANT_TO_WATCH = "want_to_watch"


class FetchPhase(StrEnum):
    """Which fetcher phase a run belongs to."""

    SYNC = "sync"
    ENRICH = "enrich"
    IMAGES = "images"


class FetchStatus(StrEnum):
    """Outcome of a fetcher run.

    ``ABORTED_SUSPICIOUS`` is the guard against layout changes: a sync that
    returns far fewer items than the previous successful run is assumed broken
    rather than treated as a mass catalog removal, so no sweep happens.
    """

    #: Still going. Written when the phase starts, so a run that never reaches
    #: an ending is distinguishable from a night when nothing was scheduled.
    RUNNING = "running"
    OK = "ok"
    FAILED = "failed"
    ABORTED_SUSPICIOUS = "aborted_suspicious"
    #: Found still RUNNING by a later fetcher: the process that opened it died
    #: without saying how it went. An OOM, a power cut, a closed laptop.
    CRASHED = "crashed"


class EnrichOutcome(StrEnum):
    """What came of putting one title through the enrichers.

    The distinction that matters is between the three ways of coming back
    empty-handed, because they deserve different waits. ``NO_MATCH`` is "we do
    not know which title this is" - no provider can be asked until matching
    improves. ``NO_DATA`` is "we know exactly which title this is, and nobody
    has rated it" - true of most of a catalog this local, and worth asking
    again only occasionally. ``ERROR`` is the provider's problem, not the
    title's, so it is the one worth retrying soon.
    """

    OK = "ok"
    NO_DATA = "no_data"
    NO_MATCH = "no_match"
    ERROR = "error"
