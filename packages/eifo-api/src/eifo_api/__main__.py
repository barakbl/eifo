"""``eifo-api`` - run the API with uvicorn.

A thin wrapper so ``uv run eifo-api`` binds the right host and port without
anyone having to remember ``--port``. The bare ``uvicorn eifo_api.main:app``
still works; it just falls back to uvicorn's own default of 8000.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence

import uvicorn

from eifo_core.settings import get_settings


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
    args = parser.parse_args(argv)

    uvicorn.run(
        "eifo_api.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )


if __name__ == "__main__":
    main()
