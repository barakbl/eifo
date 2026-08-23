"""The source plugin contract.

A plugin is a **pure producer**: it yields :class:`RawItem` values and never
touches the database. Persistence, matching and the availability sweep all live
in the pipeline, which is what keeps a plugin small enough to test entirely from
recorded fixtures (docs.internal/05-fetcher.md).
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

from eifo_core.enums import OfferType, SourceKind, TitleKind
from eifo_core.settings import Settings, SourceConfig
from eifo_fetcher.http import HttpClient


@dataclass(frozen=True, slots=True)
class SourceInfo:
    """Identity of a tracked service, as the plugin declares it."""

    key: str
    name: str
    kind: SourceKind
    website_url: str
    logo_path: str | None = None


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
        if (self.price_minor is None) != (self.price_currency is None):
            raise ValueError("RawItem price needs both an amount and a currency, or neither")
        if self.price_minor is not None and self.price_minor < 0:
            raise ValueError("RawItem.price_minor must not be negative")


class FetchContext:
    """Everything a plugin is allowed to reach: HTTP, config, logging, errors.

    Errors recorded here surface in ``fetch_runs.stats`` rather than aborting the
    run, so one malformed listing does not cost a whole catalog.
    """

    #: Beyond this many consecutive failures a source is assumed broken.
    max_consecutive_errors = 25
    #: Only the first errors are stored; the count is always exact.
    max_recorded_errors = 20

    def __init__(
        self,
        *,
        source_key: str,
        http: HttpClient,
        settings: Settings,
        logger: logging.Logger | None = None,
    ) -> None:
        self.source_key = source_key
        self.http = http
        self.settings = settings
        self.logger = logger or logging.getLogger(f"eifo.fetch.source.{source_key}")
        self.errors: list[str] = []
        self.error_count = 0
        self._consecutive_errors = 0

    @property
    def config(self) -> SourceConfig:
        """Configuration for the source being fetched."""
        return self.settings.source_config(self.source_key)

    def apply_rate_limit(self, host: str) -> None:
        """Apply this source's configured rate limit to a host it calls.

        Plugins call this for hosts they own. It is deliberately not applied
        automatically: several sources share one upstream API, and letting each
        of them retune a shared host would make the effective rate depend on
        sync order.
        """
        rps = self.config.rate_limit_rps
        if rps is not None:
            self.http.rate_limiter.set_host_rate(host, rps)

    def record_error(self, message: str, *, exc: BaseException | None = None) -> None:
        """Note a recoverable problem with one item.

        Raises:
            TooManyErrorsError: once failures stop looking incidental.
        """
        self.error_count += 1
        self._consecutive_errors += 1
        if len(self.errors) < self.max_recorded_errors:
            self.errors.append(message if exc is None else f"{message}: {exc!r}")
        self.logger.warning("%s: %s", self.source_key, message, exc_info=exc)

        if self._consecutive_errors >= self.max_consecutive_errors:
            raise TooManyErrorsError(self.source_key, self._consecutive_errors)

    def record_success(self) -> None:
        """Reset the consecutive-error streak after an item parses cleanly."""
        self._consecutive_errors = 0


class TooManyErrorsError(RuntimeError):
    """A source failed so consistently that continuing is pointless."""

    def __init__(self, source_key: str, count: int) -> None:
        super().__init__(f"source {source_key!r} failed {count} times in a row; aborting it")
        self.source_key = source_key
        self.count = count


class SourcePlugin(ABC):
    """Base class for catalog producers."""

    @abstractmethod
    def sources(self) -> list[SourceInfo]:
        """Services this plugin can populate - at least one, often several."""

    @abstractmethod
    def fetch(self, ctx: FetchContext) -> Iterator[RawItem]:
        """Yield every currently listed item for the enabled services.

        Implementations should stream rather than build a list: catalogs run to
        tens of thousands of items.
        """
