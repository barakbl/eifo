"""Plugin discovery.

Built-in plugins are registered here; third-party ones are found through the
``tvil.sources`` entry-point group, so an out-of-tree source installs as an
ordinary pip package with no change to this codebase.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from importlib.metadata import entry_points

from tvil_core.settings import Settings
from tvil_fetcher.sources.base import SourceInfo, SourcePlugin

ENTRY_POINT_GROUP = "tvil.sources"

logger = logging.getLogger("tvil.fetch.registry")


def _builtin_plugins() -> list[SourcePlugin]:
    # Imported lazily so a broken optional dependency cannot break the CLI.
    from tvil_fetcher.sources.disney_plus import DisneyPlusPlugin
    from tvil_fetcher.sources.freetv import FreetvPlugin
    from tvil_fetcher.sources.mako import MakoPlugin
    from tvil_fetcher.sources.tmdb_providers import TmdbProvidersPlugin

    return [TmdbProvidersPlugin(), DisneyPlusPlugin(), FreetvPlugin(), MakoPlugin()]


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


def enabled_sources(
    plugins: Iterable[SourcePlugin],
    settings: Settings,
) -> dict[str, SourceInfo]:
    """Declared sources that configuration switches on.

    A source absent from the configuration file defaults to enabled, so adding a
    plugin is one file rather than a file plus a config edit.
    """
    return {
        key: info
        for key, info in declared_sources(plugins).items()
        if settings.source_config(key).enabled
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
