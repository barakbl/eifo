"""Run rows, including for the runs that never reach an ending.

The failure these exist to catch is an absence: before this, a fetcher killed
mid-phase left a database indistinguishable from one where nothing was
scheduled at all.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from eifo_core.enums import FetchPhase, FetchStatus
from eifo_core.models import FetchRun
from eifo_core.settings import Settings
from eifo_core.types import utcnow
from eifo_fetcher.http import HttpClient
from eifo_fetcher.runner import IMDB_RUN_KEY, enrich_all, fetch_images
from eifo_fetcher.runs import close_abandoned_runs, close_run, open_run


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
    def test_a_run_still_open_from_a_dead_process_is_marked_crashed(self, session: Session) -> None:
        """The lock means nothing else is running, so an open row is abandoned."""
        open_run(session, phase=FetchPhase.SYNC, source_key="freetv")

        assert close_abandoned_runs(session) == 1

        run = _runs(session)[0]
        assert run.status is FetchStatus.CRASHED
        assert run.finished_at is not None

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
        session_factory: sessionmaker[Session],
        settings: Settings,
        http: HttpClient,
        session: Session,
    ) -> None:
        fetch_images(session_factory, settings, http=http)

        run = session.scalars(select(FetchRun).where(FetchRun.phase == FetchPhase.IMAGES)).one()
        assert run.status is FetchStatus.OK
        assert run.finished_at is not None
        assert run.stats == {"downloaded": 0, "skipped": 0, "failed": 0}

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
