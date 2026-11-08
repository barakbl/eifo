"""Write the recorded runs as Prometheus history, for importing once.

Prometheus knows only what it has scraped, and the database remembers every run
since the first one. This writes the run figures (:mod:`eifo_api.metrics.runs`)
as they stood at every hour from the first run until now, in the OpenMetrics
format promtool imports. A new Prometheus then starts with the months before it
existed:

    python -m eifo_api.metrics.history > history.om
    promtool tsdb create-blocks-from openmetrics history.om /prometheus

The values come from the same fold the live endpoint uses, so the imported
series continue into the scraped ones without a step at the join.

Only the run figures are written. They are folds over rows that carry their
own times. The catalog's figures count what is there now, and nothing records
what was there in August.
"""

from __future__ import annotations

import argparse
import datetime as dt
import math
import sys
from array import array
from collections.abc import Iterable, Mapping, Sequence
from typing import TextIO

from eifo_api.metrics import Family
from eifo_api.metrics.runs import (
    Run,
    Tally,
    active_sources,
    ended_by,
    last_start_at,
    load_runs,
    running_at,
)
from eifo_core.db import create_engine_from_settings, make_session_factory
from eifo_core.settings import get_settings
from eifo_core.types import utcnow

#: One point an hour. Runs are nightly; finer than this only makes the file
#: longer, and dashboards read history over days.
DEFAULT_STEP = dt.timedelta(hours=1)

#: The labels Prometheus gives everything it scrapes from the API, by the job
#: and target in monitoring/prometheus/prometheus.yml. The history must carry
#: them too, or it is a different series from the one being scraped, and every
#: figure is drawn twice where the two meet.
SCRAPE_LABELS = {"job": "eifo", "instance": "api:3436"}


def steps(runs: Sequence[Run], until: dt.datetime, step: dt.timedelta) -> list[dt.datetime]:
    """The moments to write: every ``step`` from the first run, then ``until``."""
    if not runs:
        return []
    first = min(run.started_at for run in runs)
    seconds = int(step.total_seconds())
    start = dt.datetime.fromtimestamp(int(first.timestamp()) // seconds * seconds, dt.UTC)
    moments = []
    moment = start
    while moment < until:
        moments.append(moment)
        moment += step
    moments.append(until)
    return moments


def write_history(
    runs: Sequence[Run],
    out: TextIO,
    *,
    sources: Iterable[str],
    until: dt.datetime,
    step: dt.timedelta = DEFAULT_STEP,
    labels: Mapping[str, str] | None = None,
) -> int:
    """Write the run figures at each moment up to ``until``; returns the sample count.

    ``labels`` go on every series: the ones the scrape adds (:data:`SCRAPE_LABELS`).
    """
    sources = list(sources)
    constant = dict(SCRAPE_LABELS if labels is None else labels)
    moments = steps(runs, until, step)
    ended = ended_by(runs, until)
    tally = Tally()
    position = 0

    # Families in the order they are first seen, and for each a column of
    # values per series: NaN where the series did not exist yet.
    meta: dict[str, Family] = {}
    columns: dict[str, dict[tuple[str, ...], array[float]]] = {}
    for index, moment in enumerate(moments):
        while position < len(ended) and ended[position][2] <= moment:
            tally.add(*ended[position])
            position += 1
        families = tally.families(
            sources=sources,
            running=running_at(runs, moment),
            last_start=last_start_at(runs, moment),
        )
        for family in families:
            meta.setdefault(family.name, family)
            series = columns.setdefault(family.name, {})
            for values, value in family.samples.items():
                column = series.get(values)
                if column is None:
                    column = series[values] = array("d", [math.nan]) * len(moments)
                column[index] = value

    written = 0
    stamps = [_seconds(moment) for moment in moments]
    for name, family in meta.items():
        out.write(f"# HELP {name} {_escape_help(family.help)}\n")
        out.write(f"# TYPE {name} {family.kind}\n")
        sample = f"{name}_total" if family.kind == "counter" else name
        for values, column in sorted(columns[name].items()):
            selector = _selector((*constant.keys(), *family.labels), (*constant.values(), *values))
            for value, stamp in zip(column, stamps, strict=True):
                if math.isnan(value):
                    continue
                out.write(f"{sample}{selector} {value!r} {stamp}\n")
                written += 1
    out.write("# EOF\n")
    return written


def _seconds(moment: dt.datetime) -> int:
    return int(moment.timestamp())


def _selector(names: tuple[str, ...], values: tuple[str, ...]) -> str:
    if not names:
        return ""
    pairs = ",".join(
        f'{name}="{_escape_label(value)}"' for name, value in zip(names, values, strict=True)
    )
    return "{" + pairs + "}"


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _escape_help(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m eifo_api.metrics.history",
        description="Write recorded plugin runs as OpenMetrics, for promtool to import.",
    )
    parser.add_argument(
        "--label",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="a label for every series, replacing the defaults "
        f"({', '.join(f'{k}={v}' for k, v in SCRAPE_LABELS.items())}); repeatable",
    )
    parser.add_argument(
        "--step-minutes",
        type=int,
        default=int(DEFAULT_STEP.total_seconds() // 60),
        help="minutes between points (default: 60)",
    )
    args = parser.parse_args(argv)
    if args.step_minutes < 1:
        parser.error("--step-minutes must be at least 1")
    labels = dict(SCRAPE_LABELS)
    if args.label:
        try:
            labels = dict(pair.split("=", 1) for pair in args.label)
        except ValueError:
            parser.error("--label takes NAME=VALUE")

    settings = get_settings()
    engine = create_engine_from_settings(settings)
    try:
        with make_session_factory(engine)() as session:
            runs = load_runs(session)
            sources = active_sources(session)
        written = write_history(
            runs,
            sys.stdout,
            sources=sources,
            until=utcnow(),
            step=dt.timedelta(minutes=args.step_minutes),
            labels=labels,
        )
    finally:
        engine.dispose()
    print(f"wrote {written} samples from {len(runs)} runs", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
