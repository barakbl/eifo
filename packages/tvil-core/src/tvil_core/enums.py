"""Enumerations shared by the schema, the fetcher plugins and the API."""

from enum import StrEnum


class TitleKind(StrEnum):
    """What a canonical title is."""

    MOVIE = "movie"
    SERIES = "series"


class SourceKind(StrEnum):
    """How a source charges for access — drives grouping in the UI."""

    SUBSCRIPTION = "subscription"
    FREE = "free"
    RENT_BUY = "rent_buy"


class OfferType(StrEnum):
    """How a specific title is offered by a source."""

    STREAM = "stream"
    RENT = "rent"
    BUY = "buy"
    FREE = "free"


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

    OK = "ok"
    FAILED = "failed"
    ABORTED_SUSPICIOUS = "aborted_suspicious"
