"""``eifo-api`` - run the API with uvicorn.

A thin wrapper so ``uv run eifo-api`` binds the right host and port without
anyone having to remember ``--port``. The bare ``uvicorn eifo_api.main:app``
still works; it just falls back to uvicorn's own default of 8000.
"""

from __future__ import annotations

import argparse
import os
import tempfile
from collections.abc import Sequence
from pathlib import Path

import uvicorn

from eifo_core.settings import get_settings

#: Mirrors eifo_api.metrics.traffic.MULTIPROCESS_ENV. Not imported from there:
#: importing prometheus_client in this process before the variable is set
#: would fix it in single-process mode for good.
MULTIPROCESS_ENV = "PROMETHEUS_MULTIPROC_DIR"


def main(argv: Sequence[str] | None = None) -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser(prog="eifo-api", description="Serve the Eifo API.")
    parser.add_argument("--host", default=settings.serve_host)
    parser.add_argument("--port", type=int, default=settings.serve_port)
    parser.add_argument(
        "--reload",
        action="store_true",
        help="restart on code changes (development only)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=settings.serve_workers,
        help="worker processes; more than one keeps the web app quick while a sync writes",
    )
    args = parser.parse_args(argv)

    # uvicorn cannot do both: reloading watches one process, workers forks
    # several. Saying so beats uvicorn quietly ignoring one of them.
    if args.reload and args.workers > 1:
        parser.error("--reload and --workers are mutually exclusive")

    # Every worker runs the lifespan, so every worker would migrate, and a
    # pending migration would be applied by several processes at once. One
    # process bringing the schema to head is the whole point of auto_migrate;
    # several racing for it is not, so say so rather than let it happen on the
    # one deploy that has a migration waiting.
    if args.workers > 1 and settings.auto_migrate:
        parser.error(
            "--workers needs auto_migrate off, or each worker races the others to "
            "apply the same migration. Run `eifo-fetch db upgrade` first and set "
            "EIFO_AUTO_MIGRATE=false (the Docker image already has a migrate step)."
        )

    if args.workers > 1:
        _share_request_counts()

    uvicorn.run(
        "eifo_api.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        # Passed only when it is not the default: uvicorn treats workers=1 and
        # workers=None differently, and None is the single-process path this
        # has always taken.
        workers=args.workers if args.workers > 1 else None,
        # Who the client really is, when a proxy says so - and only when the
        # one saying so is a proxy we trust. The rate limiter keys on it.
        proxy_headers=True,
        forwarded_allow_ips=settings.trusted_proxies,
    )


def _share_request_counts() -> None:
    """Give the workers somewhere to add up the requests they serve.

    Each worker counts its own requests; without a shared directory a scrape
    of /api/v1/metrics would report whichever worker answered it. Set before
    uvicorn starts them, so every worker inherits it. Emptied first: counts a
    previous run left behind belong to processes that no longer exist.
    """
    if os.environ.get(MULTIPROCESS_ENV):
        directory = Path(os.environ[MULTIPROCESS_ENV])
        directory.mkdir(parents=True, exist_ok=True)
        for leftover in directory.glob("*.db"):
            leftover.unlink()
        return
    os.environ[MULTIPROCESS_ENV] = tempfile.mkdtemp(prefix="eifo-metrics-")


if __name__ == "__main__":
    main()
