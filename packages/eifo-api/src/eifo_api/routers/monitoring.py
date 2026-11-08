"""What the monitoring stack in ``monitoring/`` asks this API.

Two endpoints, neither of them for people:

* ``GET /metrics``, which Prometheus reads once a minute. It is off unless
  ``EIFO_METRICS_TOKEN`` is set, and answers only to that token.
* ``GET /monitoring/gate``, which the reverse proxy asks before each request it
  passes to Grafana: is the browser behind this request one of this site's
  administrators? Grafana then trusts the address the proxy puts in
  ``X-Eifo-User``, so signing in to Eifo is signing in to Grafana, and nobody
  else gets in.
"""

from __future__ import annotations

import hmac
import time

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily, Metric
from prometheus_client.registry import Collector, CollectorRegistry

from eifo_api.deps import OptionalPrincipalDep, SessionDep, SettingsDep
from eifo_api.metrics import PREFIX, Family
from eifo_api.metrics.catalog import catalog_families
from eifo_api.metrics.runs import run_families
from eifo_api.metrics.traffic import runtime_registry, traffic_registry
from eifo_core.types import utcnow

router = APIRouter(tags=["monitoring"])

#: The header Grafana's auth proxy reads the signed-in address from. The proxy
#: deletes any copy a browser sent before it asks the gate, so the only one
#: that reaches Grafana is the one written here.
USER_HEADER = "X-Eifo-User"


@router.get("/metrics", include_in_schema=False)
def metrics(request: Request, session: SessionDep, settings: SettingsDep) -> Response:
    """Every figure, in Prometheus's text format."""
    expected = settings.metrics_token
    if expected is None:
        # Off: no different from any other path that is not there.
        raise HTTPException(status_code=404, detail="Not found.")
    if not _presented(request, expected.get_secret_value()):
        raise HTTPException(
            status_code=401,
            detail="Send the metrics token as a Bearer token.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    began = time.perf_counter()
    now = utcnow()
    families = [*run_families(session, now), *catalog_families(session, settings, now)]
    took = Family(
        f"{PREFIX}metrics_collect_seconds",
        "gauge",
        "How long reading these figures from the database took.",
    )
    took.set(time.perf_counter() - began)

    database = CollectorRegistry(auto_describe=False)
    database.register(_Families([*families, took]))
    body = b"".join(
        generate_latest(registry)
        for registry in (database, traffic_registry(), runtime_registry())
        if registry is not None
    )
    return Response(
        content=body,
        media_type=CONTENT_TYPE_LATEST,
        headers={"Cache-Control": "no-store"},
    )


@router.get("/monitoring/gate", include_in_schema=False)
def gate(principal: OptionalPrincipalDep) -> Response:
    """Let an administrator's browser through to Grafana, and nobody else.

    Somebody not signed in is sent to the home page, where they can sign in.
    The proxy passes this answer straight back to the browser, so a redirect
    here is a redirect there. A token is not a browser and is refused like a
    stranger: Grafana is a page to look at, and the gate's only job is to say
    who is looking.
    """
    if principal is None or principal.via_token:
        return RedirectResponse("/", status_code=302, headers={"Cache-Control": "no-store"})
    email = principal.user.email
    if not principal.is_admin or not email:
        raise HTTPException(status_code=403, detail="The dashboards are for administrators.")
    return Response(status_code=204, headers={USER_HEADER: email, "Cache-Control": "no-store"})


def _presented(request: Request, expected: str) -> bool:
    scheme, _, value = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not value:
        return False
    return hmac.compare_digest(value.strip().encode(), expected.encode())


class _Families(Collector):
    """Hands :class:`Family` objects to prometheus_client as its own kinds."""

    def __init__(self, families: list[Family]) -> None:
        self.families = families

    def collect(self) -> list[Metric]:
        out: list[Metric] = []
        for family in self.families:
            kind = CounterMetricFamily if family.kind == "counter" else GaugeMetricFamily
            metric = kind(family.name, family.help, labels=list(family.labels))
            for labels, value in family.samples.items():
                metric.add_metric(list(labels), value)
            out.append(metric)
        return out
