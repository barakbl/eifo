"""Seret's page index, and what a title resolves to in it.

Israeli scores are reached through an index of Seret's own pages rather than by
searching: the site's search is unreliable for exactly the titles that need it
most. The crawl that fills the index lives with the fetcher, because crawling
is fetching. Everything about *reading* the index - what a title resolves to,
and which titles a new page has just made answerable - lives here, because both
services need it and only one of them has the catalog.

Resolution is deliberately unwilling to guess. An IMDb id shared by both sides
is decisive. Failing that a name must match exactly once, after normalisation
and with the years close enough; a name that matches two different Seret pages
resolves to neither, because attaching an Israeli score to the wrong film is
worse than attaching none.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from eifo_core.enriching import view_of
from eifo_core.enums import EnrichOutcome, TitleKind
from eifo_core.findings import TitleView
from eifo_core.match import normalise, years_match
from eifo_core.models import EnrichAttempt, SeretTitle, Title
from eifo_core.types import utcnow

logger = logging.getLogger("eifo.seret")

#: How far Seret's year may sit from the catalog's and still be one title.
#:
#: Wider than :data:`eifo_fetcher.match.YEAR_TOLERANCE` on purpose, and in one
#: direction for a reason: ``datePublished`` here is the *Israeli release date*,
#: not the production year, so it trails what every other source reports. "The
#: Big Short" is 2015 upstream and 2016-01-28 on Seret; a festival film can
#: reach Israeli screens two years after it was made.
SERET_YEAR_TOLERANCE = 2


#: Where the site lives. Here as well as in the enricher because a stored entry
#: has to be able to say where its score can be read, and a score is never shown
#: without a link back to whoever gave it.
SERET_HOST = "www.seret.co.il"
SERET_BASE_URL = f"https://{SERET_HOST}"

#: Which endpoint and id parameter each kind is served from. Films and series
#: are numbered separately and served by two different scripts, so the kind is
#: part of the address rather than a detail of it.
SERET_ENDPOINTS: dict[TitleKind, tuple[str, str]] = {
    TitleKind.MOVIE: (f"{SERET_BASE_URL}/movies/s_movies.asp", "MID"),
    TitleKind.SERIES: (f"{SERET_BASE_URL}/series/s_series.asp", "SID"),
}


def page_url(kind: TitleKind, seret_id: int) -> str:
    """The canonical address of a title page in the right numbering."""
    url, param = SERET_ENDPOINTS[kind]
    return f"{url}?{param}={seret_id}"


@dataclass(frozen=True, slots=True)
class SeretEntry:
    """One Seret page, reduced to what identity and scoring need.

    The same shape whether it came from the stored index or from a page just
    fetched, so the enricher does not care which it is holding.
    """

    kind: TitleKind
    seret_id: int
    name_he: str | None = None
    name_en: str | None = None
    year: int | None = None
    imdb_id: str | None = None
    #: The audience score on Seret's own 0-10 scale, and its vote count.
    viewers_score: float | None = None
    viewers_votes: int | None = None
    #: "Seret Score", the site's composite editorial figure, also 0-10.
    critics_score: float | None = None
    url: str | None = None

    @property
    def page_url(self) -> str:
        """Where to send a reader. A score is never shown without one."""
        return self.url or page_url(self.kind, self.seret_id)

    def names(self) -> list[str]:
        """Every name this page gives the title, Hebrew first."""
        return [name for name in (self.name_he, self.name_en) if name]


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
