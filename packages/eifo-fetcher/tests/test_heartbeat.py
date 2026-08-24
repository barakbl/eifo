"""Pinging a watchdog, and never letting it get in the way."""

from __future__ import annotations

import httpx
import pytest
import respx
from pydantic import SecretStr

from eifo_core.settings import Settings
from eifo_fetcher.heartbeat import ping

URL = "https://hc.example/ping/2f1b8c7e"


@pytest.fixture
def watched(settings: Settings) -> Settings:
    return settings.model_copy(update={"healthcheck_url": SecretStr(URL)})


class TestPing:
    @respx.mock
    def test_success_pings_the_url_itself(self, watched: Settings) -> None:
        route = respx.get(URL).mock(return_value=httpx.Response(200))

        ping(watched)

        assert route.called

    @respx.mock
    @pytest.mark.parametrize("event", ["start", "fail"])
    def test_an_event_is_a_path_below_it(self, watched: Settings, event: str) -> None:
        route = respx.get(f"{URL}/{event}").mock(return_value=httpx.Response(200))

        ping(watched, event)

        assert route.called

    @respx.mock
    def test_a_trailing_slash_does_not_double_up(self, settings: Settings) -> None:
        watched = settings.model_copy(update={"healthcheck_url": SecretStr(f"{URL}/")})
        route = respx.get(f"{URL}/start").mock(return_value=httpx.Response(200))

        ping(watched, "start")

        assert route.called

    @respx.mock
    def test_nothing_is_pinged_when_no_watchdog_is_configured(self, settings: Settings) -> None:
        route = respx.get(URL)

        ping(settings)

        assert not route.called


class TestPingNeverGetsInTheWay:
    """Monitoring exists to report on the run, not to be able to end it."""

    @respx.mock
    def test_an_unreachable_watchdog_is_not_an_error(self, watched: Settings) -> None:
        respx.get(URL).mock(side_effect=httpx.ConnectError("no route to host"))

        ping(watched)

    @respx.mock
    def test_a_watchdog_returning_an_error_is_not_an_error(self, watched: Settings) -> None:
        respx.get(URL).mock(return_value=httpx.Response(500))

        ping(watched)

    @respx.mock
    def test_the_token_never_reaches_the_log(
        self, watched: Settings, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The URL is the credential: anything holding it can silence the alarm."""
        respx.get(URL).mock(side_effect=httpx.ConnectError("no route to host"))

        with caplog.at_level("DEBUG", logger="eifo.fetch.heartbeat"):
            ping(watched)

        assert "2f1b8c7e" not in caplog.text
