"""How fast anybody may call the API, with time under the test's control."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from helpers import SignIn

from eifo_api.ratelimit import (
    ADDRESS,
    ANONYMOUS,
    MEMBER,
    SIGN_IN,
    WRITES,
    Buckets,
    Draw,
    Rate,
    credential_of,
    draws_for,
)
from eifo_api.security import CSRF_HEADER


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(app: FastAPI) -> Clock:
    """The limiter switched on, on a clock that only moves when told to."""
    clock = Clock()
    app.state.settings.rate_limit = True
    app.state.rate_buckets = Buckets(clock=clock)
    return clock


def burst(client: TestClient, path: str, count: int, **kwargs: object) -> list[int]:
    return [client.get(path, **kwargs).status_code for _ in range(count)]  # type: ignore[arg-type]


class TestBuckets:
    def test_a_burst_then_the_steady_rate(self) -> None:
        clock = Clock()
        buckets = Buckets(clock=clock)
        draw = [Draw("k", Rate(per_second=2, burst=3))]

        assert [buckets.take(draw) for _ in range(3)] == [0, 0, 0]
        assert buckets.take(draw) == pytest.approx(0.5)

        clock.now += 0.5
        assert buckets.take(draw) == 0
        assert buckets.take(draw) > 0

    def test_never_more_than_the_burst_saved_up(self) -> None:
        clock = Clock()
        buckets = Buckets(clock=clock)
        draw = [Draw("k", Rate(per_second=1, burst=2))]
        clock.now += 3600

        assert [buckets.take(draw) == 0 for _ in range(3)] == [True, True, False]

    def test_all_or_nothing(self) -> None:
        """A request one bucket refuses does not spend the others."""
        buckets = Buckets(clock=Clock())
        roomy = Draw("roomy", Rate(per_second=1, burst=5))
        tight = Draw("tight", Rate(per_second=1, burst=1))
        assert buckets.take([roomy, tight]) == 0

        for _ in range(3):
            assert buckets.take([roomy, tight]) > 0

        assert [buckets.take([roomy]) == 0 for _ in range(5)] == [True, True, True, True, False]


class TestWhoIsWho:
    def test_the_api_and_nothing_else(self) -> None:
        for path in ("/", "/js/app.js", "/css/app.css", "/images/posters/1/w200.jpg"):
            assert draws_for("GET", path, credential=None, address="a") == []

    def test_a_stranger_reads_on_their_address(self) -> None:
        draws = draws_for("GET", "/api/v1/titles", credential=None, address="203.0.113.7")

        assert draws == [
            Draw("address:203.0.113.7", ADDRESS),
            Draw("addr:203.0.113.7", ANONYMOUS),
        ]

    def test_a_member_is_the_same_member_anywhere(self) -> None:
        here = draws_for("GET", "/api/v1/titles", credential="abc", address="1.1.1.1")
        there = draws_for("GET", "/api/v1/titles", credential="abc", address="2.2.2.2")

        assert here[1] == there[1] == Draw("cred:abc", MEMBER)

    def test_a_write_also_draws_from_the_write_bucket(self) -> None:
        draws = draws_for("PUT", "/api/v1/me/items/1", credential="abc", address="a")

        assert draws[-1] == Draw("write:cred:abc", WRITES)

    def test_signing_in_has_its_own_slow_bucket(self) -> None:
        for path in ("/api/v1/auth/login/google", "/api/v1/auth/callback/google"):
            assert draws_for("GET", path, credential=None, address="a") == [
                Draw("sign-in:a", SIGN_IN)
            ]

    def test_the_fetcher_is_exempt_but_a_stranger_is_not(self) -> None:
        assert draws_for("POST", "/api/v1/ingest/runs", credential="abc", address="a") == []
        assert draws_for("POST", "/api/v1/ingest/runs", credential=None, address="a") != []


class TestCredentials:
    def test_a_bearer_token_is_hashed(self) -> None:
        found = credential_of([(b"authorization", b"Bearer eifo_pat_secret")])

        assert found is not None
        assert "secret" not in found
        assert len(found) == 24

    def test_the_session_cookie(self) -> None:
        cookie = credential_of([(b"cookie", b"other=1; eifo_session=abc")])

        assert cookie == credential_of([(b"authorization", b"Bearer abc")])

    @pytest.mark.parametrize(
        "headers",
        [
            [],
            [(b"cookie", b"other=1")],
            [(b"authorization", b"Basic dXNlcg==")],
            [(b"authorization", b"Bearer   ")],
            [(b"cookie", b"\x00garbage;;==")],
        ],
    )
    def test_none_when_there_is_none(self, headers: list[tuple[bytes, bytes]]) -> None:
        assert credential_of(headers) is None


class TestInTheApp:
    def test_off_switch(self, client: TestClient, app: FastAPI) -> None:
        """The test suite's default: everything goes through."""
        assert set(burst(client, "/api/v1/genres", 40)) == {200}

    def test_a_stranger_gets_a_burst_then_429(self, client: TestClient, clock: Clock) -> None:
        codes = burst(client, "/api/v1/genres", ANONYMOUS.burst + 1)

        assert codes[:-1] == [200] * ANONYMOUS.burst
        assert codes[-1] == 429

    def test_the_refusal_says_when_to_come_back(self, client: TestClient, clock: Clock) -> None:
        burst(client, "/api/v1/genres", ANONYMOUS.burst)

        refused = client.get("/api/v1/genres")

        assert refused.status_code == 429
        assert refused.headers["Retry-After"] == "1"
        assert refused.headers["content-type"].startswith("application/problem+json")
        assert "Try again" in refused.json()["detail"]

    def test_the_bucket_refills(self, client: TestClient, clock: Clock) -> None:
        burst(client, "/api/v1/genres", ANONYMOUS.burst)
        assert client.get("/api/v1/genres").status_code == 429

        clock.now += 1

        assert burst(client, "/api/v1/genres", int(ANONYMOUS.per_second)) == [200] * 5

    def test_static_files_are_never_counted(self, client: TestClient, clock: Clock) -> None:
        burst(client, "/", 50)

        assert client.get("/api/v1/genres").status_code == 200

    def test_a_member_has_more_room_than_a_stranger(
        self, client: TestClient, app: FastAPI, sign_in: SignIn, clock: Clock
    ) -> None:
        sign_in()
        app.state.rate_buckets.forget()

        codes = burst(client, "/api/v1/genres", MEMBER.burst + 1)

        assert codes.count(200) == MEMBER.burst
        assert codes[-1] == 429

    def test_writes_have_their_own_smaller_bucket(
        self, client: TestClient, app: FastAPI, sign_in: SignIn, clock: Clock
    ) -> None:
        headers = {CSRF_HEADER: sign_in()}
        app.state.rate_buckets.forget()

        codes = [
            client.patch("/api/v1/me", json={"display_name": f"n{i}"}, headers=headers).status_code
            for i in range(WRITES.burst + 1)
        ]

        assert codes[:-1] == [200] * WRITES.burst
        assert codes[-1] == 429
        # Reading is not held up by having written.
        assert client.get("/api/v1/genres").status_code == 200

    def test_signing_in_is_slow_and_per_address(self, client: TestClient, clock: Clock) -> None:
        codes = [
            client.get("/api/v1/auth/login/google", follow_redirects=False).status_code
            for _ in range(SIGN_IN.burst + 1)
        ]

        assert 429 not in codes[:-1]
        assert codes[-1] == 429

    def test_made_up_tokens_do_not_buy_fresh_buckets(
        self, client: TestClient, clock: Clock
    ) -> None:
        """Each is its own caller - but they all share the address ceiling."""
        codes = [
            client.get(
                "/api/v1/genres", headers={"Authorization": f"Bearer made-up-{i}"}
            ).status_code
            for i in range(ADDRESS.burst + 1)
        ]

        assert codes[-1] == 429


class TestTheRealClient:
    def test_uvicorn_is_told_which_proxies_to_believe(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from eifo_api import __main__ as command
        from eifo_core import settings as settings_module

        asked: dict[str, object] = {}
        monkeypatch.setattr(command.uvicorn, "run", lambda app, **kw: asked.update(kw))
        monkeypatch.setenv("EIFO_TRUSTED_PROXIES", "127.0.0.1,172.16.0.0/12")
        settings_module.get_settings.cache_clear()
        try:
            command.main([])
        finally:
            settings_module.get_settings.cache_clear()

        assert asked["proxy_headers"] is True
        assert asked["forwarded_allow_ips"] == "127.0.0.1,172.16.0.0/12"

    def test_a_forwarded_address_only_from_a_trusted_proxy(self) -> None:
        """uvicorn's own middleware, as configured: the rule the limiter relies on."""
        import asyncio

        from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

        seen: list[str] = []

        async def app(scope: dict[str, object], receive: object, send: object) -> None:
            client = scope["client"]
            assert isinstance(client, tuple)
            seen.append(client[0])

        wrapped = ProxyHeadersMiddleware(app, trusted_hosts="127.0.0.1,172.16.0.0/12")

        def ask(peer: str) -> None:
            scope = {
                "type": "http",
                "client": (peer, 1234),
                "headers": [(b"x-forwarded-for", b"198.51.100.9")],
                "scheme": "http",
            }
            asyncio.run(wrapped(scope, None, None))  # type: ignore[arg-type]

        ask("172.18.0.5")  # the Caddy container
        ask("203.0.113.66")  # somebody else claiming to forward

        assert seen == ["198.51.100.9", "203.0.113.66"]
