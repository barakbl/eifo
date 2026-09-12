"""Orchestration across sources.

One source failing must never stop the others, so every source is isolated:
its exception becomes a failed ``fetch_runs`` row and the run continues. The
process exit code reports whether anything failed.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from typing import Any

from eifo_core.enums import FetchPhase, FetchStatus
from eifo_core.settings import Settings
from eifo_core.types import utcnow
from eifo_fetcher import attempts
from eifo_fetcher.credentials import api_client
from eifo_fetcher.enrich import (
    EnrichResultTally,
    enrich_titles,
)
from eifo_fetcher.enrichers import discover_enrichers
from eifo_fetcher.enrichers.imdb import ImdbDatasetLoader
from eifo_fetcher.enrichers.seret_index import IndexResult, SeretIndexer
from eifo_fetcher.http import HttpClient
from eifo_fetcher.images import ImageFetcher, ImageResult
from eifo_fetcher.ingest import IngestClient, IngestError
from eifo_fetcher.pipeline import SyncResult, sync_source
from eifo_fetcher.prefetch import FetchUnit, Prefetcher
from eifo_fetcher.providers import (
    refresh_declared_providers,
)
from eifo_fetcher.registry import (
    declared_sources,
    discover_plugins,
    enabled_sources,
    plugins_for,
)
from eifo_fetcher.runs import capture_log, new_capture
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

#: Mislabelled names asked about in one repair, when nobody says how many.
#:
#: Its own number rather than the enrich batch size: this is a one-off fixing a
#: backlog of a few thousand, not a nightly pass pacing itself over a catalog,
#: and the endpoint that answers it caps at a thousand anyway.
MISLABELLED_BATCH = 1000

logger = logging.getLogger("eifo.fetch.runner")


@contextmanager
def phase_client(
    settings: Settings, phase: FetchPhase, *, api: IngestClient | None = None
) -> Iterator[IngestClient]:
    """A client for one phase, with the housekeeping every phase wants done.

    Three things, in this order and for three separate reasons:

    * **The attempt is noted for the whole of it**, including the construction
      of the client, because the commonest failure of all - no token, and none
      mintable - happens before there is a client to fail with and would
      otherwise be the one failure that left no trace anywhere.
    * **The previous attempt is posted**, now that there is something to post it
      to. Late, and deliberately: the fetcher that could not reach the server
      had nowhere to put it at the time.
    * **What the plugins declare is declared.** The thing that reads that table
      is a title page being rendered, and a page is rendered between runs rather
      than during one - so hanging it off the one phase that happens to write
      ratings left an upgraded deployment crediting its scores by database key
      until the next nightly had finished.

    Wanted by every phase, so it is here rather than in the CLI: the daemon runs
    the same phases without going through a command line.

    Args:
        api: a connection to borrow instead of opening one, left open on the
            way out. For a caller that has already asked the API a question and
            is now going to act on the answer - the daemon's backfill poll,
            which is a cheap GET every thirty seconds and must not do any of
            the housekeeping above until it finds there is work.
    """
    with attempts.attempted(settings, phase), ExitStack() as stack:
        client = api if api is not None else stack.enter_context(api_client(settings))
        _report_previous_attempt(settings, client)
        refresh_declared_providers(client, settings)
        yield client


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
    settings: Settings,
    *,
    http: HttpClient,
    api: IngestClient,
    only: list[str] | None = None,
    plugins: list[SourcePlugin] | None = None,
) -> SyncReport:
    """Sync every enabled source, or just the ones named in ``only``.

    Takes no ``session_factory``: this phase opens no database. It asks the API
    what is switched on, reads the catalogs, and ships the listings - which is
    what lets it run on a machine the catalog is not on.
    """
    plugins = plugins if plugins is not None else discover_plugins()
    declared = declared_sources(plugins)

    # One exchange: every plugin gets a row whether or not it is switched on,
    # and the answer carries the operator's overrides. Asked per run rather
    # than held, because the daemon is long-lived and a source switched off at
    # midnight should be off tonight without a restart.
    #
    # Retiring only on a full run: a sync of one source says nothing about the
    # ones it was not asked to touch.
    registry = api.register_sources(
        declared,
        enabled=enabled_sources(plugins, settings, overrides={}),
        retire_missing=not only,
    )
    available = enabled_sources(plugins, settings, overrides=registry.get("overrides") or {})

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
                logger.info("syncing %s", unit.info.key)
                result = sync_source(
                    api,
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
        api.clear_backfills(wanted)

    # Retiring happened in the registry exchange above, against what the
    # plugins declare rather than what is switched on: off and gone are
    # different claims, and conflating them badged a live plugin "no longer
    # tracked" on the next full run.
    report.retired_sources = list(registry.get("retired") or [])
    if report.retired_sources:
        logger.info("retired sources (data kept): %s", ", ".join(report.retired_sources))
    for key in registry.get("added") or []:
        logger.info("source now known: %s", key)

    return report


def enrich_options(settings: Settings) -> list[tuple[str, str]]:
    """Every unit an enrich can be narrowed to, and how to say each one.

    The enrichers configuration leaves switched on, plus the two passes that
    are not enrichers but are separately runnable and separately able to fail -
    the IMDb bulk join and the Seret page crawl, both of which already have a
    name here because they get a row of their own in the run log.

    For whoever is offering the choice. The menu-bar app reads this rather than
    keeping its own list, because a list of providers kept in two languages is
    a list that disagrees with itself the first time one is added.
    """
    listed = [(e.key, e.called()) for e in discover_enrichers(settings)]
    listed.append((IMDB_RUN_KEY, "IMDb ratings"))
    listed.append((SERET_INDEX_RUN_KEY, "Seret page index"))
    return listed


def unknown_enrichers(settings: Settings, only: Iterable[str] | None) -> list[str]:
    """The names in ``only`` that nothing here answers to.

    Its own function so a caller can ask before it has opened anything. A
    mistyped name is worth refusing at the moment it can still be explained,
    and refusing it inside the run leaves a row in the log for a run that never
    enriched anything.
    """
    return sorted(_named(only) - dict(enrich_options(settings)).keys())


def _named(keys: Iterable[str] | None) -> set[str]:
    """The keys somebody actually typed, tidied. Blanks are not names."""
    return {key.strip().casefold() for key in (keys or ()) if key.strip()}


def enrich_all(
    settings: Settings,
    *,
    http: HttpClient,
    api: IngestClient,
    force: bool = False,
    limit: int | None = None,
    skip_imdb: bool = False,
    skip: Iterable[str] | None = None,
    only: Iterable[str] | None = None,
) -> EnrichResultTally:
    """Run the per-title enrichers, then the IMDb bulk pass, then rescore.

    IMDb goes last because it depends on ``imdb_id`` values the TMDB enricher
    fills in, and it runs as one bulk join rather than per title.

    Takes no ``session_factory``: like sync, this phase opens no database. What
    is due, what a finding means and when a title next falls due are all
    properties of the catalog, so they are asked for and reported rather than
    decided here - which is what lets an enrich run on a machine the catalog is
    not on.

    Args:
        skip: enricher keys to leave out of this run only, without touching the
            configured set. What it is for: one enricher can be an order of
            magnitude slower than the rest - ``rt`` is scraped, so it runs at a
            rate chosen to be polite to somebody's website while TMDB answers
            twenty times faster - and a catch-up over a large backlog is a
            different job from a nightly refresh.
        only: the mirror of ``skip``, and the one to reach for when the question
            is about a single provider: run these and nothing else. Naming the
            one you want is a great deal harder to get wrong than naming the
            five you do not, and a run narrowed by ``skip`` silently widens
            every time an enricher is added.

    Raises:
        ValueError: when ``only`` names something that is not on offer. Said
            rather than shrugged at, because a mistyped ``--only`` would
            otherwise enrich with nothing at all and report a clean run.
    """
    skipped = _named(skip)
    wanted = _named(only)

    if wanted:
        offered = dict(enrich_options(settings))
        unknown = sorted(wanted - offered.keys())
        if unknown:
            raise ValueError(
                f"no enricher called {', '.join(unknown)}. On offer: {', '.join(sorted(offered))}"
            )
        # `only` decides the whole shape of the run, so it decides these two as
        # well. An --only that left the IMDb download in would be a lie about
        # what it was narrowing the run to, and tens of megabytes of one.
        skipped = offered.keys() - wanted
        skip_imdb = IMDB_RUN_KEY not in wanted

    skip_imdb = skip_imdb or IMDB_RUN_KEY in skipped

    # Before the per-title pass, so pages read tonight are scored tonight
    # rather than waiting for tomorrow's run. Bounded by [seret] batch_size,
    # which is sized to disappear into a nightly run.
    if _seret_is_on(api, settings, skipped):
        index_seret(settings, http=http, api=api)

    # Loaded once for the whole run and handed to the enricher: enrichers are
    # pure readers with no catalog access of their own, and the index is
    # thousands of rows against a batch of a few hundred titles.
    lookup = api.seret_lookup()
    available = discover_enrichers(settings, seret_lookup=lookup)
    enrichers = [e for e in available if e.key not in skipped]

    # What credits each score is declared by phase_client, on the way in to any
    # phase - not here. It used to be here, on the reasoning that the enrich is
    # what produces ratings; that reasoning stopped holding when the table began
    # to be read by a title page rendered between runs rather than during one.
    # Declaring in both places is one redundant request per enrich, which is
    # exactly the sort of thing a log of every call makes obvious.

    unknown = sorted(skipped - {e.key for e in available} - {IMDB_RUN_KEY, SERET_INDEX_RUN_KEY})
    if unknown:
        # Said out loud: a typo that silently skips nothing would look like the
        # flag not working, on a run that takes hours.
        logger.warning("nothing to skip called: %s", ", ".join(unknown))

    logger.info("enriching with: %s", ", ".join(e.key for e in enrichers) or "nothing")
    logger.info("seret page index holds %d titles", len(lookup))

    ctx = FetchContext(source_key="enrich", http=http, settings=settings)
    tally = enrich_titles(api, enrichers, ctx, settings, force=force, limit=limit)

    if not skip_imdb:
        _imdb_pass(http, api, tally)

    logger.info(
        "enrich: %d titles, %d ratings, %d aggregates",
        tally.titles_seen,
        tally.ratings_written,
        tally.aggregates_computed,
    )
    return tally


def _imdb_pass(http: HttpClient, api: IngestClient, tally: EnrichResultTally) -> None:
    """The bulk join over IMDb's dataset, on its own row.

    Its own row because the pass downloads tens of megabytes and rewrites
    thousands of ratings, and used to run entirely after the enrich row had
    been written - so its tally was never persisted and a failure in it left
    nothing behind at all.

    A failure here is recorded and swallowed. It is one provider among several
    and it runs last; losing it should not throw away the ratings every other
    provider has just supplied, nor turn a good enrich into a failed one.
    """
    started_at = utcnow()
    try:
        run_id = api.open_run(FetchPhase.ENRICH, started_at=started_at, source_key=IMDB_RUN_KEY)
    except IngestError as exc:
        logger.warning("could not open a run for the IMDb pass: %s", exc)
        return

    with capture_log() as captured:
        try:
            imdb = ImdbDatasetLoader(http).run(api)
        except Exception as exc:
            logger.exception("imdb dataset pass failed")
            tally.errors.append(f"imdb: {type(exc).__name__}: {exc}")
            _close_quietly(
                api,
                run_id,
                status=FetchStatus.FAILED,
                stats={"errors": [f"fatal: {type(exc).__name__}: {exc}"]},
                log=captured.text(),
            )
            return
        tally.by_enricher[IMDB_RUN_KEY] = imdb.written
        tally.errors.extend(imdb.rejected)

    _close_quietly(
        api,
        run_id,
        status=FetchStatus.FAILED if imdb.rejected else FetchStatus.OK,
        stats=imdb.as_stats(),
        log=captured.text(),
    )

    # IMDb writes ratings without going through the per-title path, so nothing
    # has rescored the titles it touched. One pass over the catalog afterwards
    # rather than one per title during it is the whole reason it is a bulk pass.
    try:
        tally.aggregates_computed += api.rescore()
    except IngestError as exc:
        logger.warning("could not rescore after the IMDb pass: %s", exc)
        tally.errors.append(f"imdb: could not rescore afterwards: {exc}")


def _seret_is_on(api: IngestClient, settings: Settings, skipped: set[str]) -> bool:
    """Whether tonight's enrich should crawl part of Seret's sitemap.

    Three ways it should not, and all three are about not spending somebody
    else's bandwidth for nothing: the index is skipped by name, the enricher
    that reads it is off or skipped, or there is no catalog for it to serve
    yet. The last is the same guard the IMDb pass makes when no title carries
    an ``imdb_id`` - a fresh install should not crawl 8,900 pages to enrich
    nothing.

    The catalog's size is asked for rather than counted: this process has no
    database. A server that will not answer is treated as a reason not to crawl
    - the enrich that follows will fail on its own and say why, and starting a
    patient crawl whose results have nowhere to go would only waste somebody
    else's bandwidth first.

    ``eifo-fetch seret index`` is unconditional: somebody typing that has said
    what they want, including on an empty database.
    """
    if SERET_INDEX_RUN_KEY in skipped or SERET_ENRICHER_KEY in skipped:
        return False
    if not any(e.key == SERET_ENRICHER_KEY for e in discover_enrichers(settings)):
        return False

    try:
        status = api.seret_status()
    except IngestError as exc:
        logger.warning("could not ask whether a Seret crawl is worth it: %s", exc)
        return False
    if not status.get("catalog_titles"):
        logger.info("no titles in the catalog yet; not crawling Seret's sitemap")
        return False
    return True


def index_seret(
    settings: Settings,
    *,
    http: HttpClient,
    api: IngestClient,
    limit: int | None = None,
    rate_limit_rps: float | None = None,
    force: bool = False,
) -> IndexResult:
    """Crawl seret.co.il's sitemap and refresh the stored page index.

    A bulk pass beside the IMDb one, and run from the same place: catalog-wide
    rather than per title, sending what it read in batches, and carrying its own
    row in ``fetch_runs`` so a crawl that fails leaves something behind to look
    at.

    ``[seret] batch_size`` bounds it to about ten minutes, so a first index
    fills itself in over a month of nightly runs instead of holding the site
    for five hours the night somebody upgrades. ``eifo-fetch seret index
    --limit 9000`` is the same pass, told not to hold back.
    """
    started_at = utcnow()
    run_id = api.open_run(FetchPhase.ENRICH, started_at=started_at, source_key=SERET_INDEX_RUN_KEY)
    ctx = FetchContext(source_key=SERET_INDEX_RUN_KEY, http=http, settings=settings)

    with capture_log() as captured:
        try:
            result = SeretIndexer(ctx, rate_limit_rps=rate_limit_rps).run(
                api, limit=limit, force=force
            )
        except Exception as exc:
            # Reported, not raised: this runs inside the nightly enrich, and
            # Seret being down is not a reason to lose the ratings every other
            # provider was about to supply.
            logger.exception("seret index crawl failed")
            failed = IndexResult(errors=[f"fatal: {type(exc).__name__}: {exc}"], error_count=1)
            _close_quietly(
                api,
                run_id,
                status=FetchStatus.FAILED,
                stats=failed.as_stats(),
                log=captured.text(),
            )
            return failed

    _close_quietly(
        api,
        run_id,
        # Errors on individual pages are ordinary over a crawl this wide -
        # withdrawn ids, the odd timeout - and are counted in the stats rather
        # than failing the run. A crawl that could not read the sitemap at all
        # returned above and never reaches here.
        status=FetchStatus.OK,
        stats=result.as_stats(),
        log=captured.text(),
    )

    if result.woken:
        # A title waiting out a month's backoff for a page this crawl has just
        # read should not go on waiting for it. The catalog moves the due dates
        # as the pages land; the next ordinary enrich does the scoring.
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
    settings: Settings,
    *,
    http: HttpClient,
    api: IngestClient,
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

    # Which names are in the wrong script is a question about the catalog, so
    # it is asked rather than worked out here.
    targets = api.mislabelled(limit=limit if limit is not None else MISLABELLED_BATCH)
    if not targets:
        logger.info("every English name is already in Latin script")
        return EnrichResultTally()

    logger.info("re-asking TMDB about %d mislabelled names", len(targets))
    ctx = FetchContext(source_key="repair-names", http=http, settings=settings)
    tally = enrich_titles(api, enrichers, ctx, settings, titles=targets)

    logger.info(
        "repair-names: %d titles seen, %d corrected", tally.titles_seen, tally.metadata_updated
    )
    return tally


def fetch_images(
    settings: Settings,
    *,
    http: HttpClient,
    api: IngestClient,
    force: bool = False,
    limit: int | None = None,
) -> ImageResult:
    """Download artwork for titles that still lack it, and post it to the API.

    Takes no ``session_factory``, and neither does anything else here now: it
    asks the API what is outstanding, sends back what it downloaded, and reports
    the run over the same connection. This was the first phase to work that way
    and is no longer the only one.
    """
    started_at = utcnow()
    # Most artwork comes from TMDB's image CDN, which was being asked for one
    # poster a second - a static CDN, at the pace set for scraping somebody's
    # website. Anything hosted elsewhere keeps the polite default.
    http.rate_limiter.set_host_rate(IMAGE_HOST, settings.tmdb.rate_limit_rps)
    fetcher = ImageFetcher(http, api)

    # FetchPhase.IMAGES existed and had never once been written: poster
    # downloads reported themselves only to a log line that scrolled away.
    run_id = api.open_run(FetchPhase.IMAGES, started_at=started_at)
    with capture_log() as captured:
        try:
            result = fetcher.fetch_missing(force=force, limit=limit)
        except Exception as exc:
            logger.exception("artwork download failed")
            _close_quietly(
                api,
                run_id,
                status=FetchStatus.FAILED,
                stats={"errors": [f"fatal: {type(exc).__name__}: {exc}"]},
                log=captured.text(),
            )
            raise
    _close_quietly(
        api,
        run_id,
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


def _close_quietly(
    api: IngestClient,
    run_id: int,
    *,
    status: FetchStatus,
    stats: dict[str, Any],
    log: str | None,
) -> None:
    """Close the run, and do not let failing to say so become the failure.

    The row is bookkeeping. If the API has gone away between the work and the
    report, the interesting news is whatever the work did - and raising here
    would replace it with a second, less useful exception thrown while handling
    the first.

    The row stays open when it cannot be closed, which is what an unfinished
    run is supposed to look like - and the server closes it once it is old
    enough to be certainly dead. No note is left here for it: the open row is
    already the record, and a note would produce a second row saying the same
    thing.
    """
    try:
        api.close_run(run_id, status=status, stats=stats, log=log)
    except IngestError as exc:
        logger.warning("could not record how the run ended: %s", exc)


def _report_previous_attempt(settings: Settings, api: IngestClient) -> None:
    """Post the run the last attempt could not report, now that we can.

    Late, and deliberately so: a fetcher that could not reach the server had
    nowhere to put this at the time. The alternative is that the night it
    failed leaves no trace at all, which is indistinguishable from a night
    nobody asked it to run.
    """
    previous = attempts.pending(settings)
    if previous is None:
        return

    try:
        run_id = api.open_run(previous.phase, started_at=previous.started_at)
        api.close_run(run_id, status=FetchStatus.CRASHED, stats=previous.as_stats(), log=None)
    except IngestError as exc:
        # Still cannot reach it. The note keeps until something can.
        logger.warning("the previous attempt still cannot be reported: %s", exc)
        return

    logger.info(
        "recorded the %s attempt of %s, which could not report itself: %s",
        previous.phase.value,
        previous.started_at,
        previous.reason,
    )
    attempts.clear(settings)


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
