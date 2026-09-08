"""Saying what was asked of the catalog, and what it said back.

The fetcher does nothing else now. Every phase is a conversation with the API,
so a phase that has gone quiet is either waiting on somebody else's website or
waiting on the catalog - and without a line per call there is no way to tell
those apart from outside the process.

At INFO, and every call, because the two places this most needs to be readable
have nobody watching a console: a nightly run at three in the morning, and a
process the menu-bar companion started.
"""

from __future__ import annotations

import logging

import pytest
from live import LiveApi

from eifo_core.enums import FetchPhase
from eifo_core.types import utcnow
from eifo_fetcher.ingest import IngestClient, IngestError


@pytest.fixture
def said(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    """What the client logged, at INFO, which is the level under test.

    The fixture itself rather than ``caplog.messages``: that property builds a
    new list each time it is read, so a list captured here would be the empty
    one from before the test ran.
    """
    caplog.set_level(logging.INFO, logger="eifo.fetch.ingest")
    return caplog


class TestEveryCallIsReported:
    def test_the_call_and_the_status_are_one_line(
        self, api: IngestClient, said: pytest.LogCaptureFixture
    ) -> None:
        api.seret_status()

        [line] = [entry for entry in said.messages if "seret/status" in entry]
        assert line.startswith("GET ")
        assert "-> 200" in line

    def test_a_write_says_so_too(self, api: IngestClient, said: pytest.LogCaptureFixture) -> None:
        api.open_run(FetchPhase.SYNC, started_at=utcnow())

        assert any("POST /api/v1/ingest/runs -> 201" in entry for entry in said.messages)

    def test_the_parameters_are_there_to_tell_two_calls_apart(
        self, api: IngestClient, said: pytest.LogCaptureFixture
    ) -> None:
        """Which page of the index, how far through the queue - a line without
        them says a request happened and nothing about which."""
        api.titles_due(limit=5)

        assert any("limit=5" in entry for entry in said.messages)

    def test_how_long_it_took_is_part_of_the_answer(
        self, api: IngestClient, said: pytest.LogCaptureFixture
    ) -> None:
        """A chunk taking four seconds and one taking four minutes are different
        problems on different machines."""
        api.seret_status()

        assert any(entry.endswith(("ms", "s")) for entry in said.messages)

    def test_a_refusal_is_logged_before_it_is_raised(
        self, live_api: LiveApi, said: pytest.LogCaptureFixture
    ) -> None:
        """The raise is caught in several places and turned into a tally or a
        warning, so the line is the only record of which call was refused."""
        with pytest.raises(IngestError):
            live_api.api.offer(9999, [])

        assert any("-> 404" in entry for entry in said.messages)

    def test_a_server_that_cannot_be_reached_says_which_call_it_was_on(
        self, said: pytest.LogCaptureFixture
    ) -> None:
        import httpx

        def refuse(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("nothing listening")

        api = IngestClient(
            "https://eifo.test",
            "eifo_pat_x",
            http=httpx.Client(transport=httpx.MockTransport(refuse)),
        )
        with api, pytest.raises(IngestError):
            api.seret_status()

        assert any(
            "could not be reached" in entry and "seret/status" in entry for entry in said.messages
        )


class TestTheTokenIsNeverPrinted:
    def test_not_in_the_line_for_a_call(
        self, api: IngestClient, said: pytest.LogCaptureFixture
    ) -> None:
        """It is a header, and a log file is a thing people paste into issues."""
        api.seret_status()

        assert not any("eifo_pat" in entry for entry in said.messages)

    def test_nor_in_the_line_for_a_refusal(
        self, live_api: LiveApi, said: pytest.LogCaptureFixture
    ) -> None:
        with pytest.raises(IngestError):
            live_api.api.offer(9999, [])

        assert not any("eifo_pat" in entry for entry in said.messages)


class TestItReadsAsARun:
    def test_a_phase_leaves_the_whole_conversation_behind(
        self, api: IngestClient, said: pytest.LogCaptureFixture
    ) -> None:
        """Which is the point: what was asked, in order, with what came back."""
        api.seret_status()
        api.titles_due(limit=1)

        calls = [entry for entry in said.messages if "->" in entry]
        assert len(calls) == 2
        assert all(entry.split()[0] in {"GET", "POST", "PATCH"} for entry in calls)
