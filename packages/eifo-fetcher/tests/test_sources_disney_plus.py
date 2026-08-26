"""The Disney+ plugin, parsed entirely from recorded sitemaps.

The fixtures are trimmed copies of the real Israeli sitemaps, so a change in
Disney's layout shows up here rather than as a silently empty catalog.
"""

from __future__ import annotations

import httpx
import pytest
import respx
from recorded import load_fixture

from eifo_core.enums import OfferType, SourceKind, TitleKind
from eifo_fetcher.sources.base import FetchContext
from eifo_fetcher.sources.disney_plus import (
    SITEMAP_INDEX,
    DisneyCatalogError,
    DisneyPlusPlugin,
    locations,
    title_from_slug,
    to_item,
)

MOVIE_SITEMAP = "https://www.apps.disneyplus.com/il/new-sitemap-MOVIE-1.xml"
SHOWS_SITEMAP = "https://www.apps.disneyplus.com/il/new-sitemap-SHOWS-1.xml"

EMPTY_INDEX = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"></sitemapindex>
"""

#: The index shape that would trap a naive "does the name contain MOVIE" test.
DECOY_INDEX = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://www.apps.disneyplus.com/il/new-sitemap-MOVIE_WATCH_PAGES-1.xml</loc></sitemap>
  <sitemap><loc>https://www.apps.disneyplus.com/il/new-sitemap-EPISODE-1.xml</loc></sitemap>
</sitemapindex>
"""


@pytest.fixture
def disney_ctx(http: object, settings: object) -> FetchContext:
    return FetchContext(source_key="disney_plus_il", http=http, settings=settings)  # type: ignore[arg-type]


def mock_catalog(index: str | None = None) -> None:
    if index is None:
        index = load_fixture("disney_plus", "sitemap_index.xml")

    respx.get(SITEMAP_INDEX).mock(return_value=httpx.Response(200, text=index))
    respx.get(MOVIE_SITEMAP).mock(
        return_value=httpx.Response(200, text=load_fixture("disney_plus", "movie.xml"))
    )
    respx.get(SHOWS_SITEMAP).mock(
        return_value=httpx.Response(200, text=load_fixture("disney_plus", "shows.xml"))
    )


class TestSourceDeclaration:
    def test_declares_one_subscription_source(self) -> None:
        sources = DisneyPlusPlugin().sources()

        assert len(sources) == 1
        assert sources[0].key == "disney_plus_il"
        assert sources[0].kind is SourceKind.SUBSCRIPTION


class TestFetch:
    @respx.mock
    def test_yields_films_and_series_from_both_catalogs(self, disney_ctx: FetchContext) -> None:
        mock_catalog()

        items = list(DisneyPlusPlugin().fetch(disney_ctx))

        assert len(items) == 10
        assert sum(item.kind is TitleKind.MOVIE for item in items) == 6
        assert sum(item.kind is TitleKind.SERIES for item in items) == 4
        assert all(item.source_key == "disney_plus_il" for item in items)
        assert all(item.offer_type is OfferType.STREAM for item in items)

    @respx.mock
    def test_costs_one_request_per_sitemap_and_no_title_pages(
        self, disney_ctx: FetchContext
    ) -> None:
        """3,500 title pages would be 3,500 requests and would return nothing."""
        mock_catalog()

        list(DisneyPlusPlugin().fetch(disney_ctx))

        assert len(respx.calls) == 3
        assert {str(call.request.url) for call in respx.calls} == {
            SITEMAP_INDEX,
            MOVIE_SITEMAP,
            SHOWS_SITEMAP,
        }

    @respx.mock
    def test_carries_the_deep_link_and_disney_content_id(self, disney_ctx: FetchContext) -> None:
        mock_catalog()

        items = {item.name: item for item in DisneyPlusPlugin().fetch(disney_ctx)}
        coyote = items["Coyote Ugly"]

        assert coyote.deep_link_url == (
            "https://www.apps.disneyplus.com/il/movies/coyote-ugly/1260092090"
        )
        assert coyote.extra["content_id"] == "1260092090"
        assert coyote.extra["slug"] == "coyote-ugly"

    @respx.mock
    def test_carries_no_year_because_the_sitemap_has_none(self, disney_ctx: FetchContext) -> None:
        """Enrichment supplies the year; inventing one here would be a lie."""
        mock_catalog()

        assert all(item.year is None for item in DisneyPlusPlugin().fetch(disney_ctx))

    @respx.mock
    def test_follows_the_index_rather_than_guessing_child_names(
        self, disney_ctx: FetchContext
    ) -> None:
        """A second page appearing as the catalog grows must be picked up."""
        respx.get(SITEMAP_INDEX).mock(
            return_value=httpx.Response(
                200,
                text=(
                    '<?xml version="1.0" encoding="UTF-8"?>'
                    '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                    "<sitemap><loc>https://www.apps.disneyplus.com/il/new-sitemap-MOVIE-2.xml</loc></sitemap>"
                    "</sitemapindex>"
                ),
            )
        )
        second_page = respx.get("https://www.apps.disneyplus.com/il/new-sitemap-MOVIE-2.xml").mock(
            return_value=httpx.Response(200, text=load_fixture("disney_plus", "movie.xml"))
        )

        items = list(DisneyPlusPlugin().fetch(disney_ctx))

        assert second_page.called
        assert len(items) == 6

    @respx.mock
    def test_ignores_episode_and_duplicate_route_sitemaps(self, disney_ctx: FetchContext) -> None:
        """MOVIE_WATCH_PAGES must not be mistaken for the MOVIE catalog."""
        mock_catalog(index=DECOY_INDEX)

        with pytest.raises(DisneyCatalogError, match="no MOVIE or SHOWS children"):
            list(DisneyPlusPlugin().fetch(disney_ctx))

    @respx.mock
    def test_an_empty_index_fails_loudly(self, disney_ctx: FetchContext) -> None:
        """Silence here would look like a service that carries nothing."""
        mock_catalog(index=EMPTY_INDEX)

        with pytest.raises(DisneyCatalogError):
            list(DisneyPlusPlugin().fetch(disney_ctx))

    @respx.mock
    def test_an_html_error_page_served_as_200_fails_loudly(self, disney_ctx: FetchContext) -> None:
        """Real HTML is not well-formed XML - unclosed <meta> is enough."""
        respx.get(SITEMAP_INDEX).mock(
            return_value=httpx.Response(
                200,
                text=(
                    "<!DOCTYPE html><html><head><meta charset=utf-8>"
                    "<title>Not found</title></head><body>Not found</body></html>"
                ),
            )
        )

        with pytest.raises(DisneyCatalogError, match="valid XML"):
            list(DisneyPlusPlugin().fetch(disney_ctx))

    @respx.mock
    def test_well_formed_xml_that_is_not_a_sitemap_fails_loudly(
        self, disney_ctx: FetchContext
    ) -> None:
        """Parsing cleanly but carrying no catalog is still a layout change."""
        respx.get(SITEMAP_INDEX).mock(return_value=httpx.Response(200, text="<html>nope</html>"))

        with pytest.raises(DisneyCatalogError, match="no MOVIE or SHOWS children"):
            list(DisneyPlusPlugin().fetch(disney_ctx))

    @respx.mock
    def test_a_stray_url_is_recorded_not_stored(self, disney_ctx: FetchContext) -> None:
        respx.get(SITEMAP_INDEX).mock(
            return_value=httpx.Response(200, text=load_fixture("disney_plus", "sitemap_index.xml"))
        )
        respx.get(MOVIE_SITEMAP).mock(
            return_value=httpx.Response(
                200,
                text=(
                    '<?xml version="1.0" encoding="UTF-8"?>'
                    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                    "<url><loc>https://www.apps.disneyplus.com/il/brand/marvel</loc></url>"
                    "<url><loc>https://www.apps.disneyplus.com/il/movies/real-film/123</loc></url>"
                    "</urlset>"
                ),
            )
        )
        respx.get(SHOWS_SITEMAP).mock(
            return_value=httpx.Response(200, text=load_fixture("disney_plus", "shows.xml"))
        )

        items = list(DisneyPlusPlugin().fetch(disney_ctx))

        assert [item.name for item in items if item.kind is TitleKind.MOVIE] == ["Real Film"]
        assert disney_ctx.error_count == 1


class TestLocations:
    def test_reads_locations_regardless_of_namespace_prefix(self) -> None:
        xml = (
            '<?xml version="1.0"?>'
            '<sm:urlset xmlns:sm="http://www.sitemaps.org/schemas/sitemap/0.9">'
            "<sm:url><sm:loc>https://example.test/a</sm:loc></sm:url>"
            "</sm:urlset>"
        )

        assert locations(xml) == ["https://example.test/a"]

    def test_skips_blank_entries(self) -> None:
        xml = (
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            "<url><loc>  </loc></url><url><loc> https://example.test/b </loc></url>"
            "</urlset>"
        )

        assert locations(xml) == ["https://example.test/b"]


class TestTitleFromSlug:
    @pytest.mark.parametrize(
        ("slug", "expected"),
        [
            ("coyote-ugly", "Coyote Ugly"),
            ("inside-out-2", "Inside Out 2"),
            ("the-gods-must-be-crazy-ii", "The Gods Must Be Crazy II"),
            ("x-men-origins-wolverine", "X Men Origins Wolverine"),
            # Disney strips non-ASCII from slugs: "Shōgun" arrives like this.
            ("sh-gun", "Sh Gun"),
        ],
    )
    def test_reads_as_a_title(self, slug: str, expected: str) -> None:
        assert title_from_slug(slug) == expected

    @pytest.mark.parametrize("word", ["civil", "mix", "did", "lid"])
    def test_ordinary_words_of_numeral_letters_are_not_shouted(self, word: str) -> None:
        """A naive roman-numeral rule turns "civil" into "CIVIL"."""
        assert title_from_slug(word) == word.capitalize()


class TestToItem:
    def test_rejects_a_url_that_is_not_a_title(self) -> None:
        assert to_item("https://www.apps.disneyplus.com/il/brand/pixar", TitleKind.MOVIE) is None

    def test_rejects_a_url_with_no_content_id(self) -> None:
        assert to_item("https://www.apps.disneyplus.com/il/movies/thing", TitleKind.MOVIE) is None

    def test_accepts_a_trailing_slash(self) -> None:
        item = to_item("https://www.apps.disneyplus.com/il/shows/loki/123/", TitleKind.SERIES)

        assert item is not None
        assert item.name == "Loki"
        assert item.kind is TitleKind.SERIES


class TestAPossessiveSurvivesTheSlug:
    """A slug cannot carry an apostrophe, so Disney drops it and the s is
    stranded as a word of its own.

    Left alone it becomes a word of the title: "Disney S Fairy Tale Weddings",
    which is the name the catalog stores and a viewer reads. Of the Disney
    titles that failed to resolve against TMDB, 58 carried a stranded s -
    against none of the 2,760 that resolved.
    """

    @pytest.mark.parametrize(
        ("slug", "expected"),
        [
            ("disney-s-fairy-tale-weddings", "Disney's Fairy Tale Weddings"),
            ("william-shakespeare-s-romeo-juliet", "William Shakespeare's Romeo Juliet"),
            ("what-s-my-name", "What's My Name"),
            ("mickey-s-country-farm", "Mickey's Country Farm"),
        ],
    )
    def test_it_is_rejoined_to_the_word_it_belongs_to(self, slug: str, expected: str) -> None:
        assert title_from_slug(slug) == expected

    def test_a_leading_s_is_a_word_in_its_own_right(self) -> None:
        """There is nothing before it to be possessive about."""
        assert title_from_slug("s-club-7") == "S Club 7"

    def test_a_single_letter_that_is_not_s_is_left_alone(self) -> None:
        """BURN-E is a Pixar short, not a possessive."""
        assert title_from_slug("burn-e") == "Burn E"

    def test_an_ordinary_slug_is_unchanged(self) -> None:
        assert title_from_slug("star-wars-the-force-awakens") == "Star Wars The Force Awakens"
