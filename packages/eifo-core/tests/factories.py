"""Object factories for the core test suite."""

from __future__ import annotations

from typing import Any

from eifo_core.enums import AuthProvider, SourceKind, TitleKind
from eifo_core.models import Source, Title, User


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


def make_user(**overrides: Any) -> User:
    """A Google account with nothing but the defaults."""
    values: dict[str, Any] = {
        "auth_provider": AuthProvider.GOOGLE,
        "auth_subject": "108100000000000000001",
        "email": "viewer@example.com",
        "display_name": "צופה",
    }
    values.update(overrides)
    return User(**values)
