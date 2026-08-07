"""``tvil-fetch`` command line entry point.

Exit codes are cron-friendly and stable across stages:

* 0 — everything succeeded
* 1 — fatal error (bad configuration, unreachable database)
* 2 — completed, but at least one source failed (stage S1 onwards)
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence

from tvil_core import __version__ as core_version
from tvil_core import migrate
from tvil_core.db import create_engine_from_settings
from tvil_core.settings import MissingSettingsError, Settings, get_settings

EXIT_OK = 0
EXIT_FATAL = 1
EXIT_PARTIAL = 2

logger = logging.getLogger("tvil.fetch")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tvil-fetch",
        description="TVIL catalog fetcher and enricher.",
    )
    parser.add_argument("--version", action="version", version=f"tvil-fetch {core_version}")
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="log at DEBUG level",
    )

    subcommands = parser.add_subparsers(dest="command", required=True)

    db = subcommands.add_parser("db", help="database maintenance")
    db_actions = db.add_subparsers(dest="db_command", required=True)

    upgrade = db_actions.add_parser("upgrade", help="apply migrations (creates the schema)")
    upgrade.add_argument("revision", nargs="?", default="head")

    downgrade = db_actions.add_parser("downgrade", help="revert migrations")
    downgrade.add_argument("revision")

    db_actions.add_parser("current", help="show the applied migration revision")

    return parser


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    )


def _cmd_db(args: argparse.Namespace, settings: Settings) -> int:
    if args.db_command == "upgrade":
        logger.info("upgrading %s to %s", settings.db_url, args.revision)
        migrate.upgrade(settings.db_url, args.revision)
        logger.info("schema is up to date")
        return EXIT_OK

    if args.db_command == "downgrade":
        logger.info("downgrading %s to %s", settings.db_url, args.revision)
        migrate.downgrade(settings.db_url, args.revision)
        return EXIT_OK

    engine = create_engine_from_settings(settings)
    try:
        revision = migrate.current_revision(engine)
    finally:
        engine.dispose()
    print(revision or "<no migrations applied>")
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI. Returns a process exit code rather than raising."""
    args = build_parser().parse_args(argv)
    _configure_logging(args.verbose)

    try:
        settings = get_settings()
        if args.command == "db":
            return _cmd_db(args, settings)
    except MissingSettingsError as exc:
        logger.error("%s", exc)
        return EXIT_FATAL
    except Exception:
        logger.exception("tvil-fetch failed")
        return EXIT_FATAL

    # argparse enforces a known subcommand, so this is unreachable in practice.
    return EXIT_FATAL


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
