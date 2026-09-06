"""The enricher contract.

An enricher attaches ratings, and sometimes metadata, to a canonical title.
Like sources, enrichers are plugins (entry-point group ``eifo.enrichers``) so a
ratings provider can be added or dropped without touching the pipeline
(docs.internal/06-enrichment.md).

Enrichers are pure readers: they return what they found and never write to the
database. Persistence, the refresh policy and score aggregation live behind the
ingest API now, which is what lets an enrich run somewhere the catalog is not.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from eifo_core.enums import RatingProvider
from eifo_core.findings import EnrichResult, ProviderInfo, Rating, TitleView
from eifo_fetcher.sources.base import FetchContext

# Re-exported, not redefined. What an enricher is asked and what it answers now
# cross the wire, so they live in core - but an enricher author looks for them
# here, beside the class they are implementing.
__all__ = [
    "ICONS_DIR",
    "EnrichResult",
    "Enricher",
    "ProviderInfo",
    "Rating",
    "TitleView",
]


#: Where the built-in marks live. A plugin outside this tree points at its own
#: folder the same way: ``Path(__file__).parent / "icons" / "thing.svg"``.
ICONS_DIR = Path(__file__).parent / "icons"


class Enricher(ABC):
    """Base class for metadata and ratings providers."""

    #: Providers this enricher can return. Used to decide what a refresh covers.
    providers: tuple[RatingProvider, ...] = ()

    #: How each of those credits itself on the page - name, mark, and which of
    #: them are one service. Optional: an enricher that declares nothing still
    #: works, and its scores are credited by the provider key, which is what
    #: any unknown provider has always fallen back to.
    provider_info: tuple[ProviderInfo, ...] = ()

    #: The host this provider reads, when it reads one site of its own.
    #:
    #: Declared rather than rate-limited by hand, so politeness is a property
    #: of the pipeline rather than of each plugin author's diligence - the same
    #: reason every request goes through one HttpClient (eifo_fetcher.http).
    #: None for a provider that talks to an API with its own configured pace,
    #: which is TMDB and its [tmdb] section.
    host: str | None = None

    #: Requests per second to ask that host for, unless ``[enrich.rate_limits]``
    #: overrides it. None leaves the client-wide default in place.
    default_rate_limit_rps: float | None = None

    @property
    @abstractmethod
    def key(self) -> str:
        """Short stable name, used in configuration and logs."""

    @abstractmethod
    def enrich(self, title: TitleView, ctx: FetchContext) -> EnrichResult | None:
        """Look this title up. Return None when the provider has nothing.

        "Nothing" is an ordinary outcome, not a failure: plenty of Israeli
        titles do not exist on Rotten Tomatoes, and plenty of foreign ones do
        not exist on Seret.
        """
