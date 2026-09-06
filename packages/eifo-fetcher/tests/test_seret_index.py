"""The Seret page index: the sitemap crawl that fills it.

The crawl is the only part of this provider that talks to seret.co.il and it
asks for thousands of pages, so most of what is asserted here is restraint:
that it goes slowly, stops where it was told to, does not ask twice for what it
already has, and gives up when the site stops answering.

It writes through the API like everything else, so the far end below is the
real application over a real catalog. Reading the index - what a title
resolves to, which parked titles a new page wakes - is tested in eifo-core,
because that is where it happens: this side sends what it read and is told
what that made of it.
"""

from __future__ import annotations

import datetime as dt
import time
from typing import Any

import httpx
import pytest
import respx
from live import LiveApi
from recorded import FIXTURES
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from eifo_core.enums import EnrichOutcome, FetchPhase, FetchStatus, TitleKind
from eifo_core.models import EnrichAttempt, FetchRun, SeretTitle, Title
from eifo_core.settings import Settings
from eifo_core.types import utcnow
from eifo_fetcher.enrichers.base import TitleView
from eifo_fetcher.enrichers.seret import BASE_URL, HOST, MOVIE_URL, SERIES_URL, SeretEntry
from eifo_fetcher.enrichers.seret_index import (
    SITEMAP_INDEX_URL,
    SeretIndexer,
    SeretIndexError,
    child_sitemaps,
)
from eifo_fetcher.http import HttpClient, RateLimiter
from eifo_fetcher.ingest import IngestClient
from eifo_fetcher.runner import SERET_INDEX_RUN_KEY, index_seret
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


@pytest.fixture
def session_factory(live_api: LiveApi) -> sessionmaker[Session]:
    """The catalog the crawl sends its pages to."""
    return live_api.session_factory


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
        self, api: IngestClient, session: Session, http: HttpClient
    ) -> None:
        """The real sitemap lists some pages twice."""
        mock_site()

        result = SeretIndexer(indexer_ctx(http)).run(api)

        assert result.pages_listed == 3
        assert set(rows(session)) == {
            (TitleKind.MOVIE, 4242),
            (TitleKind.MOVIE, 8620),
            (TitleKind.SERIES, 268),
        }

    @respx.mock
    def test_a_sitemap_naming_no_titles_is_a_failure(
        self, api: IngestClient, session: Session, http: HttpClient
    ) -> None:
        """Better to fail than to quietly conclude Seret has no films."""
        respx.get(ROBOTS_URL).mock(return_value=httpx.Response(200, text=ROBOTS_TXT))
        respx.get(SITEMAP_INDEX_URL).mock(return_value=httpx.Response(200, text="<urlset/>"))

        with pytest.raises(SeretIndexError):
            SeretIndexer(indexer_ctx(http)).run(api)

    @respx.mock
    def test_a_broken_child_does_not_lose_the_rest(
        self, api: IngestClient, session: Session, http: HttpClient
    ) -> None:
        mock_site()
        respx.get(CHILD_NEWS).mock(return_value=httpx.Response(500))

        result = SeretIndexer(indexer_ctx(http)).run(api)

        assert result.pages_listed == 3
        assert result.error_count == 1


class TestWhatItStores:
    @respx.mock
    def test_keeps_both_audience_figures_and_the_critic_score(
        self, api: IngestClient, session: Session, http: HttpClient
    ) -> None:
        mock_site()

        SeretIndexer(indexer_ctx(http)).run(api)

        row = rows(session)[(TitleKind.MOVIE, 4242)]
        assert row.viewers_score == 9.1
        assert row.viewers_votes == 42
        assert row.critics_score == 6.8

    @respx.mock
    def test_keeps_what_identity_is_settled_from(
        self, api: IngestClient, session: Session, http: HttpClient
    ) -> None:
        mock_site()

        SeretIndexer(indexer_ctx(http)).run(api)

        row = rows(session)[(TitleKind.MOVIE, 4242)]
        assert row.name_he == "פוקסטרוט"
        assert row.name_en == "Foxtrot"
        assert row.year == 2017
        assert row.imdb_id == "tt6896536"
        assert row.url == MOVIE_4242

    @respx.mock
    def test_stores_a_series_under_its_own_numbering(
        self, api: IngestClient, session: Session, http: HttpClient
    ) -> None:
        mock_site()

        SeretIndexer(indexer_ctx(http)).run(api)

        row = rows(session)[(TitleKind.SERIES, 268)]
        assert row.name_he == "פאודה"
        assert row.viewers_score == 8.4
        assert row.critics_score == 7.9

    @respx.mock
    def test_an_unrated_film_is_indexed_without_inventing_a_zero(
        self, api: IngestClient, session: Session, http: HttpClient
    ) -> None:
        """It is still worth having: it resolves, it just has nothing to say yet."""
        mock_site()

        SeretIndexer(indexer_ctx(http)).run(api)

        row = rows(session)[(TitleKind.MOVIE, 8620)]
        assert row.name_he == "הרשי"
        assert row.viewers_score is None
        assert row.viewers_votes is None
        assert row.critics_score is None

    @respx.mock
    def test_a_page_with_no_title_is_recorded_rather_than_retried_forever(
        self, api: IngestClient, session: Session, http: HttpClient
    ) -> None:
        mock_site()
        respx.get(MOVIE_8620).mock(return_value=httpx.Response(200, content=b"<html></html>"))

        first = SeretIndexer(indexer_ctx(http)).run(api)
        assert first.unreadable == 1
        assert rows(session)[(TitleKind.MOVIE, 8620)].unreadable is True

        second = SeretIndexer(indexer_ctx(http)).run(api)
        assert second.fetched == 0
        assert second.skipped_fresh == 3


class TestBeingGentle:
    @respx.mock
    def test_asks_at_the_configured_rate(self, api: IngestClient) -> None:
        """Half a request a second by default: one page every two seconds."""
        limiter = RateLimiter(default_rps=0)
        with HttpClient(rate_limiter=limiter, sleep=lambda _s: None) as http:
            mock_site()
            SeretIndexer(indexer_ctx(http)).run(api)

        assert spacing_of(limiter, HOST) == pytest.approx(2.0)

    @respx.mock
    def test_the_rate_can_be_overridden_for_one_run(self, api: IngestClient) -> None:
        limiter = RateLimiter(default_rps=0)
        with HttpClient(rate_limiter=limiter, sleep=lambda _s: None) as http:
            mock_site()
            SeretIndexer(indexer_ctx(http), rate_limit_rps=4.0).run(api)

        assert spacing_of(limiter, HOST) == pytest.approx(0.25)

    @respx.mock
    def test_the_rate_comes_from_the_shared_enricher_section(self, api: IngestClient) -> None:
        """The same place rt's pace is set, not a section of Seret's own."""
        limiter = RateLimiter(default_rps=0)
        with HttpClient(rate_limiter=limiter, sleep=lambda _s: None) as http:
            mock_site()
            ctx = FetchContext(
                source_key="seret-index",
                http=http,
                settings=Settings(_env_file=None, enrich={"rate_limits": {"seret": 0.2}}),
            )
            SeretIndexer(ctx).run(api)

        assert spacing_of(limiter, HOST) == pytest.approx(5.0)

    @respx.mock
    def test_stops_after_the_batch_and_says_what_is_left(
        self, api: IngestClient, session: Session, http: HttpClient
    ) -> None:
        mock_site()

        result = SeretIndexer(indexer_ctx(http, batch_size=1)).run(api)

        assert result.fetched == 1
        assert result.remaining == 2

    @respx.mock
    def test_reads_the_newest_ids_first(
        self, api: IngestClient, session: Session, http: HttpClient
    ) -> None:
        """A half-built index should already cover the films people look for."""
        mock_site()

        SeretIndexer(indexer_ctx(http, batch_size=1)).run(api)

        assert set(rows(session)) == {(TitleKind.MOVIE, 8620)}

    @respx.mock
    def test_a_second_run_asks_for_nothing_it_already_has(
        self, api: IngestClient, session: Session, http: HttpClient
    ) -> None:
        mock_site()
        SeretIndexer(indexer_ctx(http)).run(api)

        result = SeretIndexer(indexer_ctx(http)).run(api)

        assert result.fetched == 0
        assert result.skipped_fresh == 3

    @respx.mock
    def test_a_stale_row_is_read_again(
        self, api: IngestClient, session: Session, http: HttpClient
    ) -> None:
        mock_site()
        SeretIndexer(indexer_ctx(http)).run(api)
        for row in session.scalars(select(SeretTitle)).all():
            row.indexed_at = utcnow() - dt.timedelta(days=200)
        session.commit()

        result = SeretIndexer(indexer_ctx(http, refresh_days=120)).run(api)

        assert result.fetched == 3
        assert result.updated == 3
        assert result.created == 0

    @respx.mock
    def test_force_reads_everything_however_fresh(
        self, api: IngestClient, session: Session, http: HttpClient
    ) -> None:
        mock_site()
        SeretIndexer(indexer_ctx(http)).run(api)

        result = SeretIndexer(indexer_ctx(http)).run(api, force=True)

        assert result.fetched == 3
        assert result.skipped_fresh == 0

    @respx.mock
    def test_gives_up_when_the_site_stops_answering(
        self, api: IngestClient, session: Session, http: HttpClient
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

        result = SeretIndexer(indexer_ctx(http)).run(api)

        assert result.fetched < 199
        assert result.aborted is not None
        assert "in a row" in result.aborted
        # Every page it asked for failed, so none of them is done: a page
        # counts as read when it has a row, not when it was merely asked for.
        assert result.remaining == 199

    @respx.mock
    def test_never_fetches_a_page_robots_disallows(
        self, api: IngestClient, session: Session, http: HttpClient
    ) -> None:
        mock_site()
        respx.get(ROBOTS_URL).mock(
            return_value=httpx.Response(200, text="User-agent: *\nDisallow: /series/\n")
        )

        result = SeretIndexer(indexer_ctx(http)).run(api)

        assert (TitleKind.SERIES, 268) not in rows(session)
        # Not owed to a later run either: robots will still forbid it tomorrow.
        assert result.skipped_disallowed == 1
        assert result.remaining == 0


class TestWhatTheCatalogMakesOfIt:
    """The crawl sends pages and is told what they changed.

    It cannot work any of this out itself: whether a page is new, and whether it
    can score something it could not score before, are both questions about the
    row being overwritten - and only the catalog has ever seen that row.
    """

    @respx.mock
    def test_only_pages_that_can_actually_score_are_counted(
        self, api: IngestClient, http: HttpClient
    ) -> None:
        """An unreleased film is indexed but has no score to wake anybody for."""
        mock_site()

        result = SeretIndexer(indexer_ctx(http)).run(api)

        # movie.html and series.html carry scores; unrated.html does not.
        assert result.created == 3
        assert result.newly_scorable == 2

    @respx.mock
    def test_a_page_read_again_is_not_newly_anything(
        self, api: IngestClient, http: HttpClient
    ) -> None:
        """Re-reading last month's page must not wake a title all over again."""
        mock_site()
        SeretIndexer(indexer_ctx(http)).run(api)

        result = SeretIndexer(indexer_ctx(http)).run(api, force=True)

        assert result.updated == 3
        assert result.newly_scorable == 0

    @respx.mock
    def test_a_film_that_has_since_been_rated_is_newly_scorable(
        self, api: IngestClient, http: HttpClient
    ) -> None:
        """Seret scores appear after release, and the title is parked by then."""
        mock_site()
        SeretIndexer(indexer_ctx(http)).run(api)

        # The same page, now carrying the ratings it did not have last time.
        respx.get(MOVIE_8620).mock(
            return_value=httpx.Response(200, content=fixture_bytes("movie.html"))
        )
        result = SeretIndexer(indexer_ctx(http)).run(api, force=True)

        assert result.newly_scorable == 1

    @respx.mock
    def test_a_batch_that_will_not_send_does_not_lose_the_rest_of_the_crawl(
        self, live_api: LiveApi, http: HttpClient
    ) -> None:
        """Six hundred patiently-read pages must not go for one failed request.

        The pages are still in the sitemap, so the next run is owed them again -
        which is a far cheaper answer than throwing away everything read so far.
        """
        mock_site()
        calls = {"n": 0}

        def failing(pages: list[Any]) -> Any:
            calls["n"] += 1
            raise RuntimeError("the connection dropped")

        live_api.api.store_seret_pages = failing  # type: ignore[method-assign]

        result = SeretIndexer(indexer_ctx(http)).run(live_api.api)

        assert calls["n"] == 1
        assert result.fetched == 3
        assert result.created == 0
        assert any("could not store" in error for error in result.errors)


class TestReadingBackWhatItWrote:
    @respx.mock
    def test_the_lookup_is_built_from_what_the_crawl_stored(
        self, api: IngestClient, http: HttpClient
    ) -> None:
        """The whole point of the crawl, asserted across the wire."""
        mock_site()
        SeretIndexer(indexer_ctx(http)).run(api)

        found = api.seret_lookup().find(view())

        assert found is not None
        assert (found.viewers_score, found.viewers_votes, found.critics_score) == (9.1, 42, 6.8)

    @respx.mock
    def test_rows_that_carried_no_title_are_left_out_of_it(
        self, api: IngestClient, http: HttpClient
    ) -> None:
        mock_site()
        respx.get(MOVIE_8620).mock(return_value=httpx.Response(200, content=b"<html></html>"))
        SeretIndexer(indexer_ctx(http)).run(api)

        assert len(api.seret_lookup()) == 2

    @respx.mock
    def test_but_the_crawl_still_sees_them(self, api: IngestClient, http: HttpClient) -> None:
        """Or it would pay for that id again on every single run."""
        mock_site()
        respx.get(MOVIE_8620).mock(return_value=httpx.Response(200, content=b"<html></html>"))
        SeretIndexer(indexer_ctx(http)).run(api)

        assert len(api.seret_index(include_unreadable=True)) == 3

    @respx.mock
    def test_the_index_is_paged_by_the_whole_key(self, api: IngestClient, http: HttpClient) -> None:
        """Films and series are numbered apart, so an id alone does not order it.

        A page boundary landing between a film and a series that share an id
        would drop whichever came second - silently, and only for the ids that
        happen to fall there.
        """
        mock_site()
        SeretIndexer(indexer_ctx(http)).run(api)

        stored = api.seret_index(include_unreadable=True)

        keys = [(page.entry.kind, page.entry.seret_id) for page in stored]
        assert len(keys) == len(set(keys)) == 3

    @respx.mock
    def test_when_a_page_was_last_read_survives_the_wire(
        self, api: IngestClient, http: HttpClient
    ) -> None:
        """The crawl decides staleness by it and cannot work it out from here."""
        mock_site()
        SeretIndexer(indexer_ctx(http)).run(api)

        stored = api.seret_index(include_unreadable=True)

        assert all(page.indexed_at is not None for page in stored)


class TestWakingParkedTitles:
    """End to end: the crawl wakes them without anybody remembering --force.

    Which titles get woken is tested in eifo-core, where the deciding happens.
    This is the wiring: a crawl inside a nightly enrich has to move the due
    dates on its own, or a score sits in the index for weeks with the one thing
    that reads it declining to look.
    """

    @respx.mock
    def test_the_crawl_wakes_them_without_being_asked(
        self,
        live_api: LiveApi,
        session_factory: sessionmaker[Session],
        settings: Settings,
        http: HttpClient,
    ) -> None:
        mock_site()
        with session_factory() as setup:
            title = Title(type=TitleKind.MOVIE, name_he="פוקסטרוט", name_en="Foxtrot", year=2017)
            setup.add(title)
            setup.flush()
            setup.add(
                EnrichAttempt(
                    title_id=title.id,
                    outcome=EnrichOutcome.NO_MATCH,
                    fruitless=3,
                    due_at=utcnow() + dt.timedelta(days=30),
                )
            )
            setup.commit()

        result = index_seret(settings, http=http, api=live_api.api)

        assert result.woken == 1
        with session_factory() as check:
            attempt = check.scalars(select(EnrichAttempt)).one()
            assert attempt.due_at <= utcnow()

    @respx.mock
    def test_the_crawl_leaves_a_run_row_behind(
        self,
        live_api: LiveApi,
        session_factory: sessionmaker[Session],
        settings: Settings,
        http: HttpClient,
    ) -> None:
        """A long job that can fail on its own needs somewhere to say that it did."""
        mock_site()

        index_seret(settings, http=http, api=live_api.api)

        with session_factory() as check:
            run = check.scalars(
                select(FetchRun).where(FetchRun.source_key == SERET_INDEX_RUN_KEY)
            ).one()
        assert run.phase is FetchPhase.ENRICH
        assert run.status is FetchStatus.OK
        assert run.stats["created"] == 3

    @respx.mock
    def test_a_crawl_that_could_not_read_the_sitemap_says_so_on_its_row(
        self,
        live_api: LiveApi,
        session_factory: sessionmaker[Session],
        settings: Settings,
        http: HttpClient,
    ) -> None:
        """Reported, not raised: Seret being down must not cost the whole enrich."""
        respx.get(ROBOTS_URL).mock(return_value=httpx.Response(200, text=ROBOTS_TXT))
        respx.get(SITEMAP_INDEX_URL).mock(return_value=httpx.Response(500))

        result = index_seret(settings, http=http, api=live_api.api)

        assert result.error_count == 1
        with session_factory() as check:
            run = check.scalars(
                select(FetchRun).where(FetchRun.source_key == SERET_INDEX_RUN_KEY)
            ).one()
        assert run.status is FetchStatus.FAILED
        assert any("fatal" in entry for entry in run.stats["errors"])
