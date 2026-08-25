"""Plugin discovery.

Built-in plugins are registered here; third-party ones are found through the
``eifo.sources`` entry-point group, so an out-of-tree source installs as an
ordinary pip package with no change to this codebase.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from importlib.metadata import entry_points

from sqlalchemy import select
from sqlalchemy.orm import Session

from eifo_core.models import Source
from eifo_core.settings import Settings
from eifo_fetcher.sources.base import SourceInfo, SourcePlugin

ENTRY_POINT_GROUP = "eifo.sources"

logger = logging.getLogger("eifo.fetch.registry")


def _builtin_plugins() -> list[SourcePlugin]:
    # Imported lazily so a broken optional dependency cannot break the CLI.
    from eifo_fetcher.sources.cinematheque_vod import CinemathequeVodPlugin
    from eifo_fetcher.sources.disney_plus import DisneyPlusPlugin
    from eifo_fetcher.sources.freetv import FreetvPlugin
    from eifo_fetcher.sources.israel_film_archive import IsraelFilmArchivePlugin
    from eifo_fetcher.sources.kan import KanPlugin
    from eifo_fetcher.sources.mako import MakoPlugin
    from eifo_fetcher.sources.reshet13 import Reshet13Plugin
    from eifo_fetcher.sources.tmdb_providers import TmdbProvidersPlugin

    return [
        TmdbProvidersPlugin(),
        CinemathequeVodPlugin(),
        DisneyPlusPlugin(),
        FreetvPlugin(),
        IsraelFilmArchivePlugin(),
        KanPlugin(),
        MakoPlugin(),
        Reshet13Plugin(),
    ]


def _entry_point_plugins() -> list[SourcePlugin]:
    plugins: list[SourcePlugin] = []
    for entry_point in entry_points(group=ENTRY_POINT_GROUP):
        try:
            plugin = entry_point.load()()
        except Exception:
            logger.exception("could not load source plugin %r", entry_point.name)
            continue
        if isinstance(plugin, SourcePlugin):
            plugins.append(plugin)
        else:
            logger.error("%r is not a SourcePlugin; ignoring", entry_point.name)
    return plugins


def discover_plugins() -> list[SourcePlugin]:
    """Every installed plugin, built-in first."""
    return [*_builtin_plugins(), *_entry_point_plugins()]


def declared_sources(plugins: Iterable[SourcePlugin]) -> dict[str, SourceInfo]:
    """Map every declared source key to its info.

    Raises:
        ValueError: if two plugins claim the same key - silently letting one win
            would make catalogs depend on import order.
    """
    declared: dict[str, SourceInfo] = {}
    owners: dict[str, str] = {}
    for plugin in plugins:
        name = type(plugin).__name__
        for info in plugin.sources():
            if info.key in declared:
                raise ValueError(
                    f"source key {info.key!r} declared by both {owners[info.key]} and {name}"
                )
            declared[info.key] = info
            owners[info.key] = name
    return declared


def source_overrides(session: Session) -> dict[str, bool]:
    """Operator switches set from the Manage tab, by source key.

    Only the rows that carry one: a NULL ``sources.enabled`` is not an answer,
    it is the absence of one, and means the configuration file still decides.
    """
    rows = session.execute(
        select(Source.key, Source.enabled).where(Source.enabled.is_not(None))
    ).all()
    return {key: bool(enabled) for key, enabled in rows}


def enabled_sources(
    plugins: Iterable[SourcePlugin],
    settings: Settings,
    *,
    overrides: Mapping[str, bool] | None = None,
) -> dict[str, SourceInfo]:
    """Declared sources that are switched on.

    A source absent from the configuration file defaults to enabled, so adding a
    plugin is one file rather than a file plus a config edit.

    Args:
        overrides: an operator's answer per source key, from
            :func:`source_overrides`, which wins over the file. Absent means the
            file decides, which is what every source did before the Manage tab
            existed and what every source still does until somebody uses it.
    """
    switched = overrides or {}
    return {
        key: info
        for key, info in declared_sources(plugins).items()
        if switched.get(key, settings.source_config(key).enabled)
    }


def plugins_for(
    plugins: Iterable[SourcePlugin],
    source_keys: Iterable[str],
) -> list[tuple[SourcePlugin, list[SourceInfo]]]:
    """Pair each plugin with the requested sources it owns, skipping the rest."""
    wanted = set(source_keys)
    pairs: list[tuple[SourcePlugin, list[SourceInfo]]] = []
    for plugin in plugins:
        owned = [info for info in plugin.sources() if info.key in wanted]
        if owned:
            pairs.append((plugin, owned))
    return pairs
