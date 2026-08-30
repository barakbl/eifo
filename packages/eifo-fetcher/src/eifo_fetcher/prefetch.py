"""Reading several catalogs at once, while still writing them one at a time.

A sync is two jobs stitched together, and they have opposite constraints:

* **Reading a catalog** is nearly all waiting - on a site that answers one
  request a second, on a headless browser clearing a challenge, on TMDB paging
  through a provider. Eight of those wait perfectly well side by side.
* **Writing what it says** is nearly all lock. SQLite takes one writer at a
  time, and :func:`~eifo_fetcher.pipeline._ingest` holds that writer open across
  a batch of items, so a second sync's first write waits on the whole of the
  first sync's batch and then fails outright once ``busy_timeout`` runs out.
  That is measured rather than assumed: two sessions writing concurrently
  against this project's engine settings, one holding its transaction longer
  than the other's timeout, ends as ``OperationalError: database is locked``.

So the reading is what runs in parallel here, and the writing is left exactly
where it was: one thread, one writer, sources in the order they were asked for.
That split is only available because a source plugin is contractually a pure
producer - it yields :class:`~eifo_fetcher.sources.base.RawItem` values and never
touches the database (see ``sources/base.py``) - which leaves the workers with
nothing to contend over but the network, and the network is already governed
per host by the shared rate limiter. A site does not get asked for more per
second because of this; a run just stops asking one site at a time.

**One plugin at a time.** The unit of parallelism is the plugin, not the source.
A plugin owning a dozen services fetches them one after another on its own
thread, because those services come from one upstream API on one rate limit, and
running them together would buy nothing but a longer queue at the same host.

**Bounded.** Each source gets a queue of a fixed size. A plugin that has read
its whole catalog into the queue releases its worker for the next plugin instead
of idling until the ingester reaches it; a plugin whose queue fills up waits,
which is the backpressure that keeps a big catalog from becoming a big list in
memory.

Ordering is what keeps that from deadlocking. Workers are started in the same
order the ingester consumes them, and a pool runs its queued work first-in
first-out, so the source the ingester is waiting for always belongs to a plugin
that is already running rather than one still queued behind it.
"""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Iterator, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from types import TracebackType
from typing import Self

from eifo_fetcher.progress import ProgressTicker
from eifo_fetcher.runs import RunLogCapture, capturing
from eifo_fetcher.sources.base import FetchContext, RawItem, SourceInfo, SourcePlugin

logger = logging.getLogger("eifo.fetch.prefetch")

#: How long a blocked worker waits before checking whether it has been abandoned.
#: Short enough that shutting down is not a visible pause, long enough that a
#: full queue is not a spin loop.
_PUT_TIMEOUT_SECONDS = 0.5


@dataclass(frozen=True, slots=True)
class FetchUnit:
    """One source to read, with everything reading it needs."""

    plugin: SourcePlugin
    info: SourceInfo
    ctx: FetchContext
    #: Opened before the fetch starts so the plugin's own lines land in this
    #: source's row, rather than in whichever row happened to be open.
    capture: RunLogCapture


class _Done:
    """End of one source's stream."""

    __slots__ = ("error",)

    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error


class _AbandonedError(Exception):
    """The consumer went away; stop reading and let the worker finish."""


class Prefetcher:
    """Reads the given sources in the background, one plugin at a time.

    Use as a context manager, and take each source's items with :meth:`items` in
    the order the units were given. Leaving the block stops every worker, so an
    ingester that raises does not strand a plugin blocked on a queue nobody will
    ever drain.
    """

    def __init__(
        self,
        units: Sequence[FetchUnit],
        *,
        concurrency: int,
        buffer_size: int,
    ) -> None:
        self._units = list(units)
        self._buffer_size = max(1, buffer_size)
        self._queues: dict[str, queue.Queue[RawItem | _Done]] = {
            unit.info.key: queue.Queue(maxsize=self._buffer_size) for unit in self._units
        }
        self._stopping = threading.Event()
        #: Set per source once the ingester will read no more of it.
        self._abandoned = {unit.info.key: threading.Event() for unit in self._units}
        self._pool: ThreadPoolExecutor | None = None
        self._futures: list[Future[None]] = []
        self._groups = _by_plugin(self._units)
        self._concurrency = max(1, min(concurrency, len(self._groups) or 1))

    @property
    def concurrency(self) -> int:
        """Workers actually started - never more than there are plugins to run."""
        return self._concurrency

    def __enter__(self) -> Self:
        if not self._units:
            return self
        self._pool = ThreadPoolExecutor(
            max_workers=self._concurrency,
            thread_name_prefix="eifo-fetch",
        )
        # Submitted in the order the ingester will consume them: the pool runs
        # its backlog first-in first-out, so whatever the ingester is waiting
        # for is always already running rather than queued behind something it
        # has not asked for yet.
        self._futures = [self._pool.submit(self._run_group, group) for group in self._groups]
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._stopping.set()
        # Drain as we go: a worker parked on a queue nobody is reading any more
        # would otherwise hold the shutdown open for its full put timeout, once
        # per item it still has in hand.
        for q in self._queues.values():
            _drain(q)
        if self._pool is not None:
            self._pool.shutdown(wait=True)
            self._pool = None

    def items(self, unit: FetchUnit) -> Iterator[RawItem]:
        """Everything this source yielded, in order, as its worker produces it.

        Raises whatever the plugin raised, in the consuming thread, so a fetch
        that failed fails the sync exactly as it did when the fetch was inline.
        """
        q = self._queues[unit.info.key]
        while True:
            value = q.get()
            if isinstance(value, _Done):
                if value.error is not None:
                    raise value.error
                return
            yield value

    def done(self, unit: FetchUnit) -> None:
        """Say that nothing more will be read from this source.

        Normally the stream is already exhausted and this does nothing at all.
        It matters when the ingest stopped early - it raised, and the sync is
        recorded as failed - because the worker is then sitting on a full queue
        nobody is ever going to empty, and every later source belonging to the
        same plugin would wait behind it for good.
        """
        self._abandoned[unit.info.key].set()
        _drain(self._queues[unit.info.key])

    # -- worker side ------------------------------------------------------

    def _run_group(self, group: Sequence[FetchUnit]) -> None:
        """Read one plugin's sources, in order, on this worker's thread.

        Every unit is closed, whatever happens - including whatever nobody
        thought of. A unit left unclosed is not a failed source, it is an
        ingester blocked on a queue that will never see its end marker.
        """
        for unit in group:
            error: BaseException | None = None
            try:
                if self._stopping.is_set():
                    error = _AbandonedError()
                else:
                    self._read(unit)
            except BaseException as exc:
                # Not handled here. The consumer holds the database session and
                # the open run row, so it is the one that can record a failure;
                # re-raising it there keeps a failed fetch behaving exactly as
                # it did when the fetch happened inline.
                error = exc
            finally:
                self._finish(unit, error)

    def _read(self, unit: FetchUnit) -> None:
        """Pull one source's catalog into its queue, saying so as it goes."""
        key = unit.info.key
        # Routed for this thread before the plugin says anything, so the fetch's
        # own lines are on the row for the source that produced them.
        with capturing(unit.capture):
            ticker = ProgressTicker()
            count = 0
            unit.ctx.logger.info("reading the catalog")
            for item in unit.plugin.fetch(unit.ctx):
                self._put(key, item)
                count += 1
                if ticker.due(count):
                    unit.ctx.logger.info("read %s listings so far", f"{count:,}")
            unit.ctx.logger.info("read %s listings", f"{count:,}")

    def _put(self, key: str, item: RawItem) -> None:
        q = self._queues[key]
        while True:
            if self._stopping.is_set() or self._abandoned[key].is_set():
                raise _AbandonedError
            try:
                q.put(item, timeout=_PUT_TIMEOUT_SECONDS)
                return
            except queue.Full:
                continue

    def _finish(self, unit: FetchUnit, error: BaseException | None) -> None:
        """Close this source's stream, even if nobody is reading it any more."""
        if isinstance(error, _AbandonedError):
            logger.debug("%s: stopped reading, nobody is listening", unit.info.key)
            error = None
        marker = _Done(error)
        key = unit.info.key
        while True:
            try:
                self._queues[key].put(marker, timeout=_PUT_TIMEOUT_SECONDS)
                return
            except queue.Full:
                if self._stopping.is_set() or self._abandoned[key].is_set():
                    # Nobody will read it; the consumer has already gone.
                    return
                continue


def _by_plugin(units: Sequence[FetchUnit]) -> list[list[FetchUnit]]:
    """Group units by their plugin, keeping the order they were given in.

    Identity rather than equality: two instances of one plugin class are two
    plugins, and a plugin is not required to be hashable.
    """
    groups: list[list[FetchUnit]] = []
    seen: dict[int, list[FetchUnit]] = {}
    for unit in units:
        group = seen.get(id(unit.plugin))
        if group is None:
            group = []
            seen[id(unit.plugin)] = group
            groups.append(group)
        group.append(unit)
    return groups


def _drain(q: queue.Queue[RawItem | _Done]) -> None:
    while True:
        try:
            q.get_nowait()
        except queue.Empty:
            return
