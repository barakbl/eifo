"""Orchestration across sources, and the scheduled phases."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from eifo_core.enums import FetchStatus, SourceKind, TitleKind
from eifo_core.models import Source
from eifo_core.settings import Settings, SourceConfig
from eifo_fetcher.daemon import _parse_time, run_once
from eifo_fetcher.http import HttpClient
from eifo_fetcher.runner import sync_all
from eifo_fetcher.sources.base import FetchContext, RawItem, SourceInfo, SourcePlugin


def info(key: str) -> SourceInfo:
    return SourceInfo(
        key=key,
        name=key.title(),
        kind=SourceKind.SUBSCRIPTION,
        website_url=f"https://{key}.example",
    )


class TwoSourcePlugin(SourcePlugin):
    """One plugin owning two sources, like the real provider harvester."""

    def __init__(self, *, failing: str | None = None) -> None:
        self._failing = failing

    def sources(self) -> list[SourceInfo]:
        return [info("alpha"), info("beta")]

    def fetch(self, ctx: FetchContext) -> Iterator[RawItem]:
        if ctx.source_key == self._failing:
            raise RuntimeError(f"{ctx.source_key} is down")
        yield RawItem(
            source_key=ctx.source_key,
            kind=TitleKind.SERIES,
            name=f"תוכנית {ctx.source_key}",
            year=2020,
        )


class TestSyncAll:
    def test_syncs_every_enabled_source(
        self, session_factory: sessionmaker[Session], settings: Settings, http: HttpClient
    ) -> None:
        report = sync_all(session_factory, settings, http=http, plugins=[TwoSourcePlugin()])

        assert len(report.results) == 2
        assert report.items_seen == 2
        assert report.failed == []

    def test_one_failing_source_does_not_stop_the_others(
        self, session_factory: sessionmaker[Session], settings: Settings, http: HttpClient
    ) -> None:
        report = sync_all(
            session_factory,
            settings,
            http=http,
            plugins=[TwoSourcePlugin(failing="alpha")],
        )

        statuses = {result.source_key: result.status for result in report.results}
        assert statuses["alpha"] is FetchStatus.FAILED
        assert statuses["beta"] is FetchStatus.OK

    def test_skips_a_disabled_source(
        self, session_factory: sessionmaker[Session], tmp_path: object, http: HttpClient
    ) -> None:
        settings = Settings(
            _env_file=None,
            db_url="sqlite:///:memory:",
            sources={"alpha": SourceConfig(enabled=False)},
        )

        report = sync_all(session_factory, settings, http=http, plugins=[TwoSourcePlugin()])

        assert [result.source_key for result in report.results] == ["beta"]

    def test_only_syncs_the_requested_source(
        self, session_factory: sessionmaker[Session], settings: Settings, http: HttpClient
    ) -> None:
        report = sync_all(
            session_factory, settings, http=http, only=["beta"], plugins=[TwoSourcePlugin()]
        )

        assert [result.source_key for result in report.results] == ["beta"]

    def test_an_unknown_requested_source_is_ignored(
        self, session_factory: sessionmaker[Session], settings: Settings, http: HttpClient
    ) -> None:
        report = sync_all(
            session_factory, settings, http=http, only=["nope"], plugins=[TwoSourcePlugin()]
        )

        assert report.results == []

    def test_a_full_run_retires_sources_no_longer_declared(
        self, session_factory: sessionmaker[Session], settings: Settings, http: HttpClient
    ) -> None:
        sync_all(session_factory, settings, http=http, plugins=[TwoSourcePlugin()])

        class OnlyAlpha(TwoSourcePlugin):
            def sources(self) -> list[SourceInfo]:
                return [info("alpha")]

        report = sync_all(session_factory, settings, http=http, plugins=[OnlyAlpha()])

        assert report.retired_sources == ["beta"]
        with session_factory() as session:
            beta = session.scalar(select(Source).where(Source.key == "beta"))
            assert beta is not None and beta.active is False

    def test_a_targeted_run_never_retires_anything(
        self, session_factory: sessionmaker[Session], settings: Settings, http: HttpClient
    ) -> None:
        """Syncing one source says nothing about the ones left untouched."""
        sync_all(session_factory, settings, http=http, plugins=[TwoSourcePlugin()])

        report = sync_all(
            session_factory, settings, http=http, only=["alpha"], plugins=[TwoSourcePlugin()]
        )

        assert report.retired_sources == []
        with session_factory() as session:
            beta = session.scalar(select(Source).where(Source.key == "beta"))
            assert beta is not None and beta.active is True


class TestScheduleParsing:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [("03:00", (3, 0)), ("04:30", (4, 30)), ("23:59", (23, 59))],
    )
    def test_parses_valid_times(self, value: str, expected: tuple[int, int]) -> None:
        assert _parse_time(value) == expected

    @pytest.mark.parametrize("value", ["", "not-a-time", "3h00"])
    def test_rejects_invalid_times(self, value: str) -> None:
        with pytest.raises(ValueError, match="expected HH:MM"):
            _parse_time(value)


class TestRunOnce:
    def test_a_failing_phase_does_not_propagate(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A scheduled run must never take the daemon down with it."""

        def explode(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("catalog exploded")

        monkeypatch.setattr("eifo_fetcher.daemon.sync_all", explode)
        monkeypatch.setattr("eifo_fetcher.daemon.require_schema", lambda *_a: None)

        assert run_once(settings) == 0
