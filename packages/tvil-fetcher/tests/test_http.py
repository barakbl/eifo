"""The shared HTTP client: politeness and retries."""

from __future__ import annotations

import httpx
import pytest
import respx

from tvil_fetcher.http import (
    USER_AGENT,
    HttpClient,
    RateLimiter,
    RetryableStatusError,
)


class TestRateLimiter:
    def test_first_request_to_a_host_is_immediate(self) -> None:
        limiter = RateLimiter(default_rps=1.0)
        clock = iter([0.0, 0.0])

        waited = limiter.wait("example.com", sleep=lambda _s: None, now=lambda: next(clock))

        assert waited == 0.0

    def test_second_request_waits_for_the_interval(self) -> None:
        limiter = RateLimiter(default_rps=2.0)  # one request every 0.5s
        slept: list[float] = []
        times = iter([0.0, 0.0])

        limiter.wait("example.com", sleep=slept.append, now=lambda: next(times))
        waited = limiter.wait("example.com", sleep=slept.append, now=lambda: 0.0)

        assert waited == pytest.approx(0.5)
        assert slept == [pytest.approx(0.5)]

    def test_hosts_are_limited_independently(self) -> None:
        limiter = RateLimiter(default_rps=1.0)
        limiter.wait("a.example", sleep=lambda _s: None, now=lambda: 0.0)

        waited = limiter.wait("b.example", sleep=lambda _s: None, now=lambda: 0.0)

        assert waited == 0.0

    def test_per_host_override_applies(self) -> None:
        limiter = RateLimiter(default_rps=100.0)
        limiter.set_host_rate("slow.example", 0.5)  # one request every 2s
        limiter.wait("slow.example", sleep=lambda _s: None, now=lambda: 0.0)

        waited = limiter.wait("slow.example", sleep=lambda _s: None, now=lambda: 0.0)

        assert waited == pytest.approx(2.0)

    def test_zero_rate_disables_limiting(self) -> None:
        limiter = RateLimiter(default_rps=0)
        limiter.wait("example.com", sleep=lambda _s: None, now=lambda: 0.0)

        assert limiter.wait("example.com", sleep=lambda _s: None, now=lambda: 0.0) == 0.0


class TestHttpClient:
    @respx.mock
    def test_sends_an_identifying_user_agent(self, http: HttpClient) -> None:
        route = respx.get("https://example.com/x").mock(return_value=httpx.Response(200))

        http.get("https://example.com/x")

        assert route.calls.last.request.headers["user-agent"] == USER_AGENT
        assert "tvil-fetcher" in USER_AGENT

    @respx.mock
    def test_decodes_json(self, http: HttpClient) -> None:
        respx.get("https://example.com/j").mock(return_value=httpx.Response(200, json={"a": 1}))

        assert http.get_json("https://example.com/j") == {"a": 1}

    @respx.mock
    def test_retries_a_server_error_then_succeeds(self, http: HttpClient) -> None:
        route = respx.get("https://example.com/flaky").mock(
            side_effect=[httpx.Response(503), httpx.Response(200, json={"ok": True})]
        )

        assert http.get_json("https://example.com/flaky") == {"ok": True}
        assert route.call_count == 2

    @respx.mock
    def test_gives_up_after_the_attempt_limit(self, http: HttpClient) -> None:
        route = respx.get("https://example.com/down").mock(return_value=httpx.Response(500))

        with pytest.raises(RetryableStatusError):
            http.get("https://example.com/down")

        assert route.call_count == 3

    @respx.mock
    def test_does_not_retry_a_client_error(self, http: HttpClient) -> None:
        """A 404 will still be a 404 next time; retrying only adds load."""
        route = respx.get("https://example.com/missing").mock(return_value=httpx.Response(404))

        with pytest.raises(httpx.HTTPStatusError):
            http.get("https://example.com/missing")

        assert route.call_count == 1

    @respx.mock
    def test_retries_a_transport_error(self, http: HttpClient) -> None:
        route = respx.get("https://example.com/reset").mock(
            side_effect=[httpx.ConnectError("reset"), httpx.Response(200)]
        )

        http.get("https://example.com/reset")

        assert route.call_count == 2

    @respx.mock
    def test_honours_retry_after(self) -> None:
        slept: list[float] = []
        client = HttpClient(rate_limiter=RateLimiter(default_rps=0), sleep=slept.append)
        respx.get("https://example.com/429").mock(
            side_effect=[
                httpx.Response(429, headers={"Retry-After": "7"}),
                httpx.Response(200),
            ]
        )

        with client:
            client.get("https://example.com/429")

        assert 7.0 in slept

    @respx.mock
    def test_ignores_an_unparsable_retry_after(self) -> None:
        """An HTTP-date Retry-After is legal; backoff covers it either way."""
        slept: list[float] = []
        client = HttpClient(rate_limiter=RateLimiter(default_rps=0), sleep=slept.append)
        respx.get("https://example.com/429").mock(
            side_effect=[
                httpx.Response(429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}),
                httpx.Response(200),
            ]
        )

        with client:
            client.get("https://example.com/429")  # must not raise
