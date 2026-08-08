"""Typed helpers shared by the API test suite."""

from __future__ import annotations

import datetime as dt
from typing import Protocol

from tvil_core.enums import AuthProvider, FetchStatus

#: The deployment the suite pretends to be. HTTPS because cookies are Secure.
PUBLIC_ORIGIN = "https://tvil.test"
SECRET_KEY = "test-secret-key-not-used-anywhere-real"


class SeedSource(Protocol):
    """Signature of the ``seed_source`` fixture."""

    def __call__(
        self,
        key: str = ...,
        *,
        name: str = ...,
        active: bool = ...,
        synced_at: dt.datetime | None = ...,
        status: FetchStatus = ...,
    ) -> None: ...


class SignIn(Protocol):
    """Signature of the ``sign_in`` fixture; returns the session's CSRF token."""

    def __call__(self, provider: AuthProvider = ...) -> str: ...
