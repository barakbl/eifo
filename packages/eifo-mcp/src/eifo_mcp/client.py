"""The Eifo API, as seen from an AI assistant's machine.

A thin client: one instance, one token, plain GETs. Everything an assistant
learns goes through endpoints the web app uses too, with the token's own scope
deciding what it may reach - so this package adds no way into an instance that
was not already there.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import httpx

API_PREFIX = "/api/v1"

#: Long enough for a slow page of the catalog, short enough that an assistant
#: waiting on a server that has gone away gives up and says so.
TIMEOUT_SECONDS = 15.0

#: How long the services and genres lists are reused. They change when a
#: source is added, which is a deploy, not a minute-by-minute thing.
REFERENCE_TTL_SECONDS = 600.0

#: Hosts a token may be sent to over plain HTTP: this machine, where a
#: developer runs the API, and nowhere else.
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class EifoError(Exception):
    """The instance said no, or could not be reached. The message says which."""


class ConfigError(EifoError):
    """The URL or token is missing or unsafe."""


def normalise_base_url(raw: str) -> str:
    """``eifo.example.com`` -> ``https://eifo.example.com``, refusing plain HTTP.

    The token travels on every request. Over plain HTTP to anywhere but this
    machine it travels readable, so that is refused rather than warned about.
    """
    text = (raw or "").strip()
    if not text:
        raise ConfigError("EIFO_URL is not set. Set it to your instance, e.g. eifo.example.com")
    if "://" not in text:
        text = f"https://{text}"
    parts = urlsplit(text)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ConfigError(f"EIFO_URL is not a web address: {raw!r}")
    if parts.scheme == "http" and parts.hostname not in LOCAL_HOSTS:
        raise ConfigError(
            "EIFO_URL uses plain http, which would send your token unencrypted. Use https."
        )
    path = parts.path.rstrip("/")
    if path.endswith(API_PREFIX):
        path = path[: -len(API_PREFIX)]
    return f"{parts.scheme}://{parts.netloc}{path}"


class EifoClient:
    """GET requests to one instance, as the holder of one token."""

    def __init__(
        self,
        base_url: str,
        token: str | Callable[[], str],
        *,
        http: httpx.Client | None = None,
        clock: Any = time.monotonic,
    ) -> None:
        """``token`` is the token, or - for the remote connector, where every
        call carries the token of whoever made it - a function returning it."""
        if not callable(token) and (not token or not token.strip()):
            raise ConfigError(
                "EIFO_TOKEN is not set. Create a read-only token in Eifo's Settings and use that."
            )
        self.base_url = normalise_base_url(base_url)
        self._http = http or httpx.Client(timeout=TIMEOUT_SECONDS)
        self._token = token
        self._clock = clock
        self._reference: dict[str, tuple[float, Any]] = {}

    def close(self) -> None:
        self._http.close()

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """One API call; ``path`` is relative to ``/api/v1``."""
        query = {key: value for key, value in (params or {}).items() if value is not None}
        token = self._token() if callable(self._token) else self._token
        headers = {
            "Authorization": f"Bearer {token.strip()}",
            "Accept": "application/json",
            "User-Agent": "eifo-mcp",
        }
        try:
            response = self._http.get(
                f"{self.base_url}{API_PREFIX}{path}", params=query, headers=headers
            )
        except httpx.HTTPError as cause:
            raise EifoError(
                f"Could not reach Eifo at {self.base_url}: {type(cause).__name__}"
            ) from cause

        if response.status_code == 401:
            raise EifoError("Eifo did not accept the token. It may have been revoked.")
        if response.status_code >= 400:
            raise EifoError(_detail(response))
        try:
            return response.json()
        except ValueError as cause:
            raise EifoError("Eifo answered with something that is not JSON.") from cause

    def link(self, title_id: int) -> str:
        """Where a person opens this title in the web app."""
        return f"{self.base_url}/#/title/{title_id}"

    # -- reference data, reused for a while ---------------------------------

    def sources(self) -> list[dict[str, Any]]:
        return list(self._remembered("sources", lambda: self.get("/sources")))

    def genres(self) -> list[dict[str, Any]]:
        return list(self._remembered("genres", lambda: self.get("/genres")))

    def me(self) -> dict[str, Any]:
        """The token's owner. Not remembered: "my services" can change any time."""
        payload: dict[str, Any] = self.get("/me")
        return payload

    def _remembered(self, key: str, load: Any) -> Any:
        now = self._clock()
        held = self._reference.get(key)
        if held is not None and now - held[0] < REFERENCE_TTL_SECONDS:
            return held[1]
        value = load()
        self._reference[key] = (now, value)
        return value


def _detail(response: httpx.Response) -> str:
    """The server's own explanation, which usually says what to do next."""
    try:
        payload = response.json()
    except ValueError:
        payload = None
    detail = payload.get("detail") if isinstance(payload, dict) else None
    if isinstance(detail, str) and detail:
        return detail
    return f"Eifo answered {response.status_code}."
