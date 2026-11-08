"""The files in ``monitoring/`` agree with this API, and with each other.

The stack is mostly configuration: Prometheus's scrape job, the proxy's rules,
Grafana's settings, the dashboards. Each names something the API decides: a
path, a header, a metric. Nothing at run time would say they had drifted apart.
A dashboard drawing a metric that was renamed shows "No data", and a proxy
asking the wrong path for the gate locks everybody out. So they are checked
here, against the code.
"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families
from pydantic import SecretStr
from seed import Seeded

from eifo_api.metrics.history import SCRAPE_LABELS
from eifo_api.ratelimit import GATE_PATH
from eifo_api.routers.monitoring import USER_HEADER

ROOT = Path(__file__).resolve().parents[3]
MONITORING = ROOT / "monitoring"
DASHBOARDS = MONITORING / "grafana" / "dashboards"
RULES = MONITORING / "grafana" / "provisioning" / "alerting" / "eifo.yml"
REGION = MONITORING / "caddy" / "eifo-monitoring.caddy"
TOKEN = "stack-test-token"


def load_build() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "monitoring_build", MONITORING / "grafana" / "build.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered first: dataclasses look their module up while they are made.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def compose() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load((MONITORING / "docker-compose.yml").read_text())
    return loaded


def expressions() -> list[str]:
    found = []
    for path in sorted(DASHBOARDS.glob("*.json")):
        for panel in json.loads(path.read_text())["panels"]:
            found.extend(target["expr"] for target in panel.get("targets", []))
    for group in yaml.safe_load(RULES.read_text())["groups"]:
        for rule in group["rules"]:
            found.extend(step["model"]["expr"] for step in rule["data"] if "expr" in step["model"])
    return found


class TestTheDashboards:
    def test_the_committed_files_are_what_the_builder_writes(self) -> None:
        for path, text in load_build().outputs().items():
            assert path.read_text(encoding="utf-8") == text, (
                f"{path.name} is stale: run python3 monitoring/grafana/build.py"
            )

    def test_every_eifo_figure_they_draw_is_served(
        self, app: FastAPI, client: TestClient, catalog: Seeded
    ) -> None:
        app.state.settings.metrics_token = SecretStr(TOKEN)
        client.get("/api/v1/meta")
        scrape = client.get("/api/v1/metrics", headers={"Authorization": f"Bearer {TOKEN}"})
        served = set()
        for family in text_string_to_metric_families(scrape.text):
            suffixes = {
                "counter": ("_total",),
                "histogram": ("_bucket", "_sum", "_count"),
            }.get(family.type, ("",))
            served.update(family.name + suffix for suffix in suffixes)

        drawn = {name for expr in expressions() for name in re.findall(r"\beifo_\w+", expr)}

        assert drawn, "found no eifo figures in the dashboards at all"
        assert drawn <= served, f"drawn but not served: {sorted(drawn - served)}"

    def test_panels_fit_and_use_the_provisioned_source(self) -> None:
        datasources = yaml.safe_load(
            (MONITORING / "grafana/provisioning/datasources/prometheus.yml").read_text()
        )
        uid = datasources["datasources"][0]["uid"]
        for path in DASHBOARDS.glob("*.json"):
            panels = json.loads(path.read_text())["panels"]
            assert len({panel["id"] for panel in panels}) == len(panels), path.name
            for panel in panels:
                grid = panel["gridPos"]
                assert grid["x"] + grid["w"] <= 24, (path.name, panel["title"])
                if panel["type"] != "row":
                    assert panel["datasource"]["uid"] == uid

    def test_the_home_dashboard_exists(self) -> None:
        home = compose()["services"]["grafana"]["environment"][
            "GF_DASHBOARDS_DEFAULT_HOME_DASHBOARD_PATH"
        ]
        assert (DASHBOARDS / Path(home).name).is_file()

    def test_every_alert_starts_paused(self) -> None:
        for group in yaml.safe_load(RULES.read_text())["groups"]:
            for rule in group["rules"]:
                assert rule["isPaused"] is True, rule["title"]


class TestPrometheus:
    def test_it_scrapes_what_the_history_is_labelled_as(self) -> None:
        """Otherwise the imported months and the scraped minutes are two series."""
        config = yaml.safe_load((MONITORING / "prometheus/prometheus.yml").read_text())
        [job] = [job for job in config["scrape_configs"] if job["job_name"] == SCRAPE_LABELS["job"]]

        assert job["metrics_path"] == "/api/v1/metrics"
        assert job["static_configs"] == [{"targets": [SCRAPE_LABELS["instance"]]}]
        assert job["authorization"]["type"] == "Bearer"

    def test_the_images_are_pinned(self) -> None:
        for name, service in compose()["services"].items():
            image = service["image"]
            assert re.search(r":v?\d+\.\d+\.\d+$", image), f"{name} runs {image}"


class TestTheWayIn:
    def test_the_proxy_asks_the_gate_this_api_serves(self) -> None:
        region = REGION.read_text()

        assert f"uri {GATE_PATH}" in region
        assert f"copy_headers {USER_HEADER}" in region
        # Deleted before the gate is asked, on every path to Grafana.
        assert region.count(f"request_header -{USER_HEADER}") == 2

    def test_grafana_trusts_the_header_the_gate_writes(self) -> None:
        grafana = compose()["services"]["grafana"]["environment"]

        assert grafana["GF_AUTH_PROXY_HEADER_NAME"] == USER_HEADER
        assert grafana["GF_AUTH_DISABLE_LOGIN_FORM"] == "true"
        assert grafana["GF_AUTH_ANONYMOUS_ENABLED"] == "false"

    def test_only_the_proxy_can_reach_grafana(self) -> None:
        """Grafana believes a header; nothing but the proxy may send it one."""
        stack = compose()

        assert stack["services"]["grafana"]["networks"] == ["monitoring"]
        assert "ports" not in stack["services"]["grafana"]
        assert stack["networks"]["monitoring"]["internal"] is True

    def test_the_metrics_are_closed_to_the_outside(self) -> None:
        assert "handle /api/v1/metrics* {\n\t\trespond 404" in REGION.read_text()


# -- the Caddyfile edit deploy.sh makes on the server -------------------------------

SITE = "eifo.example.com"
CADDYFILE = f"""# A comment.
{SITE} {{
\treverse_proxy api:3436
}}

# >>> bblog - managed by bblog's deploy/deploy.sh; edits in here are overwritten
(bblog) {{
\treverse_proxy bblog-fastapi:8000
}}
blog.example.com {{
\timport bblog
}}
# <<< bblog
"""


@pytest.fixture
def merge(tmp_path: Path) -> Any:
    """Run deploy.sh's own awk program, as the server will."""
    awk = shutil.which("awk")
    if awk is None:
        pytest.skip("no awk")
    script = (MONITORING / "deploy.sh").read_text()
    match = re.search(r"^MERGE_AWK='(.*?)^'$", script, re.S | re.M)
    assert match, "deploy.sh no longer defines MERGE_AWK"
    program = tmp_path / "merge.awk"
    program.write_text(match.group(1))

    def run(text: str, site: str = SITE) -> tuple[int, str]:
        source = tmp_path / "Caddyfile"
        source.write_text(text)
        done = subprocess.run(
            [awk, "-v", f"site={site}", "-v", f"region={REGION}", "-f", str(program), str(source)],
            capture_output=True,
            text=True,
            check=False,
        )
        return done.returncode, done.stdout

    return run


class TestTheCaddyfileEdit:
    def test_the_rules_go_inside_eifo_s_block_and_nowhere_else(self, merge: Any) -> None:
        code, merged = merge(CADDYFILE)

        assert code == 0
        _before, after = merged.split(f"{SITE} {{\n", 1)
        assert after.startswith(REGION.read_text())
        assert "# >>> bblog" in merged and "reverse_proxy bblog-fastapi:8000" in merged
        assert merged.replace(REGION.read_text(), "") == CADDYFILE

    def test_running_it_again_changes_nothing(self, merge: Any) -> None:
        _code, once = merge(CADDYFILE)
        _code, twice = merge(once)

        assert twice == once

    def test_old_rules_are_replaced(self, merge: Any) -> None:
        stale = CADDYFILE.replace(
            f"{SITE} {{\n",
            f"{SITE} {{\n\t# >>> eifo-monitoring - old\n\trespond /grafana 200\n"
            "\t# <<< eifo-monitoring\n",
        )

        _code, merged = merge(stale)

        assert "respond /grafana 200" not in merged
        assert merged.count("# >>> eifo-monitoring") == 1

    def test_a_site_block_with_a_scheme_or_more_names_is_found(self, merge: Any) -> None:
        text = CADDYFILE.replace(f"{SITE} {{", f"https://{SITE}, www.{SITE} {{")

        code, merged = merge(text)

        assert code == 0
        assert merged.count("# >>> eifo-monitoring") == 1

    def test_no_site_block_is_a_failure_not_a_guess(self, merge: Any) -> None:
        code, _merged = merge(CADDYFILE, site="elsewhere.example.com")

        assert code == 3

    def test_a_longer_name_is_not_mistaken_for_the_site(self, merge: Any) -> None:
        code, _merged = merge(CADDYFILE.replace(f"{SITE} {{", f"{SITE}.evil {{"))

        assert code == 3
