"""Asking the enrichers, and reporting what they said.

Enrichers report what they found and never write, for the same reason source
plugins never write: it keeps each provider small enough to test from a
recorded fixture. What a finding *means* - which score may overwrite which,
whether a name is in the wrong script, when the title next falls due - is a
question about the catalog, so it is answered where the catalog is.

What is left here is the loop: ask the API what is due, put each title through
every enricher, and send the answers back in batches. That is the part that
needs the network and the plugins, and it is the part that can run on a machine
the catalog is not on.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from eifo_core import ingest as wire
from eifo_core.enums import FetchStatus
from eifo_core.settings import Settings
from eifo_core.types import utcnow
from eifo_fetcher.enrichers.base import Enricher, EnrichResult, TitleView
from eifo_fetcher.ingest import IngestClient, IngestError
from eifo_fetcher.progress import ProgressTicker, position, remaining
from eifo_fetcher.progress import tally as tally_of
from eifo_fetcher.runs import capture_log
from eifo_fetcher.sources.base import FetchContext, TooManyErrorsError

logger = logging.getLogger("eifo.fetch.enrich")

#: Refusals kept for the run row. The same reasoning as FetchContext's cap: if
#: the parse has gone wrong every title fails the same way, and the first
#: handful says so as well as a thousand would.
MAX_REPORTED_REFUSALS = 20

# What may be written and how often anything is committed moved to
# eifo_core.enriching with the writing itself. Nothing here writes: this side
# asks what is due, asks the enrichers, and reports what they said.


@dataclass(slots=True)
class EnrichResultTally:
    """What one enrichment run did."""

    titles_seen: int = 0
    #: Ratings the enrichers turned up, counted the moment they were returned.
    #:
    #: Separate from ``ratings_written``, which is what the catalog says it
    #: stored and only arrives when a batch is reported. The progress line needs
    #: this one: with a batch of twenty-five, a run reporting only what had
    #: landed said "nothing yet" for the whole first chunk - and "a third of the
    #: way through and nothing found" is precisely the signal it exists to give.
    ratings_found: int = 0
    ratings_written: int = 0
    metadata_updated: int = 0
    aggregates_computed: int = 0
    by_enricher: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def as_stats(self) -> dict[str, Any]:
        return {
            "titles_seen": self.titles_seen,
            "ratings_found": self.ratings_found,
            "ratings_written": self.ratings_written,
            "metadata_updated": self.metadata_updated,
            "aggregates_computed": self.aggregates_computed,
            "by_enricher": self.by_enricher,
            "errors": self.errors,
            "error_count": len(self.errors),
        }


def apply_rate_limits(
    enrichers: list[Enricher],
    ctx: FetchContext,
    settings: Settings,
) -> None:
    """Set each scraped provider's pace before any of them is asked anything.

    Here rather than inside the enrichers because that is where it actually
    happens: an enricher calling ``ctx.apply_rate_limit`` resolved its rate
    from ``[sources.enrich]``, a section that exists nowhere, so the call set
    nothing and every scraped provider ran at the client-wide default.
    """
    for enricher in enrichers:
        if enricher.host is None:
            continue
        rps = settings.enrich.rate_limit_for(enricher.key, enricher.default_rate_limit_rps)
        if rps is None:
            continue
        ctx.http.rate_limiter.set_host_rate(enricher.host, rps)
        logger.info("%s reads %s at %.2f requests/second", enricher.key, enricher.host, rps)


def enrich_titles(
    api: IngestClient,
    enrichers: list[Enricher],
    ctx: FetchContext,
    settings: Settings,
    *,
    force: bool = False,
    limit: int | None = None,
    titles: list[TitleView] | None = None,
    chunk_size: int = wire.ENRICH_CHUNK_SIZE,
) -> EnrichResultTally:
    """Look up whatever is due and report what the enrichers found.

    Opens no database. Which titles are due is a property of the catalog - the
    schedule, the backoff, what was attempted when - so it is asked for rather
    than worked out here, and the findings go back the same way.

    Args:
        titles: enrich exactly these instead of whatever is due. For repairs
            that already know which titles they are about, and for tests.
    """
    started_at = utcnow()
    tally = EnrichResultTally()
    status = FetchStatus.OK
    fatal: str | None = None
    run_id = api.begin_enrich(started_at=started_at)

    apply_rate_limits(enrichers, ctx, settings)
    #: What the catalog would not take. Kept apart from ``ctx.errors``, which
    #: is what went wrong on this side, because the two are gathered at
    #: different moments and the run row wants both.
    refused: list[str] = []

    with capture_log() as captured:
        try:
            wanted = limit if limit is not None else settings.enrich.batch_size
            due = _worklist(api, enrichers, wanted, titles=titles, force=force)
            # Said before the first title, because it is the number that decides
            # whether to wait for this or go to bed: a run with nine titles due
            # and a run with five thousand look identical until it is over.
            logger.info("%d title(s) due for enrichment", len(due))
            ticker = ProgressTicker()
            began = time.monotonic()

            batch: list[dict[str, Any]] = []
            for index, view in enumerate(due, 1):
                tally.titles_seen += 1
                # Every title by name, for anybody who wants to watch it work or
                # to find which one a provider choked on. At DEBUG because a
                # batch of five thousand would otherwise bury the progress lines
                # and spend the run row's whole log budget on a list of titles.
                logger.debug("enriching %s", view.describe())
                found = _findings_for(enrichers, view, ctx, tally)
                if found["findings"]:
                    # Per title, at DEBUG, because "which one did this provider
                    # choke on" is answerable only from a line naming the title.
                    # What was *stored* is the catalog's answer and arrives per
                    # batch; this is what was offered.
                    logger.debug(
                        "%s: %d finding(s) offered", view.describe(), len(found["findings"])
                    )
                batch.append(found)

                if len(batch) >= chunk_size:
                    _report_batch(api, run_id, batch, tally, refused)
                    batch = []
                if ticker.due(index):
                    logger.info("%s", _progress(tally, index, len(due), began))

            if batch:
                _report_batch(api, run_id, batch, tally, refused)
        except TooManyErrorsError as exc:
            logger.error("%s", exc)
            status = FetchStatus.FAILED
            fatal = f"{type(exc).__name__}: {exc}"
        except Exception as exc:
            logger.exception("enrichment failed")
            status = FetchStatus.FAILED
            fatal = f"{type(exc).__name__}: {exc}"

        # The provider failures this side saw, then the refusals the catalog
        # sent back, then whatever ended the run. Assembled rather than assigned:
        # this used to be `tally.errors = list(ctx.errors)`, which ran after the
        # batches had already filed their rejections into the same list and
        # threw every one of them away - so a score refused for being outside
        # its provider's scale, which is always a parser bug, reached the run
        # row as silence.
        tally.errors = [*ctx.errors, *refused]
        if fatal is not None:
            tally.errors.append(f"fatal: {fatal}")

    try:
        outcome = api.finish_enrich(
            run_id,
            status=status,
            errors=tally.errors,
            # What this side knows and the catalog cannot: which provider
            # produced how many of the ratings. The catalog sees findings, not
            # who was asked, so without this the run row could say a thousand
            # ratings were written and nothing about where they came from.
            stats={"by_enricher": tally.by_enricher, "ratings_found": tally.ratings_found},
            log=captured.text(),
        )
    except IngestError as exc:
        logger.warning("could not finish the enrich run: %s", exc)
        return tally

    tally.ratings_written = int(outcome.get("ratings_written", tally.ratings_written))
    tally.metadata_updated = int(outcome.get("metadata_updated", tally.metadata_updated))
    tally.aggregates_computed = int(outcome.get("aggregates_computed", tally.aggregates_computed))
    return tally


def _worklist(
    api: IngestClient,
    enrichers: list[Enricher],
    wanted: int,
    *,
    titles: list[TitleView] | None,
    force: bool,
) -> list[TitleView]:
    """The titles this run is about, from whichever list answers its question.

    Three of them, in order of how specific they are. Titles handed in win:
    a repair already knows what it is about. Then a run made up entirely of
    price enrichers for one service, which walks that service's offers that
    carry no figure. Everything else is the ratings queue.

    The middle case exists because the queue answers the wrong question for a
    price. ``--only apple_prices`` used to ask it anyway, and on a catalog
    where every title had backed off after a fruitless attempt or ten, the
    answer was an empty list: a run that took two seconds, priced nothing, and
    looked for all the world like a finished job.
    """
    if titles is not None:
        return titles

    source_key = _prices_only_for(enrichers)
    if source_key is None:
        return _due(api, wanted, force=force)

    if force:
        # Nothing to override: this list is what is missing, not what is
        # overdue. Said rather than ignored, so a flag that did nothing does
        # not read as a flag that did something.
        logger.info("--force has no effect on a price pass; it walks what has no price")
    found = api.offers_missing_price(source_key=source_key, limit=wanted)
    logger.info("%d %s offer(s) carry no price yet", len(found), source_key)
    return found


def _prices_only_for(enrichers: list[Enricher]) -> str | None:
    """The one service every enricher in this run prices, if that is the run.

    None the moment anything else is in it - a rating provider, or a second
    service's prices - because then the run is about more than one list and the
    queue is the only one that covers all of it.
    """
    declared = {enricher.prices_for for enricher in enrichers}
    if len(declared) != 1:
        return None
    return declared.pop()


def _due(api: IngestClient, wanted: int, *, force: bool) -> list[TitleView]:
    """The queue, in one ask, because it cannot honestly be asked for twice.

    A title is due until something reports on it, so the queue does not move
    while it is being read: a second ask hands back the same head. Treating it
    as pageable meant a nightly run with the default batch of five hundred
    enriched the first hundred titles five times and left the other four
    hundred exactly where they were.

    So one request, and a cap that is said out loud when it bites - a batch
    larger than the far end will answer is a configuration that is quietly not
    doing what it says.
    """
    if wanted <= 0:
        # A batch of nothing is a real thing to ask for - `--limit 0` is how a
        # run exercises everything around the loop without touching a title -
        # and the queue refuses a limit below one, so asking would be a wasted
        # round trip that fails.
        return []
    if wanted > wire.MAX_DUE_PAGE:
        logger.warning(
            "the configured batch of %d is more than the catalog will hand over at once; "
            "enriching %d this run and the rest on the next",
            wanted,
            wire.MAX_DUE_PAGE,
        )
    return api.titles_due(limit=min(wanted, wire.MAX_DUE_PAGE), force=force)


def _findings_for(
    enrichers: list[Enricher],
    view: TitleView,
    ctx: FetchContext,
    tally: EnrichResultTally,
) -> dict[str, Any]:
    """Ask every enricher about one title, and shape the answer for the wire."""
    findings: list[dict[str, Any]] = []
    errored = False

    for enricher in enrichers:
        found = _run_one(enricher, view, ctx, tally)
        if found is None:
            errored = True
        elif not found.is_empty:
            findings.append({"source": enricher.key, "result": wire.finding_to_wire(found)})

    return {"title_id": view.id, "findings": findings, "errored": errored}


def _report_batch(
    api: IngestClient,
    run_id: int,
    batch: list[dict[str, Any]],
    tally: EnrichResultTally,
    refused: list[str],
) -> None:
    """Send one batch of findings, and keep what the far side would not take.

    A refusal is nearly always a parser that has gone wrong - a percentage read
    as a score out of ten - and it is invisible from here unless it is carried
    back and written down. The index is the title's place in this batch, so it
    is resolved to the title before it is recorded: "#3" means nothing by the
    time anybody reads the row.
    """
    answer = api.report(run_id, batch)
    tally.ratings_written += int(answer.get("ratings_written", 0))

    for rejection in answer.get("rejected") or []:
        index = rejection.get("index")
        known = isinstance(index, int) and 0 <= index < len(batch)
        title_id = batch[index]["title_id"] if known else "?"
        message = f"title {title_id}: {rejection.get('reason')}"
        logger.warning("%s", message)
        # Capped for the same reason FetchContext caps its own: a systematically
        # broken parser fails on every title, and these go into a JSON column on
        # a row that is never deleted.
        if len(refused) < MAX_REPORTED_REFUSALS:
            refused.append(message)


def _run_one(
    enricher: Enricher,
    view: TitleView,
    ctx: FetchContext,
    tally: EnrichResultTally,
) -> EnrichResult | None:
    """Ask one enricher about one title, tolerating provider failures.

    Returns:
        What it found, or None if the provider itself failed - a distinction
        the caller needs, because "nobody rates this title" and "this provider
        is down" deserve different waits before trying again.

    It no longer writes anything: what a finding means for the catalog is
    decided where the catalog is. Counting still happens here, because the
    per-enricher tally is about this run rather than about the rows.
    """
    try:
        result = enricher.enrich(view, ctx)
    except TooManyErrorsError:
        raise
    except Exception as exc:
        ctx.record_error(f"{enricher.key} failed for title {view.id}", exc=exc)
        return None

    if result is None or result.is_empty:
        # A provider having nothing on a title is ordinary, not a failure.
        return EnrichResult()

    ctx.record_success()
    found = len(result.ratings)
    tally.ratings_found += found
    tally.by_enricher[enricher.key] = tally.by_enricher.get(enricher.key, 0) + found
    if result.metadata_patch:
        tally.metadata_updated += 1
    return result


def _progress(tally: EnrichResultTally, done: int, total: int, began: float) -> str:
    """One line saying how far into the batch this is, and to what effect.

    The position answers "should I wait for this"; the tally answers "is it
    doing anything" - a run that is a third of the way through and has written
    no ratings at all is a run worth interrupting.
    """
    return "enrich: {}{} - {}".format(
        position(done, total),
        _tail(remaining(done, total, time.monotonic() - began)),
        tally_of(
            ratings=tally.ratings_found,
            metadata_updated=tally.metadata_updated,
            errors=len(tally.errors),
        ),
    )


def _tail(estimate: str | None) -> str:
    """An estimate as a clause, or nothing at all when there is none to give."""
    return f", {estimate}" if estimate else ""
