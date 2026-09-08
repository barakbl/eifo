"""Writing down what each ratings provider says about itself.

The API credits every score - a rating without its source is a rumour - but it
has no way to ask a plugin anything: it depends on ``eifo-core`` and nothing
else. So how a provider is named, which of its figures belong together and what
its mark looks like used to be a dictionary in the API, hand-kept, a package
away from the enricher that actually produced the score.

This moves it the same way sources moved: the fetcher is the only process that
knows what plugins exist, so it declares what they say and the catalog writes it
down. What crosses is a declaration, not a row - deciding what the declaration
means, and where the mark lands, is the catalog's business and happens there.

**Marks travel as bytes.** They are files that ship with the plugin, and the
plugin is not necessarily on the same machine as the images directory any more.
Sent whole on every declaration rather than negotiated: they are four small
files, the request is a few tens of kilobytes, and the alternative is a digest
exchange with a second round trip to save nothing anybody would notice.
"""

from __future__ import annotations

import logging
from base64 import b64encode
from collections.abc import Iterable
from typing import Any

from eifo_core.settings import Settings
from eifo_fetcher.enrichers.base import ProviderInfo
from eifo_fetcher.ingest import IngestClient, IngestError

logger = logging.getLogger("eifo.fetch.providers")


def refresh_declared_providers(api: IngestClient, settings: Settings) -> list[str]:
    """Tell the catalog what the plugins installed right now declare.

    Called wherever the fetcher builds a client, not from one phase, because
    the thing that needs this table is a title page being rendered - and a page
    is rendered between runs, not during one. Hanging it off the enrich alone
    meant a deployment that upgraded on Tuesday credited its scores by database
    key until Wednesday's nightly had finished: hours of the feature looking
    broken, fixed by a job nobody would think to connect it to.

    Cheap enough to belong there. Seven declarations compared against seven
    rows, four small files that are already where they should be, and nothing
    written on a run that finds the catalog already saying what the plugins say
    - which is every run but the first after an upgrade.

    A refusal is reported and swallowed. This is a caption on a chip; a fetcher
    that would not sync a catalog because it could not update a logo would be a
    poor trade, and the phase itself will fail on its own if the API is really
    unreachable.
    """
    # Imported here rather than at module scope: this is the one function that
    # needs the plugins, and importing them costs every enricher's imports.
    from eifo_fetcher.enrichers import discover_enrichers
    from eifo_fetcher.enrichers.imdb import ImdbDatasetLoader

    declared = declared_providers([*discover_enrichers(settings), ImdbDatasetLoader])
    try:
        return api.declare_providers([provider_to_wire(info) for info in declared])
    except IngestError as exc:
        logger.warning("could not declare what credits each score: %s", exc)
        return []


def declared_providers(sources: Iterable[object]) -> list[ProviderInfo]:
    """Every ``ProviderInfo`` the given plugins declare, in the order found.

    Takes anything with a ``provider_info`` attribute rather than an
    ``Enricher``: the IMDb pass is a bulk join over a dataset rather than an
    enricher, and it is still the thing that produces the IMDb score and so
    still the thing that should say how that score is credited.

    A provider declared twice keeps its first declaration, which is the one
    from the plugin that ships with Eifo - a third-party plugin may add a
    provider but should not be able to silently rename an existing one.
    """
    seen: set[str] = set()
    found: list[ProviderInfo] = []
    for source in sources:
        for info in getattr(source, "provider_info", ()):
            if info.provider.value in seen:
                logger.debug("provider %s already declared; keeping the first", info.provider)
                continue
            seen.add(info.provider.value)
            found.append(info)
    return found


def provider_to_wire(info: ProviderInfo) -> dict[str, Any]:
    """One declaration, with its mark inlined.

    A mark that is declared and not there is sent without one rather than
    refused: the chip then says the provider's name, which is what every chip
    said before marks existed at all, and it is not a reason to fail an enrich
    that is about to write ten thousand ratings.
    """
    logo = _mark(info)
    return {
        "provider": info.provider.value,
        "label": info.label,
        "group_key": info.group_key,
        "group_name": info.group_name,
        "website_url": info.website_url,
        "position": info.position,
        "logo": None if logo is None else b64encode(logo).decode("ascii"),
        "logo_suffix": info.icon.suffix if info.icon is not None else "",
    }


def _mark(info: ProviderInfo) -> bytes | None:
    if info.icon is None:
        return None
    if not info.icon.is_file():
        logger.warning("%s declares a logo at %s, which is not there", info.provider, info.icon)
        return None
    try:
        return info.icon.read_bytes()
    except OSError as exc:  # pragma: no cover - a permissions problem
        logger.warning("could not read the %s mark at %s: %s", info.provider, info.icon, exc)
        return None
