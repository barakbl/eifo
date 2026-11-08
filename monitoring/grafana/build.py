"""Write the Grafana dashboards and alert rules from this one file.

    python3 monitoring/grafana/build.py

Grafana's JSON is long, repetitive and easy to get subtly wrong by hand, so
the dashboards are described here and written out to ``dashboards/``, along
with the alert rules in ``provisioning/alerting/``. The written files are
committed (Grafana reads them, not this), and a test checks they match what
this script writes now, and that every eifo_* figure they draw is one the API
actually serves.

Standard library only: it runs wherever Python 3.12 does.

Two habits in the queries:

* Counts over a window are the counter now minus the counter then, not
  increase(): these counters never reset and move one run at a time, and
  increase() extrapolates a nightly run into fractions of one.
* A series that did not exist at the start of the window counts from zero
  (``or ... * 0``), so a plugin's first-ever failure is not lost.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
DASHBOARDS = HERE / "dashboards"
ALERTING = HERE / "provisioning" / "alerting" / "eifo.yml"

DATASOURCE = {"type": "prometheus", "uid": "prometheus"}
SCHEMA_VERSION = 41
WIDTH = 24

GREEN, ORANGE, RED, BLUE, GREY = "green", "orange", "red", "blue", "text"


# -- queries -----------------------------------------------------------------


def grew(selector: str, window: str, by: str = "") -> str:
    """How much a counter rose over ``window``, exactly, grouped ``by``."""
    group = f" by ({by})" if by else ""
    return (
        f"(sum{group} ({selector}) - sum{group} (({selector} offset {window}) or ({selector} * 0)))"
    )


def named(expr: str) -> str:
    """``expr`` with each source's display name as a ``name`` label.

    Runs that belong to no source (a whole enrich pass, the imdb load) have no
    entry in eifo_source_info; they keep their key as their name.
    """
    return (
        f"(({expr}) * on (source) group_left (name) eifo_source_info)"
        f' or on (phase, source) label_replace({expr}, "name", "$1", "source", "(.*)")'
    )


def per_plugin(expr: str) -> str:
    """One row per (phase, source), named: what every column of the table needs."""
    return f"max by (phase, source, name) ({named(expr)})"


def as_sync(expr: str) -> str:
    """A per-source catalog figure, labelled as the sync plugin's own row."""
    return f'label_replace({expr}, "phase", "sync", "", "")'


RUNS = "eifo_fetch_runs_total"
FAILED = f'{RUNS}{{status!="ok"}}'


# -- panels ------------------------------------------------------------------


@dataclass
class Target:
    expr: str
    legend: str = ""
    instant: bool = False
    table: bool = False
    interval: str = ""

    def json(self, ref: str) -> dict[str, Any]:
        out: dict[str, Any] = {
            "refId": ref,
            "datasource": DATASOURCE,
            "expr": self.expr,
            "legendFormat": self.legend or "__auto",
            "instant": self.instant,
            "range": not self.instant,
            "format": "table" if self.table else "time_series",
            "editorMode": "code",
        }
        if self.interval:
            out["interval"] = self.interval
        return out


@dataclass
class Panel:
    kind: str
    title: str
    targets: list[Target]
    width: int = 12
    height: int = 8
    description: str = ""
    unit: str = ""
    decimals: int | None = None
    minimum: float | None = None
    maximum: float | None = None
    thresholds: list[tuple[str, float | None]] = field(default_factory=lambda: [(GREEN, None)])
    color_mode: str = "palette-classic"
    mappings: list[dict[str, Any]] = field(default_factory=list)
    no_value: str = ""
    options: dict[str, Any] = field(default_factory=dict)
    custom: dict[str, Any] = field(default_factory=dict)
    overrides: list[dict[str, Any]] = field(default_factory=list)
    transformations: list[dict[str, Any]] = field(default_factory=list)

    def json(self, panel_id: int, x: int, y: int) -> dict[str, Any]:
        defaults: dict[str, Any] = {
            "color": {"mode": self.color_mode},
            "thresholds": {
                "mode": "absolute",
                "steps": [{"color": color, "value": value} for color, value in self.thresholds],
            },
            "mappings": self.mappings,
        }
        if self.unit:
            defaults["unit"] = self.unit
        if self.decimals is not None:
            defaults["decimals"] = self.decimals
        if self.minimum is not None:
            defaults["min"] = self.minimum
        if self.maximum is not None:
            defaults["max"] = self.maximum
        if self.no_value:
            defaults["noValue"] = self.no_value
        if self.custom:
            defaults["custom"] = self.custom
        refs = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        out: dict[str, Any] = {
            "id": panel_id,
            "type": self.kind,
            "title": self.title,
            "description": self.description,
            "datasource": DATASOURCE,
            "gridPos": {"x": x, "y": y, "w": self.width, "h": self.height},
            "targets": [target.json(refs[index]) for index, target in enumerate(self.targets)],
            "fieldConfig": {"defaults": defaults, "overrides": self.overrides},
            "options": self.options,
        }
        if self.transformations:
            out["transformations"] = self.transformations
        return out


def stat(
    title: str,
    expr: str,
    *,
    unit: str = "short",
    thresholds: list[tuple[str, float | None]] | None = None,
    description: str = "",
    width: int = 4,
    no_value: str = "",
    decimals: int | None = None,
    legend: str = "",
    show: str = "auto",
) -> Panel:
    return Panel(
        "stat",
        title,
        [Target(expr, legend=legend, instant=True)],
        width=width,
        height=4,
        description=description,
        unit=unit,
        decimals=decimals,
        thresholds=thresholds or [(BLUE, None)],
        color_mode="thresholds",
        no_value=no_value,
        options={
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "colorMode": "background",
            "graphMode": "none",
            "textMode": show,
            "justifyMode": "auto",
            "orientation": "auto",
            "wideLayout": True,
        },
    )


def series(
    title: str,
    targets: list[Target],
    *,
    unit: str = "short",
    bars: bool = False,
    stacked: bool = False,
    description: str = "",
    width: int = 12,
    height: int = 8,
    minimum: float | None = 0,
    legend_right: bool = False,
) -> Panel:
    return Panel(
        "timeseries",
        title,
        targets,
        width=width,
        height=height,
        description=description,
        unit=unit,
        minimum=minimum,
        custom={
            "drawStyle": "bars" if bars else "line",
            "lineWidth": 1,
            "fillOpacity": 70 if bars else 10,
            "showPoints": "never",
            "spanNulls": False,
            "stacking": {"mode": "normal" if stacked else "none", "group": "A"},
            "axisSoftMin": 0,
            "barAlignment": -1,
        },
        options={
            "legend": {
                "displayMode": "table" if legend_right else "list",
                "placement": "right" if legend_right else "bottom",
                "calcs": ["lastNotNull", "max"] if legend_right else [],
                "showLegend": True,
            },
            "tooltip": {"mode": "multi", "sort": "desc"},
        },
    )


def bars(
    title: str,
    expr: str,
    legend: str,
    *,
    unit: str = "short",
    description: str = "",
    width: int = 12,
    height: int = 8,
    maximum: float | None = None,
) -> Panel:
    return Panel(
        "bargauge",
        title,
        [Target(expr, legend=legend, instant=True)],
        width=width,
        height=height,
        description=description,
        unit=unit,
        minimum=0,
        maximum=maximum,
        thresholds=[(BLUE, None)],
        color_mode="thresholds",
        options={
            "orientation": "horizontal",
            "displayMode": "basic",
            "valueMode": "color",
            "showUnfilled": True,
            "namePlacement": "left",
            "sizing": "auto",
            "minVizHeight": 10,
            "maxVizHeight": 24,
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
        },
    )


def column(name: str, *properties: tuple[str, Any]) -> dict[str, Any]:
    """A table column's settings, by the name the column ends up with."""
    return {
        "matcher": {"id": "byName", "options": name},
        "properties": [{"id": key, "value": value} for key, value in properties],
    }


def table(
    title: str,
    columns: list[tuple[str, str]],
    *,
    overrides: list[dict[str, Any]],
    sort_by: str,
    description: str = "",
    width: int = 24,
    height: int = 12,
    labels: dict[str, str] | None = None,
    hide: list[str] | None = None,
) -> Panel:
    """A table of instant queries, one column each, joined on their labels."""
    labels = labels or {}
    rename = {f"Value #{chr(65 + index)}": name for index, (name, _expr) in enumerate(columns)}
    if len(columns) == 1:
        # Grafana leaves a lone query's column unlettered.
        rename = {"Value": columns[0][0]}
    values = list(rename)
    rename.update(labels)
    # By the names the fields arrive with: Grafana orders before it renames.
    order = {field: index for index, field in enumerate([*labels, *values])}
    return Panel(
        "table",
        title,
        [Target(expr, instant=True, table=True) for _name, expr in columns],
        width=width,
        height=height,
        description=description,
        overrides=overrides,
        custom={"align": "auto", "cellOptions": {"type": "auto"}, "inspect": False, "minWidth": 70},
        options={
            "showHeader": True,
            "cellHeight": "sm",
            "footer": {"show": False, "reducer": ["sum"], "fields": ""},
            "sortBy": [{"displayName": sort_by, "desc": False}],
        },
        transformations=[
            {"id": "merge", "options": {}},
            {
                "id": "organize",
                "options": {
                    "excludeByName": {"Time": True, **dict.fromkeys(hide or [], True)},
                    "renameByName": rename,
                    "indexByName": order,
                },
            },
        ],
    )


def timeline(title: str, expr: str, legend: str, *, description: str = "") -> Panel:
    return Panel(
        "state-timeline",
        title,
        [Target(expr, legend=legend)],
        width=24,
        height=10,
        description=description,
        color_mode="thresholds",
        thresholds=[("transparent", None), (BLUE, 1)],
        mappings=[
            {
                "type": "value",
                "options": {
                    "0": {"text": "idle", "color": "transparent", "index": 0},
                    "1": {"text": "running", "color": BLUE, "index": 1},
                },
            }
        ],
        options={
            "mergeValues": True,
            "showValue": "never",
            "rowHeight": 0.8,
            "alignValue": "left",
            "legend": {"showLegend": False, "displayMode": "list", "placement": "bottom"},
            "tooltip": {"mode": "single", "sort": "none"},
        },
    )


def row(title: str) -> Panel:
    return Panel("row", title, [], width=WIDTH, height=1)


# -- dashboards ----------------------------------------------------------------


@dataclass
class Dashboard:
    uid: str
    title: str
    description: str
    panels: list[Panel]
    time_from: str = "now-30d"

    def json(self) -> dict[str, Any]:
        placed = []
        x = y = row_height = 0
        for index, panel in enumerate(self.panels, 1):
            if panel.kind == "row" or x + panel.width > WIDTH:
                y += row_height
                x = row_height = 0
            body = panel.json(index, x, y)
            if panel.kind == "row":
                body = {
                    "id": index,
                    "type": "row",
                    "title": panel.title,
                    "collapsed": False,
                    "gridPos": {"x": 0, "y": y, "w": WIDTH, "h": 1},
                    "panels": [],
                }
                y += 1
            else:
                x += panel.width
                row_height = max(row_height, panel.height)
            placed.append(body)
        return {
            "uid": self.uid,
            "title": self.title,
            "description": self.description,
            "tags": ["eifo"],
            "timezone": "browser",
            "editable": True,
            "graphTooltip": 1,
            "schemaVersion": SCHEMA_VERSION,
            "version": 1,
            "refresh": "1m",
            "time": {"from": self.time_from, "to": "now"},
            "fiscalYearStartMonth": 0,
            "liveNow": False,
            "weekStart": "",
            "templating": {"list": []},
            "annotations": {"list": []},
            "links": [
                {
                    "title": "Eifo dashboards",
                    "type": "dashboards",
                    "tags": ["eifo"],
                    "asDropdown": True,
                    "includeVars": False,
                    "keepTime": True,
                    "icon": "external link",
                    "targetBlank": False,
                    "tooltip": "",
                    "url": "",
                }
            ],
            "panels": placed,
        }


OK_OR_FAILED = [
    {
        "type": "value",
        "options": {
            "0": {"text": "Failed", "color": RED, "index": 0},
            "1": {"text": "OK", "color": GREEN, "index": 1},
        },
    }
]
COLOURED_CELL = ("custom.cellOptions", {"type": "color-background", "mode": "basic"})
BY_THRESHOLD = ("color", {"mode": "thresholds"})


def plugins() -> Dashboard:
    window = "$__range"
    return Dashboard(
        "eifo-plugins",
        "Eifo · Plugins",
        "Every plugin run, from whichever machine ran it: how it ended, how long it "
        "took, and what each service holds.",
        [
            stat(
                "Plugins failing",
                "count(eifo_fetch_last_run_ok == 0) or vector(0)",
                thresholds=[(GREEN, None), (RED, 1)],
                description="Plugins whose latest run failed, crashed or was aborted.",
            ),
            stat(
                "Failed runs",
                f"sum({grew(FAILED, window)}) / sum({grew(RUNS, window)})",
                unit="percentunit",
                decimals=1,
                thresholds=[(GREEN, None), (ORANGE, 0.1), (RED, 0.25)],
                no_value="no runs",
                description="Share of the runs in the selected time range that did not end well.",
            ),
            stat(
                "Runs",
                f"sum({grew(RUNS, window)})",
                description="Plugin runs that ended in the selected time range.",
            ),
            stat(
                "Since the fetcher last ran",
                "time() - max(eifo_fetch_last_start_timestamp_seconds)",
                unit="dtdurations",
                thresholds=[(GREEN, None), (ORANGE, 36 * 3600), (RED, 72 * 3600)],
                description="Time since any fetcher, on any machine, started a run.",
            ),
            stat("Movies", 'max(eifo_titles{type="movie"})'),
            stat("Series", 'max(eifo_titles{type="series"})'),
            table(
                "Plugins",
                [
                    ("Latest run", per_plugin("eifo_fetch_last_run_ok")),
                    (
                        "Ended",
                        per_plugin("eifo_fetch_last_run_end_timestamp_seconds * 1000"),
                    ),
                    ("Took", per_plugin("eifo_fetch_last_run_duration_seconds")),
                    ("Errors", per_plugin("eifo_fetch_last_run_errors")),
                    (
                        "Failed in range",
                        per_plugin(
                            f"{grew(FAILED, window, 'phase, source')}"
                            f" / ({grew(RUNS, window, 'phase, source')} > 0)"
                        ),
                    ),
                    (
                        "Average run",
                        per_plugin(
                            f"{grew('eifo_fetch_run_seconds_total', window, 'phase, source')}"
                            f" / ({grew(RUNS, window, 'phase, source')} > 0)"
                        ),
                    ),
                    ("Items seen", per_plugin(as_sync("eifo_sync_last_items_seen"))),
                    (
                        "Movies",
                        per_plugin(as_sync('sum by (source) (eifo_source_titles{type="movie"})')),
                    ),
                    (
                        "Series",
                        per_plugin(as_sync('sum by (source) (eifo_source_titles{type="series"})')),
                    ),
                ],
                labels={"name": "Plugin", "phase": "Phase"},
                hide=["source"],
                sort_by="Plugin",
                height=14,
                description="One row per plugin. 'Failed in range' and 'Average run' cover "
                "the selected time range; the rest describe the latest run.",
                overrides=[
                    column("Latest run", ("mappings", OK_OR_FAILED), COLOURED_CELL, BY_THRESHOLD),
                    column("Plugin", ("custom.width", 220)),
                    column("Phase", ("custom.width", 80)),
                    column("Ended", ("unit", "dateTimeFromNow")),
                    column("Took", ("unit", "dtdurations")),
                    column("Average run", ("unit", "dtdurations")),
                    column(
                        "Failed in range",
                        ("unit", "percentunit"),
                        ("decimals", 0),
                        COLOURED_CELL,
                        BY_THRESHOLD,
                        (
                            "thresholds",
                            {
                                "mode": "absolute",
                                "steps": [
                                    {"color": "transparent", "value": None},
                                    {"color": ORANGE, "value": 0.01},
                                    {"color": RED, "value": 0.25},
                                ],
                            },
                        ),
                    ),
                    column(
                        "Errors",
                        COLOURED_CELL,
                        BY_THRESHOLD,
                        (
                            "thresholds",
                            {
                                "mode": "absolute",
                                "steps": [
                                    {"color": "transparent", "value": None},
                                    {"color": ORANGE, "value": 1},
                                ],
                            },
                        ),
                    ),
                ],
            ),
            row("Over time"),
            series(
                "Runs that did not end well, per day",
                [Target(grew(FAILED, "1d", "source"), "{{source}}", interval="1d")],
                bars=True,
                stacked=True,
                description="Failed, crashed and aborted runs, by plugin.",
            ),
            series(
                "Share of runs that failed, past 7 days",
                [
                    Target(
                        f"sum({grew(FAILED, '7d')}) / (sum({grew(RUNS, '7d')}) > 0)",
                        "failed",
                    )
                ],
                unit="percentunit",
                description="A rolling week, so one bad night shows without dominating.",
            ),
            series(
                "How long each plugin's latest run took",
                [
                    Target(
                        "max by (phase, source) (eifo_fetch_last_run_duration_seconds)",
                        "{{source}} ({{phase}})",
                    )
                ],
                unit="s",
                height=10,
                legend_right=True,
            ),
            series(
                "Time spent per plugin, per day",
                [
                    Target(
                        grew("eifo_fetch_run_seconds_total", "1d", "source"),
                        "{{source}}",
                        interval="1d",
                    )
                ],
                unit="s",
                bars=True,
                stacked=True,
                height=10,
            ),
            series(
                "Titles per service",
                [Target("sum by (source) (eifo_source_titles)", "{{source}}")],
                height=10,
                legend_right=True,
                description="Movies and series each service offers now.",
            ),
            series(
                "Items each sync listed",
                [Target("max by (source) (eifo_sync_last_items_seen)", "{{source}}")],
                height=10,
                legend_right=True,
                description="A sudden drop usually means the source changed its pages.",
            ),
            timeline(
                "When each plugin ran",
                "max by (phase, source) (eifo_fetch_running)",
                "{{source}} ({{phase}})",
            ),
        ],
    )


def enrichment() -> Dashboard:
    return Dashboard(
        "eifo-enrichment",
        "Eifo · Enrichment & matching",
        "Ratings, posters and how listings are matched to titles.",
        [
            stat(
                "Titles with a score",
                "sum(eifo_titles_scored) / sum(eifo_titles)",
                unit="percentunit",
                decimals=1,
            ),
            stat(
                "Due for enrichment",
                "max(eifo_enrich_due)",
                description="Never looked up, or due to be looked up again.",
            ),
            stat(
                "Waiting for review",
                "sum(eifo_review_queue)",
                thresholds=[(GREEN, None), (ORANGE, 1), (RED, 50)],
                description="Listings the matcher could not place on its own.",
            ),
            stat(
                "Missing a poster",
                "sum(eifo_titles_missing_poster)",
                thresholds=[(GREEN, None), (ORANGE, 1)],
            ),
            stat(
                "Ratings found",
                f"sum({grew('eifo_enrich_ratings_found_total', '$__range')})",
                description="In the selected time range.",
            ),
            stat(
                "Lookups that failed",
                f"sum({grew('eifo_enrich_errors_total', '$__range')})",
                thresholds=[(GREEN, None), (ORANGE, 1)],
                description="In the selected time range. Counted by fetchers from this version on.",
            ),
            bars(
                "Titles each provider has rated",
                "max by (provider) (eifo_title_ratings) / scalar(sum(eifo_titles))",
                "{{provider}}",
                unit="percentunit",
                maximum=1,
            ),
            bars(
                "Where titles stand with the enrichers",
                "max by (outcome) (eifo_enrich_attempts)",
                "{{outcome}}",
                description="By their latest lookup: ok found ratings, no_data found none, "
                "no_match could not identify the title, error met a provider failing.",
            ),
            series(
                "Ratings found per day, by enricher",
                [
                    Target(
                        grew("eifo_enrich_ratings_found_total", "1d", "enricher"),
                        "{{enricher}}",
                        interval="1d",
                    )
                ],
                bars=True,
                stacked=True,
            ),
            series(
                "Time spent per day, by enricher",
                [
                    Target(
                        grew("eifo_enrich_seconds_total", "1d", "enricher"),
                        "{{enricher}}",
                        interval="1d",
                    )
                ],
                unit="s",
                bars=True,
                stacked=True,
            ),
            series(
                "Failed lookups per day, by enricher",
                [
                    Target(
                        grew("eifo_enrich_errors_total", "1d", "enricher"),
                        "{{enricher}}",
                        interval="1d",
                    )
                ],
                bars=True,
                stacked=True,
            ),
            series(
                "Posters per day",
                [
                    Target(
                        grew("eifo_images_total", "1d", "result"),
                        "{{result}}",
                        interval="1d",
                    )
                ],
                bars=True,
                stacked=True,
            ),
            row("Matching"),
            series(
                "How listings were matched, per day",
                [
                    Target(
                        grew("eifo_sync_matched_total", "1d", "method"),
                        "{{method}}",
                        interval="1d",
                    )
                ],
                bars=True,
                stacked=True,
                description="external_id and alias are certain; tmdb and fuzzy are "
                "inferred; review waited for a person.",
            ),
            series(
                "Offers found and lost per day",
                [
                    Target(grew("eifo_sync_offers_added_total", "1d"), "added", interval="1d"),
                    Target(grew("eifo_sync_offers_retired_total", "1d"), "retired", interval="1d"),
                    Target(
                        grew("eifo_sync_titles_created_total", "1d"),
                        "new titles",
                        interval="1d",
                    ),
                ],
                bars=True,
            ),
            bars(
                "Waiting for review, by service",
                "max by (source) (eifo_review_queue) > 0",
                "{{source}}",
            ),
            table(
                "How each service's listings were matched, in range",
                [
                    (
                        "Listings",
                        f"{grew('eifo_sync_matched_total', '$__range', 'source, method')} > 0",
                    )
                ],
                labels={"source": "Service", "method": "Method"},
                sort_by="Service",
                width=12,
                height=8,
                overrides=[],
            ),
        ],
    )


def traffic() -> Dashboard:
    # The site's own traffic: not files, and not the monitoring stack asking
    # for metrics or checking a Grafana visitor at the gate.
    api = 'route!~"static|/api/v1/metrics|/api/v1/monitoring/gate"'
    # Response times leave out the fetcher's uploads too: a chunk of listings
    # takes seconds to match by design, and would drown out what people wait for.
    served = 'route!~"static|/api/v1/metrics|/api/v1/monitoring/gate|/api/v1/ingest/.*"'
    return Dashboard(
        "eifo-traffic",
        "Eifo · API traffic",
        "Requests the API served, how fast, and how many went wrong.",
        [
            stat(
                "Requests per second",
                f"sum(rate(eifo_http_requests_total{{{api}}}[5m]))",
                unit="reqps",
                decimals=2,
            ),
            stat(
                "Server errors",
                f'sum(rate(eifo_http_requests_total{{{api},status="5xx"}}[1h]))'
                f" / sum(rate(eifo_http_requests_total{{{api}}}[1h]))",
                unit="percentunit",
                decimals=2,
                thresholds=[(GREEN, None), (ORANGE, 0.01), (RED, 0.05)],
                no_value="none",
                description="Share of API requests in the past hour that ended in a 5xx.",
            ),
            stat(
                "Slowest 5%",
                "histogram_quantile(0.95, sum by (le) "
                f"(rate(eifo_http_request_duration_seconds_bucket{{{served}}}[1h])))",
                unit="s",
                thresholds=[(GREEN, None), (ORANGE, 0.5), (RED, 2)],
                description="95% of API requests in the past hour answered faster than this. "
                "Leaves out the fetcher's uploads.",
            ),
            stat(
                "API memory",
                'process_resident_memory_bytes{job="eifo"}',
                unit="bytes",
            ),
            stat(
                "Reading the metrics took",
                "max(eifo_metrics_collect_seconds)",
                unit="s",
                thresholds=[(GREEN, None), (ORANGE, 1), (RED, 5)],
                description="What one scrape costs the database.",
            ),
            stat(
                "Version", "max by (version) (eifo_build_info)", legend="{{version}}", show="name"
            ),
            series(
                "Requests per second, busiest routes",
                [
                    Target(
                        "topk(10, sum by (route) "
                        f"(rate(eifo_http_requests_total{{{api}}}[$__rate_interval])))",
                        "{{route}}",
                    )
                ],
                unit="reqps",
                width=24,
                height=10,
                legend_right=True,
            ),
            series(
                "Errors per second, by route",
                [
                    Target(
                        "sum by (route, status) (rate(eifo_http_requests_total"
                        f'{{{api},status=~"4xx|5xx"}}[$__rate_interval])) > 0',
                        "{{route}} {{status}}",
                    )
                ],
                unit="reqps",
                description="4xx includes requests the rate limiter turned away.",
            ),
            series(
                "Response time",
                [
                    Target(
                        f"histogram_quantile({quantile}, sum by (le) "
                        f"(rate(eifo_http_request_duration_seconds_bucket{{{served}}}"
                        "[$__rate_interval])))",
                        label,
                    )
                    for quantile, label in (("0.5", "median"), ("0.95", "95%"), ("0.99", "99%"))
                ],
                unit="s",
            ),
            series(
                "API process",
                [
                    Target('process_resident_memory_bytes{job="eifo"}', "memory"),
                ],
                unit="bytes",
            ),
            series(
                "API CPU",
                [Target('rate(process_cpu_seconds_total{job="eifo"}[$__rate_interval])', "cpu")],
                unit="percentunit",
            ),
        ],
        time_from="now-24h",
    )


def server() -> Dashboard:
    container = 'name!=""'
    return Dashboard(
        "eifo-server",
        "Eifo · Server & containers",
        "The machine both sites run on, and each container on it.",
        [
            stat(
                "CPU",
                '1 - avg(rate(node_cpu_seconds_total{mode="idle"}[5m]))',
                unit="percentunit",
                decimals=1,
                thresholds=[(GREEN, None), (ORANGE, 0.7), (RED, 0.9)],
            ),
            stat("Cores", 'count(node_cpu_seconds_total{mode="idle"})'),
            stat(
                "Memory used",
                "1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes",
                unit="percentunit",
                decimals=1,
                thresholds=[(GREEN, None), (ORANGE, 0.8), (RED, 0.9)],
                description="Excludes the disk cache, which the system frees when needed.",
            ),
            stat(
                "Swap used",
                "node_memory_SwapTotal_bytes - node_memory_SwapFree_bytes",
                unit="bytes",
                thresholds=[(GREEN, None), (ORANGE, 1)],
                description="0 when there is no swap at all.",
            ),
            stat(
                "Disk used",
                '1 - node_filesystem_avail_bytes{mountpoint="/"}'
                ' / node_filesystem_size_bytes{mountpoint="/"}',
                unit="percentunit",
                decimals=1,
                thresholds=[(GREEN, None), (ORANGE, 0.75), (RED, 0.9)],
            ),
            stat(
                "Up for",
                "time() - node_boot_time_seconds",
                unit="dtdurations",
            ),
            series(
                "CPU",
                [
                    Target(
                        "sum by (mode) (rate(node_cpu_seconds_total"
                        '{mode!="idle"}[$__rate_interval]))'
                        ' / scalar(count(node_cpu_seconds_total{mode="idle"}))',
                        "{{mode}}",
                    )
                ],
                unit="percentunit",
                stacked=True,
            ),
            series(
                "Memory",
                [
                    Target(
                        "node_memory_MemTotal_bytes - node_memory_MemAvailable_bytes",
                        "used",
                    ),
                    Target("node_memory_Cached_bytes + node_memory_Buffers_bytes", "cache"),
                    Target("node_memory_MemAvailable_bytes", "available"),
                ],
                unit="bytes",
            ),
            series(
                "Load",
                [
                    Target("node_load1", "1 min"),
                    Target("node_load5", "5 min"),
                    Target("node_load15", "15 min"),
                ],
            ),
            series(
                "Disk",
                [
                    Target(
                        'node_filesystem_size_bytes{mountpoint="/"}'
                        ' - node_filesystem_avail_bytes{mountpoint="/"}',
                        "used",
                    ),
                    Target('node_filesystem_size_bytes{mountpoint="/"}', "size"),
                ],
                unit="bytes",
            ),
            row("Containers"),
            series(
                "CPU by container",
                [
                    Target(
                        "sum by (name) (rate(container_cpu_usage_seconds_total"
                        f"{{{container}}}[$__rate_interval]))",
                        "{{name}}",
                    )
                ],
                unit="percentunit",
                legend_right=True,
                width=24,
            ),
            series(
                "Memory by container",
                [
                    Target(
                        f"sum by (name) (container_memory_working_set_bytes{{{container}}})",
                        "{{name}}",
                    )
                ],
                unit="bytes",
                legend_right=True,
                width=24,
            ),
            series(
                "Network received by container",
                [
                    Target(
                        "sum by (name) (rate(container_network_receive_bytes_total"
                        f"{{{container}}}[$__rate_interval]))",
                        "{{name}}",
                    )
                ],
                unit="Bps",
            ),
            series(
                "Network sent by container",
                [
                    Target(
                        "sum by (name) (rate(container_network_transmit_bytes_total"
                        f"{{{container}}}[$__rate_interval]))",
                        "{{name}}",
                    )
                ],
                unit="Bps",
            ),
            table(
                "Containers",
                [
                    (
                        "Up for",
                        f"max by (name) (time() - container_start_time_seconds{{{container}}})",
                    ),
                    (
                        "Memory",
                        f"max by (name) (container_memory_working_set_bytes{{{container}}})",
                    ),
                    (
                        "CPU",
                        "sum by (name) (rate(container_cpu_usage_seconds_total"
                        f"{{{container}}}[5m]))",
                    ),
                ],
                labels={"name": "Container"},
                sort_by="Container",
                height=9,
                overrides=[
                    column("Up for", ("unit", "dtdurations")),
                    column("Memory", ("unit", "bytes")),
                    column("CPU", ("unit", "percentunit"), ("decimals", 1)),
                ],
            ),
        ],
        time_from="now-24h",
    )


# -- alert rules -----------------------------------------------------------------


@dataclass
class Rule:
    uid: str
    title: str
    expr: str
    above: float
    summary: str
    wait: str = "0s"

    def json(self) -> dict[str, Any]:
        return {
            "uid": self.uid,
            "title": self.title,
            "condition": "C",
            "data": [
                {
                    "refId": "A",
                    "relativeTimeRange": {"from": 600, "to": 0},
                    "datasourceUid": DATASOURCE["uid"],
                    "model": {"refId": "A", "expr": self.expr, "instant": True},
                },
                {
                    "refId": "C",
                    "datasourceUid": "__expr__",
                    "model": {
                        "refId": "C",
                        "type": "threshold",
                        "expression": "A",
                        "conditions": [{"evaluator": {"type": "gt", "params": [self.above]}}],
                    },
                },
            ],
            "noDataState": "OK",
            "execErrState": "Error",
            "for": self.wait,
            # Written now, switched on later: unpause in Grafana once a
            # contact point is set up, or change this and deploy.
            "isPaused": True,
            "annotations": {"summary": self.summary},
            "labels": {"service": "eifo"},
        }


RULES = [
    Rule(
        "eifo-plugin-failed",
        "A plugin's latest run failed",
        "1 - eifo_fetch_last_run_ok",
        0,
        "{{ $labels.source }} ({{ $labels.phase }}) did not end well on its latest run.",
    ),
    Rule(
        "eifo-fetcher-silent",
        "No fetcher has run for two days",
        "time() - max(eifo_fetch_last_start_timestamp_seconds)",
        2 * 24 * 3600,
        "No fetcher has started a run in two days. Is the laptop's schedule running?",
    ),
    Rule(
        "eifo-sync-stale",
        "A service has not synced for three days",
        '(time() - eifo_fetch_last_success_timestamp_seconds{phase="sync"})'
        " * on (source) group_left eifo_source_enabled",
        3 * 24 * 3600,
        "{{ $labels.source }} has not finished a sync in three days.",
    ),
    Rule(
        "eifo-api-errors",
        "The API is answering with server errors",
        'sum(rate(eifo_http_requests_total{status="5xx"}[15m]))'
        " / sum(rate(eifo_http_requests_total[15m]))",
        0.05,
        "More than 5% of requests in the past 15 minutes ended in a server error.",
        wait="10m",
    ),
    Rule(
        "eifo-disk",
        "The disk is filling up",
        '1 - node_filesystem_avail_bytes{mountpoint="/"}'
        ' / node_filesystem_size_bytes{mountpoint="/"}',
        0.85,
        "The server's disk is over 85% full.",
        wait="30m",
    ),
    Rule(
        "eifo-memory",
        "Memory is running out",
        "1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes",
        0.9,
        "The server has less than 10% of its memory available, and no swap.",
        wait="15m",
    ),
]


def rules_file() -> str:
    body = {
        "apiVersion": 1,
        "groups": [
            {
                "orgId": 1,
                "name": "eifo",
                "folder": "Eifo",
                "interval": "5m",
                "rules": [rule.json() for rule in RULES],
            }
        ],
    }
    # JSON is YAML; the header is the only part that needs to be YAML's own.
    return (
        "# Written by monitoring/grafana/build.py - edit that, not this.\n"
        "# Every rule starts paused. Unpause one in Grafana (Alerting > Alert rules)\n"
        "# once a contact point is set up to receive it.\n"
        + json.dumps(body, indent=2, ensure_ascii=False)
        + "\n"
    )


def outputs() -> dict[Path, str]:
    """Every file this writes, and what goes in it."""
    files = {
        DASHBOARDS / f"{dashboard.uid}.json": json.dumps(
            dashboard.json(), indent=2, ensure_ascii=False
        )
        + "\n"
        for dashboard in (plugins(), enrichment(), traffic(), server())
    }
    files[ALERTING] = rules_file()
    return files


def main() -> None:
    for path, text in outputs().items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        print(f"wrote {path.relative_to(HERE.parent.parent)}")


if __name__ == "__main__":
    main()
