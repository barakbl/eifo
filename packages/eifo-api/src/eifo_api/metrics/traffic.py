"""Requests this process served, counted as they happen.

The one part of the metrics kept in memory, because nothing else records it.
Labelled by route *template* (``/api/v1/titles/{title_id}``), never by the
path a caller typed: one label per title would be a new series per title, and
a path invented to make noise would be a new series per guess.

With several worker processes, each counts its own requests. uvicorn's
``--workers`` starts them through ``eifo-api``, which then points
:data:`MULTIPROCESS_ENV` at a shared directory (see ``eifo_api.__main__``), and
:func:`traffic_registry` adds up every worker's counts rather than answering
for whichever one Prometheus happened to reach.
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from prometheus_client import (
    GC_COLLECTOR,
    PLATFORM_COLLECTOR,
    PROCESS_COLLECTOR,
    CollectorRegistry,
    Counter,
    Histogram,
)
from prometheus_client.multiprocess import MultiProcessCollector

from eifo_api.metrics import PREFIX

#: prometheus_client's own switch for counting across processes.
MULTIPROCESS_ENV = "PROMETHEUS_MULTIPROC_DIR"

#: This process's counts. A registry of its own rather than the library's
#: global one, which also holds the process collectors below and would answer
#: for the wrong process in multiprocess mode.
REGISTRY = CollectorRegistry(auto_describe=True)

REQUESTS = Counter(
    f"{PREFIX}http_requests",
    "Requests served, by route and the class of their status code.",
    ("method", "route", "status"),
    registry=REGISTRY,
)
LATENCY = Histogram(
    f"{PREFIX}http_request_duration_seconds",
    "How long requests took to answer, by route.",
    ("method", "route"),
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
    registry=REGISTRY,
)

#: Anything else is counted as OTHER, so a made-up method is not a new series.
METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})

#: The remote MCP connector. A plain Starlette route, which does not say which
#: route it was the way FastAPI's do, so it is recognised by its path.
MCP_PATH = "/mcp"

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


def route_of(scope: Scope) -> str:
    """The template of the route that answered, or a fixed name for the rest.

    Read after the request is handled: the router writes the matched route
    into the scope as it dispatches, and this middleware sees the same scope.
    """
    raw = str(scope.get("path", ""))
    route = scope.get("route")
    template = getattr(route, "path_format", None) or getattr(route, "path", None)
    if isinstance(template, str) and template:
        return _prefix(raw, getattr(route, "path_regex", None)) + template
    if raw == MCP_PATH or raw.startswith(f"{MCP_PATH}/"):
        return MCP_PATH
    if raw.startswith("/api/"):
        return "unmatched"
    # The web app, its assets and the posters: files, which cost nothing to
    # tell apart and are not what anyone wants a graph of.
    return "static"


def _prefix(path: str, pattern: re.Pattern[str] | None) -> str:
    """The part of ``path`` in front of the route that matched the rest.

    FastAPI keeps an included router's routes as they were declared, so a
    route under ``/api/v1`` says ``/titles/{title_id}``. The prefix is
    whatever comes before the longest tail of the path the route's own
    pattern matches. It is one of the app's own prefixes, because routing
    already matched it, so it cannot be a caller's invention.
    """
    if pattern is None or pattern.fullmatch(path):
        return ""
    for index, character in enumerate(path):
        if character == "/" and index and pattern.fullmatch(path[index:]):
            return path[:index]
    return ""


class TrafficMiddleware:
    """Count every HTTP request and how long it took."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        began = time.perf_counter()
        # Unless a response starts, the request failed: the server error
        # handler outside this middleware answers it with a 500.
        status = 500

        async def watch(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, watch)
        finally:
            method = str(scope.get("method", "")).upper()
            method = method if method in METHODS else "OTHER"
            route = route_of(scope)
            REQUESTS.labels(method, route, f"{status // 100}xx").inc()
            LATENCY.labels(method, route).observe(time.perf_counter() - began)


def traffic_registry() -> CollectorRegistry:
    """Where the request counts are read from: this process, or all of them."""
    directory = os.environ.get(MULTIPROCESS_ENV)
    if not directory:
        return REGISTRY
    registry = CollectorRegistry()
    MultiProcessCollector(registry, path=directory)
    return registry


def runtime_registry() -> CollectorRegistry | None:
    """The Python process's own figures: memory, CPU, open files, collections.

    None with several workers, where they would describe whichever worker
    answered the scrape; cAdvisor reports the container as a whole instead.
    """
    if os.environ.get(MULTIPROCESS_ENV):
        return None
    return _RUNTIME


_RUNTIME = CollectorRegistry()
for _collector in (PROCESS_COLLECTOR, PLATFORM_COLLECTOR, GC_COLLECTOR):
    _RUNTIME.register(_collector)
