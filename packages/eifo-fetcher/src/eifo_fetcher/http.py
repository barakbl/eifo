"""Shared HTTP client.

Every outbound request in the fetcher goes through here so politeness is a
property of the system rather than of each plugin author's diligence: one
identifying User-Agent, a per-host rate limit, retries with backoff, and
``Retry-After`` honoured on 429s (docs.internal/03-sources.md).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from types import TracebackType
from typing import Any, Self

import httpx
from tenacity import (
    RetryCallState,
    Retrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from eifo_fetcher import __version__

logger = logging.getLogger("eifo.fetch.http")

USER_AGENT = f"eifo-fetcher/{__version__} (+https://github.com/barakbl/eifo)"

DEFAULT_RATE_LIMIT_RPS = 1.0
DEFAULT_TIMEOUT_SECONDS = 20.0
DEFAULT_ATTEMPTS = 3

#: Server-side failures worth retrying. 404 and other 4xx are not.
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class RetryableStatusError(httpx.HTTPError):
    """A response whose status code warrants another attempt."""

    def __init__(self, response: httpx.Response) -> None:
        super().__init__(f"retryable status {response.status_code} for {response.request.url}")
        self.response = response

    @property
    def retry_after_seconds(self) -> float | None:
        """The server's ``Retry-After`` in seconds, when it sent a usable one."""
        raw = self.response.headers.get("retry-after")
        if not raw:
            return None
        try:
            return max(0.0, float(raw))
        except ValueError:
            # The HTTP-date form is legal but rare; backoff covers that case.
            return None


class RateLimiter:
    """Per-host minimum spacing between requests.

    Thread-safe because a future stage may fetch sources concurrently; the
    fetcher is single-threaded today and this costs nothing.
    """

    def __init__(self, default_rps: float = DEFAULT_RATE_LIMIT_RPS) -> None:
        self._default_rps = default_rps
        self._overrides: dict[str, float] = {}
        self._next_allowed: dict[str, float] = {}
        self._lock = threading.Lock()

    def set_host_rate(self, host: str, rps: float) -> None:
        """Override the rate for one host. ``rps <= 0`` disables limiting."""
        self._overrides[host] = rps

    def _interval(self, host: str) -> float:
        rps = self._overrides.get(host, self._default_rps)
        return 0.0 if rps <= 0 else 1.0 / rps

    def wait(self, host: str, *, sleep: Any = time.sleep, now: Any = time.monotonic) -> float:
        """Block until the next request to ``host`` is allowed.

        Returns the number of seconds actually waited, which the tests assert on
        instead of measuring wall-clock time.
        """
        interval = self._interval(host)
        if interval <= 0:
            return 0.0

        with self._lock:
            current = now()
            earliest = self._next_allowed.get(host, 0.0)
            delay = max(0.0, earliest - current)
            self._next_allowed[host] = max(current, earliest) + interval

        if delay > 0:
            sleep(delay)
        return delay


class HttpClient:
    """Rate-limited, retrying HTTP client shared by every plugin."""

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        rate_limiter: RateLimiter | None = None,
        attempts: int = DEFAULT_ATTEMPTS,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        sleep: Any = time.sleep,
    ) -> None:
        self._client = client or httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        )
        self.rate_limiter = rate_limiter or RateLimiter()
        self._attempts = attempts
        self._sleep = sleep

    def get(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        """GET with rate limiting and retries. Raises on a final failure."""
        return self._request("GET", url, params=params, headers=headers)

    def get_json(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        """GET and decode JSON."""
        return self.get(url, params=params, headers=headers).json()

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None,
        headers: Mapping[str, str] | None,
    ) -> httpx.Response:
        host = httpx.URL(url).host

        for attempt in Retrying(
            stop=stop_after_attempt(self._attempts),
            wait=wait_exponential_jitter(initial=1.0, max=30.0),
            retry=retry_if_exception_type((RetryableStatusError, httpx.TransportError)),
            before_sleep=self._honour_retry_after,
            reraise=True,
        ):
            with attempt:
                self.rate_limiter.wait(host, sleep=self._sleep)
                response = self._client.request(method, url, params=params, headers=headers)
                if response.status_code in RETRYABLE_STATUS:
                    raise RetryableStatusError(response)
                response.raise_for_status()
                return response

        raise AssertionError("unreachable: Retrying either returns or raises")

    def _honour_retry_after(self, state: RetryCallState) -> None:
        """Sleep out a server-requested delay on top of tenacity's backoff."""
        exc = state.outcome.exception() if state.outcome else None
        if isinstance(exc, RetryableStatusError):
            delay = exc.retry_after_seconds
            if delay:
                logger.debug("honouring Retry-After of %.1fs", delay)
                self._sleep(delay)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


@contextmanager
def http_client(**kwargs: Any) -> Iterator[HttpClient]:
    """Context-managed :class:`HttpClient`."""
    client = HttpClient(**kwargs)
    try:
        yield client
    finally:
        client.close()
