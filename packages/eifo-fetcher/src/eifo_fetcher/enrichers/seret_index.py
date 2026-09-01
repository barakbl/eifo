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
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from collections import defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from eifo_core.enums import EnrichOutcome, TitleKind
from eifo_core.models import EnrichAttempt, SeretTitle, Title
from eifo_core.types import utcnow
from eifo_fetcher.enrich import view_of
from eifo_fetcher.enrichers.base import TitleView
from eifo_fetcher.enrichers.seret import (
    BASE_URL,
    DEFAULT_RATE_LIMIT_RPS,
    HOST,
    SERET_YEAR_TOLERANCE,
    SeretEntry,
    decode,
    entry_from,
    page_url,
    parse_links,
    parse_title_node,
)
from eifo_fetcher.http import USER_AGENT
from eifo_fetcher.match import normalise, years_match
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

#: Rows written between commits. SQLite takes one writer at a time and every
#: row here costs a request, so the transaction is kept short for the same
#: reason the enrich loop keeps its own short.
COMMIT_EVERY = 50

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
    #: looked. What :func:`wake_titles_newly_covered` works from.
    newly_scorable: list[SeretEntry] = field(default_factory=list)
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
            "newly_scorable": len(self.newly_scorable),
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
        session: Session,
        *,
        limit: int | None = None,
        force: bool = False,
    ) -> IndexResult:
        """Crawl what is due and write it to ``seret_index``.

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

        due = self._due(session, listed, force=force, result=result)
        logger.info(
            "seret: %d pages listed, %d fresh, %d due; reading %d of them this run",
            result.pages_listed,
            result.skipped_fresh,
            len(due),
            min(batch, len(due)),
        )

        self._read(session, due[:batch], result)
        session.commit()

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
        session: Session,
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
        stored = {
            (row.kind, row.seret_id): row for row in session.scalars(select(SeretTitle)).all()
        }
        cutoff = utcnow() - dt.timedelta(days=self._settings.seret.refresh_days)

        unseen: list[tuple[TitleKind, int]] = []
        stale: list[tuple[dt.datetime, tuple[TitleKind, int]]] = []
        dead: list[tuple[dt.datetime, tuple[TitleKind, int]]] = []

        for page in listed:
            row = stored.get(page)
            if row is None:
                unseen.append(page)
            elif force or row.indexed_at < cutoff:
                (dead if row.unreadable else stale).append((row.indexed_at, page))
            else:
                result.skipped_fresh += 1

        unseen.sort(key=lambda page: page[1], reverse=True)
        stale.sort(key=lambda item: item[0])
        dead.sort(key=lambda item: item[0])
        return unseen + [page for _, page in stale] + [page for _, page in dead]

    def _read(
        self,
        session: Session,
        due: list[tuple[TitleKind, int]],
        result: IndexResult,
    ) -> None:
        """Fetch each page and write what it says."""
        ticker = ProgressTicker(every=PROGRESS_EVERY_PAGES, first=PROGRESS_FIRST_PAGES)

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
            _store(session, kind, seret_id, entry, result)

            if result.fetched % COMMIT_EVERY == 0:
                session.commit()
            if ticker.due(result.fetched):
                logger.info("seret: %d of %d pages read", result.fetched, len(due))

    def _get(self, url: str) -> str:
        """A sitemap document. These are UTF-8, unlike the title pages."""
        return self._http.get(url).text


def child_sitemaps(xml: str) -> list[str]:
    """The child documents a sitemap index points at."""
    return [url for url in _LOC.findall(xml) if url.lower().endswith(".xml")]


def _store(
    session: Session,
    kind: TitleKind,
    seret_id: int,
    entry: SeretEntry | None,
    result: IndexResult,
) -> None:
    """Write one page's answer, creating the row or refreshing it.

    A page that carried no title node still gets a row, marked
    ``unreadable``: without one the crawl would pay for that id again on every
    single run, and there are enough withdrawn ids for that to matter.
    """
    row = session.get(SeretTitle, {"kind": kind, "seret_id": seret_id})
    if row is None:
        row = SeretTitle(kind=kind, seret_id=seret_id)
        session.add(row)
        result.created += 1
        had_scores = False
    else:
        result.updated += 1
        had_scores = row.viewers_score is not None or row.critics_score is not None

    row.indexed_at = utcnow()
    row.unreadable = entry is None
    if entry is None:
        return

    # Worth telling the enrich queue about only if this page can score
    # something now and could not before: a page we had never read, or one
    # whose film has been released and rated since we last looked.
    gains_scores = entry.viewers_score is not None or entry.critics_score is not None
    if gains_scores and not had_scores:
        result.newly_scorable.append(entry)

    row.name_he = entry.name_he
    row.name_en = entry.name_en
    row.year = entry.year
    row.imdb_id = entry.imdb_id
    row.viewers_score = entry.viewers_score
    row.viewers_votes = entry.viewers_votes
    row.critics_score = entry.critics_score
    row.url = entry.page_url


def wake_titles_newly_covered(session: Session, entries: list[SeretEntry]) -> int:
    """Bring parked titles forward when the crawl has just learned about them.

    A title nobody could rate backs off for a month, then two, then four. That
    is right when the reason is that no provider carries it, and wrong when the
    reason is that its Seret page had not been read yet - which, while the index
    is still filling in, is most of them. Left alone, a score would sit in
    ``seret_index`` for weeks with the one thing that reads it declining to look.

    The crawl knows which pages it has just made scorable, so it can say which
    of those waits have stopped making sense. Only ``due_at`` moves: the outcome
    and the fruitless count are the enrich pass's to write, and the next
    ordinary run resets them when it succeeds. Nothing here is fetched.

    Returns:
        How many titles were brought forward.
    """
    if not entries:
        return 0

    # Built from this run's pages alone - a few hundred - rather than the whole
    # index, so what comes back is titles that are newly answerable and not
    # every parked title Seret happens to carry.
    lookup = SeretLookup(entries)
    parked = session.scalars(
        select(Title)
        .join(EnrichAttempt, EnrichAttempt.title_id == Title.id)
        .where(
            EnrichAttempt.due_at > utcnow(),
            EnrichAttempt.outcome != EnrichOutcome.OK,
        )
    ).all()

    now = utcnow()
    woken = 0
    for title in parked:
        if lookup.find(view_of(title)) is None:
            continue
        # The relationship is loaded: these titles were reached through it.
        attempt = title.enrich_attempt
        if attempt is not None:
            attempt.due_at = now
            woken += 1

    if woken:
        logger.info("seret: %d parked title(s) are now covered by the index and due again", woken)
    return woken


class SeretLookup:
    """The stored index, in memory, keyed the two ways a title resolves.

    Loaded once per enrich run rather than queried per title: the index is
    thousands of rows against a run of a few hundred titles, and each title
    would otherwise cost a query per name it is known by.

    Resolution is deliberately unwilling to guess. An IMDb id shared by both
    sides is decisive. Failing that a name must match exactly once, after
    normalisation and with the years close enough; a name that matches two
    different Seret pages resolves to neither, because attaching an Israeli
    score to the wrong film is worse than attaching none.
    """

    def __init__(self, entries: Iterable[SeretEntry]) -> None:
        self._by_imdb: dict[str, list[SeretEntry]] = defaultdict(list)
        self._by_name: dict[tuple[TitleKind, str], list[SeretEntry]] = defaultdict(list)
        self._count = 0

        for entry in entries:
            self._count += 1
            if entry.imdb_id:
                self._by_imdb[entry.imdb_id].append(entry)
            for name in entry.names():
                key = normalise(name)
                if key:
                    self._by_name[(entry.kind, key)].append(entry)

    @classmethod
    def load(cls, session: Session) -> SeretLookup:
        """Every usable row of ``seret_index``.

        Rows with no name are left out: an id that carried no title node
        cannot be matched against anything, and keeping it would only make the
        lookup larger.
        """
        return cls(_stored_entries(session))

    def find(self, title: TitleView) -> SeretEntry | None:
        """The Seret page for this title, or None if it cannot be settled."""
        found = self._by_imdb_id(title)
        if found is not None:
            return found
        return self._by_title_name(title)

    def _by_imdb_id(self, title: TitleView) -> SeretEntry | None:
        if not title.imdb_id:
            return None
        candidates = self._by_imdb.get(title.imdb_id, [])
        if len(candidates) == 1:
            return candidates[0]
        # Seret occasionally files a work under both numberings - a miniseries
        # entered as a film as well - and then the kind is the tiebreak.
        same_kind = [entry for entry in candidates if entry.kind is title.kind]
        return same_kind[0] if len(same_kind) == 1 else None

    def _by_title_name(self, title: TitleView) -> SeretEntry | None:
        for name in title.names():
            key = normalise(name)
            if not key:
                continue
            candidates = [
                entry
                for entry in self._by_name.get((title.kind, key), [])
                if years_match(title.year, entry.year, tolerance=SERET_YEAR_TOLERANCE)
            ]
            if len(candidates) == 1:
                return candidates[0]
            if len(candidates) > 1:
                logger.debug(
                    "seret: %r matches %d pages; declining to guess", name, len(candidates)
                )
        return None

    def __len__(self) -> int:
        return self._count


def _stored_entries(session: Session) -> Iterator[SeretEntry]:
    rows = session.scalars(select(SeretTitle).where(SeretTitle.unreadable.is_(False))).all()
    for row in rows:
        if not row.names():
            continue
        yield SeretEntry(
            kind=row.kind,
            seret_id=row.seret_id,
            name_he=row.name_he,
            name_en=row.name_en,
            year=row.year,
            imdb_id=row.imdb_id,
            viewers_score=row.viewers_score,
            viewers_votes=row.viewers_votes,
            critics_score=row.critics_score,
            url=row.url,
        )


def index_status(session: Session) -> dict[str, int]:
    """A count of what the index currently holds, for ``seret status``."""
    rows = session.scalars(select(SeretTitle)).all()
    return {
        "pages": len(rows),
        "movies": sum(1 for row in rows if row.kind is TitleKind.MOVIE),
        "series": sum(1 for row in rows if row.kind is TitleKind.SERIES),
        "with_imdb_id": sum(1 for row in rows if row.imdb_id),
        "with_viewer_score": sum(1 for row in rows if row.viewers_score is not None),
        "with_critic_score": sum(1 for row in rows if row.critics_score is not None),
        "unreadable": sum(1 for row in rows if row.unreadable),
    }
