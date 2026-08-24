"""``eifo-fetch`` command line entry point.

Exit codes are cron-friendly and stable across stages:

* 0 - everything succeeded
* 1 - fatal error (bad configuration, unreachable database)
* 2 - completed, but at least one source failed
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from eifo_core import __version__ as core_version
from eifo_core import migrate
from eifo_core.db import create_engine_from_settings, make_session_factory, require_schema
from eifo_core.enums import FetchPhase, FetchStatus
from eifo_core.fts import ensure_search_triggers
from eifo_core.models import Availability, FetchRun, MatchReview, Source, Title
from eifo_core.settings import MissingSettingsError, Settings, get_settings
from eifo_core.types import utcnow
from eifo_fetcher.http import HttpClient
from eifo_fetcher.lock import AlreadyRunningError, single_flight
from eifo_fetcher.registry import declared_sources, discover_plugins
from eifo_fetcher.runner import enrich_all, fetch_images, sync_all
from eifo_fetcher.sources.base import SourceInfo

EXIT_OK = 0
EXIT_FATAL = 1
EXIT_PARTIAL = 2

logger = logging.getLogger("eifo.fetch")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="eifo-fetch",
        description="Eifo catalog fetcher and enricher.",
    )
    parser.add_argument("--version", action="version", version=f"eifo-fetch {core_version}")
    parser.add_argument("-v", "--verbose", action="store_true", help="log at DEBUG level")

    subcommands = parser.add_subparsers(dest="command", required=True)

    sync = subcommands.add_parser("sync", help="pull catalogs and update availability")
    sync.add_argument(
        "--source",
        action="append",
        dest="sources",
        metavar="KEY",
        help="limit to this source; repeatable",
    )

    enrich = subcommands.add_parser("enrich", help="refresh ratings and metadata")
    enrich.add_argument(
        "--force",
        action="store_true",
        help="re-enrich regardless of how fresh the ratings are",
    )
    enrich.add_argument("--limit", type=int, default=None, help="stop after N titles")
    enrich.add_argument(
        "--skip-imdb",
        action="store_true",
        help="skip the IMDb dataset download (tens of megabytes)",
    )

    images = subcommands.add_parser("images", help="download missing artwork")
    images.add_argument("--force", action="store_true", help="re-download existing artwork")
    images.add_argument("--limit", type=int, default=None, help="stop after N titles")

    subcommands.add_parser("all", help="sync, enrich, then fetch artwork")

    sources = subcommands.add_parser("sources", help="inspect configured sources")
    sources.add_subparsers(dest="sources_command", required=True).add_parser(
        "list", help="show every source with its last run"
    )

    review = subcommands.add_parser("review", help="work through unresolved matches")
    review_actions = review.add_subparsers(dest="review_command", required=True)
    review_actions.add_parser("list", help="show unresolved items")
    resolve = review_actions.add_parser("resolve", help="attach an item to a title")
    resolve.add_argument("review_id", type=int)
    resolve.add_argument("--title-id", type=int, required=True)
    skip = review_actions.add_parser("skip", help="dismiss an item without resolving it")
    skip.add_argument("review_id", type=int)

    daemon = subcommands.add_parser("daemon", help="run phases on the configured schedule")
    daemon.add_argument(
        "--once",
        action="store_true",
        help="run every scheduled phase immediately, then exit",
    )

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
    # httpx logs whole request URLs, and TMDB takes its key as a query
    # parameter - so at INFO the key would be written to every log file and
    # into anything those files get pasted into.
    logging.getLogger("httpx").setLevel(logging.WARNING)


# -- commands -------------------------------------------------------------


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


def _cmd_sync(args: argparse.Namespace, settings: Settings) -> int:
    with single_flight(settings), _database(settings) as session_factory, HttpClient() as http:
        report = sync_all(session_factory, settings, http=http, only=args.sources)

    if not report.results:
        logger.warning("no sources were synced; check [sources] in your configuration")
    return EXIT_PARTIAL if report.failed else EXIT_OK


def _cmd_images(args: argparse.Namespace, settings: Settings) -> int:
    with single_flight(settings), _database(settings) as session_factory, HttpClient() as http:
        result = fetch_images(
            session_factory, settings, http=http, force=args.force, limit=args.limit
        )
    return EXIT_PARTIAL if result.failed else EXIT_OK


def _cmd_enrich(args: argparse.Namespace, settings: Settings) -> int:
    with single_flight(settings), _database(settings) as session_factory, HttpClient() as http:
        tally = enrich_all(
            session_factory,
            settings,
            http=http,
            force=args.force,
            limit=args.limit,
            skip_imdb=args.skip_imdb,
        )
    return EXIT_PARTIAL if tally.errors else EXIT_OK


def _cmd_all(_args: argparse.Namespace, settings: Settings) -> int:
    """The nightly run: sync, enrich, then artwork, in dependency order.

    The same function the daemon schedules, so a catalog kept current by cron
    and one kept current by the daemon are kept current identically.
    """
    from eifo_fetcher.daemon import run_nightly

    return EXIT_OK if run_nightly(settings) else EXIT_PARTIAL


def _cmd_sources(_args: argparse.Namespace, settings: Settings) -> int:
    declared = declared_sources(discover_plugins())

    with _database(settings) as session_factory, session_factory() as session:
        stored = {source.key: source for source in session.scalars(select(Source)).all()}
        rows = []
        for key in sorted(set(declared) | set(stored)):
            source = stored.get(key)
            rows.append(
                (
                    key,
                    declared[key].name if key in declared else (source.name if source else key),
                    _source_state(key, declared, source),
                    str(_count_current(session, source)) if source else "-",
                    _last_run_label(session, key),
                )
            )

    _print_table(("source", "name", "state", "titles", "last sync"), rows)
    return EXIT_OK


def _cmd_review(args: argparse.Namespace, settings: Settings) -> int:
    with _database(settings) as session_factory, session_factory() as session:
        if args.review_command == "list":
            pending = session.scalars(
                select(MatchReview).where(MatchReview.resolved_at.is_(None)).limit(100)
            ).all()
            if not pending:
                print("nothing awaiting review")
                return EXIT_OK
            rows = [
                (
                    str(review.id),
                    review.source_key,
                    str(review.raw_payload.get("name", ""))[:40],
                    str(review.raw_payload.get("year") or "-"),
                    _closest_label(review),
                )
                for review in pending
            ]
            _print_table(("id", "source", "name", "year", "closest match"), rows)
            return EXIT_OK

        review = session.get(MatchReview, args.review_id)
        if review is None:
            logger.error("no review with id %s", args.review_id)
            return EXIT_FATAL

        if args.review_command == "resolve":
            title = session.get(Title, args.title_id)
            if title is None:
                logger.error("no title with id %s", args.title_id)
                return EXIT_FATAL
            review.resolved_title_id = title.id

        review.resolved_at = utcnow()
        session.commit()
        print(f"review {review.id} {args.review_command}d")
        return EXIT_OK


def _cmd_daemon(args: argparse.Namespace, settings: Settings) -> int:
    from eifo_fetcher.daemon import run_daemon, run_once

    if args.once:
        return EXIT_OK if run_once(settings) else EXIT_PARTIAL
    return run_daemon(settings)


# -- helpers --------------------------------------------------------------


@contextmanager
def _database(settings: Settings) -> Iterator[sessionmaker[Session]]:
    """A session factory for one command, refusing an unmigrated database.

    This is the process that writes titles, so it is the one that must not write
    into a search index nothing is updating: a rebuild of ``titles`` drops the
    FTS triggers silently, and every title written after that would be invisible
    to search with no sign anything was wrong.
    """
    engine = create_engine_from_settings(settings)
    try:
        require_schema(engine, settings.db_url)
        ensure_search_triggers(engine)
        yield make_session_factory(engine)
    finally:
        engine.dispose()


def _source_state(key: str, declared: Mapping[str, SourceInfo], source: Source | None) -> str:
    if key not in declared:
        return "retired"
    if source is None:
        return "never synced"
    return "active" if source.active else "retired"


def _count_current(session: Session, source: Source) -> int:
    """Titles a source currently offers."""
    return (
        session.scalar(
            select(func.count())
            .select_from(Availability)
            .where(Availability.source_id == source.id, Availability.is_current.is_(True))
        )
        or 0
    )


def _last_run_label(session: Session, key: str) -> str:
    run = session.scalar(
        select(FetchRun)
        .where(FetchRun.source_key == key, FetchRun.phase == FetchPhase.SYNC)
        .order_by(FetchRun.started_at.desc())
        .limit(1)
    )
    if run is None:
        return "-"
    when = run.finished_at or run.started_at
    marker = "" if run.status is FetchStatus.OK else f" ({run.status.value})"
    return f"{when:%Y-%m-%d %H:%M}{marker}"


def _closest_label(review: MatchReview) -> str:
    closest = review.candidates.get("closest") if review.candidates else None
    if not isinstance(closest, dict):
        return "-"
    name = closest.get("name_he") or closest.get("name_en") or "?"
    return f"#{closest.get('title_id')} {name} ({closest.get('similarity')}%)"


def _print_table(headers: tuple[str, ...], rows: Sequence[Sequence[str]]) -> None:
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(str(cell)))

    print("  ".join(header.ljust(widths[i]) for i, header in enumerate(headers)))
    print("  ".join("-" * width for width in widths))
    for row in rows:
        print("  ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(row)))


_COMMANDS = {
    "db": _cmd_db,
    "sync": _cmd_sync,
    "enrich": _cmd_enrich,
    "images": _cmd_images,
    "all": _cmd_all,
    "sources": _cmd_sources,
    "review": _cmd_review,
    "daemon": _cmd_daemon,
}


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI. Returns a process exit code rather than raising."""
    args = build_parser().parse_args(argv)
    _configure_logging(args.verbose)

    try:
        settings = get_settings()
        return _COMMANDS[args.command](args, settings)
    except MissingSettingsError as exc:
        logger.error("%s", exc)
        return EXIT_FATAL
    except AlreadyRunningError as exc:
        # Not a failure: the work is in hand, just not by this process. Cron
        # firing over a long-running daemon must not look like an incident.
        logger.warning("%s", exc)
        return EXIT_OK
    except KeyboardInterrupt:
        logger.warning("interrupted")
        return EXIT_FATAL
    except Exception:
        logger.exception("eifo-fetch failed")
        return EXIT_FATAL


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
