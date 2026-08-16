"""The Kan plugin, parsed entirely from recorded fixtures.

The fixtures are trimmed copies of the real rendered lobby pages
(``/lobby/kan-box/``, ``/lobby/series/``, ``/lobby/digital-lobby/``), so a
change in Kan's markup shows up here rather than in production. The browser
transport is faked: these tests never launch Chromium or touch the network.
"""

# The parametrised _clean_name cases quote Kan's CMS junk verbatim, and it
# mixes Hebrew into Latin filename tokens - that is exactly what is under test.
# ruff: noqa: RUF001

from __future__ import annotations

from types import TracebackType
from typing import Self

import pytest
from recorded import load_fixture

from eifo_core.enums import OfferType, SourceKind, TitleKind
from eifo_fetcher.robots import RobotsDisallowedError
from eifo_fetcher.sources.base import FetchContext
from eifo_fetcher.sources.kan import (
    CATALOG_URL,
    LOBBY_URLS,
    KanCatalogError,
    KanPlugin,
    parse_catalog,
    parse_lobby,
    to_item,
)

KAN_BOX_URL, SERIES_URL, DIGITAL_URL = LOBBY_URLS


class FakeBrowser:
    """Stands in for BrowserSession: serves recorded pages, records calls."""

    def __init__(
        self,
        pages: dict[str, str],
        robots: tuple[int, str] = (403, ""),
    ) -> None:
        self.pages = pages
        self.robots = robots
        self.navigations: list[str] = []
        self.resets = 0
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

    def reset(self) -> None:
        self.resets += 1

    def get_html(self, url: str, *, ready_selector: str | None = None) -> str:
        self.navigations.append(url)
        if url not in self.pages:
            raise AssertionError(f"unexpected navigation to {url}")
        return self.pages[url]

    def fetch_text(self, url: str) -> tuple[int, str]:
        self.fetches.append(url)
        return self.robots


def all_lobby_fixtures() -> dict[str, str]:
    return {
        KAN_BOX_URL: load_fixture("kan", "kan_box_lobby.html"),
        SERIES_URL: load_fixture("kan", "series_lobby.html"),
        DIGITAL_URL: load_fixture("kan", "digital_lobby.html"),
    }


@pytest.fixture
def kan_ctx(http: object, settings: object) -> FetchContext:
    return FetchContext(source_key="kan", http=http, settings=settings)  # type: ignore[arg-type]


def _plugin(pages: dict[str, str] | None = None, robots: tuple[int, str] = (403, "")) -> KanPlugin:
    browser = FakeBrowser(pages if pages is not None else all_lobby_fixtures(), robots)
    plugin = KanPlugin(browser_factory=lambda _ctx: browser)
    plugin.fake_browser = browser  # type: ignore[attr-defined]
    return plugin


class TestSourceDeclaration:
    def test_declares_one_free_source(self) -> None:
        sources = KanPlugin().sources()

        assert len(sources) == 1
        assert sources[0].key == "kan"
        assert sources[0].kind is SourceKind.FREE
        assert sources[0].website_url == CATALOG_URL


class TestFetch:
    def test_yields_every_unique_program(self, kan_ctx: FetchContext) -> None:
        plugin = _plugin()

        items = list(plugin.fetch(kan_ctx))

        assert len(items) == 14
        assert all(item.source_key == "kan" for item in items)
        assert all(item.offer_type is OfferType.FREE for item in items)

    def test_three_lobbies_in_three_sessions_plus_one_robots_fetch(
        self, kan_ctx: FetchContext
    ) -> None:
        """The whole sync is one page view per lobby, each in a fresh browser
        context (Kan's WAF serves one document per cleared session), plus a
        single robots.txt fetch."""
        plugin = _plugin()

        list(plugin.fetch(kan_ctx))

        assert plugin.fake_browser.navigations == [KAN_BOX_URL, SERIES_URL, DIGITAL_URL]  # type: ignore[attr-defined]
        assert plugin.fake_browser.resets == 2  # type: ignore[attr-defined]
        assert plugin.fake_browser.fetches == ["https://www.kan.org.il/robots.txt"]  # type: ignore[attr-defined]

    def test_maps_the_real_card_fields(self, kan_ctx: FetchContext) -> None:
        plugin = _plugin()

        first = next(iter(plugin.fetch(kan_ctx)))

        assert first.name == "דודו טסה לתפוס דג"
        assert first.kind is TitleKind.MOVIE
        assert first.deep_link_url == "https://www.kan.org.il/content/kan/kan-11/p-1043786/"
        assert first.poster_url is not None
        assert first.poster_url.startswith("https://www.kan.org.il/media/")
        assert first.extra["sections"] == ["סרטים", "דוקו"]
        assert first.extra["description"]

    def test_reads_the_digital_lobbys_own_markup(self, kan_ctx: FetchContext) -> None:
        plugin = _plugin()

        items = {item.name: item for item in plugin.fetch(kan_ctx)}

        tram = items["הטרמפיסטים"]
        assert tram.kind is TitleKind.SERIES
        assert tram.deep_link_url == "https://www.kan.org.il/content/dig/digital/p-11540/"
        assert tram.extra["sections"] == ["דיגיטל"]

    def test_a_program_on_two_lobbies_is_yielded_once(self, kan_ctx: FetchContext) -> None:
        """זהו זה sits on both kan-box and the series archive lobby."""
        plugin = _plugin()

        names = [item.name for item in plugin.fetch(kan_ctx)]

        assert names.count("זהו זה") == 1

    def test_catalog_has_no_years(self, kan_ctx: FetchContext) -> None:
        """Verified against the real page; enrichment fills this in later."""
        plugin = _plugin()

        assert all(item.year is None for item in plugin.fetch(kan_ctx))

    @pytest.mark.parametrize("bad_url", LOBBY_URLS)
    def test_any_unreadable_lobby_fails_loudly(self, kan_ctx: FetchContext, bad_url: str) -> None:
        """One block page or layout change fails the whole sync (no sweep over
        a partial catalog), whichever lobby it hits."""
        pages = all_lobby_fixtures()
        pages[bad_url] = "<html><body>Sorry, you have been blocked</body></html>"
        plugin = _plugin(pages)

        with pytest.raises(KanCatalogError, match="interstitial"):
            list(plugin.fetch(kan_ctx))

    def test_robots_disallowing_a_lobby_stops_the_sync(self, kan_ctx: FetchContext) -> None:
        """If Kan ever serves a robots.txt that disallows the lobbies, we stop
        before anything is stored (the check runs after the page loads, because
        a second navigation in one session would be blocked)."""
        robots = (200, "User-agent: *\nDisallow: /lobby/\n")
        plugin = _plugin(robots=robots)

        with pytest.raises(RobotsDisallowedError):
            list(plugin.fetch(kan_ctx))

        assert kan_ctx.error_count == 0  # failed cleanly, not item-by-item

    def test_unservable_robots_means_no_restrictions(self, kan_ctx: FetchContext) -> None:
        """Kan's robots.txt 403s even to browsers; RFC 9309 treats that as open."""
        plugin = _plugin(robots=(403, "blocked"))

        assert len(list(plugin.fetch(kan_ctx))) == 14

    def test_a_titleless_card_is_one_counted_error(self, kan_ctx: FetchContext) -> None:
        """The fixtures hold one card whose alt is pure filename junk; it must
        be loud in fetch_runs but must not cost the sync."""
        plugin = _plugin()

        list(plugin.fetch(kan_ctx))

        assert kan_ctx.error_count == 1
        assert "no usable title" in kan_ctx.errors[0]


class TestParseCatalog:
    def test_dedupes_across_lobbies_and_sections(self) -> None:
        pages = [
            load_fixture("kan", "kan_box_lobby.html"),
            load_fixture("kan", "series_lobby.html"),
            load_fixture("kan", "digital_lobby.html"),
        ]

        cards, dropped = parse_catalog(pages)

        urls = [card.url for card in cards]
        assert len(urls) == len(set(urls)) == 14
        assert dropped == 1

    def test_a_program_in_the_movies_section_is_a_movie(self) -> None:
        """דודו טסה sits under both סרטים and דוקו; סרטים wins."""
        cards, _ = parse_catalog([load_fixture("kan", "kan_box_lobby.html")])

        kinds = {card.name: to_item(card).kind for card in cards}  # type: ignore[union-attr]

        assert kinds["דודו טסה לתפוס דג"] is TitleKind.MOVIE
        assert kinds["הדרבי האחרון"] is TitleKind.MOVIE
        assert kinds["מתחת לבלטות"] is TitleKind.SERIES

    def test_kids_programs_keep_their_kankids_links(self) -> None:
        cards, _ = parse_catalog([load_fixture("kan", "kan_box_lobby.html")])

        kids = [card for card in cards if "kankids.org.il" in card.url]
        assert len(kids) == 2
        assert all(card.sections == ("ילדים ונוער",) for card in kids)

    def test_recovers_a_title_from_a_filename_alt(self) -> None:
        """'Poster Image Small 239X360 בהסתורה' is the show בהסתורה."""
        cards, _ = parse_catalog([load_fixture("kan", "kan_box_lobby.html")])

        names = [card.name for card in cards]
        assert "בהסתורה" in names


class TestParseLobby:
    def test_reads_the_genre_card_markup(self) -> None:
        cards, dropped = parse_lobby(load_fixture("kan", "series_lobby.html"))

        assert len(cards) == 4
        assert dropped == 0
        assert all(card.sections for card in cards)

    def test_reads_the_digital_program_markup(self) -> None:
        cards, dropped = parse_lobby(load_fixture("kan", "digital_lobby.html"))

        assert dropped == 0
        by_name = {card.name: card for card in cards}
        assert set(by_name) == {"הטרמפיסטים", "טעימות", "משתפצות"}
        assert by_name["הטרמפיסטים"].sections == ("דיגיטל",)
        assert by_name["הטרמפיסטים"].poster_url is not None
        assert by_name["הטרמפיסטים"].description is not None

    def test_a_page_without_cards_is_rejected(self) -> None:
        with pytest.raises(KanCatalogError, match="interstitial"):
            parse_lobby("<html><body>nothing here</body></html>")

    def test_cards_missing_a_title_or_link_are_dropped_and_counted(self) -> None:
        html = """
        <div class="block-list-item"><h2 class="h3 title-elem">דרמה</h2>
          <div class="card vods-by-category">
            <a href="https://www.kan.org.il/content/kan/kan-11/p-1/" class="card-link">
              <img src="/media/x.jpg" alt="תקין">
            </a>
          </div>
          <div class="card vods-by-category">
            <a href="https://www.kan.org.il/content/kan/kan-11/p-2/" class="card-link">
              <img src="/media/y.jpg">
            </a>
          </div>
          <div class="card vods-by-category">
            <img src="/media/z.jpg" alt="אין קישור">
          </div>
        </div>
        """
        cards, dropped = parse_lobby(html)

        assert [card.name for card in cards] == ["תקין"]
        assert dropped == 2

    def test_dropped_cards_are_recorded_as_errors(self, kan_ctx: FetchContext) -> None:
        """A malformed card must be loud in fetch_runs, never silently lost."""
        html = """
        <div class="block-list-item"><h2 class="h3 title-elem">דרמה</h2>
          <div class="card vods-by-category">
            <a href="https://www.kan.org.il/content/kan/kan-11/p-1/" class="card-link">
              <img src="/media/x.jpg" alt="תקין">
            </a>
          </div>
          <div class="card vods-by-category">
            <img src="/media/z.jpg" alt="אין קישור">
          </div>
        </div>
        """
        plugin = _plugin({KAN_BOX_URL: html, SERIES_URL: html, DIGITAL_URL: html})

        items = list(plugin.fetch(kan_ctx))

        # The same page three times merges into a single item, three drops.
        assert [item.name for item in items] == ["תקין"]
        assert kan_ctx.error_count == 1
        assert "3 catalog cards" in kan_ctx.errors[0]


class TestItemConversion:
    def test_requires_a_name_and_a_url(self) -> None:
        from eifo_fetcher.sources.kan import CatalogCard

        assert (
            to_item(CatalogCard(name="", url="/x", poster_url=None, description=None, genre=None))
            is None
        )
        assert (
            to_item(CatalogCard(name="x", url="", poster_url=None, description=None, genre=None))
            is None
        )

    def test_makes_relative_poster_urls_absolute(self) -> None:
        from eifo_fetcher.sources.kan import CatalogCard

        item = to_item(
            CatalogCard(
                name="x",
                url="https://www.kan.org.il/p-1/",
                poster_url=None,
                description=None,
                genre=None,
            )
        )

        assert item is not None
        assert item.poster_url is None

    def test_extra_carries_sections_for_match_reviews(self) -> None:
        from eifo_fetcher.sources.kan import CatalogCard

        item = to_item(
            CatalogCard(
                name="x",
                url="https://www.kan.org.il/p-1/",
                poster_url=None,
                description="d",
                genre="g",
                sections=("דרמה", "פשע"),
            )
        )

        assert item is not None
        assert item.extra["sections"] == ["דרמה", "פשע"]


class TestCleanName:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            # Real junk observed on the live page (August 2026).
            ("Poster Image Small 239X360 בהסתורה", "בהסתורה"),
            ("5 Poster Image Small  המדרשיה חדש", "המדרשיה חדש"),
            ("Small Poster 360X236קופה ראשית הסרט", "קופה ראשית הסרט"),
            ("Poster 239 360האחיין שלי בנץ", "האחיין שלי בנץ"),
            ("Share Image 1200X630 סיטון", "סיטון"),
            ("1800X1200 דפיקה בדלת", "דפיקה בדלת"),
            ("239X360 זהו זה פוסטר קטן", "זהו זה"),
            ("1200 1800  במאי", "במאי"),
            ("אולפן פתוח Poster Image Small 239X360", "אולפן פתוח"),
            ("פוסטר קטן רגע עם דודלי", "רגע עם דודלי"),
            ("פוסטר מה הרעש  קטן", "מה הרעש"),
            ("גרוסמן פוסטר קטן", "גרוסמן"),
            ("שומרי הסף לוגו", "שומרי הסף"),
            ("אורות וצללים רכיבים", "אורות וצללים"),
            # Nothing title-like remains: dropped.
            ("Poster Image Small 239X360", ""),
            ("6 Poster Image Big 1200X1800 (7)", ""),
            ("4_Poster image_small_239X360", ""),
            ("פוסטר קטן@1X 1", ""),
            ("תמונת אורך פרק", ""),
            ("-", ""),
            ("", ""),
            # Real titles pass through untouched.
            ("טהרן", "טהרן"),
            ("1948: לזכור ולשכוח", "1948: לזכור ולשכוח"),
            ("301 פדויים", "301 פדויים"),
            ("שמונים וארבע", "שמונים וארבע"),
            ("מחוננת עונה 5", "מחוננת עונה 5"),
        ],
    )
    def test_cleans_cms_filename_junk(self, raw: str, expected: str) -> None:
        from eifo_fetcher.sources.kan import _clean_name

        assert _clean_name(raw) == expected
