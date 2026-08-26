"""Orchestration across sources.

One source failing must never stop the others, so every source is isolated:
its exception becomes a failed ``fetch_runs`` row and the run continues. The
process exit code reports whether anything failed.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from eifo_core.enums import FetchPhase, FetchStatus
from eifo_core.settings import Settings
from eifo_core.types import utcnow
from eifo_fetcher.enrich import (
    EnrichResultTally,
    enrich_titles,
    mislabelled_names,
    recompute_all_aggregates,
)
from eifo_fetcher.enrichers import discover_enrichers
from eifo_fetcher.enrichers.imdb import ImdbDatasetLoader
from eifo_fetcher.http import HttpClient
from eifo_fetcher.images import ImageFetcher, ImageResult
from eifo_fetcher.pipeline import (
    SyncResult,
    deactivate_missing_sources,
    register_declared_sources,
    sync_source,
)
from eifo_fetcher.registry import (
    declared_sources,
    discover_plugins,
    enabled_sources,
    plugins_for,
    source_overrides,
)
from eifo_fetcher.runs import capture_log, close_run, open_run
from eifo_fetcher.sources.base import FetchContext, SourcePlugin
from eifo_fetcher.tmdb import IMAGE_HOST, TmdbClient

#: Source key the IMDb bulk pass records itself under. It is one join over a
#: dataset rather than a catalog, so it is not a source, but it needs a name.
IMDB_RUN_KEY = "imdb"

#: The enricher that knows what a title is called in English.
TMDB_ENRICHER_KEY = "tmdb"

logger = logging.getLogger("eifo.fetch.runner")


@dataclass(slots=True)
class SyncReport:
    """Outcome of syncing every requested source."""

    results: list[SyncResult] = field(default_factory=list)
    retired_sources: list[str] = field(default_factory=list)

    @property
    def failed(self) -> list[SyncResult]:
        return [result for result in self.results if result.status is not FetchStatus.OK]

    @property
    def items_seen(self) -> int:
        return sum(result.items_seen for result in self.results)

    @property
    def titles_created(self) -> int:
        return sum(result.titles_created for result in self.results)


def sync_all(
    session_factory: sessionmaker[Session],
    settings: Settings,
    *,
    http: HttpClient,
    only: list[str] | None = None,
    plugins: list[SourcePlugin] | None = None,
) -> SyncReport:
    """Sync every enabled source, or just the ones named in ``only``."""
    plugins = plugins if plugins is not None else discover_plugins()
    # Read per run rather than held: the daemon is long-lived, and a source
    # switched off at midnight should be off tonight without a restart.
    with session_factory() as session:
        overrides = source_overrides(session)
    available = enabled_sources(plugins, settings, overrides=overrides)

    # Every plugin gets a row, switched on or not. Without this a source only
    # existed once it had synced, so one that was off could not be seen - let
    # alone switched on - from the operator's source list.
    with session_factory() as session:
        added = register_declared_sources(session, declared_sources(plugins), enabled=available)
        session.commit()
    if added:
        logger.info("sources now known: %s", ", ".join(sorted(added)))

    if only:
        unknown = sorted(set(only) - set(available))
        if unknown:
            logger.warning("ignoring unknown or disabled sources: %s", ", ".join(unknown))
        wanted = [key for key in only if key in available]
    else:
        wanted = list(available)

    report = SyncReport()
    tmdb = _tmdb_client(http, settings)

    for plugin, owned in plugins_for(plugins, wanted):
        for info in owned:
            with session_factory() as session:
                ctx = FetchContext(source_key=info.key, http=http, settings=settings)
                logger.info("syncing %s", info.key)
                result = sync_source(session, plugin, info, ctx, tmdb=tmdb)
            report.results.append(result)
            logger.info(
                "%s: %s, %d items, %d new titles, %d retired",
                info.key,
                result.status.value,
                result.items_seen,
                result.titles_created,
                result.retired,
            )

    # Only prune when syncing everything: a targeted run says nothing about the
    # sources it was not asked to touch.
    if not only:
        with session_factory() as session:
            report.retired_sources = deactivate_missing_sources(session, available)
            session.commit()
        if report.retired_sources:
            logger.info("retired sources (data kept): %s", ", ".join(report.retired_sources))

    return report


def enrich_all(
    session_factory: sessionmaker[Session],
    settings: Settings,
    *,
    http: HttpClient,
    force: bool = False,
    limit: int | None = None,
    skip_imdb: bool = False,
    skip: Iterable[str] | None = None,
) -> EnrichResultTally:
    """Run the per-title enrichers, then the IMDb bulk pass, then rescore.

    IMDb goes last because it depends on ``imdb_id`` values the TMDB enricher
    fills in, and it runs as one bulk join rather than per title.

    Args:
        skip: enricher keys to leave out of this run only, without touching the
            configured set. What it is for: one enricher can be an order of
            magnitude slower than the rest - ``rt`` is scraped, so it runs at a
            rate chosen to be polite to somebody's website while TMDB answers
            twenty times faster - and a catch-up over a large backlog is a
            different job from a nightly refresh.
    """
    skipped = {key.strip().casefold() for key in (skip or ()) if key.strip()}
    enrichers = [e for e in discover_enrichers(settings) if e.key not in skipped]
    skip_imdb = skip_imdb or IMDB_RUN_KEY in skipped

    unknown = sorted(skipped - {e.key for e in discover_enrichers(settings)} - {IMDB_RUN_KEY})
    if unknown:
        # Said out loud: a typo that silently skips nothing would look like the
        # flag not working, on a run that takes hours.
        logger.warning("nothing to skip called: %s", ", ".join(unknown))

    logger.info("enriching with: %s", ", ".join(e.key for e in enrichers) or "nothing")

    with session_factory() as session:
        ctx = FetchContext(source_key="enrich", http=http, settings=settings)
        tally = enrich_titles(session, enrichers, ctx, settings, force=force, limit=limit)

    if not skip_imdb:
        # Its own row: the bulk pass downloads tens of megabytes and rewrites
        # thousands of ratings, and used to run entirely after the enrich row
        # had been written - so its tally was never persisted and a failure in
        # it left nothing behind at all.
        with session_factory() as session:
            run = open_run(session, phase=FetchPhase.ENRICH, source_key=IMDB_RUN_KEY)
            with capture_log() as captured:
                try:
                    imdb = ImdbDatasetLoader(http).run(session)
                except Exception as exc:
                    logger.exception("imdb dataset pass failed")
                    session.rollback()
                    failure: str | None = f"fatal: {type(exc).__name__}: {exc}"
                else:
                    failure = None
                    tally.by_enricher["imdb"] = imdb.created + imdb.updated

            if failure is not None:
                close_run(
                    session,
                    run,
                    status=FetchStatus.FAILED,
                    stats={"errors": [failure]},
                    log=captured.text(),
                )
            else:
                close_run(
                    session,
                    run,
                    status=FetchStatus.OK,
                    stats=imdb.as_stats(),
                    log=captured.text(),
                )

        # IMDb writes ratings directly, so aggregates need recomputing after it.
        with session_factory() as session:
            tally.aggregates_computed += recompute_all_aggregates(session, settings)

    logger.info(
        "enrich: %d titles, %d ratings, %d aggregates",
        tally.titles_seen,
        tally.ratings_written,
        tally.aggregates_computed,
    )
    return tally


def repair_names(
    session_factory: sessionmaker[Session],
    settings: Settings,
    *,
    http: HttpClient,
    limit: int | None = None,
) -> EnrichResultTally:
    """Re-ask TMDB for the English name of every title stored under another script.

    The nightly pass corrects these on its own now that a wrong-script name can
    be overwritten, but only as each title comes round. This asks about all of
    them at once, which is the difference between a fortnight and a few minutes.

    Only the TMDB enricher runs: it is the one that knows English titles, and
    scraping Rotten Tomatoes for three thousand titles to fix their names would
    be neither quick nor polite.
    """
    enrichers = [e for e in discover_enrichers(settings) if e.key == TMDB_ENRICHER_KEY]
    if not enrichers:
        logger.warning("the TMDB enricher is switched off; nothing can be repaired")
        return EnrichResultTally()

    with session_factory() as session:
        targets = mislabelled_names(session, limit=limit)
        if not targets:
            logger.info("every English name is already in Latin script")
            return EnrichResultTally()

        logger.info("re-asking TMDB about %d mislabelled names", len(targets))
        ctx = FetchContext(source_key="repair-names", http=http, settings=settings)
        tally = enrich_titles(session, enrichers, ctx, settings, titles=targets)

    logger.info(
        "repair-names: %d titles seen, %d corrected", tally.titles_seen, tally.metadata_updated
    )
    return tally


def fetch_images(
    session_factory: sessionmaker[Session],
    settings: Settings,
    *,
    http: HttpClient,
    force: bool = False,
    limit: int | None = None,
) -> ImageResult:
    """Download artwork for titles that still lack it."""
    started_at = utcnow()
    # Most artwork comes from TMDB's image CDN, which was being asked for one
    # poster a second - a static CDN, at the pace set for scraping somebody's
    # website. Anything hosted elsewhere keeps the polite default.
    http.rate_limiter.set_host_rate(IMAGE_HOST, settings.tmdb.rate_limit_rps)
    fetcher = ImageFetcher(http, Path(settings.images_dir))
    with session_factory() as session:
        # FetchPhase.IMAGES existed and had never once been written: poster
        # downloads reported themselves only to a log line that scrolled away.
        run = open_run(session, phase=FetchPhase.IMAGES, started_at=started_at)
        with capture_log() as captured:
            try:
                result = fetcher.fetch_missing(session, force=force, limit=limit)
            except Exception as exc:
                logger.exception("artwork download failed")
                session.rollback()
                close_run(
                    session,
                    run,
                    status=FetchStatus.FAILED,
                    stats={"errors": [f"fatal: {type(exc).__name__}: {exc}"]},
                    log=captured.text(),
                )
                raise
        close_run(
            session,
            run,
            status=FetchStatus.FAILED if result.failed else FetchStatus.OK,
            stats=result.as_stats(),
            log=captured.text(),
        )
    logger.info(
        "images: %d downloaded, %d skipped, %d failed",
        result.downloaded,
        result.skipped,
        result.failed,
    )
    return result


def _tmdb_client(http: HttpClient, settings: Settings) -> TmdbClient | None:
    """A TMDB client when a key is configured, else None.

    The matcher degrades to external ids and local fuzzy matching without one,
    which keeps a keyless install usable for scraped sources.
    """
    if settings.tmdb_api_key is None:
        logger.warning("EIFO_TMDB_API_KEY is not set; TMDB matching is disabled")
        return None
    return TmdbClient(
        http,
        settings.tmdb_api_key.get_secret_value(),
        rate_limit_rps=settings.tmdb.rate_limit_rps,
    )
