"""The source plugin contract.

A plugin is a **pure producer**: it yields :class:`~eifo_core.items.RawItem`
values and never touches the database. Persistence, matching and the availability sweep all live
in the pipeline, which is what keeps a plugin small enough to test entirely from
recorded fixtures (docs.internal/05-fetcher.md).
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Iterator

from eifo_core.items import (
    EARLIEST_YEAR,
    FUTURE_YEAR_ALLOWANCE,
    RawItem,
    SourceInfo,
    plausible_year,
)
from eifo_core.settings import Settings, SourceConfig
from eifo_fetcher.http import HttpClient

# Re-exported, not redefined. A listing is now something both services have to
# agree about, so it lives in core - but a plugin author looks for it here,
# beside the class they are implementing, and should keep finding it.
__all__ = [
    "EARLIEST_YEAR",
    "FUTURE_YEAR_ALLOWANCE",
    "FetchContext",
    "RawItem",
    "SourceInfo",
    "SourcePlugin",
    "TooManyErrorsError",
    "plausible_year",
]


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
