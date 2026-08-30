"""Reading several catalogs at once, while still writing them one at a time."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator

import pytest

from eifo_core.enums import SourceKind, TitleKind
from eifo_core.settings import Settings
from eifo_fetcher.http import HttpClient
from eifo_fetcher.prefetch import FetchUnit, Prefetcher
from eifo_fetcher.runs import RunLogCapture, new_capture
from eifo_fetcher.sources.base import FetchContext, RawItem, SourceInfo, SourcePlugin

#: Long enough that a loaded machine does not fail the test, short enough that a
#: genuine deadlock is reported as one rather than as a hung suite.
PATIENCE_SECONDS = 10.0


def info(key: str) -> SourceInfo:
    return SourceInfo(
        key=key,
        name=key.title(),
        kind=SourceKind.SUBSCRIPTION,
        website_url=f"https://{key}.example",
    )


def item(key: str, index: int) -> RawItem:
    return RawItem(source_key=key, kind=TitleKind.MOVIE, name=f"{key}-{index}")


class ListPlugin(SourcePlugin):
    """Yields a fixed number of items for each of the sources it owns."""

    def __init__(self, keys: list[str], *, count: int = 3) -> None:
        self._keys = keys
        self._count = count

    def sources(self) -> list[SourceInfo]:
        return [info(key) for key in self._keys]

    def fetch(self, ctx: FetchContext) -> Iterator[RawItem]:
        for index in range(self._count):
            yield item(ctx.source_key, index)


class FailingPlugin(SourcePlugin):
    def __init__(self, key: str, *, after: int = 0) -> None:
        self._key = key
        self._after = after

    def sources(self) -> list[SourceInfo]:
        return [info(self._key)]

    def fetch(self, ctx: FetchContext) -> Iterator[RawItem]:
        for index in range(self._after):
            yield item(ctx.source_key, index)
        raise RuntimeError(f"{ctx.source_key} is down")


@pytest.fixture
def make_unit(http: HttpClient, settings: Settings):
    def build(plugin: SourcePlugin, key: str, capture: RunLogCapture | None = None) -> FetchUnit:
        return FetchUnit(
            plugin=plugin,
            info=info(key),
            ctx=FetchContext(source_key=key, http=http, settings=settings),
            capture=capture or new_capture(),
        )

    return build


def drain(prefetcher: Prefetcher, unit: FetchUnit) -> list[str]:
    return [raw.name for raw in prefetcher.items(unit)]


class TestReading:
    def test_every_source_arrives_whole_and_in_order(self, make_unit) -> None:
        plugin = ListPlugin(["alpha", "beta"], count=4)
        units = [make_unit(plugin, "alpha"), make_unit(plugin, "beta")]

        with Prefetcher(units, concurrency=2, buffer_size=10) as prefetcher:
            read = {unit.info.key: drain(prefetcher, unit) for unit in units}

        assert read["alpha"] == [f"alpha-{index}" for index in range(4)]
        assert read["beta"] == [f"beta-{index}" for index in range(4)]

    def test_a_catalog_larger_than_the_buffer_still_arrives_whole(self, make_unit) -> None:
        """The buffer bounds memory, not the catalog: the reader waits its turn."""
        plugin = ListPlugin(["alpha"], count=250)
        units = [make_unit(plugin, "alpha")]

        with Prefetcher(units, concurrency=2, buffer_size=5) as prefetcher:
            assert len(drain(prefetcher, units[0])) == 250

    def test_nothing_to_read_is_not_an_error(self) -> None:
        with Prefetcher([], concurrency=4, buffer_size=10) as prefetcher:
            assert prefetcher.concurrency == 1

    def test_no_more_workers_than_there_are_plugins(self, make_unit) -> None:
        """Four workers for two plugins is two idle threads and nothing else."""
        plugin = ListPlugin(["alpha", "beta"])
        other = ListPlugin(["gamma"])
        units = [make_unit(plugin, "alpha"), make_unit(plugin, "beta"), make_unit(other, "gamma")]

        with Prefetcher(units, concurrency=8, buffer_size=10) as prefetcher:
            assert prefetcher.concurrency == 2


class TestOnePluginAtATime:
    def test_a_plugin_reads_its_own_sources_one_after_another(self, make_unit) -> None:
        """Two services on one upstream API must not be asked for at once."""
        running: list[str] = []
        overlapped = threading.Event()

        class SerialPlugin(SourcePlugin):
            def sources(self) -> list[SourceInfo]:
                return [info("alpha"), info("beta")]

            def fetch(self, ctx: FetchContext) -> Iterator[RawItem]:
                if running:
                    overlapped.set()
                running.append(ctx.source_key)
                try:
                    time.sleep(0.05)
                    yield item(ctx.source_key, 0)
                finally:
                    running.remove(ctx.source_key)

        plugin = SerialPlugin()
        units = [make_unit(plugin, "alpha"), make_unit(plugin, "beta")]

        with Prefetcher(units, concurrency=4, buffer_size=10) as prefetcher:
            for unit in units:
                drain(prefetcher, unit)

        assert not overlapped.is_set()

    def test_different_plugins_do_read_at_once(self, make_unit) -> None:
        """The whole point. Each waits for the other, so neither can finish alone."""
        met = threading.Barrier(2)

        class MeetingPlugin(SourcePlugin):
            def __init__(self, key: str) -> None:
                self._key = key

            def sources(self) -> list[SourceInfo]:
                return [info(self._key)]

            def fetch(self, ctx: FetchContext) -> Iterator[RawItem]:
                met.wait(timeout=PATIENCE_SECONDS)
                yield item(ctx.source_key, 0)

        first, second = MeetingPlugin("alpha"), MeetingPlugin("beta")
        units = [make_unit(first, "alpha"), make_unit(second, "beta")]

        with Prefetcher(units, concurrency=2, buffer_size=10) as prefetcher:
            # A barrier neither side can pass alone: if these ran one after the
            # other this raises BrokenBarrierError rather than hanging the suite.
            assert [drain(prefetcher, unit) for unit in units] == [["alpha-0"], ["beta-0"]]


class TestWhenSomethingGoesWrong:
    def test_a_failing_fetch_fails_in_the_consumer(self, make_unit) -> None:
        """Where the session and the open run row are, so it can be recorded."""
        units = [make_unit(FailingPlugin("alpha"), "alpha")]

        with (
            Prefetcher(units, concurrency=2, buffer_size=10) as prefetcher,
            pytest.raises(RuntimeError, match="alpha is down"),
        ):
            drain(prefetcher, units[0])

    def test_what_it_read_before_it_failed_arrives_first(self, make_unit) -> None:
        units = [make_unit(FailingPlugin("alpha", after=2), "alpha")]

        with Prefetcher(units, concurrency=2, buffer_size=10) as prefetcher:
            stream = prefetcher.items(units[0])
            assert [next(stream).name, next(stream).name] == ["alpha-0", "alpha-1"]
            with pytest.raises(RuntimeError):
                next(stream)

    def test_one_plugin_failing_does_not_stop_another(self, make_unit) -> None:
        good = ListPlugin(["beta"])
        units = [make_unit(FailingPlugin("alpha"), "alpha"), make_unit(good, "beta")]

        with Prefetcher(units, concurrency=2, buffer_size=10) as prefetcher:
            with pytest.raises(RuntimeError):
                drain(prefetcher, units[0])
            assert drain(prefetcher, units[1]) == ["beta-0", "beta-1", "beta-2"]

    def test_abandoning_a_source_frees_the_rest_of_its_plugin(self, make_unit) -> None:
        """An ingest that raised leaves its reader parked on a queue nobody reads.

        Without ``done`` every later source from the same plugin waits behind it
        for good, which is a hung nightly run rather than one failed source.
        """
        plugin = ListPlugin(["alpha", "beta"], count=500)
        units = [make_unit(plugin, "alpha"), make_unit(plugin, "beta")]

        with Prefetcher(units, concurrency=2, buffer_size=2) as prefetcher:
            assert next(prefetcher.items(units[0])).name == "alpha-0"
            prefetcher.done(units[0])

            finished = _with_patience(lambda: len(drain(prefetcher, units[1])))

        assert finished == 500

    def test_leaving_the_block_does_not_wait_on_a_reader_nobody_is_reading(self, make_unit) -> None:
        plugin = ListPlugin(["alpha"], count=100_000)
        units = [make_unit(plugin, "alpha")]

        started = time.monotonic()
        with Prefetcher(units, concurrency=2, buffer_size=2) as prefetcher:
            next(prefetcher.items(units[0]))
        assert time.monotonic() - started < PATIENCE_SECONDS


class TestWhatEachSourceSaid:
    def test_a_plugin_s_lines_land_on_its_own_row(
        self, make_unit, fetcher_logs_at_info: None
    ) -> None:
        """Read in the background, so nothing else can be relied on to be open."""
        alpha_log, beta_log = new_capture(), new_capture()

        class TalkativePlugin(SourcePlugin):
            def __init__(self, key: str) -> None:
                self._key = key

            def sources(self) -> list[SourceInfo]:
                return [info(self._key)]

            def fetch(self, ctx: FetchContext) -> Iterator[RawItem]:
                ctx.logger.info("a word from %s", ctx.source_key)
                yield item(ctx.source_key, 0)

        units = [
            make_unit(TalkativePlugin("alpha"), "alpha", alpha_log),
            make_unit(TalkativePlugin("beta"), "beta", beta_log),
        ]

        with Prefetcher(units, concurrency=2, buffer_size=10) as prefetcher:
            for unit in units:
                drain(prefetcher, unit)

        alpha, beta = alpha_log.text() or "", beta_log.text() or ""
        assert "a word from alpha" in alpha
        assert "a word from alpha" not in beta
        assert "a word from beta" in beta
        assert "a word from beta" not in alpha

    def test_it_says_how_far_it_has_got(self, make_unit, fetcher_logs_at_info: None) -> None:
        capture = new_capture()
        units = [make_unit(ListPlugin(["alpha"], count=250), "alpha", capture)]

        with Prefetcher(units, concurrency=1, buffer_size=300) as prefetcher:
            drain(prefetcher, units[0])

        said = capture.text() or ""
        assert "reading the catalog" in said
        assert "read 200 listings so far" in said
        assert "read 250 listings" in said


def _with_patience(work):
    """Run ``work`` on a thread, failing rather than hanging if it deadlocks."""
    result: list[object] = []
    thread = threading.Thread(target=lambda: result.append(work()), daemon=True)
    thread.start()
    thread.join(timeout=PATIENCE_SECONDS)
    assert not thread.is_alive(), "the prefetcher deadlocked"
    return result[0]
