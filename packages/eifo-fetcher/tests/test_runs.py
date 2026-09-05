"""Run rows, including for the runs that never reach an ending.

The failure these exist to catch is an absence: before this, a fetcher killed
mid-phase left a database indistinguishable from one where nothing was
scheduled at all.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from eifo_core.enums import FetchPhase, FetchStatus
from eifo_core.ingest import ABANDONED_AFTER
from eifo_core.models import FetchRun
from eifo_core.settings import Settings
from eifo_core.types import utcnow
from eifo_fetcher.http import HttpClient
from eifo_fetcher.runner import IMDB_RUN_KEY, enrich_all, fetch_images
from eifo_fetcher.runs import (
    FETCHER_LOGGER,
    RunLogCapture,
    capture_log,
    close_abandoned_runs,
    close_run,
    open_run,
)


def _runs(session: Session) -> list[FetchRun]:
    return list(session.scalars(select(FetchRun).order_by(FetchRun.id)).all())


class TestOpeningARun:
    def test_a_started_phase_is_visible_before_it_finishes(self, session: Session) -> None:
        open_run(session, phase=FetchPhase.SYNC, source_key="mako")

        run = _runs(session)[0]
        assert run.status is FetchStatus.RUNNING
        assert run.finished_at is None

    def test_it_is_committed_rather_than_left_pending(self, session: Session) -> None:
        """A row nobody else can see would be no better than no row at all."""
        open_run(session, phase=FetchPhase.SYNC, source_key="mako")

        session.rollback()

        assert len(_runs(session)) == 1

    def test_the_start_time_can_be_the_phase_s_own(self, session: Session) -> None:
        started = utcnow() - dt.timedelta(minutes=5)

        run = open_run(session, phase=FetchPhase.ENRICH, started_at=started)

        assert run.started_at == started


class TestClosingARun:
    def test_an_outcome_replaces_the_running_state(self, session: Session) -> None:
        run = open_run(session, phase=FetchPhase.IMAGES)

        close_run(session, run, status=FetchStatus.OK, stats={"downloaded": 3})

        stored = _runs(session)[0]
        assert stored.status is FetchStatus.OK
        assert stored.finished_at is not None
        assert stored.stats == {"downloaded": 3}

    def test_closing_updates_rather_than_adds(self, session: Session) -> None:
        run = open_run(session, phase=FetchPhase.SYNC, source_key="kan")

        close_run(session, run, status=FetchStatus.FAILED, stats={})

        assert len(_runs(session)) == 1


class TestRunsLeftBehind:
    def test_a_sync_still_open_from_a_dead_process_is_marked_at_once(
        self, session: Session
    ) -> None:
        """The lock still proves this one: a sync can only run on this machine."""
        open_run(session, phase=FetchPhase.SYNC, source_key="freetv")

        assert close_abandoned_runs(session) == 1

        run = _runs(session)[0]
        assert run.status is FetchStatus.CRASHED
        assert run.finished_at is not None

    def test_a_recent_artwork_run_is_left_alone(self, session: Session) -> None:
        """The lock proves nothing about this one.

        The artwork phase writes through the API, so a fetcher on somebody
        else's machine can be mid-run against this catalog while this process
        holds a lock that means nothing to it. Sweeping on sight would mark
        that live run crashed, and running any command on the server would be
        enough to do it.
        """
        open_run(session, phase=FetchPhase.IMAGES)

        assert close_abandoned_runs(session) == 0
        assert _runs(session)[0].status is FetchStatus.RUNNING

    def test_an_old_artwork_run_is_swept_by_the_clock(self, session: Session) -> None:
        """Nothing could still be working on it, whoever started it."""
        run = open_run(session, phase=FetchPhase.IMAGES)
        run.started_at = utcnow() - ABANDONED_AFTER - dt.timedelta(minutes=1)
        session.commit()

        assert close_abandoned_runs(session) == 1
        assert _runs(session)[0].status is FetchStatus.CRASHED

    def test_the_clock_is_not_imposed_on_phases_that_do_not_need_it(self, session: Session) -> None:
        """A dead source must not sit there reading "running" until tomorrow.

        That would be a worse answer than the one this gave before the artwork
        phase moved, and it would be worse for the install that never had a
        remote fetcher at all.
        """
        open_run(session, phase=FetchPhase.ENRICH)

        assert close_abandoned_runs(session) == 1

    def test_it_says_why_it_was_marked(self, session: Session) -> None:
        open_run(session, phase=FetchPhase.ENRICH)

        close_abandoned_runs(session)

        assert _runs(session)[0].stats["errors"] == ["run ended without recording an outcome"]

    def test_finished_runs_are_left_alone(self, session: Session) -> None:
        run = open_run(session, phase=FetchPhase.SYNC, source_key="mako")
        close_run(session, run, status=FetchStatus.OK, stats={"items_seen": 12})

        assert close_abandoned_runs(session) == 0
        assert _runs(session)[0].status is FetchStatus.OK

    def test_a_clean_database_has_nothing_to_sweep(self, session: Session) -> None:
        """Every start calls this; the normal answer is nothing."""
        assert close_abandoned_runs(session) == 0


class TestPhasesThatUsedToRecordNothing:
    """FetchPhase.IMAGES existed and had never once been written to the table."""

    def test_the_artwork_phase_records_a_run(
        self,
        settings: Settings,
        http: HttpClient,
        ingest_api: Any,
    ) -> None:
        """Over the API now, because this phase no longer opens the database.

        The row is still opened before the work and closed after it, which is
        the part that matters: a fetcher that never comes back cannot write its
        own obituary, wherever the row lives.
        """
        with ingest_api.client() as api:
            fetch_images(settings, http=http, api=api)

        assert ingest_api.opened[0]["phase"] == FetchPhase.IMAGES.value
        assert ingest_api.closed[0]["status"] == FetchStatus.OK.value
        assert ingest_api.closed[0]["stats"] == {"downloaded": 0, "skipped": 0, "failed": 0}

    def test_the_imdb_bulk_pass_records_its_own_run(
        self,
        session_factory: sessionmaker[Session],
        settings: Settings,
        http: HttpClient,
        session: Session,
    ) -> None:
        """It ran after the enrich row was written, so its tally was never persisted."""
        enrich_all(session_factory, settings, http=http)

        run = session.scalars(select(FetchRun).where(FetchRun.source_key == IMDB_RUN_KEY)).one()
        assert run.phase is FetchPhase.ENRICH
        assert run.status is FetchStatus.OK
        assert "rows_read" in run.stats


class TestWhatARunSaid:
    """Until this existed, the only record of a failed night was on a stderr
    nobody was watching - so "why did mako return nothing" was answerable only
    by running it again and watching."""

    def test_it_keeps_what_the_fetcher_logged(self, fetcher_logs_at_info: None) -> None:
        with capture_log() as captured:
            logging.getLogger("eifo.fetch.test").info("mako: 0 items")
            logging.getLogger("eifo.fetch.test").error("parser found no cards")

        text = captured.text()
        assert text is not None
        assert "mako: 0 items" in text
        assert "parser found no cards" in text

    def test_a_quietened_fetcher_records_as_little_as_it_says(self) -> None:
        """The row is what the run said, not what it would have said at INFO."""
        logging.getLogger(FETCHER_LOGGER).setLevel(logging.ERROR)
        try:
            with capture_log() as captured:
                logging.getLogger("eifo.fetch.test").info("routine")
                logging.getLogger("eifo.fetch.test").error("the part that matters")
        finally:
            logging.getLogger(FETCHER_LOGGER).setLevel(logging.NOTSET)

        text = captured.text()
        assert text is not None
        assert "the part that matters" in text
        assert "routine" not in text

    def test_a_run_that_said_nothing_stores_nothing(self) -> None:
        with capture_log() as captured:
            pass

        assert captured.text() is None

    def test_somebody_elses_library_is_not_this_run(self, fetcher_logs_at_info: None) -> None:
        """Attached to the eifo logger, so httpx and sqlalchemy stay out of it."""
        with capture_log() as captured:
            logging.getLogger("httpx").warning("connection reset")
            logging.getLogger("eifo.fetch.test").info("ours")

        text = captured.text()
        assert text is not None and "ours" in text
        assert "connection reset" not in text

    def test_the_handler_does_not_outlive_the_run(self) -> None:
        """One left attached would go on collecting into a row already written."""
        target = logging.getLogger(FETCHER_LOGGER)
        before = len(target.handlers)

        with capture_log():
            assert len(target.handlers) == before + 1

        assert len(target.handlers) == before

    def test_it_is_removed_even_when_the_run_explodes(self) -> None:
        target = logging.getLogger(FETCHER_LOGGER)
        before = len(target.handlers)

        with pytest.raises(RuntimeError), capture_log():
            raise RuntimeError("sync exploded")

        assert len(target.handlers) == before

    def test_a_long_run_keeps_its_tail_and_says_it_did(self) -> None:
        """The end of a run is the part that explains it; the start scrolls away."""
        handler = RunLogCapture(max_bytes=400)
        logger = logging.getLogger("eifo.fetch.noisy")
        logger.addHandler(handler)
        try:
            for index in range(200):
                logger.warning("line %03d", index)
        finally:
            logger.removeHandler(handler)

        text = handler.text()
        assert text is not None
        assert handler.truncated is True
        assert "line 199" in text
        assert "line 000" not in text
        assert "earlier lines dropped" in text


class TestClosingARunWithItsLog:
    def test_the_log_lands_on_the_row(self, session: Session) -> None:
        run = open_run(session, phase=FetchPhase.SYNC, source_key="mako")

        close_run(session, run, status=FetchStatus.OK, stats={}, log="mako: 0 items")

        assert session.get(FetchRun, run.id).log == "mako: 0 items"

    def test_a_run_with_nothing_to_say_leaves_the_column_alone(self, session: Session) -> None:
        run = open_run(session, phase=FetchPhase.SYNC, source_key="mako")

        close_run(session, run, status=FetchStatus.OK, stats={}, log=None)

        assert session.get(FetchRun, run.id).log is None
