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
from eifo_fetcher import review
from eifo_fetcher.dedupe import (
    apply_merges,
    dangling_references,
    needs_a_human,
    plan_merges,
)
from eifo_fetcher.http import HttpClient
from eifo_fetcher.lock import AlreadyRunningError, single_flight
from eifo_fetcher.registry import declared_sources, discover_plugins
from eifo_fetcher.runner import enrich_all, fetch_images, repair_names, sync_all
from eifo_fetcher.runs import close_abandoned_runs
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

    repair = subcommands.add_parser(
        "repair-names", help="re-ask TMDB for names stored in the wrong script"
    )
    repair.add_argument("--limit", type=int, default=None, help="stop after N titles")

    dedupe = subcommands.add_parser("dedupe", help="merge titles the catalog holds twice")
    dedupe.add_argument(
        "--apply",
        action="store_true",
        help="perform the merges; without it the plan is only printed",
    )

    sources = subcommands.add_parser("sources", help="inspect configured sources")
    sources.add_subparsers(dest="sources_command", required=True).add_parser(
        "list", help="show every source with its last run"
    )

    review = subcommands.add_parser("review", help="work through unresolved matches")
    review_actions = review.add_subparsers(dest="review_command", required=True)
    listing = review_actions.add_parser("list", help="show unresolved items")
    listing.add_argument("--source", default=None, help="limit to one source")
    listing.add_argument("--limit", type=int, default=50, help="how many to show")
    resolve = review_actions.add_parser("resolve", help="attach an item to a title")
    resolve.add_argument("review_id", type=int)
    resolve.add_argument("--title-id", type=int, required=True)
    skip = review_actions.add_parser("skip", help="not that title - give it one of its own")
    skip.add_argument("review_id", type=int)
    junk = review_actions.add_parser("dismiss", help="not a title at all - never offer it again")
    junk.add_argument("review_id", type=int)
    auto = review_actions.add_parser(
        "auto", help="clear the part of the queue that is not in doubt"
    )
    auto.add_argument(
        "--apply", action="store_true", help="act on it; without this the plan is only counted"
    )

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


def _cmd_repair_names(args: argparse.Namespace, settings: Settings) -> int:
    with single_flight(settings), _database(settings) as session_factory, HttpClient() as http:
        tally = repair_names(session_factory, settings, http=http, limit=args.limit)
    return EXIT_PARTIAL if tally.errors else EXIT_OK


def _cmd_dedupe(args: argparse.Namespace, settings: Settings) -> int:
    """Show what would be merged, and merge it only when asked twice.

    Merging is irreversible and a wrong one silently loses a real title, so the
    plan prints by default and ``--apply`` is the second asking.
    """
    with single_flight(settings), _database(settings) as session_factory, session_factory() as s:
        plans = plan_merges(s)
        for plan in plans:
            print(plan.describe())

        unresolved = needs_a_human(s)
        if not plans:
            print("no confident duplicates found")
        else:
            print(
                f"\n{len(plans)} group(s), {sum(len(p.losers) for p in plans)} title(s) to remove"
            )
        if any(unresolved.values()):
            print(
                f"not touching {unresolved['cross_kind']} film/series pair(s) and "
                f"{unresolved['year_gap']} pair(s) whose years disagree - these need eyes"
            )

        if not args.apply:
            if plans:
                print("\nnothing written; pass --apply to merge")
            return EXIT_OK

        tally = apply_merges(s, plans)
        print(
            f"merged {tally.groups} group(s): removed {tally.titles_removed} title(s), "
            f"moved {tally.availability_moved} offer(s) "
            f"(folded {tally.availability_folded}), {tally.ratings_moved} rating(s), "
            f"{tally.credits_moved} credit(s), {tally.user_items_moved} list entry(s); "
            f"recorded {tally.aliases_recorded} alias(es)"
        )
        for problem in dangling_references(s):
            logger.error("dangling reference after merge: %s", problem)
            tally.errors.append(f"dangling reference: {problem}")
        for failure in tally.errors:
            logger.error("%s", failure)
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
            waiting = review.pending(session, source_key=args.source)
            if not waiting:
                print("nothing awaiting review")
                return EXIT_OK
            rows = [
                (
                    str(item.id),
                    item.source_key,
                    str(item.raw_payload.get("name", ""))[:40],
                    str(item.raw_payload.get("year") or "-"),
                    _closest_label(item),
                )
                for item in waiting[: args.limit]
            ]
            _print_table(("id", "source", "name", "year", "closest match"), rows)
            if len(waiting) > args.limit:
                print(f"\n{len(waiting)} waiting; showing {args.limit}")
            return EXIT_OK

        if args.review_command == "auto":
            return _review_auto(session, apply=args.apply)

        item = session.get(MatchReview, args.review_id)
        if item is None:
            logger.error("no review with id %s", args.review_id)
            return EXIT_FATAL

        if args.review_command == "resolve":
            title = session.get(Title, args.title_id)
            if title is None:
                logger.error("no title with id %s", args.title_id)
                return EXIT_FATAL
            review.attach(session, item, title)
            print(f"review {item.id} attached to title {title.id}, and it is in the catalog now")
        elif args.review_command == "dismiss":
            review.dismiss(session, item)
            print(f"review {item.id} dismissed; it will not be offered again")
        else:
            created = review.create(session, item)
            print(f"review {item.id} became title {created.id if created else '?'}")

        session.commit()
        return EXIT_OK


def _review_auto(session: Session, *, apply: bool) -> int:
    """Clear the part of the queue whose answer is not in doubt."""
    tally = review.auto_resolve(session, apply=apply)
    verb = "cleared" if apply else "would clear"
    print(
        f"{verb} {tally.expired + tally.dismissed + tally.created}: "
        f"{tally.expired} no longer listed, {tally.dismissed} not titles, "
        f"{tally.created} given titles of their own; {tally.left} left for a human"
    )
    if not apply:
        print("\nnothing written; pass --apply to act on it")
    for failure in tally.errors:
        logger.error("%s", failure)
    return EXIT_PARTIAL if tally.errors else EXIT_OK


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
        session_factory = make_session_factory(engine)
        with session_factory() as session:
            close_abandoned_runs(session)
        yield session_factory
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
    "repair-names": _cmd_repair_names,
    "dedupe": _cmd_dedupe,
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
