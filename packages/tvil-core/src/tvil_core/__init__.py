"""Shared core for the TVIL services: settings, schema and migrations.

The fetcher and the API never talk to each other directly — this package is the
only contract between them, so the schema is defined exactly once.
"""

from tvil_core.enums import (
    FetchPhase,
    FetchStatus,
    OfferType,
    SourceKind,
    TitleKind,
)
from tvil_core.settings import MissingSettingsError, Settings, get_settings

__version__ = "0.1.0"

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
