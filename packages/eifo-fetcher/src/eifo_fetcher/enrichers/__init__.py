"""Ratings and metadata providers.

Built-ins are registered here; third-party enrichers are discovered through the
``eifo.enrichers`` entry-point group, mirroring how sources work.
"""

from __future__ import annotations

import logging
from importlib.metadata import entry_points
from typing import TYPE_CHECKING

from eifo_core.settings import Settings
from eifo_fetcher.enrichers.base import Enricher, EnrichResult, Rating, TitleView

if TYPE_CHECKING:  # pragma: no cover - typing only; the module is imported lazily
    from eifo_fetcher.enrichers.seret_index import SeretLookup

ENTRY_POINT_GROUP = "eifo.enrichers"

logger = logging.getLogger("eifo.fetch.enrichers")

__all__ = [
    "ENTRY_POINT_GROUP",
    "EnrichResult",
    "Enricher",
    "Rating",
    "TitleView",
    "discover_enrichers",
]


def _builtin_enrichers(seret_lookup: SeretLookup | None = None) -> list[Enricher]:
    # Imported lazily so one broken provider cannot break the whole CLI.
    from eifo_fetcher.enrichers.rt import RottenTomatoesEnricher
    from eifo_fetcher.enrichers.seret import SeretEnricher
    from eifo_fetcher.enrichers.tmdb_meta import TmdbMetadataEnricher

    # TMDB first: it fills the imdb_id and names the others resolve against.
    return [
        TmdbMetadataEnricher(),
        SeretEnricher(seret_lookup),
        RottenTomatoesEnricher(),
    ]


#: Providers switched off unless configuration enables them.
#:
#: Empty. Seret used to be here because it had no way to resolve a title to a
#: page; it now resolves through the index built by ``eifo-fetch seret index``,
#: and an unbuilt index costs nothing and says so rather than misbehaving.
DISABLED_BY_DEFAULT: frozenset[str] = frozenset()


def _entry_point_enrichers() -> list[Enricher]:
    found: list[Enricher] = []
    for entry_point in entry_points(group=ENTRY_POINT_GROUP):
        try:
            enricher = entry_point.load()()
        except Exception:
            logger.exception("could not load enricher %r", entry_point.name)
            continue
        if isinstance(enricher, Enricher):
            found.append(enricher)
        else:
            logger.error("%r is not an Enricher; ignoring", entry_point.name)
    return found


def discover_enrichers(
    settings: Settings | None = None,
    *,
    seret_lookup: SeretLookup | None = None,
) -> list[Enricher]:
    """Every installed enricher that configuration leaves switched on.

    Args:
        seret_lookup: the loaded Seret page index. Passed in rather than read
            here because enrichers are pure readers with no database access of
            their own - loading it is the caller's job, and the caller is the
            one that already holds a session.
    """
    enrichers = [*_builtin_enrichers(seret_lookup), *_entry_point_enrichers()]
    if settings is None:
        return enrichers

    disabled = DISABLED_BY_DEFAULT - set(settings.enrich.enabled)
    disabled |= set(settings.enrich.disabled)
    return [enricher for enricher in enrichers if enricher.key not in disabled]
