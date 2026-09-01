"""The enricher contract.

An enricher attaches ratings, and sometimes metadata, to a canonical title.
Like sources, enrichers are plugins (entry-point group ``eifo.enrichers``) so a
ratings provider can be added or dropped without touching the pipeline
(docs.internal/06-enrichment.md).

Enrichers are pure readers: they return what they found and never write to the
database. Persistence, the refresh policy and score aggregation live in the
pipeline.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from eifo_core.enums import RatingProvider, TitleKind
from eifo_fetcher.sources.base import FetchContext


@dataclass(frozen=True, slots=True)
class TitleView:
    """The read-only view of a title an enricher is given.

    A plain snapshot rather than the ORM object, so an enricher cannot acquire
    a write path to the database by accident.
    """

    id: int
    kind: TitleKind
    name_he: str | None
    name_en: str | None
    year: int | None
    tmdb_id: int | None
    imdb_id: str | None

    @property
    def display_name(self) -> str:
        return self.name_he or self.name_en or f"title#{self.id}"

    def names(self) -> list[str]:
        """Every name this title is known by, Hebrew first."""
        return [name for name in (self.name_he, self.name_en) if name]


@dataclass(frozen=True, slots=True)
class Rating:
    """A score in its provider's own scale."""

    provider: RatingProvider
    score_raw: float
    vote_count: int | None = None
    url: str | None = None

    def __post_init__(self) -> None:
        if self.score_raw < 0:
            raise ValueError(f"{self.provider} score cannot be negative: {self.score_raw}")


@dataclass(slots=True)
class EnrichResult:
    """What an enricher found.

    ``metadata_patch`` fills gaps only: it never overwrites a field that already
    has a value, so a source's guess cannot displace TMDB's canonical answer.
    """

    ratings: list[Rating] = field(default_factory=list)
    metadata_patch: dict[str, Any] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return not self.ratings and not self.metadata_patch


class Enricher(ABC):
    """Base class for metadata and ratings providers."""

    #: Providers this enricher can return. Used to decide what a refresh covers.
    providers: tuple[RatingProvider, ...] = ()

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
