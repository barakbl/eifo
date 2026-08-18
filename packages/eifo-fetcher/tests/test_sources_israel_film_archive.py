"""The Israeli Film Archive plugin, parsed entirely from recorded fixtures.

The fixtures are trimmed copies of two real jfc.org.il film pages - one the
archive streams for nothing, one it sells - plus a cut-down sitemap. The whole
point of the source is that those two look different, so both are recorded.
"""

from __future__ import annotations

import httpx
import pytest
import respx
from recorded import load_fixture

from eifo_core.enums import OfferType, SourceKind, TitleKind
from eifo_core.settings import Settings
from eifo_fetcher.robots import RobotsDisallowedError
from eifo_fetcher.sources.base import FetchContext
from eifo_fetcher.sources.israel_film_archive import (
    CATALOG_URL,
    SITEMAP_URL,
    ArchiveCatalogError,
    IsraelFilmArchivePlugin,
    parse_film,
    parse_sitemap,
)

ROBOTS_URL = "https://jfc.org.il/robots.txt"
PERMISSIVE_ROBOTS = "User-agent: *\nDisallow: /wp-admin/\nDisallow: /wp-admin/admin-ajax.php\n"
FREE_URL = "https://jfc.org.il/movie/24567-2/"
PAID_URL = "https://jfc.org.il/movie/44380-2/"


@pytest.fixture
def archive_ctx(http: object, settings: Settings) -> FetchContext:
    return FetchContext(source_key="israel_film_archive", http=http, settings=settings)  # type: ignore[arg-type]


def _mock_site(robots: str = PERMISSIVE_ROBOTS) -> None:
    respx.get(ROBOTS_URL).mock(return_value=httpx.Response(200, text=robots))
    respx.get(SITEMAP_URL).mock(
        return_value=httpx.Response(
            200, text=load_fixture("israel_film_archive", "movie_sitemap.xml")
        )
    )
    respx.get(FREE_URL).mock(
        return_value=httpx.Response(200, text=load_fixture("israel_film_archive", "film_free.html"))
    )
    respx.get(PAID_URL).mock(
        return_value=httpx.Response(200, text=load_fixture("israel_film_archive", "film_paid.html"))
    )


def _fetch(ctx: FetchContext) -> list:
    _mock_site()
    return list(IsraelFilmArchivePlugin().fetch(ctx))


class TestSourceDeclaration:
    def test_declares_one_rent_buy_source(self) -> None:
        sources = IsraelFilmArchivePlugin().sources()

        assert len(sources) == 1
        assert sources[0].key == "israel_film_archive"
        assert sources[0].kind is SourceKind.RENT_BUY


class TestFetch:
    @respx.mock
    def test_yields_a_film_per_sitemap_entry(self, archive_ctx: FetchContext) -> None:
        items = list(_fetch(archive_ctx))

        assert len(items) == 2
        assert all(item.kind is TitleKind.MOVIE for item in items)
        assert all(item.source_key == "israel_film_archive" for item in items)

    @respx.mock
    def test_a_film_the_archive_gives_away_is_free_to_watch(
        self, archive_ctx: FetchContext
    ) -> None:
        """Half this collection costs nothing; it must not look like a rental."""
        free = next(item for item in _fetch(archive_ctx) if item.name == "עורבים")

        assert free.offer_type is OfferType.FREE
        assert free.price_minor is None
        assert free.price_currency is None

    @respx.mock
    def test_a_film_the_archive_sells_carries_its_price(self, archive_ctx: FetchContext) -> None:
        paid = next(item for item in _fetch(archive_ctx) if item.name.startswith("אהבה אסורה"))

        assert paid.offer_type is OfferType.RENT
        assert paid.price_minor == 1500
        assert paid.price_currency == "ILS"

    @respx.mock
    def test_maps_the_fields_the_page_carries(self, archive_ctx: FetchContext) -> None:
        free = next(item for item in _fetch(archive_ctx) if item.name == "עורבים")

        assert free.year == 1988
        assert free.deep_link_url == FREE_URL
        assert free.poster_url == "https://jfc.org.il/media/MOVIES/ORVIM_24567_MAIN.jpg"
        assert free.extra["director"] == "איילת מנחמי"
        assert free.extra["runtime_minutes"] == 47
        assert free.extra["genre"] == "סרט סטודנטים"
        assert free.extra["description"]

    @respx.mock
    def test_an_unreadable_page_is_skipped_and_counted(self, archive_ctx: FetchContext) -> None:
        """One dead page must not cost the other 940 films."""
        _mock_site()
        respx.get(PAID_URL).mock(return_value=httpx.Response(500))

        items = list(IsraelFilmArchivePlugin().fetch(archive_ctx))

        assert [item.name for item in items] == ["עורבים"]
        assert any("could not be read" in error for error in archive_ctx.errors)


class TestParseFilm:
    def test_a_catalogued_film_with_no_way_to_watch_is_not_an_offer(self) -> None:
        """The archive catalogues more than it streams; only the rest is availability."""
        page = (
            "<html><head><meta property='og:title' content='סרט גנוז | במאי | 1970'></head>"
            "<body><h1 class='content_title'>סרט גנוז</h1>"
            "<div class='warp_title'>סרט גנוז 88 דקות, 1970</div></body></html>"
        )

        assert parse_film(page, "https://jfc.org.il/movie/1-2/") is None

    def test_the_staging_host_never_reaches_a_deep_link(self) -> None:
        """Some of the site's own links point at stage2; canonical is the truth."""
        page = load_fixture("israel_film_archive", "film_free.html").replace(
            "https://jfc.org.il/movie/24567-2/", "https://stage2.jfc.org.il/movie/24567-2/"
        )

        film = parse_film(page, "https://stage2.jfc.org.il/movie/24567-2/")

        assert film is not None
        assert film.url == FREE_URL

    def test_a_price_block_without_a_number_fails_loudly(self) -> None:
        """Reading that as free would put a paid film in the catalog as free."""
        page = (
            "<html><head><meta property='og:title' content='סרט | במאי | 1970'></head>"
            "<body><h1 class='content_title'>סרט</h1>"
            "<div class='movie-price'>מחיר :</div></body></html>"
        )

        with pytest.raises(ArchiveCatalogError, match="no amount"):
            parse_film(page, "https://jfc.org.il/movie/2-2/")

    def test_a_page_without_a_title_fails_loudly(self) -> None:
        with pytest.raises(ArchiveCatalogError, match="layout changed"):
            parse_film("<html><body><p>hello</p></body></html>", "https://jfc.org.il/movie/3-2/")


class TestParseSitemap:
    def test_lists_films_and_skips_the_index_entry(self) -> None:
        urls = parse_sitemap(load_fixture("israel_film_archive", "movie_sitemap.xml"))

        assert urls == [FREE_URL, PAID_URL]

    def test_a_sitemap_without_films_fails_loudly(self) -> None:
        with pytest.raises(ArchiveCatalogError, match="sitemap moved"):
            parse_sitemap("<?xml version='1.0'?><urlset></urlset>")


class TestRobots:
    @respx.mock
    def test_a_disallowed_catalog_stops_the_sync(self, archive_ctx: FetchContext) -> None:
        _mock_site(robots="User-agent: *\nDisallow: /movie/\n")

        with pytest.raises(RobotsDisallowedError):
            list(IsraelFilmArchivePlugin().fetch(archive_ctx))

    @respx.mock
    def test_the_check_happens_before_anything_is_fetched(self, archive_ctx: FetchContext) -> None:
        _mock_site(robots="User-agent: *\nDisallow: /movie/\n")

        with pytest.raises(RobotsDisallowedError):
            list(IsraelFilmArchivePlugin().fetch(archive_ctx))

        assert [str(call.request.url) for call in respx.calls] == [ROBOTS_URL]
        assert CATALOG_URL not in [str(call.request.url) for call in respx.calls]
