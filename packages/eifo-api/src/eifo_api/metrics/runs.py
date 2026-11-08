"""Plugin runs as Prometheus counters and gauges.

Every figure here is a fold over ``fetch_runs``. A fetcher writes its rows
through the ingest API wherever it runs, so a sync on a laptop is counted here
the moment it reports back, with no second channel to keep working.

Counters count runs that have *ended* and add up what those runs reported. Rows
are never deleted and an ended run never changes, so each counter only ever
goes up, which is what Prometheus needs to read an increase from them. The same
fold, stopped at an earlier moment, is what :mod:`eifo_api.metrics.history`
replays to fill in the months before Prometheus was watching.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from eifo_api.metrics import PREFIX, Family
from eifo_core.enums import FetchPhase, FetchStatus
from eifo_core.ingest import ABANDONED_AFTER
from eifo_core.models import FetchRun, Source

#: The ``source`` of a run that belonged to no single source: a whole enrich
#: pass, the artwork pass, or a sync that failed before it reached a source.
ALL = "all"

#: Every way a run ends. Each (phase, source) pair is given all of them, at
#: zero until one happens: Prometheus reads an increase only from a series that
#: already existed, so a first failure that appeared out of nowhere at 1 would
#: never be counted as one.
ENDINGS: tuple[str, ...] = (
    FetchStatus.OK.value,
    FetchStatus.FAILED.value,
    FetchStatus.CRASHED.value,
    FetchStatus.ABORTED_SUSPICIOUS.value,
)

#: A sync's tally fields, and the names their counters go by.
SYNC_FIGURES = {
    "items_seen": ("items_seen", "Items the source listed, summed over its syncs."),
    "titles_created": ("titles_created", "Titles a sync added to the catalog."),
    "availability_created": ("offers_added", "Offers a sync found for the first time."),
    "retired": ("offers_retired", "Offers a sync found gone from the source."),
}

#: What the artwork pass reports, one counter label each.
IMAGE_RESULTS = ("downloaded", "skipped", "failed", "from_backup")

#: The enrich sub-run that loads IMDb's own dataset. It finds ratings in bulk
#: rather than title by title, so it reports how many it wrote instead of
#: filling ``by_enricher``; they are counted as the imdb enricher's finds.
IMDB_RUN = "imdb"


@dataclass(frozen=True, slots=True)
class Run:
    """One ``fetch_runs`` row: as much of it as the figures read."""

    phase: str
    source: str
    status: str
    started_at: dt.datetime
    finished_at: dt.datetime | None
    stats: Mapping[str, Any]

    def ending(self, now: dt.datetime) -> tuple[str, dt.datetime] | None:
        """How and when this run had ended by ``now``, or None if it had not.

        A row still marked running after :data:`ABANDONED_AFTER` belongs to a
        fetcher that never came back. The API closes such rows the next time
        that phase starts, which may be days later. Counting them as crashed
        from the moment they were given up on keeps the figures from depending
        on when that happens.
        """
        given_up = self.started_at + ABANDONED_AFTER
        if self.status == FetchStatus.RUNNING.value:
            return (FetchStatus.CRASHED.value, given_up) if given_up <= now else None
        ended = self.finished_at or self.started_at
        if self.status == FetchStatus.CRASHED.value:
            # Closed by that later sweep, so its finish is when somebody
            # noticed, not when it stopped: a nine-day "run" that was one
            # dead process. It was over when it was given up on.
            ended = min(ended, given_up)
        return (self.status, ended) if ended <= now else None

    @property
    def pair(self) -> tuple[str, str]:
        return (self.phase, self.source)


def load_runs(session: Session) -> list[Run]:
    """Every run on record, oldest first."""
    rows = session.execute(
        select(
            FetchRun.phase,
            FetchRun.source_key,
            FetchRun.status,
            FetchRun.started_at,
            FetchRun.finished_at,
            FetchRun.stats,
        ).order_by(FetchRun.id)
    ).all()
    return [
        Run(
            phase=FetchPhase(phase).value,
            source=source or ALL,
            status=FetchStatus(status).value,
            started_at=started_at,
            finished_at=finished_at,
            stats=stats if isinstance(stats, Mapping) else {},
        )
        for phase, source, status, started_at, finished_at, stats in rows
    ]


def active_sources(session: Session) -> list[str]:
    """The sources whose series are worth starting at zero."""
    return list(session.scalars(select(Source.key).where(Source.active.is_(True))).all())


def run_families(session: Session, now: dt.datetime) -> list[Family]:
    """The run figures as they stand at ``now``."""
    return replay(load_runs(session), now, sources=active_sources(session))


def replay(runs: Iterable[Run], now: dt.datetime, *, sources: Iterable[str]) -> list[Family]:
    """Fold ``runs`` as they stood at ``now``: what had ended, what was going."""
    runs = list(runs)
    tally = Tally()
    for run, status, ended_at in ended_by(runs, now):
        tally.add(run, status, ended_at)
    return tally.families(
        sources=sources,
        running=running_at(runs, now),
        last_start=last_start_at(runs, now),
    )


def ended_by(runs: Iterable[Run], now: dt.datetime) -> list[tuple[Run, str, dt.datetime]]:
    """The runs that had ended by ``now``, in the order they ended."""
    ended = []
    for run in runs:
        ending = run.ending(now)
        if ending is not None:
            ended.append((run, *ending))
    ended.sort(key=lambda item: item[2])
    return ended


def running_at(runs: Iterable[Run], now: dt.datetime) -> set[tuple[str, str]]:
    """The (phase, source) pairs with a run going at ``now``."""
    return {run.pair for run in runs if run.started_at <= now and run.ending(now) is None}


def last_start_at(runs: Iterable[Run], now: dt.datetime) -> dt.datetime | None:
    """When the latest run to have started by ``now`` started."""
    return max((run.started_at for run in runs if run.started_at <= now), default=None)


@dataclass(slots=True)
class _Last:
    """The latest run of one (phase, source) pair to have ended."""

    status: str
    ended_at: dt.datetime
    seconds: float
    errors: float


@dataclass(slots=True)
class Tally:
    """What the ended runs add up to, folded in one run at a time.

    One at a time because that is how the history import uses it: it adds the
    runs that ended in each hour, and asks for the figures after each one.
    """

    runs: dict[tuple[str, str, str], int] = field(default_factory=dict)
    seconds: dict[tuple[str, str], float] = field(default_factory=dict)
    last: dict[tuple[str, str], _Last] = field(default_factory=dict)
    last_ok: dict[tuple[str, str], dt.datetime] = field(default_factory=dict)
    #: A sync that failed reports no items, which is not the same as listing
    #: none; this keeps the last figure a sync actually reported.
    last_items: dict[str, float] = field(default_factory=dict)
    sync: dict[tuple[str, str], float] = field(default_factory=dict)
    matched: dict[tuple[str, str], float] = field(default_factory=dict)
    enrich_titles: float = 0.0
    enrich_prices: float = 0.0
    enricher_ratings: dict[str, float] = field(default_factory=dict)
    enricher_seconds: dict[str, float] = field(default_factory=dict)
    enricher_errors: dict[str, float] = field(default_factory=dict)
    images: dict[str, float] = field(default_factory=dict)

    def add(self, run: Run, status: str, ended_at: dt.datetime) -> None:
        """Count one run that ended ``ended_at`` with ``status``."""
        pair = run.pair
        key = (*pair, status)
        self.runs[key] = self.runs.get(key, 0) + 1
        seconds = max(0.0, (ended_at - run.started_at).total_seconds())
        self.seconds[pair] = self.seconds.get(pair, 0.0) + seconds

        stats = run.stats
        previous = self.last.get(pair)
        if previous is None or ended_at >= previous.ended_at:
            self.last[pair] = _Last(status, ended_at, seconds, _count(stats.get("error_count")))
        if status == FetchStatus.OK.value:
            self.last_ok[pair] = max(ended_at, self.last_ok.get(pair, ended_at))

        if run.phase == FetchPhase.SYNC.value:
            self._add_sync(run.source, stats)
        elif run.phase == FetchPhase.ENRICH.value:
            self._add_enrich(run.source, stats)
        elif run.phase == FetchPhase.IMAGES.value:
            for result in IMAGE_RESULTS:
                _bump(self.images, result, _count(stats.get(result)))

    def _add_sync(self, source: str, stats: Mapping[str, Any]) -> None:
        if source == ALL:
            return
        if "items_seen" in stats:
            self.last_items[source] = _count(stats["items_seen"])
        for field_name, (figure, _help) in SYNC_FIGURES.items():
            _bump(self.sync, (source, figure), _count(stats.get(field_name)))
        for method, count in _mapping(stats.get("matched_by")).items():
            _bump(self.matched, (source, str(method)), _count(count))

    def _add_enrich(self, source: str, stats: Mapping[str, Any]) -> None:
        if source == IMDB_RUN:
            _bump(self.enricher_ratings, IMDB_RUN, _count(stats.get("written")))
            return
        if source != ALL:
            return
        self.enrich_titles += _count(stats.get("titles_seen"))
        self.enrich_prices += _count(stats.get("prices_found"))
        for name, target in (
            ("by_enricher", self.enricher_ratings),
            ("seconds_by_enricher", self.enricher_seconds),
            ("errors_by_enricher", self.enricher_errors),
        ):
            for enricher, value in _mapping(stats.get(name)).items():
                _bump(target, str(enricher), _count(value))

    def families(
        self,
        *,
        sources: Iterable[str],
        running: set[tuple[str, str]],
        last_start: dt.datetime | None,
    ) -> list[Family]:
        """The figures, every series that has ever existed included at its value."""
        sources = sorted(set(sources))
        pairs = sorted(
            {(phase, source) for phase, source, _ in self.runs}
            | {(FetchPhase.SYNC.value, source) for source in sources}
            | running
        )
        endings = sorted(set(ENDINGS) | {status for _, _, status in self.runs})

        runs = Family(
            f"{PREFIX}fetch_runs",
            "counter",
            "Plugin runs that have ended, by how they ended.",
            ("phase", "source", "status"),
        )
        seconds = Family(
            f"{PREFIX}fetch_run_seconds",
            "counter",
            "Seconds spent in plugin runs that have ended.",
            ("phase", "source"),
        )
        going = Family(
            f"{PREFIX}fetch_running",
            "gauge",
            "1 while a run of this plugin is going.",
            ("phase", "source"),
        )
        for pair in pairs:
            for status in endings:
                runs.set(self.runs.get((*pair, status), 0), *pair, status)
            seconds.set(self.seconds.get(pair, 0.0), *pair)
            going.set(1 if pair in running else 0, *pair)

        last_ok = Family(
            f"{PREFIX}fetch_last_run_ok",
            "gauge",
            "1 if the plugin's latest run ended well, 0 if it failed, crashed or was aborted.",
            ("phase", "source"),
        )
        last_seconds = Family(
            f"{PREFIX}fetch_last_run_duration_seconds",
            "gauge",
            "How long the plugin's latest run took.",
            ("phase", "source"),
        )
        last_end = Family(
            f"{PREFIX}fetch_last_run_end_timestamp_seconds",
            "gauge",
            "When the plugin's latest run ended, as a Unix time.",
            ("phase", "source"),
        )
        last_errors = Family(
            f"{PREFIX}fetch_last_run_errors",
            "gauge",
            "Errors the plugin's latest run reported.",
            ("phase", "source"),
        )
        for pair, last in sorted(self.last.items()):
            last_ok.set(1 if last.status == FetchStatus.OK.value else 0, *pair)
            last_seconds.set(last.seconds, *pair)
            last_end.set(last.ended_at.timestamp(), *pair)
            last_errors.set(last.errors, *pair)

        last_success = Family(
            f"{PREFIX}fetch_last_success_timestamp_seconds",
            "gauge",
            "When the plugin last finished a run well, as a Unix time.",
            ("phase", "source"),
        )
        for pair, moment in sorted(self.last_ok.items()):
            last_success.set(moment.timestamp(), *pair)

        started = Family(
            f"{PREFIX}fetch_last_start_timestamp_seconds",
            "gauge",
            "When any fetcher last started a run, as a Unix time.",
        )
        if last_start is not None:
            started.set(last_start.timestamp())

        return [
            runs,
            seconds,
            going,
            last_ok,
            last_seconds,
            last_end,
            last_errors,
            last_success,
            started,
            *self._sync_families(sources),
            *self._enrich_families(),
        ]

    def _sync_families(self, sources: list[str]) -> list[Family]:
        synced = sorted(set(sources) | {source for source, _ in self.sync})
        families = []
        for figure, help_text in SYNC_FIGURES.values():
            family = Family(f"{PREFIX}sync_{figure}", "counter", help_text, ("source",))
            for source in synced:
                family.set(self.sync.get((source, figure), 0.0), source)
            families.append(family)

        items = Family(
            f"{PREFIX}sync_last_items_seen",
            "gauge",
            "Items the source listed in the latest sync that reported a count.",
            ("source",),
        )
        for source, count in sorted(self.last_items.items()):
            items.set(count, source)

        matched = Family(
            f"{PREFIX}sync_matched",
            "counter",
            "Listings matched to a catalog title, by how they were matched.",
            ("source", "method"),
        )
        methods = sorted({method for _, method in self.matched})
        for source in sorted({source for source, _ in self.matched}):
            for method in methods:
                matched.set(self.matched.get((source, method), 0.0), source, method)
        return [*families, items, matched]

    def _enrich_families(self) -> list[Family]:
        titles = Family(
            f"{PREFIX}enrich_titles",
            "counter",
            "Titles put through the enrichers.",
        )
        titles.set(self.enrich_titles)
        prices = Family(
            f"{PREFIX}enrich_prices_found",
            "counter",
            "Rental and purchase prices the enrichers found.",
        )
        prices.set(self.enrich_prices)

        enrichers = sorted(
            set(self.enricher_ratings) | set(self.enricher_seconds) | set(self.enricher_errors)
        )
        per_enricher = []
        for name, values, help_text in (
            ("ratings_found", self.enricher_ratings, "Ratings an enricher found."),
            ("seconds", self.enricher_seconds, "Seconds an enricher spent looking titles up."),
            ("errors", self.enricher_errors, "Lookups an enricher failed."),
        ):
            family = Family(f"{PREFIX}enrich_{name}", "counter", help_text, ("enricher",))
            for enricher in enrichers:
                family.set(values.get(enricher, 0.0), enricher)
            per_enricher.append(family)

        images = Family(
            f"{PREFIX}images",
            "counter",
            "Posters the artwork pass handled, by what came of each.",
            ("result",),
        )
        for result in IMAGE_RESULTS:
            images.set(self.images.get(result, 0.0), result)

        return [titles, prices, *per_enricher, images]


def _count(value: Any) -> float:
    """A tally figure as a number, or 0 for anything that is not one.

    The stats column is whatever a fetcher sent, from any version of it, so
    nothing in it is trusted to be the type it usually is. A negative count is
    not a count either, and would make a counter go down.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0.0
    return max(0.0, float(value))


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _bump[K](target: dict[K, float], key: K, amount: float) -> None:
    target[key] = target.get(key, 0.0) + amount
