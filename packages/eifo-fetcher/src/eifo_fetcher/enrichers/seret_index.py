"""The Seret page index: built from the sitemap, read by the enricher.

Seret has no working title search, so there is no way to ask it about a film.
It does publish a sitemap naming all ~8,900 title pages, which turns the problem
into a different one: read each page once, keep what it says, and afterwards
resolving a title is a dictionary lookup.

That crawl is the expensive part of this provider and the only part that talks
to the site, so it is a separate, deliberate job rather than something the
nightly enrich drags along behind it:

* **Gentle.** ``[seret] rate_limit_rps`` defaults to 0.5 - one page every two
  seconds, half the project-wide default - because this is one small site being
  asked for thousands of pages it gains nothing from serving.
* **Bounded.** ``[seret] batch_size`` stops each run after a fixed number of
  pages, so a first index spreads over several nights instead of holding the
  site for five hours in one sitting.
* **Resumable and incremental.** Every row records when it was read. A later
  run fetches what it has never seen and what has gone stale, and skips the
  rest - so the second crawl costs a few dozen requests rather than 8,900.
* **Newest first.** Unseen ids are read highest-first, so a half-finished index
  already covers the films people are actually looking for.

Each page yields both the identity fields (names, year, the IMDb id newer pages
carry) and the three figures Seret reports - audience score, audience vote
count, and the composite editorial "Seret Score". Storing the scores here is
what makes enrichment free: the crawl has the page open anyway, and reading it
again per title would be the same traffic twice.

Nothing here writes to a database. The crawl reads the stored index over the
API, sends back what it read in batches, and is told how many rows that made
and how many parked titles it woke. Which pages are *newly* scorable is
decided on the far side rather than here, and has to be: the question is what
the row about to be overwritten already said, and only the store has ever seen
that row.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from eifo_core import ingest as wire
from eifo_core.enums import TitleKind
from eifo_core.seret import SeretEntry, SeretLookup
from eifo_core.types import utcnow
from eifo_fetcher.enrichers.seret import (
    BASE_URL,
    DEFAULT_RATE_LIMIT_RPS,
    HOST,
    decode,
    entry_from,
    page_url,
    parse_links,
    parse_title_node,
)
from eifo_fetcher.http import USER_AGENT
from eifo_fetcher.ingest import IngestClient, StoredSeretPage
from eifo_fetcher.progress import ProgressTicker
from eifo_fetcher.robots import RobotsPolicy
from eifo_fetcher.sources.base import FetchContext, TooManyErrorsError

logger = logging.getLogger("eifo.fetch.enrich.seret.index")

#: The enricher key this crawl feeds, and so the key its pace is configured
#: under in ``[enrich.rate_limits]``.
SERET_KEY = "seret"

#: Advertised by the site's own robots.txt.
SITEMAP_INDEX_URL = f"{BASE_URL}/Sitemap.xml"

#: Pages between progress lines, and the first line. Each page costs a network
#: round trip at a deliberately slow rate, so these are small: a crawl that
#: reports every 250 pages would say nothing for eight minutes.
PROGRESS_EVERY_PAGES = 100
PROGRESS_FIRST_PAGES = 10

#: Pages sent per request. Every one of them cost a deliberately slow round
#: trip to somebody else's site, so the batch is small: what a failed send
#: costs is that much patient crawling done again.
SEND_EVERY = wire.SERET_WRITE_CHUNK

_LOC = re.compile(r"<loc>\s*(?P<url>[^<\s]+)\s*</loc>", re.IGNORECASE)


class SeretIndexError(RuntimeError):
    """Seret's sitemap could not be read in the shape this expects."""


@dataclass(slots=True)
class IndexResult:
    """What one index crawl did."""

    #: Title pages the sitemap named, across every numbering.
    pages_listed: int = 0
    #: Pages this run asked for, whether or not they answered.
    fetched: int = 0
    created: int = 0
    updated: int = 0
    #: Pages that answered but carried no title node.
    unreadable: int = 0
    #: Pages that can score a title now and could not before this run - new
    #: ones, and ones whose film has been released and rated since we last
    #: looked. Counted by the store, which is the only side that saw what the
    #: row said before this crawl overwrote it.
    newly_scorable: int = 0
    #: Rows left alone because they were fetched recently enough.
    skipped_fresh: int = 0
    #: Pages robots.txt forbids, which are never owed to a later run.
    skipped_disallowed: int = 0
    #: Pages still owed after this run's batch ran out.
    remaining: int = 0
    #: Parked titles brought forward because this run covered them.
    woken: int = 0
    #: Why the crawl stopped early, when it did. The shared consecutive-failure
    #: guard fires when the site goes down or changes shape, and the run row
    #: should say that rather than only showing a short read.
    aborted: str | None = None
    #: The first errors only, as FetchContext caps them; the count is exact.
    errors: list[str] = field(default_factory=list)
    error_count: int = 0

    def as_stats(self) -> dict[str, int | str | list[str] | None]:
        return {
            "pages_listed": self.pages_listed,
            "fetched": self.fetched,
            "created": self.created,
            "updated": self.updated,
            "unreadable": self.unreadable,
            "newly_scorable": self.newly_scorable,
            "skipped_fresh": self.skipped_fresh,
            "skipped_disallowed": self.skipped_disallowed,
            "remaining": self.remaining,
            "woken": self.woken,
            "aborted": self.aborted,
            "errors": self.errors,
            "error_count": self.error_count,
        }


class SeretIndexer:
    """Reads Seret's sitemap and keeps the local page index up to date.

    Takes a :class:`~eifo_fetcher.sources.base.FetchContext` like every other
    thing here that fetches, so error counting, the consecutive-failure guard
    and the cap on recorded messages are the shared ones rather than a second
    set with its own thresholds.

    Args:
        rate_limit_rps: overrides ``[enrich.rate_limits] seret`` for this run
            only, which is what ``eifo-fetch seret index --rps`` sets.
    """

    def __init__(self, ctx: FetchContext, *, rate_limit_rps: float | None = None) -> None:
        self._ctx = ctx
        self._http = ctx.http
        self._settings = ctx.settings
        configured = ctx.settings.enrich.rate_limit_for(SERET_KEY, DEFAULT_RATE_LIMIT_RPS)
        self._rps = rate_limit_rps or configured or DEFAULT_RATE_LIMIT_RPS
        self._robots = RobotsPolicy(user_agent=USER_AGENT)

    def run(
        self,
        api: IngestClient,
        *,
        limit: int | None = None,
        force: bool = False,
    ) -> IndexResult:
        """Crawl what is due and send it to the catalog.

        Args:
            limit: pages this run may fetch, overriding ``[seret] batch_size``.
            force: re-read every page the sitemap names, however fresh the
                stored row is.
        """
        batch = limit if limit is not None else self._settings.seret.batch_size
        result = IndexResult()

        self._http.rate_limiter.set_host_rate(HOST, self._rps)
        logger.info(
            "indexing seret.co.il at %.2f requests/second, up to %d pages", self._rps, batch
        )

        listed = self._discover(result)
        result.pages_listed = len(listed)

        # Every row, unreadable ones included. A page that carried no title is
        # left out of the lookup but not out of this: the whole reason it has a
        # row is so the crawl does not pay for that id again on every run.
        stored = api.seret_index(include_unreadable=True)
        due = self._due(stored, listed, force=force, result=result)
        logger.info(
            "seret: %d pages listed, %d fresh, %d due; reading %d of them this run",
            result.pages_listed,
            result.skipped_fresh,
            len(due),
            min(batch, len(due)),
        )

        self._read(api, due[:batch], result)

        # Counted against the whole due list rather than this run's slice, so a
        # crawl that stopped early reports what is genuinely left rather than
        # what was left of its batch. A page counts as done when it has a row -
        # not when it was merely asked for - so one that failed is still owed,
        # and one robots forbids is owed to nobody.
        done = result.created + result.updated + result.skipped_disallowed
        result.remaining = max(0, len(due) - done)
        result.errors = list(self._ctx.errors)
        result.error_count = self._ctx.error_count

        logger.info(
            "seret index: %d fetched, %d created, %d updated, %d unreadable, "
            "%d errors, %d still to do",
            result.fetched,
            result.created,
            result.updated,
            result.unreadable,
            result.error_count,
            result.remaining,
        )
        return result

    def _discover(self, result: IndexResult) -> list[tuple[TitleKind, int]]:
        """Every title page the sitemap names, films and series alike."""
        self._robots.require_allowed(SITEMAP_INDEX_URL)

        index_xml = self._get(SITEMAP_INDEX_URL)
        pages = parse_links(index_xml)
        seen = set(pages)

        # Followed rather than hard-coded: today only one child sitemap carries
        # title pages, but which one that is is Seret's business to change.
        for child in child_sitemaps(index_xml):
            if not self._robots.allows(child):
                logger.info("skipping %s: robots.txt disallows it", child)
                continue
            try:
                for page in parse_links(self._get(child)):
                    if page not in seen:
                        seen.add(page)
                        pages.append(page)
            except Exception as exc:
                self._ctx.record_error(f"Seret child sitemap {child} failed", exc=exc)

        if not pages:
            raise SeretIndexError(
                f"no title pages in {SITEMAP_INDEX_URL}; "
                f"the sitemap moved or the response was not it"
            )
        return pages

    def _due(
        self,
        stored: Iterable[StoredSeretPage],
        listed: Iterable[tuple[TitleKind, int]],
        *,
        force: bool,
        result: IndexResult,
    ) -> list[tuple[TitleKind, int]]:
        """What to read, in the order it is worth reading.

        Unseen ids first and highest id first, so a crawl that runs out of
        batch has covered the newest films rather than an arbitrary slice;
        then stale rows, longest-unread first; and pages that turned out to
        carry no title last, since they are the least likely to repay a visit.
        """
        rows = {(page.entry.kind, page.entry.seret_id): page for page in stored}
        cutoff = utcnow() - dt.timedelta(days=self._settings.seret.refresh_days)
        # A row whose read time did not survive the wire is treated as never
        # read, which puts it in the stale pile rather than losing it: one page
        # fetched again is a cheaper mistake than a page never revisited.
        never = dt.datetime.min.replace(tzinfo=dt.UTC)

        unseen: list[tuple[TitleKind, int]] = []
        stale: list[tuple[dt.datetime, tuple[TitleKind, int]]] = []
        dead: list[tuple[dt.datetime, tuple[TitleKind, int]]] = []

        for page in listed:
            row = rows.get(page)
            if row is None:
                unseen.append(page)
                continue
            indexed_at = row.indexed_at or never
            if force or indexed_at < cutoff:
                (dead if row.unreadable else stale).append((indexed_at, page))
            else:
                result.skipped_fresh += 1

        unseen.sort(key=lambda page: page[1], reverse=True)
        stale.sort(key=lambda item: item[0])
        dead.sort(key=lambda item: item[0])
        return unseen + [page for _, page in stale] + [page for _, page in dead]

    def _read(
        self,
        api: IngestClient,
        due: list[tuple[TitleKind, int]],
        result: IndexResult,
    ) -> None:
        """Fetch each page and send what it says, a batch at a time."""
        ticker = ProgressTicker(every=PROGRESS_EVERY_PAGES, first=PROGRESS_FIRST_PAGES)
        batch: list[dict[str, Any]] = []

        for kind, seret_id in due:
            url = page_url(kind, seret_id)
            if not self._robots.allows(url):
                logger.info("skipping %s: robots.txt disallows it", url)
                result.skipped_disallowed += 1
                continue

            result.fetched += 1
            try:
                node = parse_title_node(decode(self._http.get(url).content))
            except Exception as exc:
                try:
                    self._ctx.record_error(f"Seret page {kind}/{seret_id} failed", exc=exc)
                except TooManyErrorsError as abort:
                    # A crawl this wide keeps what it has rather than throwing
                    # the run away: the site is down or has changed shape, and
                    # the pages already read are still good.
                    result.aborted = str(abort)
                    logger.error("%s", abort)
                    break
                continue

            self._ctx.record_success()
            entry = None if node is None else entry_from(kind, seret_id, node)
            if entry is None:
                result.unreadable += 1
            batch.append(page_to_wire(kind, seret_id, entry))

            if len(batch) >= SEND_EVERY:
                self._send(api, batch, result)
                batch = []
            if ticker.due(result.fetched):
                logger.info("seret: %d of %d pages read", result.fetched, len(due))

        if batch:
            self._send(api, batch, result)

    def _send(self, api: IngestClient, batch: list[dict[str, Any]], result: IndexResult) -> None:
        """Hand one batch over, and count what the store made of it.

        A batch that will not go is recorded and dropped rather than raised.
        The pages are still listed in the sitemap, so the next run is owed them
        again - and a crawl that has patiently read six hundred pages should
        not throw the other five hundred away because one send failed.
        """
        try:
            answer = api.store_seret_pages(batch)
        except Exception as exc:
            self._ctx.record_error(f"could not store {len(batch)} crawled page(s)", exc=exc)
            return
        result.created += int(answer.get("created", 0))
        result.updated += int(answer.get("updated", 0))
        result.newly_scorable += int(answer.get("newly_scorable", 0))
        result.woken += int(answer.get("woken", 0))

    def _get(self, url: str) -> str:
        """A sitemap document. These are UTF-8, unlike the title pages."""
        return self._http.get(url).text


def child_sitemaps(xml: str) -> list[str]:
    """The child documents a sitemap index points at."""
    return [url for url in _LOC.findall(xml) if url.lower().endswith(".xml")]


def page_to_wire(kind: TitleKind, seret_id: int, entry: SeretEntry | None) -> dict[str, Any]:
    """One page's answer, in the shape the store takes it in.

    A page that carried no title node is still sent, marked ``unreadable``:
    without a row the crawl would pay for that id again on every single run,
    and there are enough withdrawn ids for that to matter.
    """
    if entry is None:
        return {"kind": kind.value, "seret_id": seret_id, "unreadable": True}
    return {
        "kind": kind.value,
        "seret_id": seret_id,
        "unreadable": False,
        "name_he": entry.name_he,
        "name_en": entry.name_en,
        "year": entry.year,
        "imdb_id": entry.imdb_id,
        "viewers_score": entry.viewers_score,
        "viewers_votes": entry.viewers_votes,
        "critics_score": entry.critics_score,
        "url": entry.page_url,
    }


# Reading the index moved to core: both services need it, and only one of them
# has the catalog. The crawl above is what stayed, because crawling is fetching.
__all__ = [
    "IndexResult",
    "SeretEntry",
    "SeretIndexError",
    "SeretIndexer",
    "SeretLookup",
    "child_sitemaps",
    "page_to_wire",
]
