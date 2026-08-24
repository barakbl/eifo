"""The eifo-fetch CLI: commands, output and exit codes."""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import Session, sessionmaker

from eifo_core.db import create_engine_from_settings, make_session_factory
from eifo_core.enums import (
    FetchPhase,
    FetchStatus,
    MatchDecision,
    OfferType,
    SourceKind,
    TitleKind,
)
from eifo_core.fts import TRIGGERS, missing_triggers
from eifo_core.migrate import alembic_config, upgrade
from eifo_core.models import Availability, FetchRun, MatchReview, Source, Title
from eifo_core.settings import Settings, get_settings
from eifo_fetcher.cli import EXIT_FATAL, EXIT_OK, EXIT_PARTIAL, build_parser, main
from eifo_fetcher.lock import single_flight


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point the CLI at a throwaway database, ignoring ambient configuration."""
    path = tmp_path / "cli.db"
    monkeypatch.setenv("EIFO_CONFIG_FILE", str(tmp_path / "absent.toml"))
    monkeypatch.setenv("EIFO_DB_URL", f"sqlite:///{path}")
    monkeypatch.setenv("EIFO_IMAGES_DIR", str(tmp_path / "images"))
    get_settings.cache_clear()
    yield path
    get_settings.cache_clear()


@pytest.fixture
def migrated(db_path: Path) -> Path:
    upgrade(f"sqlite:///{db_path}")
    return db_path


@pytest.fixture
def factory(migrated: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_engine_from_settings(Settings(_env_file=None, db_url=f"sqlite:///{migrated}"))
    yield make_session_factory(engine)
    engine.dispose()


def seed_source(session: Session, key: str = "mako") -> Source:
    source = Source(
        key=key,
        name="Mako VOD (Keshet 12)",
        kind=SourceKind.FREE,
        website_url="https://www.mako.co.il/mako-vod-index",
    )
    session.add(source)
    session.flush()
    return source


class TestDatabaseCommands:
    def test_upgrade_creates_the_schema(self, db_path: Path) -> None:
        assert main(["db", "upgrade"]) == EXIT_OK

        engine = create_engine(f"sqlite:///{db_path}")
        try:
            assert {"titles", "sources", "availability"} <= set(inspect(engine).get_table_names())
        finally:
            engine.dispose()

    def test_upgrade_is_idempotent(self, db_path: Path) -> None:
        assert main(["db", "upgrade"]) == EXIT_OK
        assert main(["db", "upgrade"]) == EXIT_OK

    def test_current_reports_the_head_revision(
        self, migrated: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Compared against the actual head so a new migration cannot break this."""
        head = ScriptDirectory.from_config(alembic_config("sqlite://")).get_current_head()

        assert main(["db", "current"]) == EXIT_OK

        assert capsys.readouterr().out.strip() == head


class TestCommandsRewireSearchBeforeWriting:
    def test_a_command_restores_triggers_a_rebuild_removed(self, migrated: Path) -> None:
        """This is the process that writes titles: it must not write into a dead index."""
        engine = create_engine(f"sqlite:///{migrated}")
        try:
            with engine.begin() as connection:
                for name in TRIGGERS:
                    connection.execute(text(f"DROP TRIGGER {name}"))

            assert main(["images"]) == EXIT_OK

            with engine.connect() as connection:
                assert missing_triggers(connection) == ()
        finally:
            engine.dispose()


class TestOnlyOneFetcherAtATime:
    def _settings(self, migrated: Path) -> Settings:
        return Settings(_env_file=None, db_url=f"sqlite:///{migrated}")

    @pytest.mark.parametrize("command", [["sync"], ["enrich"], ["images"], ["all"]])
    def test_a_writing_command_stands_down_while_another_fetcher_runs(
        self, migrated: Path, caplog: pytest.LogCaptureFixture, command: list[str]
    ) -> None:
        """Cron firing over a long-running daemon is ordinary, not an incident."""
        with single_flight(self._settings(migrated)), caplog.at_level(logging.WARNING):
            assert main(command) == EXIT_OK

        assert "another fetcher is already running" in caplog.text

    def test_reading_the_catalog_is_never_blocked(self, migrated: Path) -> None:
        """The lock guards writes; asking what is in there is always allowed."""
        with single_flight(self._settings(migrated)):
            assert main(["sources", "list"]) == EXIT_OK
            assert main(["review", "list"]) == EXIT_OK


class TestTheNightlyCommand:
    def test_all_runs_the_same_chain_the_daemon_schedules(
        self, migrated: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """So a catalog kept current by cron and one kept by the daemon are kept identically."""
        runs: list[object] = []
        monkeypatch.setattr(
            "eifo_fetcher.daemon.run_nightly", lambda settings: bool(runs.append(settings)) or True
        )

        assert main(["all"]) == EXIT_OK
        assert len(runs) == 1

    def test_a_failed_phase_is_reported_to_cron(
        self, migrated: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The documented exit codes are the only thing a cron job can be judged on."""
        monkeypatch.setattr("eifo_fetcher.daemon.run_nightly", lambda _settings: False)

        assert main(["all"]) == EXIT_PARTIAL
        assert main(["daemon", "--once"]) == EXIT_PARTIAL


class TestCommandsRequireAMigratedDatabase:
    @pytest.mark.parametrize("command", [["sync"], ["images"], ["sources", "list"]])
    def test_unmigrated_database_exits_fatally(self, db_path: Path, command: list[str]) -> None:
        assert main(command) == EXIT_FATAL


class TestSourcesList:
    def test_lists_declared_sources(
        self, migrated: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["sources", "list"]) == EXIT_OK

        out = capsys.readouterr().out
        assert "mako" in out
        assert "netflix_il" in out
        assert "never synced" in out

    def test_shows_stored_state_and_counts(
        self, factory: sessionmaker[Session], capsys: pytest.CaptureFixture[str]
    ) -> None:
        with factory() as session:
            source = seed_source(session)
            title = Title(type=TitleKind.SERIES, name_he="פאודה", year=2015)
            session.add(title)
            session.flush()
            session.add(
                Availability(title_id=title.id, source_id=source.id, offer_type=OfferType.FREE)
            )
            session.add(
                FetchRun(
                    source_key="mako",
                    phase=FetchPhase.SYNC,
                    started_at=dt.datetime(2026, 8, 1, 3, 0, tzinfo=dt.UTC),
                    finished_at=dt.datetime(2026, 8, 1, 3, 5, tzinfo=dt.UTC),
                    status=FetchStatus.OK,
                    stats={},
                )
            )
            session.commit()

        main(["sources", "list"])

        out = capsys.readouterr().out
        assert "active" in out
        assert "2026-08-01 03:05" in out

    def test_flags_a_source_no_longer_declared(
        self, factory: sessionmaker[Session], capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Data for a dropped source stays visible and is labelled retired."""
        with factory() as session:
            seed_source(session, key="a_removed_service")
            session.commit()

        main(["sources", "list"])

        out = capsys.readouterr().out
        assert "a_removed_service" in out
        assert "retired" in out


class TestReview:
    def _add_review(self, session: Session) -> MatchReview:
        review = MatchReview(
            source_key="mako",
            raw_payload={"name": "סרוגים 2", "year": 2008},
            candidates={
                "closest": {
                    "title_id": 1,
                    "name_he": "סרוגים",
                    "name_en": "Srugim",
                    "year": 2008,
                    "similarity": 85.7,
                }
            },
        )
        session.add(review)
        session.commit()
        return review

    def test_list_reports_nothing_when_empty(
        self, migrated: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["review", "list"]) == EXIT_OK

        assert "nothing awaiting review" in capsys.readouterr().out

    def test_list_shows_the_closest_match(
        self, factory: sessionmaker[Session], capsys: pytest.CaptureFixture[str]
    ) -> None:
        with factory() as session:
            self._add_review(session)

        main(["review", "list"])

        out = capsys.readouterr().out
        assert "סרוגים 2" in out
        assert "85.7%" in out

    def test_resolve_attaches_a_title(self, factory: sessionmaker[Session]) -> None:
        with factory() as session:
            title = Title(type=TitleKind.SERIES, name_he="סרוגים", year=2008)
            session.add(title)
            session.flush()
            review = self._add_review(session)
            review_id, title_id = review.id, title.id

        assert main(["review", "resolve", str(review_id), "--title-id", str(title_id)]) == EXIT_OK

        with factory() as session:
            resolved = session.get(MatchReview, review_id)
            assert resolved is not None
            assert resolved.resolved_title_id == title_id
            assert resolved.resolved_at is not None

    def test_skip_gives_the_item_a_title_of_its_own(self, factory: sessionmaker[Session]) -> None:
        """Not the suggested match, but a real title - and one that exists now.

        It used to exist only after the source next synced, which is why every
        ruling anybody had made was still waiting.
        """
        with factory() as session:
            review_id = self._add_review(session).id

        assert main(["review", "skip", str(review_id)]) == EXIT_OK

        with factory() as session:
            skipped = session.get(MatchReview, review_id)
            assert skipped is not None
            assert skipped.resolved_at is not None
            assert skipped.decision is MatchDecision.CREATED
            assert skipped.resolved_title_id is not None

    def test_dismiss_creates_nothing(self, factory: sessionmaker[Session]) -> None:
        """The answer a sing-along needs, and the one that did not exist before."""
        with factory() as session:
            review_id = self._add_review(session).id

        assert main(["review", "dismiss", str(review_id)]) == EXIT_OK

        with factory() as session:
            dismissed = session.get(MatchReview, review_id)
            assert dismissed is not None
            assert dismissed.decision is MatchDecision.DISMISSED
            assert dismissed.resolved_title_id is None
            assert session.scalars(select(Title)).all() == []

    def test_an_unknown_review_is_fatal(self, migrated: Path) -> None:
        assert main(["review", "skip", "999"]) == EXIT_FATAL

    def test_an_unknown_title_is_fatal(self, factory: sessionmaker[Session]) -> None:
        with factory() as session:
            review_id = self._add_review(session).id

        assert main(["review", "resolve", str(review_id), "--title-id", "999"]) == EXIT_FATAL


class TestSyncCommand:
    def test_no_enabled_sources_still_succeeds(
        self, migrated: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unknown --source is a warning, not a crash."""
        assert main(["sync", "--source", "not_a_real_source"]) == EXIT_OK

    def test_images_with_nothing_to_do_succeeds(self, migrated: Path) -> None:
        assert main(["images"]) == EXIT_OK


class TestParser:
    def test_requires_a_subcommand(self) -> None:
        with pytest.raises(SystemExit):
            build_parser().parse_args([])

    def test_sync_accepts_repeated_sources(self) -> None:
        args = build_parser().parse_args(["sync", "--source", "mako", "--source", "netflix_il"])

        assert args.sources == ["mako", "netflix_il"]

    def test_sync_defaults_to_every_source(self) -> None:
        assert build_parser().parse_args(["sync"]).sources is None

    def test_images_flags(self) -> None:
        args = build_parser().parse_args(["images", "--force", "--limit", "5"])

        assert args.force is True
        assert args.limit == 5

    def test_daemon_once_flag(self) -> None:
        assert build_parser().parse_args(["daemon", "--once"]).once is True

    def test_review_resolve_requires_a_title(self) -> None:
        with pytest.raises(SystemExit):
            build_parser().parse_args(["review", "resolve", "1"])

    def test_reports_a_version(self) -> None:
        with pytest.raises(SystemExit) as exc_info:
            build_parser().parse_args(["--version"])

        assert exc_info.value.code == 0
