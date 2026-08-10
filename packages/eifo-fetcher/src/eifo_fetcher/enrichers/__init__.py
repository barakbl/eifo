"""Ratings and metadata providers.

Built-ins are registered here; third-party enrichers are discovered through the
``eifo.enrichers`` entry-point group, mirroring how sources work.
"""

from __future__ import annotations

import logging
from importlib.metadata import entry_points

from eifo_core.settings import Settings
from eifo_fetcher.enrichers.base import Enricher, EnrichResult, Rating, TitleView

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


def _builtin_enrichers() -> list[Enricher]:
    # Imported lazily so one broken provider cannot break the whole CLI.
    from eifo_fetcher.enrichers.rt import RottenTomatoesEnricher
    from eifo_fetcher.enrichers.seret import SeretEnricher
    from eifo_fetcher.enrichers.tmdb_meta import TmdbMetadataEnricher

    # TMDB first: it fills the imdb_id and names the others resolve against.
    return [TmdbMetadataEnricher(), SeretEnricher(), RottenTomatoesEnricher()]


#: Providers switched off unless configuration enables them. Seret has no
#: working title search, so it cannot resolve a title to a page on its own
#: (see eifo_fetcher.enrichers.seret).
DISABLED_BY_DEFAULT = frozenset({"seret"})


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


def discover_enrichers(settings: Settings | None = None) -> list[Enricher]:
    """Every installed enricher that configuration leaves switched on."""
    enrichers = [*_builtin_enrichers(), *_entry_point_enrichers()]
    if settings is None:
        return enrichers

    disabled = DISABLED_BY_DEFAULT - set(settings.enrich.enabled)
    disabled |= set(settings.enrich.disabled)
    return [enricher for enricher in enrichers if enricher.key not in disabled]
