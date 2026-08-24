"""Orchestration across sources.

One source failing must never stop the others, so every source is isolated:
its exception becomes a failed ``fetch_runs`` row and the run continues. The
process exit code reports whether anything failed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from eifo_core.enums import FetchStatus
from eifo_core.settings import Settings
from eifo_fetcher.enrich import EnrichResultTally, enrich_titles, recompute_all_aggregates
from eifo_fetcher.enrichers import discover_enrichers
from eifo_fetcher.enrichers.imdb import ImdbDatasetLoader
from eifo_fetcher.http import HttpClient
from eifo_fetcher.images import ImageFetcher, ImageResult
from eifo_fetcher.pipeline import SyncResult, deactivate_missing_sources, sync_source
from eifo_fetcher.registry import discover_plugins, enabled_sources, plugins_for
from eifo_fetcher.sources.base import FetchContext, SourcePlugin
from eifo_fetcher.tmdb import IMAGE_HOST, TmdbClient

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
    available = enabled_sources(plugins, settings)

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
) -> EnrichResultTally:
    """Run the per-title enrichers, then the IMDb bulk pass, then rescore.

    IMDb goes last because it depends on ``imdb_id`` values the TMDB enricher
    fills in, and it runs as one bulk join rather than per title.
    """
    enrichers = discover_enrichers(settings)
    logger.info("enriching with: %s", ", ".join(e.key for e in enrichers) or "nothing")

    with session_factory() as session:
        ctx = FetchContext(source_key="enrich", http=http, settings=settings)
        tally = enrich_titles(session, enrichers, ctx, settings, force=force, limit=limit)

    if not skip_imdb:
        with session_factory() as session:
            imdb = ImdbDatasetLoader(http).run(session)
            tally.by_enricher["imdb"] = imdb.created + imdb.updated
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


def fetch_images(
    session_factory: sessionmaker[Session],
    settings: Settings,
    *,
    http: HttpClient,
    force: bool = False,
    limit: int | None = None,
) -> ImageResult:
    """Download artwork for titles that still lack it."""
    # Most artwork comes from TMDB's image CDN, which was being asked for one
    # poster a second - a static CDN, at the pace set for scraping somebody's
    # website. Anything hosted elsewhere keeps the polite default.
    http.rate_limiter.set_host_rate(IMAGE_HOST, settings.tmdb.rate_limit_rps)
    fetcher = ImageFetcher(http, Path(settings.images_dir))
    with session_factory() as session:
        result = fetcher.fetch_missing(session, force=force, limit=limit)
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
