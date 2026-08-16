"""The Reshet 13 plugin, parsed entirely from recorded fixtures.

The fixtures are trimmed copies of the two real rendered screens
(``/allshows/screen/1170108/`` "כל התוכניות" and ``/allshows/screen/1170109/``
"חדשות 13"), so a change in 13tv's payload shows up here rather than in
production. The browser transport is faked: these tests never launch Chromium
or touch the network.
"""

from __future__ import annotations

from types import TracebackType
from typing import Self

import pytest
from recorded import load_fixture

from eifo_core.enums import OfferType, SourceKind, TitleKind
from eifo_fetcher.robots import RobotsDisallowedError
from eifo_fetcher.sources.base import FetchContext
from eifo_fetcher.sources.reshet13 import (
    CATALOG_URL,
    SCREEN_URLS,
    Reshet13CatalogError,
    Reshet13Plugin,
    parse_catalog,
    parse_next_data,
)

ALL_SHOWS_URL, NEWS_URL = SCREEN_URLS


class FakeBrowser:
    """Stands in for BrowserSession: serves recorded pages, records calls."""

    def __init__(
        self,
        pages: dict[str, str],
        robots: tuple[int, str] = (200, "User-agent: *\nAllow: /allshows/\n"),
    ) -> None:
        self.pages = pages
        self.robots = robots
        self.navigations: list[str] = []
        self.fetches: list[str] = []
        self.user_agent = "fake-browser/1.0"

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        pass

    def get_html(self, url: str, *, ready_selector: str | None = None) -> str:
        self.navigations.append(url)
        if url not in self.pages:
            raise AssertionError(f"unexpected navigation to {url}")
        return self.pages[url]

    def fetch_text(self, url: str) -> tuple[int, str]:
        self.fetches.append(url)
        return self.robots


def all_screen_fixtures() -> dict[str, str]:
    return {
        ALL_SHOWS_URL: load_fixture("reshet13", "all_shows_screen.html"),
        NEWS_URL: load_fixture("reshet13", "news_screen.html"),
    }


@pytest.fixture
def reshet_ctx(http: object, settings: object) -> FetchContext:
    return FetchContext(source_key="reshet13", http=http, settings=settings)  # type: ignore[arg-type]


def _plugin(
    pages: dict[str, str] | None = None,
    robots: tuple[int, str] = (200, "User-agent: *\nAllow: /allshows/\n"),
) -> Reshet13Plugin:
    browser = FakeBrowser(pages if pages is not None else all_screen_fixtures(), robots)
    plugin = Reshet13Plugin(browser_factory=lambda _ctx: browser)
    plugin.fake_browser = browser  # type: ignore[attr-defined]
    return plugin


class TestSourceDeclaration:
    def test_declares_one_free_source(self) -> None:
        sources = Reshet13Plugin().sources()

        assert len(sources) == 1
        assert sources[0].key == "reshet13"
        assert sources[0].kind is SourceKind.FREE
        assert sources[0].website_url == CATALOG_URL


class TestFetch:
    def test_yields_every_unique_programme(self, reshet_ctx: FetchContext) -> None:
        """Five shows on the all-shows screen (one broken) plus three on the
        news screen, minus the overlap: six items."""
        plugin = _plugin()

        items = list(plugin.fetch(reshet_ctx))

        assert len(items) == 6
        assert all(item.source_key == "reshet13" for item in items)
        assert all(item.offer_type is OfferType.FREE for item in items)
        assert all(item.kind is TitleKind.SERIES for item in items)

    def test_two_screens_in_one_session_plus_one_robots_fetch(
        self, reshet_ctx: FetchContext
    ) -> None:
        """Unlike Kan, 13tv places no per-session limit on page views: both
        screens are read in a single browser session, then robots.txt."""
        plugin = _plugin()

        list(plugin.fetch(reshet_ctx))

        assert plugin.fake_browser.navigations == [ALL_SHOWS_URL, NEWS_URL]  # type: ignore[attr-defined]
        assert plugin.fake_browser.fetches == ["https://13tv.co.il/robots.txt"]  # type: ignore[attr-defined]

    def test_maps_the_real_entry_fields(self, reshet_ctx: FetchContext) -> None:
        plugin = _plugin()

        items = {item.name: item for item in plugin.fetch(reshet_ctx)}

        patrick = items["פטריק"]
        assert patrick.deep_link_url == "https://13tv.co.il/allshows/series/655/"
        assert patrick.poster_url is not None
        assert patrick.extra["external_id"] == "Show-655"
        assert patrick.extra["description"]

    def test_names_are_stripped(self, reshet_ctx: FetchContext) -> None:
        """The CMS pads some names ("VOICE OVER ")."""
        plugin = _plugin()

        names = [item.name for item in plugin.fetch(reshet_ctx)]

        assert "VOICE OVER" in names

    def test_a_programme_on_both_screens_is_yielded_once(self, reshet_ctx: FetchContext) -> None:
        """המהדורה המרכזית (Show-278) sits on the all-shows screen and the
        news screen."""
        plugin = _plugin()

        names = [item.name for item in plugin.fetch(reshet_ctx)]

        assert names.count("המהדורה המרכזית") == 1

    def test_news_clips_and_full_episodes_are_not_catalog_titles(
        self, reshet_ctx: FetchContext
    ) -> None:
        """The news screen mixes dated clips and full-episode entries
        (``vod-*`` / ``news-*`` externalIds) with its programmes; only the
        programmes are yielded."""
        plugin = _plugin()

        items = list(plugin.fetch(reshet_ctx))

        assert all("/allshows/series/" in (item.deep_link_url or "") for item in items)
        assert not any("המהדורה המלאה" in item.name for item in items)

    def test_catalog_has_no_years(self, reshet_ctx: FetchContext) -> None:
        """Verified against the real payload; enrichment fills this in later."""
        plugin = _plugin()

        assert all(item.year is None for item in plugin.fetch(reshet_ctx))

    @pytest.mark.parametrize("bad_url", SCREEN_URLS)
    def test_any_unreadable_screen_fails_loudly(
        self, reshet_ctx: FetchContext, bad_url: str
    ) -> None:
        """One block page or layout change fails the whole sync (no sweep over
        a partial catalog), whichever screen it hits."""
        pages = all_screen_fixtures()
        pages[bad_url] = "<html><body>Sorry, you have been blocked</body></html>"
        plugin = _plugin(pages)

        with pytest.raises(Reshet13CatalogError, match="interstitial"):
            list(plugin.fetch(reshet_ctx))

    def test_a_screen_without_programmes_fails_loudly(self, reshet_ctx: FetchContext) -> None:
        """A valid payload whose leafs list no Show entries is a layout
        change, not an empty catalog."""
        pages = all_screen_fixtures()
        pages[NEWS_URL] = (
            '<script id="__NEXT_DATA__" type="application/json">'
            '{"props": {"pageProps": {"leafs": [{"id": 1, "child": []}]}}}</script>'
        )
        plugin = _plugin(pages)

        with pytest.raises(Reshet13CatalogError, match="no programmes"):
            list(plugin.fetch(reshet_ctx))

    def test_robots_disallowing_a_screen_stops_the_sync(self, reshet_ctx: FetchContext) -> None:
        """13tv's robots.txt allows /allshows/ today; if that ever changes we
        stop before anything is stored."""
        robots = (200, "User-agent: *\nDisallow: /allshows/\n")
        plugin = _plugin(robots=robots)

        with pytest.raises(RobotsDisallowedError):
            list(plugin.fetch(reshet_ctx))

        assert reshet_ctx.error_count == 0  # failed cleanly, not item-by-item

    def test_unservable_robots_means_no_restrictions(self, reshet_ctx: FetchContext) -> None:
        """RFC 9309 treats an unservable robots.txt as open."""
        plugin = _plugin(robots=(403, "blocked"))

        assert len(list(plugin.fetch(reshet_ctx))) == 6

    def test_a_nameless_entry_is_one_counted_error(self, reshet_ctx: FetchContext) -> None:
        """The all-shows fixture holds one Show entry with an empty name; it
        must be loud in fetch_runs but must not cost the sync."""
        plugin = _plugin()

        items = list(plugin.fetch(reshet_ctx))

        assert len(items) == 6
        assert reshet_ctx.error_count == 1
        assert "unparsable catalog entry" in reshet_ctx.errors[0]


class TestParseCatalog:
    def test_dedupes_across_screens(self) -> None:
        pages = [
            load_fixture("reshet13", "all_shows_screen.html"),
            load_fixture("reshet13", "news_screen.html"),
        ]

        entries = parse_catalog(pages)

        external_ids = [entry["externalId"] for entry in entries]
        assert len(external_ids) == len(set(external_ids)) == 7  # 6 usable + the broken one

    def test_keeps_only_show_entries(self) -> None:
        entries = parse_catalog([load_fixture("reshet13", "news_screen.html")])

        assert {entry["externalId"] for entry in entries} == {"Show-278", "Show-247", "Show-280"}


class TestParseNextData:
    def test_a_page_without_next_data_is_rejected(self) -> None:
        with pytest.raises(Reshet13CatalogError, match="interstitial"):
            parse_next_data("<html><body>nothing here</body></html>")

    def test_invalid_json_is_rejected(self) -> None:
        with pytest.raises(Reshet13CatalogError, match="valid JSON"):
            parse_next_data('<script id="__NEXT_DATA__" type="application/json">{oops</script>')


class TestItemConversion:
    def test_requires_a_name_and_a_series_id(self) -> None:
        from eifo_fetcher.sources.reshet13 import _to_item

        assert _to_item({"name": "", "externalId": "Show-1"}) is None
        assert _to_item({"name": "x", "externalId": "Show-"}) is None

    def test_prefers_the_portrait_poster_crop(self) -> None:
        from eifo_fetcher.sources.reshet13 import _to_item

        item = _to_item(
            {
                "name": "x",
                "externalId": "Show-1",
                "images": [
                    {"imageTypeName": "16x9", "url": "https://img/landscape"},
                    {"imageTypeName": "2x3", "url": "https://img/poster"},
                ],
            }
        )

        assert item is not None
        assert item.poster_url == "https://img/poster"

    def test_falls_back_to_whatever_crop_exists(self) -> None:
        from eifo_fetcher.sources.reshet13 import _to_item

        item = _to_item(
            {
                "name": "x",
                "externalId": "Show-1",
                "images": [{"imageTypeName": "Landscape", "url": "https://img/only"}],
            }
        )

        assert item is not None
        assert item.poster_url == "https://img/only"

    def test_no_images_means_no_poster(self) -> None:
        from eifo_fetcher.sources.reshet13 import _to_item

        item = _to_item({"name": "x", "externalId": "Show-1", "images": []})

        assert item is not None
        assert item.poster_url is None
