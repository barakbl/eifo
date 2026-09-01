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

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from eifo_core.enums import FetchPhase, FetchStatus
from eifo_core.models import Title
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
from eifo_fetcher.enrichers.seret_index import (
    IndexResult,
    SeretIndexer,
    SeretLookup,
    wake_titles_newly_covered,
)
from eifo_fetcher.http import HttpClient
from eifo_fetcher.images import ImageFetcher, ImageResult
from eifo_fetcher.pipeline import (
    SyncResult,
    clear_backfill_requests,
    deactivate_missing_sources,
    register_declared_sources,
    sync_source,
)
from eifo_fetcher.prefetch import FetchUnit, Prefetcher
from eifo_fetcher.registry import (
    declared_sources,
    discover_plugins,
    enabled_sources,
    plugins_for,
    source_overrides,
)
from eifo_fetcher.runs import capture_log, close_run, new_capture, open_run
from eifo_fetcher.sources.base import FetchContext, SourcePlugin
from eifo_fetcher.tmdb import IMAGE_HOST, TmdbClient

#: Source key the IMDb bulk pass records itself under. It is one join over a
#: dataset rather than a catalog, so it is not a source, but it needs a name.
IMDB_RUN_KEY = "imdb"

#: The enricher that knows what a title is called in English.
TMDB_ENRICHER_KEY = "tmdb"

#: Source key the Seret index crawl records itself under. Like the IMDb pass it
#: is not a catalog, but it is a long job that can fail on its own and so needs
#: a row of its own in ``fetch_runs``.
SERET_INDEX_RUN_KEY = "seret-index"

#: The enricher that reads what the crawl writes.
SERET_ENRICHER_KEY = "seret"

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

    # Flat, and in the order the catalogs will be written: the prefetcher reads
    # them in this same order, which is what keeps the reader ahead of the
    # writer instead of the two waiting on each other.
    units = [
        FetchUnit(
            plugin=plugin,
            info=info,
            ctx=FetchContext(source_key=info.key, http=http, settings=settings),
            # Opened here rather than inside the sync, so that the lines a
            # plugin logs while its catalog is being read - which for a
            # prefetched source is most of what it has to say, and all of it
            # said before its row exists - land on that source's row.
            capture=new_capture(),
        )
        for plugin, owned in plugins_for(plugins, wanted)
        for info in owned
    ]

    if units:
        with Prefetcher(
            units,
            concurrency=settings.fetch.concurrency,
            buffer_size=settings.fetch.buffer_size,
        ) as prefetcher:
            logger.info(
                "syncing %d source(s), reading %d catalog(s) at a time",
                len(units),
                prefetcher.concurrency,
            )
            for unit in units:
                with session_factory() as session:
                    logger.info("syncing %s", unit.info.key)
                    result = sync_source(
                        session,
                        unit.plugin,
                        unit.info,
                        unit.ctx,
                        tmdb=tmdb,
                        items=prefetcher.items(unit),
                        capture=unit.capture,
                    )
                # Whatever is left of this source's stream is nobody's business
                # now. Said out loud because a sync that stopped early leaves a
                # reader parked on a queue, and every later source from the same
                # plugin would queue behind it.
                prefetcher.done(unit)
                report.results.append(result)
                logger.info(
                    "%s: %s, %d items, %d new titles, %d retired",
                    unit.info.key,
                    result.status.value,
                    result.items_seen,
                    result.titles_created,
                    result.retired,
                )

    # An operator's ask is answered by having tried, not by having succeeded: a
    # source whose sync failed has a run in the Runs tab saying so, and leaving
    # the ask standing would put the daemon back on it every half minute. Doing
    # it here rather than in the daemon means a hand-run `eifo-fetch sync`
    # answers the ask too, instead of leaving one queued behind work just done.
    if wanted:
        with session_factory() as session:
            clear_backfill_requests(session, wanted)
            session.commit()

    # Only prune when syncing everything: a targeted run says nothing about the
    # sources it was not asked to touch.
    if not only:
        with session_factory() as session:
            # Against what the plugins declare, not what is switched on. Off and
            # gone are different claims, and this used to make them the same
            # one: turning a source off badged it "no longer tracked" on the
            # next full run, which is untrue of a plugin sitting right there,
            # and the badge outlived being switched back on because only a sync
            # clears it.
            report.retired_sources = deactivate_missing_sources(session, declared_sources(plugins))
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
    skip_imdb = skip_imdb or IMDB_RUN_KEY in skipped

    # Before the per-title pass, so pages read tonight are scored tonight
    # rather than waiting for tomorrow's run. Bounded by [seret] batch_size,
    # which is sized to disappear into a nightly run.
    if _seret_is_on(session_factory, settings, skipped):
        index_seret(session_factory, settings, http=http)

    with session_factory() as session:
        # Loaded here, where there is a session, and handed to the enricher:
        # enrichers are pure readers and have no database access of their own.
        # One query for the whole run rather than one per title per name.
        lookup = SeretLookup.load(session)
        available = discover_enrichers(settings, seret_lookup=lookup)
        enrichers = [e for e in available if e.key not in skipped]

        unknown = sorted(skipped - {e.key for e in available} - {IMDB_RUN_KEY, SERET_INDEX_RUN_KEY})
        if unknown:
            # Said out loud: a typo that silently skips nothing would look like
            # the flag not working, on a run that takes hours.
            logger.warning("nothing to skip called: %s", ", ".join(unknown))

        logger.info("enriching with: %s", ", ".join(e.key for e in enrichers) or "nothing")
        logger.info("seret page index holds %d titles", len(lookup))

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


def _seret_is_on(
    session_factory: sessionmaker[Session],
    settings: Settings,
    skipped: set[str],
) -> bool:
    """Whether tonight's enrich should crawl part of Seret's sitemap.

    Three ways it should not, and all three are about not spending somebody
    else's bandwidth for nothing: the index is skipped by name, the enricher
    that reads it is off or skipped, or there is no catalog for it to serve
    yet. The last is the same guard the IMDb pass makes when no title carries
    an ``imdb_id`` - a fresh install should not crawl 8,900 pages to enrich
    nothing.

    ``eifo-fetch seret index`` is unconditional: somebody typing that has said
    what they want, including on an empty database.
    """
    if SERET_INDEX_RUN_KEY in skipped or SERET_ENRICHER_KEY in skipped:
        return False
    if not any(e.key == SERET_ENRICHER_KEY for e in discover_enrichers(settings)):
        return False

    with session_factory() as session:
        if session.scalar(select(Title.id).limit(1)) is None:
            logger.info("no titles in the catalog yet; not crawling Seret's sitemap")
            return False
    return True


def index_seret(
    session_factory: sessionmaker[Session],
    settings: Settings,
    *,
    http: HttpClient,
    limit: int | None = None,
    rate_limit_rps: float | None = None,
    force: bool = False,
) -> IndexResult:
    """Crawl seret.co.il's sitemap and refresh the local page index.

    A bulk pass beside the IMDb one, and run from the same place: catalog-wide
    rather than per title, owning its session, writing directly, and carrying
    its own row in ``fetch_runs`` so a crawl that fails leaves something behind
    to look at.

    ``[seret] batch_size`` bounds it to about ten minutes, so a first index
    fills itself in over a month of nightly runs instead of holding the site
    for five hours the night somebody upgrades. ``eifo-fetch seret index
    --limit 9000`` is the same pass, told not to hold back.
    """
    with session_factory() as session:
        run = open_run(session, phase=FetchPhase.ENRICH, source_key=SERET_INDEX_RUN_KEY)
        ctx = FetchContext(source_key=SERET_INDEX_RUN_KEY, http=http, settings=settings)
        with capture_log() as captured:
            try:
                result = SeretIndexer(ctx, rate_limit_rps=rate_limit_rps).run(
                    session, limit=limit, force=force
                )
            except Exception as exc:
                # Reported, not raised: this runs inside the nightly enrich, and
                # Seret being down is not a reason to lose the ratings every
                # other provider was about to supply.
                logger.exception("seret index crawl failed")
                session.rollback()
                failed = IndexResult(errors=[f"fatal: {type(exc).__name__}: {exc}"], error_count=1)
                close_run(
                    session,
                    run,
                    status=FetchStatus.FAILED,
                    stats=failed.as_stats(),
                    log=captured.text(),
                )
                return failed

            # A title waiting out a month's backoff for a page this crawl has
            # just read should not go on waiting for it. Moves due dates only;
            # the next ordinary enrich does the scoring.
            result.woken = wake_titles_newly_covered(session, result.newly_scorable)
            session.commit()

        close_run(
            session,
            run,
            # Errors on individual pages are ordinary over a crawl this wide -
            # withdrawn ids, the odd timeout - and are counted in the stats
            # rather than failing the run. A crawl that could not read the
            # sitemap at all returned above and never reaches here.
            status=FetchStatus.OK,
            stats=result.as_stats(),
            log=captured.text(),
        )

    if result.woken:
        logger.info(
            "seret: %d title(s) that had been parked will be scored on the next enrich",
            result.woken,
        )
    if result.remaining:
        # Said every time, because a bounded crawl looks identical to a stalled
        # one from the outside: this is the line that says it is still working
        # through the catalogue rather than stuck.
        logger.info("seret index: %d pages still to read on later runs", result.remaining)
    return result


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
