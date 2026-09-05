"""Shared core for the Eifo services: settings, schema and migrations.

The fetcher and the API never talk to each other directly - this package is the
only contract between them, so the schema is defined exactly once.
"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _version

from eifo_core.enums import (
    FetchPhase,
    FetchStatus,
    OfferType,
    SourceKind,
    TitleKind,
)
from eifo_core.settings import MissingSettingsError, Settings, get_settings

#: The version this package was installed as, read from its own distribution
#: metadata rather than written here.
#:
#: It used to be a string in this file, and it drifted immediately: release-please
#: bumps `pyproject.toml` through an `x-release-please-version` marker and there
#: was no marker here, so every package still said 0.1.0 at 0.11.0. That is not a
#: cosmetic difference - it is what `/api/v1/meta` reports to clients, what the
#: OpenAPI document declares, and what `eifo-fetch --version` prints.
#:
#: Derived rather than marked, so there is one place to bump and it is the one
#: release tooling already owns. The fallback is for a source tree nobody has
#: installed, where there is no metadata to read and no version to be right
#: about.
try:
    __version__ = _version("eifo-core")
except PackageNotFoundError:  # pragma: no cover - a checkout that was never installed
    __version__ = "0.0.0+unknown"

__all__ = [
    "FetchPhase",
    "FetchStatus",
    "MissingSettingsError",
    "OfferType",
    "Settings",
    "SourceKind",
    "TitleKind",
    "__version__",
    "get_settings",
]
