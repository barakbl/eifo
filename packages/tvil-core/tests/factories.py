"""Object factories for the core test suite."""

from __future__ import annotations

from typing import Any

from tvil_core.enums import SourceKind, TitleKind
from tvil_core.models import Source, Title


def make_title(**overrides: Any) -> Title:
    """A valid series with both names filled in."""
    values: dict[str, Any] = {
        "type": TitleKind.SERIES,
        "name_he": "פאודה",
        "name_en": "Fauda",
        "year": 2015,
    }
    values.update(overrides)
    return Title(**values)


def make_source(**overrides: Any) -> Source:
    """A valid subscription source."""
    values: dict[str, Any] = {
        "key": "cellcom_tv",
        "name": "Cellcom TV",
        "kind": SourceKind.SUBSCRIPTION,
        "website_url": "https://cellcom.co.il",
    }
    values.update(overrides)
    return Source(**values)
