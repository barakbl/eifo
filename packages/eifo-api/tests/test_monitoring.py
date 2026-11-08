"""What the monitoring stack reads: ``/api/v1/metrics``, its history, and the gate.

The fetcher may run on another machine entirely, a laptop pushing its runs
through the ingest API. So the test that matters most here is the one that
reports a run the way that fetcher does and finds it in the scrape: if that
holds, Grafana sees every run wherever it happened.
"""

from __future__ import annotations

import datetime as dt
import io
import math
import os
from collections.abc import Iterator
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from helpers import MakeAdmin, SignIn
from prometheus_client.openmetrics.parser import (
    text_string_to_metric_families as parse_openmetrics,
)
from prometheus_client.parser import text_string_to_metric_families as parse_text
from providers import GOOGLE_EMAIL
from pydantic import SecretStr
from seed import Seeded
from sqlalchemy.orm import Session, sessionmaker

from eifo_api import __main__ as entry
from eifo_api.metrics import Family
from eifo_api.metrics.history import SCRAPE_LABELS, steps, write_history
from eifo_api.metrics.history import main as history_main
from eifo_api.metrics.runs import ENDINGS, Run, replay
from eifo_api.metrics.traffic import (
    MULTIPROCESS_ENV,
    REGISTRY,
    route_of,
    runtime_registry,
    traffic_registry,
)
from eifo_api.ratelimit import GATE_PATH, draws_for
from eifo_api.security import CSRF_HEADER
from eifo_core.enums import FetchPhase, FetchStatus, SourceKind
from eifo_core.ingest import ABANDONED_AFTER
from eifo_core.models import FetchRun, Source
from eifo_core.settings import get_settings

METRICS = "/api/v1/metrics"
GATE = "/api/v1/monitoring/gate"
TOKEN = "metrics-token-for-tests"
NOW = dt.datetime(2026, 11, 8, 12, 0, tzinfo=dt.UTC)

Samples = dict[tuple[str, frozenset[tuple[str, str]]], float]


@pytest.fixture
def metrics_on(app: FastAPI) -> None:
    """The token configured, as monitoring/deploy.sh leaves the server."""
    app.state.settings.metrics_token = SecretStr(TOKEN)


@pytest.fixture
def operator(sign_in: SignIn, make_admin: MakeAdmin) -> str:
    """An administrator signed in; the fetcher's token does the same with no CSRF."""
    make_admin()
    return sign_in()


def scrape(client: TestClient) -> Samples:
    response = client.get(METRICS, headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 200, response.text
    samples: Samples = {}
    for family in parse_text(response.text):
        for sample in family.samples:
            samples[(sample.name, frozenset(sample.labels.items()))] = sample.value
    return samples


def value(samples: Samples, name: str, **labels: str) -> float | None:
    return samples.get((name, frozenset(labels.items())))


def run(
    phase: str = "sync",
    source: str = "mako",
    status: str = "ok",
    *,
    start: dt.datetime = NOW - dt.timedelta(hours=2),
    minutes: float | None = 10,
    stats: dict[str, Any] | None = None,
) -> Run:
    return Run(
        phase=phase,
        source=source,
        status=status,
        started_at=start,
        finished_at=None if minutes is None else start + dt.timedelta(minutes=minutes),
        stats=stats or {},
    )


def figures(families: list[Family]) -> dict[str, dict[tuple[str, ...], float]]:
    return {family.name: family.samples for family in families}


class TestTheEndpoint:
    def test_it_is_not_there_until_a_token_is_set(self, client: TestClient) -> None:
        assert client.get(METRICS).status_code == 404

    @pytest.mark.parametrize(
        "header",
        [None, "Bearer wrong", f"Basic {TOKEN}", "Bearer", f"Token {TOKEN}"],
    )
    def test_it_answers_only_the_token(
        self, client: TestClient, metrics_on: None, header: str | None
    ) -> None:
        headers = {"Authorization": header} if header else {}

        response = client.get(METRICS, headers=headers)

        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"] == "Bearer"

    def test_a_signed_in_administrator_is_not_enough(
        self, client: TestClient, metrics_on: None, operator: str
    ) -> None:
        """Only Prometheus reads this, and it reads with the token."""
        assert client.get(METRICS).status_code == 401

    def test_it_speaks_prometheus(self, client: TestClient, metrics_on: None) -> None:
        response = client.get(METRICS, headers={"Authorization": f"Bearer {TOKEN}"})

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/plain")
        assert response.headers["Cache-Control"] == "no-store"
        names = {family.name for family in parse_text(response.text)}
        assert {"eifo_build_info", "eifo_fetch_runs", "eifo_source_titles"} <= names
        assert "eifo_http_requests" in names

    def test_it_counts_the_catalog(
        self, client: TestClient, metrics_on: None, catalog: Seeded
    ) -> None:
        samples = scrape(client)

        movies = value(samples, "eifo_titles", type="movie") or 0
        series = value(samples, "eifo_titles", type="series") or 0
        assert movies + series > 0
        per_source = [
            amount for (name, _labels), amount in samples.items() if name == "eifo_source_titles"
        ]
        assert sum(per_source) > 0


class TestARunFromAnotherMachine:
    """The fetcher on a laptop reports through the ingest API; nothing else."""

    def test_a_sync_reported_through_the_api_is_counted(
        self, client: TestClient, metrics_on: None, operator: str
    ) -> None:
        source = {
            "source_key": "cellcom_tv",
            "name": "Cellcom TV",
            "kind": SourceKind.SUBSCRIPTION.value,
            "website_url": "https://cellcomtv.example",
        }
        headers = {CSRF_HEADER: operator}
        opened = client.post("/api/v1/ingest/sync/runs", json=source, headers=headers)
        run_id = opened.json()["run_id"]
        listing = {
            "source_key": "cellcom_tv",
            "kind": "series",
            "name": "פאודה",
            "offer_type": "stream",
            "year": 2015,
        }
        client.post(
            f"/api/v1/ingest/sync/runs/{run_id}/items",
            json=[{"item": listing, "resolved": True}],
            headers=headers,
        )
        before = scrape(client)
        assert value(before, "eifo_fetch_running", phase="sync", source="cellcom_tv") == 1

        client.post(
            f"/api/v1/ingest/sync/runs/{run_id}/finish",
            json={"status": FetchStatus.OK.value},
            headers=headers,
        )
        samples = scrape(client)

        assert value(samples, "eifo_fetch_running", phase="sync", source="cellcom_tv") == 0
        assert (
            value(samples, "eifo_fetch_runs_total", phase="sync", source="cellcom_tv", status="ok")
            == 1
        )
        assert value(samples, "eifo_fetch_last_run_ok", phase="sync", source="cellcom_tv") == 1
        assert value(samples, "eifo_sync_items_seen_total", source="cellcom_tv") == 1
        assert value(samples, "eifo_sync_titles_created_total", source="cellcom_tv") == 1
        assert value(samples, "eifo_source_titles", source="cellcom_tv", type="series") == 1
        assert value(samples, "eifo_fetch_last_start_timestamp_seconds") is not None

    def test_a_failed_enrich_is_counted_per_enricher(
        self, client: TestClient, metrics_on: None, operator: str
    ) -> None:
        headers = {CSRF_HEADER: operator}
        run_id = client.post(
            "/api/v1/ingest/runs", json={"phase": FetchPhase.ENRICH.value}, headers=headers
        ).json()["id"]
        client.patch(
            f"/api/v1/ingest/runs/{run_id}",
            json={
                "status": FetchStatus.FAILED.value,
                "stats": {
                    "titles_seen": 40,
                    "by_enricher": {"tmdb": 30, "rt": 2},
                    "seconds_by_enricher": {"tmdb": 12.5, "rt": 80.0},
                    "errors_by_enricher": {"rt": 7},
                    "error_count": 7,
                },
            },
            headers=headers,
        )

        samples = scrape(client)

        assert (
            value(samples, "eifo_fetch_runs_total", phase="enrich", source="all", status="failed")
            == 1
        )
        assert value(samples, "eifo_fetch_last_run_ok", phase="enrich", source="all") == 0
        assert value(samples, "eifo_fetch_last_run_errors", phase="enrich", source="all") == 7
        assert value(samples, "eifo_enrich_titles_total") == 40
        assert value(samples, "eifo_enrich_ratings_found_total", enricher="tmdb") == 30
        assert value(samples, "eifo_enrich_seconds_total", enricher="rt") == 80
        assert value(samples, "eifo_enrich_errors_total", enricher="rt") == 7
        assert value(samples, "eifo_enrich_errors_total", enricher="tmdb") == 0


class TestTheFold:
    def test_every_plugin_carries_every_ending_from_the_start(self) -> None:
        """So the first failure is a rise from 0 that increase() can see."""
        counted = figures(replay([run()], NOW, sources=["kan"]))["eifo_fetch_runs"]

        for source in ("mako", "kan"):
            for ending in ENDINGS:
                assert ("sync", source, ending) in counted
        assert counted[("sync", "mako", "ok")] == 1
        assert counted[("sync", "kan", "ok")] == 0

    def test_a_run_given_up_on_counts_as_crashed(self) -> None:
        abandoned = run(status="running", start=NOW - ABANDONED_AFTER - dt.timedelta(minutes=1))
        still_going = run(source="kan", status="running", start=NOW - dt.timedelta(hours=1))

        found = figures(replay([abandoned, still_going], NOW, sources=[]))

        assert found["eifo_fetch_runs"][("sync", "mako", "crashed")] == 1
        assert found["eifo_fetch_running"][("sync", "mako")] == 0
        assert found["eifo_fetch_running"][("sync", "kan")] == 1
        assert ("sync", "kan") not in found["eifo_fetch_last_run_ok"]

    def test_a_crash_noticed_days_later_did_not_take_days(self) -> None:
        """The API closes a dead run whenever that phase next starts."""
        noticed_late = run(
            status="crashed",
            start=NOW - dt.timedelta(days=10),
            minutes=9 * 24 * 60,
        )

        found = figures(replay([noticed_late], NOW, sources=[]))

        took = found["eifo_fetch_last_run_duration_seconds"][("sync", "mako")]
        assert took == ABANDONED_AFTER.total_seconds()

    def test_the_latest_run_is_the_one_that_ended_last(self) -> None:
        long_ok = run(start=NOW - dt.timedelta(hours=5), minutes=240)
        quick_failure = run(status="failed", start=NOW - dt.timedelta(hours=4), minutes=1)

        found = figures(replay([long_ok, quick_failure], NOW, sources=[]))

        assert found["eifo_fetch_last_run_ok"][("sync", "mako")] == 1
        assert found["eifo_fetch_last_run_duration_seconds"][("sync", "mako")] == 240 * 60
        assert found["eifo_fetch_run_seconds"][("sync", "mako")] == 241 * 60

    def test_nothing_strange_in_the_stats_is_counted(self) -> None:
        """The stats column holds whatever any fetcher version sent."""
        odd = run(
            stats={
                "items_seen": "lots",
                "titles_created": True,
                "retired": -4,
                "availability_created": 3,
                "matched_by": ["not", "a", "mapping"],
                "error_count": None,
            }
        )

        found = figures(replay([odd], NOW, sources=[]))

        assert found["eifo_sync_items_seen"][("mako",)] == 0
        assert found["eifo_sync_titles_created"][("mako",)] == 0
        assert found["eifo_sync_offers_retired"][("mako",)] == 0
        assert found["eifo_sync_offers_added"][("mako",)] == 3
        assert found["eifo_sync_matched"] == {}
        assert found["eifo_fetch_last_run_errors"][("sync", "mako")] == 0

    def test_a_failed_sync_does_not_forget_the_last_count(self) -> None:
        listed = run(stats={"items_seen": 600}, start=NOW - dt.timedelta(hours=3))
        broken = run(status="failed", stats={"error_count": 1}, start=NOW - dt.timedelta(hours=1))

        found = figures(replay([listed, broken], NOW, sources=[]))

        assert found["eifo_sync_last_items_seen"][("mako",)] == 600

    def test_imdb_s_bulk_load_counts_as_imdb_s_finds(self) -> None:
        bulk = run(phase="enrich", source="imdb", stats={"written": 30_000})

        found = figures(replay([bulk], NOW, sources=[]))

        assert found["eifo_enrich_ratings_found"][("imdb",)] == 30_000

    def test_the_artwork_pass(self) -> None:
        images = run(phase="images", source="all", stats={"downloaded": 10, "from_backup": 1})

        found = figures(replay([images], NOW, sources=[]))

        assert found["eifo_images"][("downloaded",)] == 10
        assert found["eifo_images"][("from_backup",)] == 1
        assert found["eifo_images"][("failed",)] == 0

    def test_the_database_rows_are_read_as_runs(
        self,
        client: TestClient,
        metrics_on: None,
        session_factory: sessionmaker[Session],
    ) -> None:
        with session_factory() as session:
            session.add(
                Source(
                    key="kan",
                    name="Kan",
                    kind=SourceKind.FREE,
                    website_url="https://kan.example",
                )
            )
            session.add(
                FetchRun(
                    phase=FetchPhase.SYNC,
                    source_key="kan",
                    status=FetchStatus.ABORTED_SUSPICIOUS,
                    started_at=dt.datetime.now(dt.UTC) - dt.timedelta(hours=1),
                    finished_at=dt.datetime.now(dt.UTC),
                    stats={"items_seen": 3},
                )
            )
            session.commit()

        samples = scrape(client)

        assert (
            value(
                samples,
                "eifo_fetch_runs_total",
                phase="sync",
                source="kan",
                status="aborted_suspicious",
            )
            == 1
        )
        assert value(samples, "eifo_source_enabled", source="kan") == 1


class TestHistory:
    RUNS = (
        run(start=NOW - dt.timedelta(days=3)),
        run(status="failed", start=NOW - dt.timedelta(days=2), stats={"error_count": 2}),
        run(
            phase="enrich",
            source="all",
            start=NOW - dt.timedelta(days=1),
            minutes=30,
            stats={"by_enricher": {"tmdb": 5}},
        ),
        run(source="kan", status="running", start=NOW - dt.timedelta(hours=1)),
    )

    def _written(self, until: dt.datetime = NOW) -> tuple[str, int]:
        out = io.StringIO()
        written = write_history(list(self.RUNS), out, sources=["mako", "kan"], until=until)
        return out.getvalue(), written

    def _series(self, text: str) -> dict[tuple[str, frozenset[tuple[str, str]]], list[Any]]:
        series: dict[tuple[str, frozenset[tuple[str, str]]], list[Any]] = {}
        for family in parse_openmetrics(text):
            for sample in family.samples:
                key = (sample.name, frozenset(sample.labels.items()))
                series.setdefault(key, []).append(sample)
        return series

    def test_promtool_s_format(self) -> None:
        text, written = self._written()

        assert text.endswith("# EOF\n")
        assert written == sum(len(samples) for samples in self._series(text).values())

    def test_it_ends_where_the_endpoint_begins(self) -> None:
        """The last point of every series is what a scrape at that moment says."""
        text, _ = self._written()
        live = replay(list(self.RUNS), NOW, sources=["mako", "kan"])

        series = self._series(text)
        for family in live:
            sample_name = f"{family.name}_total" if family.kind == "counter" else family.name
            for labels, expected in family.samples.items():
                named = {**SCRAPE_LABELS, **dict(zip(family.labels, labels, strict=True))}
                key = (sample_name, frozenset(named.items()))
                last = series[key][-1]
                assert last.timestamp.sec == int(NOW.timestamp())
                assert last.value == pytest.approx(expected), key

    def test_counters_only_go_up(self) -> None:
        text, _ = self._written()

        for (name, _labels), samples in self._series(text).items():
            if name.endswith("_total"):
                values = [sample.value for sample in samples]
                assert values == sorted(values), name

    def test_a_figure_starts_when_it_first_exists(self) -> None:
        text, _ = self._written()
        series = self._series(text)

        labels = {**SCRAPE_LABELS, "phase": "sync", "source": "mako"}
        first_ok = series[("eifo_fetch_last_run_ok", frozenset(labels.items()))]
        first_end = self.RUNS[0].finished_at
        assert first_end is not None
        assert first_ok[0].timestamp.sec >= first_end.timestamp()

    def test_every_series_is_the_one_prometheus_scrapes(self) -> None:
        """Without the scrape's job and instance, history and live are two series."""
        text, _ = self._written()

        for (_name, labels), _samples in self._series(text).items():
            assert set(SCRAPE_LABELS.items()) <= set(labels)

    def test_the_labels_can_be_changed(self) -> None:
        out = io.StringIO()

        write_history(list(self.RUNS), out, sources=[], until=NOW, labels={"job": "other"})

        jobs = {
            sample.labels.get("job")
            for family in parse_openmetrics(out.getvalue())
            for sample in family.samples
        }
        assert jobs == {"other"}

    def test_points_are_hourly_and_end_now(self) -> None:
        moments = steps(list(self.RUNS), NOW, dt.timedelta(hours=1))

        assert moments[-1] == NOW
        gaps = {later - earlier for earlier, later in pairwise(moments[:-1])}
        assert gaps <= {dt.timedelta(hours=1)}
        assert moments[0] <= min(r.started_at for r in self.RUNS)

    def test_nothing_recorded_writes_an_empty_file(self) -> None:
        out = io.StringIO()

        assert write_history([], out, sources=[], until=NOW) == 0
        assert out.getvalue() == "# EOF\n"

    def test_awkward_label_values_survive(self) -> None:
        odd = run(source='we"ird\\source\nkey')
        out = io.StringIO()

        write_history([odd], out, sources=[], until=NOW)

        labels = {
            sample.labels.get("source")
            for family in parse_openmetrics(out.getvalue())
            for sample in family.samples
        }
        assert 'we"ird\\source\nkey' in labels


class TestTheHistoryCommand:
    """``python -m eifo_api.metrics.history``, as deploy.sh runs it in the container."""

    @pytest.fixture
    def database(
        self,
        monkeypatch: pytest.MonkeyPatch,
        settings: Any,
        session_factory: sessionmaker[Session],
    ) -> Iterator[None]:
        with session_factory() as session:
            session.add(
                FetchRun(
                    phase=FetchPhase.SYNC,
                    source_key="mako",
                    status=FetchStatus.OK,
                    started_at=dt.datetime.now(dt.UTC) - dt.timedelta(hours=3),
                    finished_at=dt.datetime.now(dt.UTC) - dt.timedelta(hours=2),
                    stats={"items_seen": 12},
                )
            )
            session.commit()
        monkeypatch.setenv("EIFO_DB_URL", settings.db_url)
        get_settings.cache_clear()
        yield
        get_settings.cache_clear()

    def test_it_writes_the_runs_to_stdout(
        self, database: None, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert history_main([]) == 0

        out, err = capsys.readouterr()
        assert out.endswith("# EOF\n")
        assert 'eifo_sync_items_seen_total{job="eifo",instance="api:3436",source="mako"}' in out
        assert "from 1 runs" in err

    def test_its_labels_can_be_replaced(
        self, database: None, capsys: pytest.CaptureFixture[str]
    ) -> None:
        history_main(["--label", "job=elsewhere", "--step-minutes", "30"])

        out, _err = capsys.readouterr()
        assert 'job="elsewhere"' in out
        assert 'instance="' not in out

    @pytest.mark.parametrize("args", [["--label", "nonsense"], ["--step-minutes", "0"]])
    def test_it_refuses_what_it_cannot_use(self, args: list[str]) -> None:
        with pytest.raises(SystemExit):
            history_main(args)


class TestTraffic:
    def _requests(self, samples: Samples, route: str, status: str = "2xx") -> float:
        return (
            value(samples, "eifo_http_requests_total", method="GET", route=route, status=status)
            or 0
        )

    def test_requests_are_counted_by_route_template(
        self, client: TestClient, metrics_on: None, catalog: Seeded
    ) -> None:
        before = scrape(client)

        client.get(f"/api/v1/titles/{catalog.fauda}")
        client.get(f"/api/v1/titles/{catalog.foxtrot}")
        client.get("/api/v1/no-such-thing")
        after = scrape(client)

        template = "/api/v1/titles/{title_id}"
        assert self._requests(after, template) - self._requests(before, template) == 2
        assert (
            self._requests(after, "unmatched", "4xx") - self._requests(before, "unmatched", "4xx")
            == 1
        )

    @pytest.mark.parametrize(
        ("scope", "route"),
        [
            ({"path": "/mcp"}, "/mcp"),
            ({"path": "/mcp/anything"}, "/mcp"),
            ({"path": "/api/v1/whatever"}, "unmatched"),
            ({"path": "/images/posters/1/w500.jpg"}, "static"),
            ({"path": "/"}, "static"),
        ],
    )
    def test_what_has_no_template_gets_a_fixed_name(
        self, scope: dict[str, Any], route: str
    ) -> None:
        assert route_of(scope) == route

    def test_several_workers_are_added_up(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv(MULTIPROCESS_ENV, str(tmp_path))

        assert traffic_registry() is not REGISTRY
        assert runtime_registry() is None

    def test_one_worker_counts_itself(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(MULTIPROCESS_ENV, raising=False)

        assert traffic_registry() is REGISTRY
        assert runtime_registry() is not None


class TestStartingWorkers:
    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
        """The function under test sets the variable itself, out of monkeypatch's
        sight; left behind, every later scrape would read counts from a
        directory that is gone."""
        monkeypatch.delenv(MULTIPROCESS_ENV, raising=False)
        yield
        os.environ.pop(MULTIPROCESS_ENV, None)

    def test_they_are_given_somewhere_to_count(self, monkeypatch: pytest.MonkeyPatch) -> None:
        entry._share_request_counts()

        directory = Path(os.environ[MULTIPROCESS_ENV])
        assert directory.is_dir()
        directory.rmdir()

    def test_counts_left_by_a_previous_run_are_cleared(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv(MULTIPROCESS_ENV, str(tmp_path))
        (tmp_path / "counter_123.db").write_bytes(b"old")

        entry._share_request_counts()

        assert list(tmp_path.glob("*.db")) == []


class TestTheGate:
    def test_a_stranger_is_sent_to_sign_in(self, client: TestClient) -> None:
        response = client.get(GATE, follow_redirects=False)

        assert response.status_code == 302
        assert response.headers["location"] == "/"
        assert "X-Eifo-User" not in response.headers

    def test_a_member_who_is_not_an_administrator_is_refused(
        self, client: TestClient, sign_in: SignIn
    ) -> None:
        sign_in()

        response = client.get(GATE, follow_redirects=False)

        assert response.status_code == 403
        assert "X-Eifo-User" not in response.headers

    def test_an_administrator_is_named(self, client: TestClient, operator: str) -> None:
        response = client.get(GATE, follow_redirects=False)

        assert response.status_code == 204
        assert response.headers["X-Eifo-User"] == GOOGLE_EMAIL
        assert response.headers["Cache-Control"] == "no-store"

    def test_a_token_is_not_a_browser(self, client: TestClient, operator: str) -> None:
        issued = client.post(
            "/api/v1/me/tokens", json={"name": "fetcher"}, headers={CSRF_HEADER: operator}
        )
        bare = TestClient(client.app, base_url="https://testserver")
        bare.headers["Authorization"] = f"Bearer {issued.json()['token']}"

        response = bare.get(GATE, follow_redirects=False)

        assert response.status_code == 302

    def test_a_signed_in_browser_is_not_rate_limited_here(self) -> None:
        """A dashboard is dozens of requests at once, each one checked."""
        assert draws_for("GET", GATE_PATH, credential="abc", address="203.0.113.7") == []
        assert draws_for("GET", GATE_PATH, credential=None, address="203.0.113.7") != []


def test_values_are_plain_numbers() -> None:
    """NaN would poison every sum a dashboard draws."""
    for family in replay([run()], NOW, sources=["kan"]):
        assert all(not math.isnan(amount) for amount in family.samples.values()), family.name
