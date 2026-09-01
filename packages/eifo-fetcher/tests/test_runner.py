"""Orchestration across sources, and the scheduled phases."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from eifo_core.db import create_engine_from_settings
from eifo_core.enums import FetchPhase, FetchStatus, SourceKind, TitleKind
from eifo_core.models import Base, FetchRun, Source, Title
from eifo_core.settings import Settings, SourceConfig
from eifo_core.types import utcnow
from eifo_fetcher.daemon import _parse_time, run_backfills, run_daemon, run_nightly, run_once
from eifo_fetcher.enrich import EnrichResultTally
from eifo_fetcher.enrichers.seret_index import IndexResult
from eifo_fetcher.http import HttpClient
from eifo_fetcher.lock import single_flight
from eifo_fetcher.runner import enrich_all, sync_all
from eifo_fetcher.sources.base import FetchContext, RawItem, SourceInfo, SourcePlugin


def info(key: str) -> SourceInfo:
    return SourceInfo(
        key=key,
        name=key.title(),
        kind=SourceKind.SUBSCRIPTION,
        website_url=f"https://{key}.example",
    )


class OneSourcePlugin(SourcePlugin):
    def __init__(self, key: str) -> None:
        self._key = key

    def sources(self) -> list[SourceInfo]:
        return [info(self._key)]

    def fetch(self, ctx: FetchContext) -> Iterator[RawItem]:
        yield RawItem(source_key=ctx.source_key, kind=TitleKind.MOVIE, name=f"סרט {ctx.source_key}")


class MeetingPlugin(SourcePlugin):
    """Cannot finish reading until another plugin is reading too."""

    def __init__(self, key: str, met: threading.Barrier) -> None:
        self._key = key
        self._met = met

    def sources(self) -> list[SourceInfo]:
        return [info(self._key)]

    def fetch(self, ctx: FetchContext) -> Iterator[RawItem]:
        self._met.wait(timeout=10)
        yield RawItem(source_key=ctx.source_key, kind=TitleKind.MOVIE, name=f"סרט {ctx.source_key}")


class OverlapWatchingPlugin(SourcePlugin):
    """Owns two sources and notices if it is ever asked for both at once."""

    def __init__(self) -> None:
        self.overlapped = threading.Event()
        self._running: list[str] = []

    def sources(self) -> list[SourceInfo]:
        return [info("alpha"), info("beta")]

    def fetch(self, ctx: FetchContext) -> Iterator[RawItem]:
        if self._running:
            self.overlapped.set()
        self._running.append(ctx.source_key)
        try:
            time.sleep(0.05)
            yield RawItem(
                source_key=ctx.source_key, kind=TitleKind.MOVIE, name=f"סרט {ctx.source_key}"
            )
        finally:
            self._running.remove(ctx.source_key)


class TalkativePlugin(SourcePlugin):
    """Says something while reading, which is when a prefetched source speaks."""

    def __init__(self, key: str) -> None:
        self._key = key

    def sources(self) -> list[SourceInfo]:
        return [info(self._key)]

    def fetch(self, ctx: FetchContext) -> Iterator[RawItem]:
        ctx.logger.info("a word from %s", ctx.source_key)
        yield RawItem(source_key=ctx.source_key, kind=TitleKind.MOVIE, name=f"סרט {ctx.source_key}")


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


def reading(settings: Settings, *, at_once: int) -> Settings:
    """The same settings, with a different number of catalogs read at a time."""
    return settings.model_copy(
        update={"fetch": settings.fetch.model_copy(update={"concurrency": at_once})}
    )


class TestReadingSeveralAtOnce:
    """Reading is what runs in parallel; writing is still one source at a time.

    See ``eifo_fetcher.prefetch`` for why that split rather than the obvious one:
    SQLite takes a single writer, and two syncs writing at once end as
    ``database is locked`` rather than as a faster night.
    """

    def test_it_finds_exactly_what_the_serial_run_finds(
        self, session_factory: sessionmaker[Session], settings: Settings, http: HttpClient
    ) -> None:
        serial = sync_all(
            session_factory,
            reading(settings, at_once=1),
            http=http,
            plugins=[TwoSourcePlugin()],
        )
        assert [(r.source_key, r.status, r.items_seen) for r in serial.results] == [
            ("alpha", FetchStatus.OK, 1),
            ("beta", FetchStatus.OK, 1),
        ]

    def test_the_parallel_run_agrees_with_it(
        self, session_factory: sessionmaker[Session], settings: Settings, http: HttpClient
    ) -> None:
        report = sync_all(
            session_factory,
            reading(settings, at_once=4),
            http=http,
            plugins=[TwoSourcePlugin(), OneSourcePlugin("gamma")],
        )

        assert [(r.source_key, r.status, r.items_seen) for r in report.results] == [
            ("alpha", FetchStatus.OK, 1),
            ("beta", FetchStatus.OK, 1),
            ("gamma", FetchStatus.OK, 1),
        ]

    def test_two_plugins_really_do_read_at_once(
        self, session_factory: sessionmaker[Session], settings: Settings, http: HttpClient
    ) -> None:
        """A barrier neither plugin can pass alone, so a serial run cannot pass it."""
        met = threading.Barrier(2)
        plugins = [MeetingPlugin("alpha", met), MeetingPlugin("beta", met)]

        report = sync_all(session_factory, reading(settings, at_once=2), http=http, plugins=plugins)

        assert report.failed == []
        assert report.items_seen == 2

    def test_one_plugin_still_reads_its_own_sources_in_turn(
        self, session_factory: sessionmaker[Session], settings: Settings, http: HttpClient
    ) -> None:
        """Two services on one upstream API and one rate limit; asked for in turn."""
        plugin = OverlapWatchingPlugin()

        sync_all(session_factory, reading(settings, at_once=4), http=http, plugins=[plugin])

        assert not plugin.overlapped.is_set()

    def test_a_failing_source_still_fails_only_itself(
        self, session_factory: sessionmaker[Session], settings: Settings, http: HttpClient
    ) -> None:
        """The fetch now raises on another thread; it must still land on its row."""
        report = sync_all(
            session_factory,
            reading(settings, at_once=4),
            http=http,
            plugins=[TwoSourcePlugin(failing="alpha")],
        )

        statuses = {result.source_key: result.status for result in report.results}
        assert statuses == {"alpha": FetchStatus.FAILED, "beta": FetchStatus.OK}
        failure = next(r for r in report.results if r.source_key == "alpha")
        assert any("alpha is down" in error for error in failure.errors)

    def test_what_a_plugin_said_while_reading_is_on_its_own_row(
        self,
        session_factory: sessionmaker[Session],
        settings: Settings,
        http: HttpClient,
        fetcher_logs_at_info: None,
    ) -> None:
        """Read in the background, long before anything opened a row for it."""
        sync_all(
            session_factory,
            reading(settings, at_once=2),
            http=http,
            plugins=[TalkativePlugin("alpha"), TalkativePlugin("beta")],
        )

        with session_factory() as session:
            logs = {
                run.source_key: run.log or ""
                for run in session.scalars(select(FetchRun)).all()
                if run.phase is FetchPhase.SYNC
            }

        assert "a word from alpha" in logs["alpha"]
        assert "a word from alpha" not in logs["beta"]
        assert "a word from beta" in logs["beta"]


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

    def test_switching_a_source_off_does_not_retire_it(
        self, session_factory: sessionmaker[Session], settings: Settings, http: HttpClient
    ) -> None:
        """Off and gone are different claims about a source.

        Retiring one badges it "no longer tracked" in the Manage tab, and only a
        successful sync clears that - so a source switched off used to come back
        wearing a label that outlived being switched on again.
        """
        sync_all(session_factory, settings, http=http, plugins=[TwoSourcePlugin()])

        off = Settings(
            _env_file=None,
            db_url="sqlite:///:memory:",
            sources={"beta": SourceConfig(enabled=False)},
        )
        report = sync_all(session_factory, off, http=http, plugins=[TwoSourcePlugin()])

        assert [result.source_key for result in report.results] == ["alpha"]
        assert report.retired_sources == []
        with session_factory() as session:
            beta = session.scalar(select(Source).where(Source.key == "beta"))
            assert beta is not None and beta.active is True

    def test_an_operator_switching_one_off_does_not_retire_it_either(
        self, session_factory: sessionmaker[Session], settings: Settings, http: HttpClient
    ) -> None:
        """The same, from the Manage tab rather than the config file."""

    def test_a_sync_answers_an_operators_ask(
        self, session_factory: sessionmaker[Session], settings: Settings, http: HttpClient
    ) -> None:
        """Cleared by having tried, so the daemon does not sit on it forever."""
        sync_all(session_factory, settings, http=http, plugins=[TwoSourcePlugin()])
        with session_factory() as session:
            source = session.scalar(select(Source).where(Source.key == "beta"))
            assert source is not None
            source.enabled = False
            session.commit()

        report = sync_all(session_factory, settings, http=http, plugins=[TwoSourcePlugin()])

        assert report.retired_sources == []
        with session_factory() as session:
            beta = session.scalar(select(Source).where(Source.key == "beta"))
            assert beta is not None and beta.active is True
            source.backfill_requested_at = utcnow()
            session.commit()

        sync_all(session_factory, settings, http=http, only=["beta"], plugins=[TwoSourcePlugin()])

        with session_factory() as session:
            beta = session.scalar(select(Source).where(Source.key == "beta"))
            assert beta is not None and beta.backfill_requested_at is None

    def test_a_failed_sync_still_answers_it(
        self, session_factory: sessionmaker[Session], settings: Settings, http: HttpClient
    ) -> None:
        """The run is in the Runs tab saying what went wrong. Leaving the ask
        standing would put the daemon back on a broken source every half
        minute; the operator can ask again by flipping the switch."""
        sync_all(session_factory, settings, http=http, plugins=[TwoSourcePlugin()])
        with session_factory() as session:
            source = session.scalar(select(Source).where(Source.key == "alpha"))
            assert source is not None
            source.backfill_requested_at = utcnow()
            session.commit()

        report = sync_all(
            session_factory,
            settings,
            http=http,
            only=["alpha"],
            plugins=[TwoSourcePlugin(failing="alpha")],
        )

        assert report.results[0].status is FetchStatus.FAILED
        with session_factory() as session:
            alpha = session.scalar(select(Source).where(Source.key == "alpha"))
            assert alpha is not None and alpha.backfill_requested_at is None

    def test_a_source_nobody_asked_about_keeps_its_ask(
        self, session_factory: sessionmaker[Session], settings: Settings, http: HttpClient
    ) -> None:
        """A targeted run answers only the ask it was pointed at."""
        sync_all(session_factory, settings, http=http, plugins=[TwoSourcePlugin()])
        with session_factory() as session:
            for key in ("alpha", "beta"):
                source = session.scalar(select(Source).where(Source.key == key))
                assert source is not None
                source.backfill_requested_at = utcnow()
            session.commit()

        sync_all(session_factory, settings, http=http, only=["alpha"], plugins=[TwoSourcePlugin()])

        with session_factory() as session:
            beta = session.scalar(select(Source).where(Source.key == "beta"))
            assert beta is not None and beta.backfill_requested_at is not None

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


@pytest.fixture
def phases(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record which phases ran, without any of them doing anything.

    The schema is real: a phase opens the database before it does any work of
    its own, to reattach search triggers and close out runs left behind by a
    fetcher that died.
    """
    engine = create_engine_from_settings(settings)
    Base.metadata.create_all(engine)
    engine.dispose()

    ran: list[str] = []

    def record(name: str) -> Callable[..., None]:
        def phase(*_args: object, **_kwargs: object) -> None:
            ran.append(name)

        return phase

    for name in ("sync_all", "enrich_all", "fetch_images"):
        monkeypatch.setattr(f"eifo_fetcher.daemon.{name}", record(name))
    return ran


class TestTheNightlyChain:
    def test_the_phases_run_in_dependency_order(
        self, settings: Settings, phases: list[str]
    ) -> None:
        """Enrichment needs the titles sync creates; artwork needs the URLs enrichment fills in."""
        assert run_nightly(settings) is True

        assert phases == ["sync_all", "enrich_all", "fetch_images"]

    def test_a_failing_phase_does_not_propagate(
        self, settings: Settings, phases: list[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A scheduled run must never take the daemon down with it."""

        def explode(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("catalog exploded")

        monkeypatch.setattr("eifo_fetcher.daemon.sync_all", explode)

        assert run_nightly(settings) is False

    def test_a_failing_phase_does_not_stop_the_ones_after_it(
        self, settings: Settings, phases: list[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """There is more to gain from enriching yesterday's titles than from standing still."""

        def explode(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("every source is down")

        monkeypatch.setattr("eifo_fetcher.daemon.sync_all", explode)

        run_nightly(settings)

        assert phases == ["enrich_all", "fetch_images"]

    def test_run_once_is_the_same_run(self, settings: Settings, phases: list[str]) -> None:
        assert run_once(settings) is True
        assert phases == ["sync_all", "enrich_all", "fetch_images"]


class TestBackfillOnRequest:
    """Switching a source on in the Manage tab should not mean waiting for 03:00."""

    def _ask_for(self, settings: Settings, key: str) -> None:
        engine = create_engine_from_settings(settings)
        with sessionmaker(engine)() as session:
            session.add(
                Source(
                    key=key,
                    name=key,
                    kind=SourceKind.SUBSCRIPTION,
                    website_url=f"https://{key}.example",
                    backfill_requested_at=utcnow(),
                )
            )
            session.commit()
        engine.dispose()

    def test_nothing_pending_runs_nothing(self, settings: Settings, phases: list[str]) -> None:
        """The common case, thirty seconds apart forever: it must cost nothing."""
        assert run_backfills(settings) is True

        assert phases == []

    def test_a_requested_source_is_synced(self, settings: Settings, phases: list[str]) -> None:
        self._ask_for(settings, "beta")

        assert run_backfills(settings) is True

        assert phases == ["sync_all"]

    def test_it_syncs_only(self, settings: Settings, phases: list[str]) -> None:
        """Enrichment and artwork cost far more than the titles and are the
        nightly chain's business; what was asked for is the service showing up."""
        self._ask_for(settings, "beta")

        run_backfills(settings)

        assert "enrich_all" not in phases and "fetch_images" not in phases

    def test_it_stands_down_while_a_nightly_run_holds_the_lock(
        self, settings: Settings, phases: list[str]
    ) -> None:
        """The ask keeps. A backfill beside a full sync is two fetchers at one
        database, which is the thing the lock exists to prevent."""
        self._ask_for(settings, "beta")

        with single_flight(settings):
            assert run_backfills(settings) is True

        assert phases == []

    def test_a_failure_does_not_take_the_daemon_down(
        self, settings: Settings, phases: list[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._ask_for(settings, "beta")

        def explode(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("the source is down")

        monkeypatch.setattr("eifo_fetcher.daemon.sync_all", explode)

        assert run_backfills(settings) is False


class TestOnlyOneFetcherAtATime:
    def test_a_run_stands_down_while_another_holds_the_lock(
        self, settings: Settings, phases: list[str]
    ) -> None:
        """Cron firing over a long-running daemon is ordinary, not an incident."""
        with single_flight(settings):
            assert run_nightly(settings) is True

        assert phases == []

    def test_the_lock_is_released_afterwards(self, settings: Settings, phases: list[str]) -> None:
        run_nightly(settings)

        with single_flight(settings):
            pass  # would raise if the nightly run had not let go

    def test_the_lock_is_released_after_a_failing_phase(
        self, settings: Settings, phases: list[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def explode(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("catalog exploded")

        monkeypatch.setattr("eifo_fetcher.daemon.sync_all", explode)
        run_nightly(settings)

        with single_flight(settings):
            pass


class TestHeartbeat:
    """A run that stops happening can only be noticed from outside the box."""

    def _pings(self, monkeypatch: pytest.MonkeyPatch) -> list[str]:
        seen: list[str] = []
        monkeypatch.setattr(
            "eifo_fetcher.daemon.ping",
            lambda _settings, event="": seen.append(event),
        )
        return seen

    def test_a_completed_run_pings_start_then_success(
        self, settings: Settings, phases: list[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pings = self._pings(monkeypatch)

        run_nightly(settings)

        assert pings == ["start", ""]

    def test_a_failed_run_says_so(
        self, settings: Settings, phases: list[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pings = self._pings(monkeypatch)

        def explode(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("catalog exploded")

        monkeypatch.setattr("eifo_fetcher.daemon.sync_all", explode)

        run_nightly(settings)

        assert pings == ["start", "fail"]

    def test_a_run_that_stood_down_pings_nothing(
        self, settings: Settings, phases: list[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The watchdog is watching the run, and this process did not make one."""
        pings = self._pings(monkeypatch)

        with single_flight(settings):
            run_nightly(settings)

        assert pings == []


class TestTheSchedule:
    def _added_job(self, settings: Settings, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        captured: dict[str, Any] = {}

        class FakeScheduler:
            def __init__(self, **_kwargs: Any) -> None: ...

            def add_job(self, _job: Any, _trigger: Any, **kwargs: Any) -> None:
                captured.update(kwargs)

            def start(self) -> None:
                raise KeyboardInterrupt

        monkeypatch.setattr("eifo_fetcher.daemon.BlockingScheduler", FakeScheduler)
        run_daemon(settings)
        return captured

    def test_a_machine_asleep_at_the_hour_runs_on_waking(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """APScheduler's default grace is one second, which writes off every suspended laptop."""
        assert self._added_job(settings, monkeypatch)["misfire_grace_time"] == 3600

    def test_a_backlog_collapses_into_one_run(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        job = self._added_job(settings, monkeypatch)

        assert job["coalesce"] is True
        assert job["max_instances"] == 1


class TestTheSeretIndexInsideEnrich:
    """The sitemap crawl is a bulk pass beside the IMDb one, not a side door.

    It runs from ``enrich_all`` for the same reasons the IMDb pass does -
    catalog-wide rather than per title, its own ``fetch_runs`` row, skippable
    by name - and is bounded by ``[seret] batch_size`` so a first index fills
    itself in over a month of nightly runs rather than holding somebody's site
    for five hours the night an operator upgrades.
    """

    def _crawls(
        self,
        session_factory: sessionmaker[Session],
        settings: Settings,
        http: HttpClient,
        monkeypatch: pytest.MonkeyPatch,
        **kwargs: Any,
    ) -> bool:
        """Whether the run reached for the crawl, without letting it fetch."""
        called: list[bool] = []
        monkeypatch.setattr(
            "eifo_fetcher.runner.index_seret",
            lambda *_args, **_kwargs: called.append(True) or IndexResult(),
        )
        enrich_all(session_factory, settings, http=http, limit=0, skip_imdb=True, **kwargs)
        return bool(called)

    def _a_title(self, session_factory: sessionmaker[Session]) -> None:
        with session_factory() as session:
            session.add(Title(type=TitleKind.MOVIE, name_he="פוקסטרוט"))
            session.commit()

    def test_a_nightly_enrich_crawls_a_batch(
        self, session_factory, settings: Settings, http: HttpClient, monkeypatch
    ) -> None:
        self._a_title(session_factory)

        assert self._crawls(session_factory, settings, http, monkeypatch)

    def test_it_can_be_skipped_by_name_like_the_imdb_pass(
        self, session_factory, settings: Settings, http: HttpClient, monkeypatch
    ) -> None:
        self._a_title(session_factory)

        assert not self._crawls(session_factory, settings, http, monkeypatch, skip=["seret-index"])

    def test_skipping_the_enricher_skips_the_crawl_that_feeds_it(
        self, session_factory, settings: Settings, http: HttpClient, monkeypatch
    ) -> None:
        """Indexing pages nothing will read is somebody's bandwidth for nothing."""
        self._a_title(session_factory)

        assert not self._crawls(session_factory, settings, http, monkeypatch, skip=["seret"])

    def test_an_empty_catalog_is_not_crawled_for(
        self, session_factory, settings: Settings, http: HttpClient, monkeypatch
    ) -> None:
        """The same guard the IMDb pass makes when no title carries an imdb_id.

        A fresh install must not spend 8,900 requests on somebody's site to
        enrich a catalog that does not exist yet.
        """
        assert not self._crawls(session_factory, settings, http, monkeypatch)

    def test_skipping_it_by_name_is_not_reported_as_a_typo(
        self, session_factory, settings: Settings, http: HttpClient, monkeypatch, caplog
    ) -> None:
        self._a_title(session_factory)

        with caplog.at_level(logging.WARNING, logger="eifo.fetch.runner"):
            self._crawls(session_factory, settings, http, monkeypatch, skip=["seret-index"])

        assert "nothing to skip" not in caplog.text


class TestSkippingAnEnricherForOneRun:
    """One enricher can be an order of magnitude slower than the rest.

    `rt` is scraped, so it runs at a rate chosen to be polite to somebody's
    website while TMDB answers twenty times faster - which makes a catch-up
    over a large backlog a different job from a nightly refresh. Skipping is
    per run and never touches the configured set: a catch-up must not quietly
    become the new normal.
    """

    def _run(
        self,
        session_factory: sessionmaker[Session],
        settings: Settings,
        http: HttpClient,
        **kwargs: Any,
    ) -> EnrichResultTally:
        """A run with nothing due, so only the choice of enrichers is exercised."""
        return enrich_all(session_factory, settings, http=http, limit=0, **kwargs)

    def test_by_default_every_configured_enricher_runs(
        self, session_factory, settings: Settings, http: HttpClient, caplog
    ) -> None:
        with caplog.at_level(logging.INFO, logger="eifo.fetch.runner"):
            self._run(session_factory, settings, http, skip_imdb=True)

        assert "enriching with: tmdb, seret, rt" in caplog.text

    def test_one_can_be_left_out(
        self, session_factory, settings: Settings, http: HttpClient, caplog
    ) -> None:
        with caplog.at_level(logging.INFO, logger="eifo.fetch.runner"):
            self._run(session_factory, settings, http, skip_imdb=True, skip=["rt"])

        assert "enriching with: tmdb, seret" in caplog.text
        assert "rt" not in caplog.text.split("enriching with:")[1].split("\n")[0]

    def test_the_case_it_is_typed_in_does_not_matter(
        self, session_factory, settings: Settings, http: HttpClient, caplog
    ) -> None:
        with caplog.at_level(logging.INFO, logger="eifo.fetch.runner"):
            self._run(
                session_factory, settings, http, skip_imdb=True, skip=["RT", " tmdb ", "Seret"]
            )

        assert "enriching with: nothing" in caplog.text

    def test_skipping_imdb_by_name_works_like_the_flag(
        self, session_factory, settings: Settings, http: HttpClient
    ) -> None:
        """So there is one obvious lever rather than two half-overlapping ones."""
        tally = self._run(session_factory, settings, http, skip=["imdb"])

        assert "imdb" not in tally.by_enricher

    def test_a_name_that_matches_nothing_is_said_out_loud(
        self, session_factory, settings: Settings, http: HttpClient, caplog
    ) -> None:
        """A typo that silently skips nothing looks like the flag not working."""
        with caplog.at_level(logging.WARNING, logger="eifo.fetch.runner"):
            self._run(session_factory, settings, http, skip_imdb=True, skip=["rotten"])

        assert "nothing to skip called: rotten" in caplog.text

    def test_skipping_nothing_is_the_same_as_not_asking(
        self, session_factory, settings: Settings, http: HttpClient, caplog
    ) -> None:
        with caplog.at_level(logging.INFO, logger="eifo.fetch.runner"):
            self._run(session_factory, settings, http, skip_imdb=True, skip=[])

        assert "enriching with: tmdb, seret, rt" in caplog.text
