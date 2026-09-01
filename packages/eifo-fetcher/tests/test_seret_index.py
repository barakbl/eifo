"""The Seret page index: the sitemap crawl, and the lookup built from it.

The crawl is the only part of this provider that talks to seret.co.il and it
asks for thousands of pages, so most of what is asserted here is restraint:
that it goes slowly, stops where it was told to, does not ask twice for what it
already has, and gives up when the site stops answering.
"""

from __future__ import annotations

import datetime as dt
import time
from typing import Any

import httpx
import pytest
import respx
from recorded import FIXTURES
from sqlalchemy import select
from sqlalchemy.orm import Session

from eifo_core.enums import EnrichOutcome, TitleKind
from eifo_core.models import EnrichAttempt, SeretTitle, Title
from eifo_core.settings import Settings
from eifo_core.types import utcnow
from eifo_fetcher.enrichers.base import TitleView
from eifo_fetcher.enrichers.seret import BASE_URL, HOST, MOVIE_URL, SERIES_URL, SeretEntry
from eifo_fetcher.enrichers.seret_index import (
    SITEMAP_INDEX_URL,
    SeretIndexer,
    SeretIndexError,
    SeretLookup,
    child_sitemaps,
    index_status,
    wake_titles_newly_covered,
)
from eifo_fetcher.http import HttpClient, RateLimiter
from eifo_fetcher.runner import index_seret
from eifo_fetcher.sources.base import FetchContext

ROBOTS_URL = f"{BASE_URL}/robots.txt"
ROBOTS_TXT = "User-agent: *\nDisallow: /ajax/getExtraMovieRatingsAjax.asp\n"

CHILD_PAGES = "https://www.seret.co.il/Sitemapsite.xml"
CHILD_NEWS = "https://www.seret.co.il/Sitemap-news.xml"

MOVIE_4242 = f"{MOVIE_URL}?MID=4242"
MOVIE_8620 = f"{MOVIE_URL}?MID=8620"
SERIES_268 = f"{SERIES_URL}?SID=268"


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / "seret" / name).read_bytes()


def fixture_text(name: str) -> str:
    return (FIXTURES / "seret" / name).read_text(encoding="utf-8")


def index_settings(**seret: Any) -> Settings:
    return Settings(_env_file=None, seret=seret or {})


def mock_site(*, pages: bool = True) -> None:
    """The sitemap index, its two children, and the three title pages."""
    respx.get(ROBOTS_URL).mock(return_value=httpx.Response(200, text=ROBOTS_TXT))
    respx.get(SITEMAP_INDEX_URL).mock(
        return_value=httpx.Response(200, text=fixture_text("sitemap-index.xml"))
    )
    respx.get(CHILD_PAGES).mock(
        return_value=httpx.Response(200, text=fixture_text("sitemap-pages.xml"))
    )
    respx.get(CHILD_NEWS).mock(
        return_value=httpx.Response(200, text=fixture_text("sitemap-news.xml"))
    )
    if pages:
        respx.get(MOVIE_4242).mock(
            return_value=httpx.Response(200, content=fixture_bytes("movie.html"))
        )
        respx.get(MOVIE_8620).mock(
            return_value=httpx.Response(200, content=fixture_bytes("unrated.html"))
        )
        respx.get(SERIES_268).mock(
            return_value=httpx.Response(200, content=fixture_bytes("series.html"))
        )


def indexer_ctx(http: HttpClient, **seret: Any) -> FetchContext:
    """What the runner hands the crawl: the shared context, error guards and all."""
    return FetchContext(source_key="seret-index", http=http, settings=index_settings(**seret))


def spacing_of(limiter: RateLimiter, host: str) -> float:
    """Seconds the limiter now insists on between two requests to ``host``.

    Read from a moment past every slot the crawl has already claimed, so what
    comes back is the configured interval rather than the tail of the run.
    """
    later = time.monotonic() + 10_000.0
    limiter.wait(host, sleep=lambda _seconds: None, now=lambda: later)
    return limiter.wait(host, sleep=lambda _seconds: None, now=lambda: later)


def rows(session: Session) -> dict[tuple[TitleKind, int], SeretTitle]:
    return {(row.kind, row.seret_id): row for row in session.scalars(select(SeretTitle)).all()}


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


class TestSitemapDiscovery:
    def test_reads_the_children_of_a_sitemap_index(self) -> None:
        assert child_sitemaps(fixture_text("sitemap-index.xml")) == [CHILD_PAGES, CHILD_NEWS]

    def test_a_document_of_page_urls_has_no_children(self) -> None:
        assert child_sitemaps(fixture_text("sitemap-pages.xml")) == []

    @respx.mock
    def test_follows_every_child_and_drops_repeats(
        self, session: Session, http: HttpClient
    ) -> None:
        """The real sitemap lists some pages twice."""
        mock_site()

        result = SeretIndexer(indexer_ctx(http)).run(session)

        assert result.pages_listed == 3
        assert set(rows(session)) == {
            (TitleKind.MOVIE, 4242),
            (TitleKind.MOVIE, 8620),
            (TitleKind.SERIES, 268),
        }

    @respx.mock
    def test_a_sitemap_naming_no_titles_is_a_failure(
        self, session: Session, http: HttpClient
    ) -> None:
        """Better to fail than to quietly conclude Seret has no films."""
        respx.get(ROBOTS_URL).mock(return_value=httpx.Response(200, text=ROBOTS_TXT))
        respx.get(SITEMAP_INDEX_URL).mock(return_value=httpx.Response(200, text="<urlset/>"))

        with pytest.raises(SeretIndexError):
            SeretIndexer(indexer_ctx(http)).run(session)

    @respx.mock
    def test_a_broken_child_does_not_lose_the_rest(
        self, session: Session, http: HttpClient
    ) -> None:
        mock_site()
        respx.get(CHILD_NEWS).mock(return_value=httpx.Response(500))

        result = SeretIndexer(indexer_ctx(http)).run(session)

        assert result.pages_listed == 3
        assert result.error_count == 1


class TestWhatItStores:
    @respx.mock
    def test_keeps_both_audience_figures_and_the_critic_score(
        self, session: Session, http: HttpClient
    ) -> None:
        mock_site()

        SeretIndexer(indexer_ctx(http)).run(session)

        row = rows(session)[(TitleKind.MOVIE, 4242)]
        assert row.viewers_score == 9.1
        assert row.viewers_votes == 42
        assert row.critics_score == 6.8

    @respx.mock
    def test_keeps_what_identity_is_settled_from(self, session: Session, http: HttpClient) -> None:
        mock_site()

        SeretIndexer(indexer_ctx(http)).run(session)

        row = rows(session)[(TitleKind.MOVIE, 4242)]
        assert row.name_he == "פוקסטרוט"
        assert row.name_en == "Foxtrot"
        assert row.year == 2017
        assert row.imdb_id == "tt6896536"
        assert row.url == MOVIE_4242

    @respx.mock
    def test_stores_a_series_under_its_own_numbering(
        self, session: Session, http: HttpClient
    ) -> None:
        mock_site()

        SeretIndexer(indexer_ctx(http)).run(session)

        row = rows(session)[(TitleKind.SERIES, 268)]
        assert row.name_he == "פאודה"
        assert row.viewers_score == 8.4
        assert row.critics_score == 7.9

    @respx.mock
    def test_an_unrated_film_is_indexed_without_inventing_a_zero(
        self, session: Session, http: HttpClient
    ) -> None:
        """It is still worth having: it resolves, it just has nothing to say yet."""
        mock_site()

        SeretIndexer(indexer_ctx(http)).run(session)

        row = rows(session)[(TitleKind.MOVIE, 8620)]
        assert row.name_he == "הרשי"
        assert row.viewers_score is None
        assert row.viewers_votes is None
        assert row.critics_score is None

    @respx.mock
    def test_a_page_with_no_title_is_recorded_rather_than_retried_forever(
        self, session: Session, http: HttpClient
    ) -> None:
        mock_site()
        respx.get(MOVIE_8620).mock(return_value=httpx.Response(200, content=b"<html></html>"))

        first = SeretIndexer(indexer_ctx(http)).run(session)
        assert first.unreadable == 1
        assert rows(session)[(TitleKind.MOVIE, 8620)].unreadable is True

        second = SeretIndexer(indexer_ctx(http)).run(session)
        assert second.fetched == 0
        assert second.skipped_fresh == 3


class TestBeingGentle:
    @respx.mock
    def test_asks_at_the_configured_rate(self, session: Session) -> None:
        """Half a request a second by default: one page every two seconds."""
        limiter = RateLimiter(default_rps=0)
        with HttpClient(rate_limiter=limiter, sleep=lambda _s: None) as http:
            mock_site()
            SeretIndexer(indexer_ctx(http)).run(session)

        assert spacing_of(limiter, HOST) == pytest.approx(2.0)

    @respx.mock
    def test_the_rate_can_be_overridden_for_one_run(self, session: Session) -> None:
        limiter = RateLimiter(default_rps=0)
        with HttpClient(rate_limiter=limiter, sleep=lambda _s: None) as http:
            mock_site()
            SeretIndexer(indexer_ctx(http), rate_limit_rps=4.0).run(session)

        assert spacing_of(limiter, HOST) == pytest.approx(0.25)

    @respx.mock
    def test_the_rate_comes_from_the_shared_enricher_section(self, session: Session) -> None:
        """The same place rt's pace is set, not a section of Seret's own."""
        limiter = RateLimiter(default_rps=0)
        with HttpClient(rate_limiter=limiter, sleep=lambda _s: None) as http:
            mock_site()
            ctx = FetchContext(
                source_key="seret-index",
                http=http,
                settings=Settings(_env_file=None, enrich={"rate_limits": {"seret": 0.2}}),
            )
            SeretIndexer(ctx).run(session)

        assert spacing_of(limiter, HOST) == pytest.approx(5.0)

    @respx.mock
    def test_stops_after_the_batch_and_says_what_is_left(
        self, session: Session, http: HttpClient
    ) -> None:
        mock_site()

        result = SeretIndexer(indexer_ctx(http, batch_size=1)).run(session)

        assert result.fetched == 1
        assert result.remaining == 2

    @respx.mock
    def test_reads_the_newest_ids_first(self, session: Session, http: HttpClient) -> None:
        """A half-built index should already cover the films people look for."""
        mock_site()

        SeretIndexer(indexer_ctx(http, batch_size=1)).run(session)

        assert set(rows(session)) == {(TitleKind.MOVIE, 8620)}

    @respx.mock
    def test_a_second_run_asks_for_nothing_it_already_has(
        self, session: Session, http: HttpClient
    ) -> None:
        mock_site()
        SeretIndexer(indexer_ctx(http)).run(session)

        result = SeretIndexer(indexer_ctx(http)).run(session)

        assert result.fetched == 0
        assert result.skipped_fresh == 3

    @respx.mock
    def test_a_stale_row_is_read_again(self, session: Session, http: HttpClient) -> None:
        mock_site()
        SeretIndexer(indexer_ctx(http)).run(session)
        for row in session.scalars(select(SeretTitle)).all():
            row.indexed_at = utcnow() - dt.timedelta(days=200)
        session.commit()

        result = SeretIndexer(indexer_ctx(http, refresh_days=120)).run(session)

        assert result.fetched == 3
        assert result.updated == 3
        assert result.created == 0

    @respx.mock
    def test_force_reads_everything_however_fresh(self, session: Session, http: HttpClient) -> None:
        mock_site()
        SeretIndexer(indexer_ctx(http)).run(session)

        result = SeretIndexer(indexer_ctx(http)).run(session, force=True)

        assert result.fetched == 3
        assert result.skipped_fresh == 0

    @respx.mock
    def test_gives_up_when_the_site_stops_answering(
        self, session: Session, http: HttpClient
    ) -> None:
        """Rather than spending the whole batch learning the same thing."""
        respx.get(ROBOTS_URL).mock(return_value=httpx.Response(200, text=ROBOTS_TXT))
        respx.get(SITEMAP_INDEX_URL).mock(
            return_value=httpx.Response(200, text=fixture_text("sitemap-index.xml"))
        )
        respx.get(CHILD_PAGES).mock(
            return_value=httpx.Response(
                200,
                text="".join(f"<url><loc>{MOVIE_URL}?MID={n}</loc></url>" for n in range(1, 200)),
            )
        )
        respx.get(CHILD_NEWS).mock(return_value=httpx.Response(200, text="<urlset/>"))
        respx.get(url__startswith=MOVIE_URL).mock(return_value=httpx.Response(503))

        result = SeretIndexer(indexer_ctx(http)).run(session)

        assert result.fetched < 199
        assert result.aborted is not None
        assert "in a row" in result.aborted
        # Every page it asked for failed, so none of them is done: a page
        # counts as read when it has a row, not when it was merely asked for.
        assert result.remaining == 199

    @respx.mock
    def test_never_fetches_a_page_robots_disallows(
        self, session: Session, http: HttpClient
    ) -> None:
        mock_site()
        respx.get(ROBOTS_URL).mock(
            return_value=httpx.Response(200, text="User-agent: *\nDisallow: /series/\n")
        )

        result = SeretIndexer(indexer_ctx(http)).run(session)

        assert (TitleKind.SERIES, 268) not in rows(session)
        # Not owed to a later run either: robots will still forbid it tomorrow.
        assert result.skipped_disallowed == 1
        assert result.remaining == 0


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

    @respx.mock
    def test_loads_from_the_index_the_crawl_wrote(self, session: Session, http: HttpClient) -> None:
        mock_site()
        SeretIndexer(indexer_ctx(http)).run(session)

        lookup = SeretLookup.load(session)

        found = lookup.find(view())
        assert found is not None
        assert (found.viewers_score, found.viewers_votes, found.critics_score) == (9.1, 42, 6.8)

    @respx.mock
    def test_leaves_out_rows_that_carried_no_title(
        self, session: Session, http: HttpClient
    ) -> None:
        mock_site()
        respx.get(MOVIE_8620).mock(return_value=httpx.Response(200, content=b"<html></html>"))
        SeretIndexer(indexer_ctx(http)).run(session)

        assert len(SeretLookup.load(session)) == 2


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

    @respx.mock
    def test_only_pages_that_can_actually_score_are_counted(
        self, session: Session, http: HttpClient
    ) -> None:
        """An unreleased film is indexed but has no score to wake anybody for."""
        mock_site()

        result = SeretIndexer(indexer_ctx(http)).run(session)

        # movie.html and series.html carry scores; unrated.html does not.
        assert result.created == 3
        assert {e.seret_id for e in result.newly_scorable} == {4242, 268}

    @respx.mock
    def test_a_film_that_has_since_been_rated_wakes_its_title(
        self, session: Session, http: HttpClient
    ) -> None:
        """Seret scores appear after release, and the title is parked by then."""
        mock_site()
        SeretIndexer(indexer_ctx(http)).run(session)
        title = self._parked(session, name_he="הרשי", name_en="Hershey", year=2026)

        # The same page, now carrying the ratings it did not have last time.
        respx.get(MOVIE_8620).mock(
            return_value=httpx.Response(200, content=fixture_bytes("movie.html"))
        )
        result = SeretIndexer(indexer_ctx(http)).run(session, force=True)

        assert any(e.seret_id == 8620 for e in result.newly_scorable)
        assert title.enrich_attempt is not None

    @respx.mock
    def test_the_crawl_wakes_them_without_being_asked(
        self, session_factory, settings: Settings, http: HttpClient
    ) -> None:
        """End to end: index_seret does it, so nobody has to remember --force."""
        mock_site()
        with session_factory() as setup:
            self._parked(setup)

        result = index_seret(session_factory, settings, http=http)

        assert result.woken == 1
        with session_factory() as check:
            attempt = check.scalars(select(EnrichAttempt)).one()
            assert attempt.due_at <= utcnow()


class TestStatus:
    @respx.mock
    def test_counts_what_the_index_holds(self, session: Session, http: HttpClient) -> None:
        mock_site()
        SeretIndexer(indexer_ctx(http)).run(session)

        counts = index_status(session)

        assert counts["pages"] == 3
        assert counts["movies"] == 2
        assert counts["series"] == 1
        assert counts["with_viewer_score"] == 2
        assert counts["with_critic_score"] == 2
        assert counts["with_imdb_id"] == 2

    def test_an_empty_index_counts_nothing(self, session: Session) -> None:
        assert index_status(session)["pages"] == 0
