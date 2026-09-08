"""The things a catalog is made of, before any of it is a row.

These used to live in ``eifo-fetcher``, beside the plugins that produce them,
which was right for as long as the fetcher was also the thing that wrote them
down. It is not any more: a sync now ships its listings to the API and the API
decides what they mean, so both services need to agree on what a listing *is*.

That makes these the same kind of thing as the schema - a definition neither
side may hold its own copy of - which is why they are here.

Nothing in this module touches the database or the network. A listing is a
statement about the world as one catalog describes it; what to do about it is
:mod:`eifo_core.match`'s business.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from eifo_core.enums import OfferType, SourceKind, TitleKind
from eifo_core.types import utcnow


@dataclass(frozen=True, slots=True)
class SourceInfo:
    """Identity of a tracked service, as the plugin declares it."""

    key: str
    name: str
    kind: SourceKind
    website_url: str
    logo_path: str | None = None
    #: What this source does when the configuration file says nothing about it.
    #:
    #: True for almost everything, which is why adding a plugin is one file
    #: rather than a file plus a config edit. A source sets it False when
    #: syncing it costs more than an operator would expect to spend without
    #: having asked - the Apple TV Store spends a request per film - because
    #: "absent means on" would then hand an existing deployment a much longer
    #: nightly run for upgrading.
    default_enabled: bool = True


#: Near enough to cinema's first year. The Israeli Film Archive holds material
#: from the 1930s, and nothing in a catalog predates the medium.
EARLIEST_YEAR = 1880
#: A title announced for next year is legitimate; a decade out is an accident.
FUTURE_YEAR_ALLOWANCE = 2


def plausible_year(value: int | None) -> int | None:
    """A production year, or None when the source has handed us something else.

    Catalogs emit 0 for "we do not know" and placeholders like 2999 for "not
    scheduled yet", and both sort to the ends of a by-year list - where they are
    the first thing anyone sorting by year sees. Dropping the year keeps the
    title: it is the year that is wrong, not the listing.
    """
    if value is None:
        return None
    if EARLIEST_YEAR <= value <= utcnow().year + FUTURE_YEAR_ALLOWANCE:
        return value
    return None


@dataclass(frozen=True, slots=True)
class RawItem:
    """One listing, exactly as a source presents it.

    ``source_key`` is per item rather than per plugin because a harvester such as
    ``tmdb-providers`` yields items for many services from a single crawl.
    """

    source_key: str
    kind: TitleKind
    name: str
    offer_type: OfferType = OfferType.STREAM
    name_alt: str | None = None
    year: int | None = None
    tmdb_id: int | None = None
    imdb_id: str | None = None
    deep_link_url: str | None = None
    #: The source's own id for this listing, when it publishes one. What makes
    #: a listing the same listing tomorrow, and the only way to tell two works
    #: apart when a catalogue names them identically.
    source_ref: str | None = None
    poster_url: str | None = None
    #: What the offer costs, in the currency's minor unit (1990 = 19.90 ILS),
    #: with its ISO-4217 code. Only a source that charges per title sets these.
    price_minor: int | None = None
    price_currency: str | None = None
    #: Who made it, when the source says so. Each entry is a dict of the
    #: :func:`~eifo_fetcher.people.apply_credits` shape: a ``role`` and a name.
    #: TMDB does not carry most Israeli cinema, so for those titles a
    #: catalogue's own credits are the only ones there will ever be.
    credits: tuple[Mapping[str, Any], ...] = ()
    #: ISO 3166-1 alpha-2, comma separated ("IL", "IL,FR").
    origin_countries: str | None = None
    #: Kept verbatim in match_reviews so an unresolved item can be debugged.
    extra: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("RawItem.name must not be blank")
        if not self.source_key.strip():
            raise ValueError("RawItem.source_key must not be blank")
        # Every source passes through here, so this is the one place a
        # placeholder year can be stopped rather than each parser's business.
        object.__setattr__(self, "year", plausible_year(self.year))
        if (self.price_minor is None) != (self.price_currency is None):
            raise ValueError("RawItem price needs both an amount and a currency, or neither")
        if self.price_minor is not None and self.price_minor < 0:
            raise ValueError("RawItem.price_minor must not be negative")


#: How TMDB spells each kind in a URL path.
MEDIA_PATH = {TitleKind.MOVIE: "movie", TitleKind.SERIES: "tv"}


@dataclass(frozen=True, slots=True)
class TmdbTitle:
    """A search or discover result, normalised across the movie/tv split."""

    tmdb_id: int
    kind: TitleKind
    name: str
    original_name: str | None
    year: int | None
    overview: str | None
    poster_path: str | None

    @property
    def media_type(self) -> str:
        return MEDIA_PATH[self.kind]
