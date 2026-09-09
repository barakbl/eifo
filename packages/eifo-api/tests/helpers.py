"""Typed helpers shared by the API test suite."""

from __future__ import annotations

import datetime as dt
from typing import Protocol
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient

from eifo_core.enums import AuthProvider, FetchStatus

#: The deployment the suite pretends to be. HTTPS because cookies are Secure.
PUBLIC_ORIGIN = "https://eifo.test"
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


class MakeAdmin(Protocol):
    """Signature of the ``make_admin`` fixture."""

    def __call__(self, email: str = ...) -> None: ...


class SignIn(Protocol):
    """Signature of the ``sign_in`` fixture; returns the session's CSRF token."""

    def __call__(self, provider: AuthProvider = ...) -> str: ...


def start_login(client: TestClient, provider: str = "google") -> tuple[str, str]:
    """Begin a login and return the provider URL and the state it minted."""
    response = client.get(f"/api/v1/auth/login/{provider}", follow_redirects=False)
    assert response.status_code == 302
    location = response.headers["location"]
    return location, parse_qs(urlparse(location).query)["state"][0]
