"""Reading Seret's page index: what a title resolves to, and who that wakes.

The crawl that fills the index lives with the fetcher, because crawling is
fetching. Everything here is about *reading* it, and lives in core because both
services need it and only one of them has the catalog: the API resolves nothing
itself, but it is the side that decides which parked titles a batch of freshly
crawled pages has just made answerable.

Resolution is deliberately unwilling to guess, and most of what is asserted
below is that unwillingness. Attaching an Israeli score to the wrong film is
worse than attaching none.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy.orm import Session

from eifo_core.enums import EnrichOutcome, TitleKind
from eifo_core.findings import TitleView
from eifo_core.models import EnrichAttempt, SeretTitle, Title
from eifo_core.seret import (
    SeretEntry,
    SeretLookup,
    index_status,
    page_url,
    wake_titles_newly_covered,
)
from eifo_core.types import utcnow


def entry(**overrides: Any) -> SeretEntry:
    values: dict[str, Any] = {
        "kind": TitleKind.MOVIE,
        "seret_id": 4242,
        "name_he": "פוקסטרוט",
        "name_en": "Foxtrot",
        "year": 2017,
        "viewers_score": 9.1,
        "viewers_votes": 42,
        "critics_score": 6.8,
    }
    values.update(overrides)
    return SeretEntry(**values)


def view(**overrides: Any) -> TitleView:
    values: dict[str, Any] = {
        "id": 1,
        "kind": TitleKind.MOVIE,
        "name_he": "פוקסטרוט",
        "name_en": "Foxtrot",
        "year": 2017,
        "tmdb_id": None,
        "imdb_id": None,
    }
    values.update(overrides)
    return TitleView(**values)


def store(session: Session, *, unreadable: bool = False, **overrides: Any) -> SeretTitle:
    """One row of the stored index, as a crawl would have left it."""
    source = entry(**overrides)
    row = SeretTitle(
        kind=source.kind,
        seret_id=source.seret_id,
        name_he=source.name_he,
        name_en=source.name_en,
        year=source.year,
        imdb_id=source.imdb_id,
        viewers_score=source.viewers_score,
        viewers_votes=source.viewers_votes,
        critics_score=source.critics_score,
        url=source.page_url,
        indexed_at=utcnow(),
        unreadable=unreadable,
    )
    session.add(row)
    session.commit()
    return row


class TestPageAddresses:
    """Films and series are numbered separately and served by two scripts.

    Worth a test of its own because the addresses moved here from the enricher
    - a stored entry has to be able to say where its score can be read - and a
    plausible-looking guess at the shape would 404 every page of a crawl and
    leave a dead link on every score it did store.
    """

    def test_each_kind_goes_to_its_own_endpoint(self) -> None:
        assert page_url(TitleKind.MOVIE, 4242).endswith("/movies/s_movies.asp?MID=4242")
        assert page_url(TitleKind.SERIES, 268).endswith("/series/s_series.asp?SID=268")

    def test_an_entry_falls_back_to_it_when_the_page_named_none(self) -> None:
        """A score is never shown without a link back to whoever gave it."""
        assert entry(url=None).page_url == page_url(TitleKind.MOVIE, 4242)

    def test_but_prefers_what_the_page_declared(self) -> None:
        assert entry(url="https://www.seret.co.il/elsewhere").page_url == (
            "https://www.seret.co.il/elsewhere"
        )


class TestLookup:
    def test_an_imdb_id_settles_it(self) -> None:
        lookup = SeretLookup([entry(name_he="שם אחר", name_en=None, imdb_id="tt6896536")])

        found = lookup.find(view(imdb_id="tt6896536", name_he="פוקסטרוט"))

        assert found is not None
        assert found.seret_id == 4242

    def test_falls_back_to_the_name(self) -> None:
        found = SeretLookup([entry()]).find(view())

        assert found is not None
        assert found.seret_id == 4242

    def test_matches_the_english_name_too(self) -> None:
        found = SeretLookup([entry()]).find(view(name_he=None))

        assert found is not None

    def test_allows_for_a_late_israeli_release(self) -> None:
        assert SeretLookup([entry(year=2019)]).find(view(year=2017)) is not None

    def test_rejects_a_year_further_off_than_that(self) -> None:
        assert SeretLookup([entry(year=2022)]).find(view(year=2017)) is None

    def test_will_not_guess_between_two_pages_of_the_same_name(self) -> None:
        """Attaching a score to the wrong film is worse than attaching none."""
        lookup = SeretLookup([entry(seret_id=1), entry(seret_id=2)])

        assert lookup.find(view()) is None

    def test_a_series_does_not_answer_for_a_film(self) -> None:
        lookup = SeretLookup([entry(kind=TitleKind.SERIES, seret_id=268)])

        assert lookup.find(view(kind=TitleKind.MOVIE)) is None

    def test_an_unknown_title_is_simply_absent(self) -> None:
        assert SeretLookup([entry()]).find(view(name_he="טהרן", name_en="Tehran")) is None

    def test_counts_what_it_holds(self) -> None:
        assert len(SeretLookup([entry(), entry(seret_id=9)])) == 2
        assert not SeretLookup([])


class TestWakingParkedTitles:
    """A backoff should not outlive the reason for it.

    A title nobody could rate waits a month, then two, then four. That is right
    when no provider carries it and wrong when its Seret page simply had not
    been read yet - which, while the index is filling in, is most of them. Left
    alone, a score would sit in ``seret_index`` for weeks with the one thing
    that reads it declining to look.
    """

    def _parked(
        self,
        session: Session,
        *,
        name_he: str = "פוקסטרוט",
        name_en: str | None = "Foxtrot",
        year: int | None = 2017,
        imdb_id: str | None = None,
        outcome: EnrichOutcome = EnrichOutcome.NO_MATCH,
        days: int = 30,
    ) -> Title:
        title = Title(
            type=TitleKind.MOVIE, name_he=name_he, name_en=name_en, year=year, imdb_id=imdb_id
        )
        session.add(title)
        session.flush()
        session.add(
            EnrichAttempt(
                title_id=title.id,
                outcome=outcome,
                fruitless=3,
                due_at=utcnow() + dt.timedelta(days=days),
            )
        )
        session.commit()
        return title

    def test_a_title_the_new_pages_cover_becomes_due_now(self, session: Session) -> None:
        title = self._parked(session)

        woken = wake_titles_newly_covered(session, [entry()])
        session.commit()

        assert woken == 1
        assert title.enrich_attempt is not None
        assert title.enrich_attempt.due_at <= utcnow()

    def test_a_title_they_do_not_cover_stays_parked(self, session: Session) -> None:
        title = self._parked(session, name_he="טהרן", name_en="Tehran", year=2020)
        was_due = title.enrich_attempt.due_at

        assert wake_titles_newly_covered(session, [entry()]) == 0
        assert title.enrich_attempt.due_at == was_due

    def test_it_does_not_touch_the_fruitless_count(self, session: Session) -> None:
        """That is the enrich pass's to write, and it resets on a success."""
        title = self._parked(session)

        wake_titles_newly_covered(session, [entry()])

        assert title.enrich_attempt.fruitless == 3
        assert title.enrich_attempt.outcome is EnrichOutcome.NO_MATCH

    def test_a_title_that_was_scored_is_left_alone(self, session: Session) -> None:
        """It is on the ordinary refresh schedule and will pick Seret up anyway."""
        title = self._parked(session, outcome=EnrichOutcome.OK, days=14)

        assert wake_titles_newly_covered(session, [entry()]) == 0
        assert title.enrich_attempt.due_at > utcnow()

    def test_a_crawl_that_learned_nothing_does_nothing(self, session: Session) -> None:
        self._parked(session)

        assert wake_titles_newly_covered(session, []) == 0


class TestLoadingTheStoredIndex:
    def test_reads_every_usable_row(self, session: Session) -> None:
        store(session)
        store(session, seret_id=9, name_he="טהרן", name_en="Tehran", year=2020)

        assert len(SeretLookup.load(session)) == 2

    def test_leaves_out_rows_that_carried_no_title(self, session: Session) -> None:
        """An id whose page had no title node cannot be matched against anything."""
        store(session)
        store(session, seret_id=9, name_he=None, name_en=None, unreadable=True)

        assert len(SeretLookup.load(session)) == 1

    def test_what_it_loads_still_resolves(self, session: Session) -> None:
        store(session)

        found = SeretLookup.load(session).find(view())

        assert found is not None
        assert (found.viewers_score, found.viewers_votes, found.critics_score) == (9.1, 42, 6.8)


class TestStatus:
    def test_counts_what_the_index_holds(self, session: Session) -> None:
        store(session, imdb_id="tt6896536")
        store(session, kind=TitleKind.SERIES, seret_id=268, imdb_id="tt8003688")
        store(session, seret_id=8620, viewers_score=None, critics_score=None)

        counts = index_status(session)

        assert counts["pages"] == 3
        assert counts["movies"] == 2
        assert counts["series"] == 1
        assert counts["with_viewer_score"] == 2
        assert counts["with_critic_score"] == 2
        assert counts["with_imdb_id"] == 2

    def test_an_empty_index_counts_nothing(self, session: Session) -> None:
        assert index_status(session)["pages"] == 0
