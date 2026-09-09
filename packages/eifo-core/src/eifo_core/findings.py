"""What an enricher is asked and what it answers.

The same shape as :mod:`eifo_core.items`, and here for the same reason. An
enricher is a pure reader: it is handed a snapshot of a title and returns what
it found, and something else decides what that means for the catalog. That
"something else" is no longer guaranteed to be in the same process - a fetcher
can run on a laptop and enrich a catalog on a server - so the question and the
answer both cross the wire, and neither side may hold its own idea of their
shape.

``TitleView`` being a snapshot rather than the ORM object was always deliberate:
an enricher that cannot reach a Session cannot write to the database by
accident. That it now also has to survive a JSON round trip only makes the same
point more firmly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from eifo_core.enums import OfferType, RatingProvider, TitleKind


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
    #: Which services currently offer this title.
    #:
    #: An enricher that speaks for one service needs to know whether that
    #: service is even involved: asking Apple about a title nobody says is on
    #: Apple spends a request from a rate-limited budget to learn nothing, and
    #: there are twice as many titles as there are Apple ones.
    offered_by: frozenset[str] = frozenset()

    @property
    def display_name(self) -> str:
        return self.name_he or self.name_en or f"title#{self.id}"

    def describe(self) -> str:
        """A title as a person would recognise it, with the id to look it up by.

        The same sentence :func:`eifo_core.enriching._describe` builds from the
        row, so a title logged by the fetcher and the same title logged by the
        catalog read as one thing rather than two. English first here, unlike
        ``display_name``: this is for somebody reading a log, and the log is in
        English.
        """
        name = self.name_en or self.name_he or "untitled"
        return f"{name!r} (id {self.id})"

    def names(self) -> list[str]:
        """Every name this title is known by, Hebrew first."""
        return [name for name in (self.name_he, self.name_en) if name]


@dataclass(frozen=True, slots=True)
class ProviderInfo:
    """How one score credits itself on the page.

    Declared here rather than in the API, which is where it used to live as a
    dictionary of names. A provider that produces scores is the thing that
    knows what it is called, which of its figures belong together and what its
    mark looks like; the API only knows what it has been told, and had to be
    edited to be told anything.

    The fetcher writes these to ``rating_providers`` on every enrich, so the
    client is never taught a provider - it renders whatever is in the table.
    """

    provider: RatingProvider
    #: This figure's own name: "Tomatometer", "Audience", "מבקרים".
    label: str
    #: The service behind it. Figures sharing a group are one chip, because
    #: they are one service having measured two things - not two raters.
    group_key: str
    group_name: str
    #: The service's mark, as a file this plugin ships. None is ordinary: the
    #: chip falls back to ``group_name``, which is what it showed before marks
    #: existed at all.
    icon: Path | None = None
    website_url: str | None = None
    #: Order within the group. Critics before the crowd, which is the order
    #: both sites that report two figures print them in.
    position: int = 0


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


@dataclass(frozen=True, slots=True)
class OfferFact:
    """What a service itself says about an offer somebody else told us exists.

    The provider harvester learns *that* a title is on Apple TV from JustWatch,
    which carries neither a price nor a link. Apple publishes both. This is how
    the second gets attached to the first: not a new offer - the offer is
    already known - but the two facts about it that the source it came from
    could not supply.

    Addressed by service and kind of deal rather than by row id, because the
    fetcher does not have row ids and should not: it says "Apple TV, rental,
    this price", and the catalog decides which row that is.
    """

    source_key: str
    offer_type: OfferType
    price_minor: int | None = None
    price_currency: str | None = None
    deep_link_url: str | None = None

    @property
    def is_empty(self) -> bool:
        return self.price_minor is None and not self.deep_link_url


@dataclass(slots=True)
class EnrichResult:
    """What an enricher found.

    ``metadata_patch`` fills gaps only: it never overwrites a field that already
    has a value, so a source's guess cannot displace TMDB's canonical answer.
    """

    ratings: list[Rating] = field(default_factory=list)
    metadata_patch: dict[str, Any] = field(default_factory=dict)
    #: Prices and links for offers this title already has. Never creates one:
    #: an enricher knows what a service charges, not what it carries.
    offers: list[OfferFact] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.ratings and not self.metadata_patch and not self.offers
