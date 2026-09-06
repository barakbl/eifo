"""The note a fetcher leaves when it could not tell the server anything.

Moving the run history behind the API widened a blind spot. The record of a run
lives in the catalog, the catalog is reached over HTTP, so every failure that
consists of *not reaching the catalog* is exactly the failure that cannot
record itself - and the operator would be left looking at nothing, which is
what a night with no fetcher scheduled also looks like.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import httpx
import pytest

from eifo_core.enums import FetchPhase, FetchStatus
from eifo_core.settings import Settings
from eifo_core.types import utcnow
from eifo_fetcher import attempts
from eifo_fetcher.http import HttpClient
from eifo_fetcher.ingest import IngestClient, IngestError
from eifo_fetcher.runner import fetch_images, phase_client


class TestTheNoteItself:
    def test_a_failed_attempt_is_written_down(self, settings: Settings) -> None:
        attempts.failed(settings, FetchPhase.IMAGES, utcnow(), "connection refused")

        pending = attempts.pending(settings)
        assert pending is not None
        assert pending.phase is FetchPhase.IMAGES

    def test_clearing_it_leaves_nothing(self, settings: Settings) -> None:
        attempts.failed(settings, FetchPhase.IMAGES, utcnow(), "connection refused")

        attempts.clear(settings)

        assert attempts.pending(settings) is None

    def test_clearing_one_that_was_never_written_is_fine(self, settings: Settings) -> None:
        # The common case by far: nearly every run reports itself.
        attempts.clear(settings)

        assert attempts.pending(settings) is None

    def test_a_failure_keeps_the_reason(self, settings: Settings) -> None:
        attempts.failed(settings, FetchPhase.IMAGES, utcnow(), "connection refused")

        pending = attempts.pending(settings)
        assert pending is not None
        assert "connection refused" in pending.reason

    def test_a_note_that_cannot_be_read_is_not_a_reason_to_stop(self, settings: Settings) -> None:
        """This is bookkeeping about failures; it must not become one.

        Refusing to fetch artwork because the note about the last failure is
        corrupt would be the mechanism eating the thing it was built to serve.
        """
        attempts.attempt_path(settings).parent.mkdir(parents=True, exist_ok=True)
        attempts.attempt_path(settings).write_text("{ not json", encoding="utf-8")

        assert attempts.pending(settings) is None
        assert not attempts.attempt_path(settings).exists(), "the bad note is cleared away"

    def test_only_the_latest_attempt_is_kept(self, settings: Settings) -> None:
        """Five nights of one broken token are five copies of one fact."""
        attempts.failed(settings, FetchPhase.IMAGES, utcnow(), "first")
        attempts.failed(settings, FetchPhase.IMAGES, utcnow(), "second")

        pending = attempts.pending(settings)
        assert pending is not None and pending.reason == "second"


class TestTheContextManager:
    def test_it_keeps_the_note_when_the_api_cannot_be_reached(self, settings: Settings) -> None:
        with pytest.raises(IngestError), attempts.attempted(settings, FetchPhase.IMAGES):
            raise IngestError("the server is not there")

        pending = attempts.pending(settings)
        assert pending is not None
        assert "not there" in pending.reason

    def test_it_covers_the_client_being_built_at_all(self, tmp_path: Any) -> None:
        """No token configured is the commonest failure and the earliest.

        It happens before there is a client to fail with, so anything that
        wrapped only the run would leave the one failure most people hit as the
        one that left no trace.
        """
        settings = Settings(
            _env_file=None,
            db_url=f"sqlite:///{tmp_path / 'x.db'}",
            images_dir=tmp_path / "images",
        )

        with pytest.raises(IngestError), attempts.attempted(settings, FetchPhase.IMAGES):
            IngestClient.from_settings(settings)

        assert attempts.pending(settings) is not None

    def test_something_that_is_not_an_ingest_failure_passes_through(
        self, settings: Settings
    ) -> None:
        # A bug in the fetcher is not a "could not reach the API", and filing
        # it as one would send whoever reads the row somewhere useless. The
        # run row was opened and gets closed FAILED by the phase itself.
        with pytest.raises(ZeroDivisionError), attempts.attempted(settings, FetchPhase.IMAGES):
            raise ZeroDivisionError

        assert attempts.pending(settings) is None

    def test_nothing_is_written_on_the_way_in(self, settings: Settings) -> None:
        """The bug the first version of this had.

        A note written at the start is overwritten by the next run before that
        run has read it - so the next run reports its own start as the previous
        run\'s failure: right number of rows, wrong timestamp, no reason.
        """
        earlier = utcnow() - dt.timedelta(days=1)
        attempts.failed(settings, FetchPhase.IMAGES, earlier, "no token configured")

        with attempts.attempted(settings, FetchPhase.IMAGES):
            pass

        kept = attempts.pending(settings)
        assert kept is not None
        assert kept.reason == "no token configured", "the older note was clobbered"
        assert kept.started_at == earlier


class TestReportingItLate:
    """Posted by whatever opens a phase, rather than by the artwork phase.

    It used to live inside ``fetch_images``, which was the only phase that had
    an API client to post it with. All three do now, and a note left by a failed
    sync would have waited for the next artwork run to carry it - which on an
    install that never fetches artwork is for ever.
    """

    def test_the_next_run_posts_the_attempt_that_could_not_report_itself(
        self, settings: Settings, ingest_api: Any
    ) -> None:
        when = utcnow() - dt.timedelta(hours=8)
        attempts.failed(settings, FetchPhase.IMAGES, when, "connection refused")

        with ingest_api.client() as client, phase_client(settings, FetchPhase.SYNC, api=client):
            pass

        assert len(ingest_api.opened) == 1
        assert ingest_api.opened[0]["started_at"].startswith(when.isoformat()[:16])
        assert ingest_api.closed[0]["status"] == FetchStatus.CRASHED.value
        assert "connection refused" in ingest_api.closed[0]["stats"]["errors"][0]

    def test_any_phase_carries_it_not_only_the_one_that_left_it(
        self, settings: Settings, ingest_api: Any
    ) -> None:
        """A note from a failed sync must not wait on an artwork run to post it."""
        attempts.failed(settings, FetchPhase.SYNC, utcnow(), "connection refused")

        with ingest_api.client() as client, phase_client(settings, FetchPhase.IMAGES, api=client):
            pass

        assert ingest_api.opened[0]["phase"] == FetchPhase.SYNC.value

    def test_and_then_forgets_it(self, settings: Settings, ingest_api: Any) -> None:
        attempts.failed(settings, FetchPhase.IMAGES, utcnow(), "connection refused")

        with ingest_api.client() as client, phase_client(settings, FetchPhase.IMAGES, api=client):
            pass

        assert attempts.pending(settings) is None, "reported once, not every run after"

    def test_a_clean_run_reports_nothing_extra(
        self, settings: Settings, http: HttpClient, ingest_api: Any
    ) -> None:
        with ingest_api.client() as api:
            fetch_images(settings, http=http, api=api)

        assert len(ingest_api.opened) == 1

    def test_a_note_kept_because_the_server_is_still_away_is_not_lost(
        self, settings: Settings, http: HttpClient
    ) -> None:
        """It keeps until something can carry it."""
        attempts.failed(settings, FetchPhase.IMAGES, utcnow(), "connection refused")

        def refuse(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("still refused")

        api = IngestClient(
            "https://eifo.test",
            "eifo_pat_x",
            http=httpx.Client(transport=httpx.MockTransport(refuse)),
        )
        with pytest.raises(IngestError), api, phase_client(settings, FetchPhase.IMAGES, api=api):
            fetch_images(settings, http=http, api=api)

        assert attempts.pending(settings) is not None

    def test_a_run_that_cannot_be_closed_leaves_the_row_open_and_no_note(
        self, settings: Settings, http: HttpClient
    ) -> None:
        """The open row is already the record; a note would make a second one.

        This run reached the server, so its row exists and says RUNNING. The
        server closes it once it is old enough to be certainly dead. Writing a
        note here as well would have the next run open a third row saying the
        same thing.
        """
        state = {"closes": 0}

        def handle(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/posters/pending"):
                return httpx.Response(200, json=[])
            if path.endswith("/ingest/runs"):
                return httpx.Response(201, json={"id": 1})
            state["closes"] += 1
            raise httpx.ConnectError("gone away mid-run")

        api = IngestClient(
            "https://eifo.test",
            "eifo_pat_x",
            http=httpx.Client(transport=httpx.MockTransport(handle)),
        )
        with api:
            fetch_images(settings, http=http, api=api)

        assert state["closes"] == 1, "it tried to close the run"
        assert attempts.pending(settings) is None


def test_the_note_sits_beside_the_lock_not_in_the_catalog(settings: Settings) -> None:
    """It belongs to this machine's fetcher, like the lock does."""
    from eifo_fetcher.lock import lock_path

    assert attempts.attempt_path(settings).parent == lock_path(settings).parent


def test_what_is_written_is_readable_json(settings: Settings) -> None:
    # Somebody debugging a fetcher at 3am should be able to cat this.
    attempts.failed(settings, FetchPhase.IMAGES, utcnow(), "connection refused")

    payload = json.loads(attempts.attempt_path(settings).read_text(encoding="utf-8"))

    assert payload["phase"] == "images"
    assert payload["reason"] == "connection refused"
