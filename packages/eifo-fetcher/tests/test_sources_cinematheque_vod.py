"""The Cinematheque VOD plugin, parsed entirely from a recorded fixture.

The fixture is a trimmed copy of the real ``cinema.co.il/vod/`` page, keeping
one card of each awkward shape the live page serves: a plain film, a
co-production whose country field carries its own slashes, a heading the theme
truncated mid-title, a WordPress duplicate slug, a heading with a marketing
tail after the VOD marker, and one film rendered twice in different rails.
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
from eifo_fetcher.sources.cinematheque_vod import (
    CATALOG_URL,
    PRICE_API,
    PRICE_CURRENCY,
    CinemathequeCatalogError,
    CinemathequeVodPlugin,
    parse_catalog,
)

ROBOTS_URL = "https://www.cinema.co.il/robots.txt"
PERMISSIVE_ROBOTS = "User-Agent: *\nAllow: /wp-admin/admin-ajax.php\nDisallow: /wp-admin\n"
#: The order ids the recorded catalog page links to, cheapest ticket type each.
FIXTURE_PRICES = {
    "132926": 19.9,
    "132385": 24.9,  # prices differ per film, verified against the real API
    "126461": 0,  # this one the Cinematheque gives away
    "126460": 19.9,
    "46171": 19.9,
}


@pytest.fixture
def cinematheque_ctx(http: object, settings: Settings) -> FetchContext:
    return FetchContext(source_key="cinematheque_vod", http=http, settings=settings)  # type: ignore[arg-type]


def _price_payload(price: float, name: str = "רגיל") -> dict:
    """The slice of the ticketing document this plugin reads."""
    return {
        "presentation": {
            "id": 1,
            "priceLevels": [
                {"ticketGroupId": 1, "name": name, "minPrice": price, "maxPrice": price}
            ],
        },
        "serverTime": "2026-08-18T18:00:00",
    }


def _mock_prices(prices: dict[str, float] | None = None) -> None:
    for order_id, price in (FIXTURE_PRICES if prices is None else prices).items():
        respx.get(PRICE_API.format(order_id=order_id)).mock(
            return_value=httpx.Response(200, json=_price_payload(price))
        )


def _mock_site(html: str | None = None, robots: str = PERMISSIVE_ROBOTS) -> None:
    respx.get(ROBOTS_URL).mock(return_value=httpx.Response(200, text=robots))
    respx.get(CATALOG_URL).mock(
        return_value=httpx.Response(
            200,
            text=html if html is not None else load_fixture("cinematheque_vod", "vod_catalog.html"),
        )
    )
    _mock_prices()


class TestSourceDeclaration:
    def test_declares_one_rent_buy_source(self) -> None:
        sources = CinemathequeVodPlugin().sources()

        assert len(sources) == 1
        assert sources[0].key == "cinematheque_vod"
        assert sources[0].kind is SourceKind.RENT_BUY


class TestFetch:
    @respx.mock
    def test_yields_every_film_once(self, cinematheque_ctx: FetchContext) -> None:
        """The same film sits in several rails; it is one offer, not several."""
        items = list(_fetch(cinematheque_ctx))

        assert len(items) == 5
        assert len({item.deep_link_url for item in items}) == 5

    @respx.mock
    def test_reads_the_catalog_in_one_request(self, cinematheque_ctx: FetchContext) -> None:
        list(_fetch(cinematheque_ctx))

        catalog_calls = [c for c in respx.calls if str(c.request.url) == CATALOG_URL]
        assert len(catalog_calls) == 1

    @respx.mock
    def test_maps_the_real_fields(self, cinematheque_ctx: FetchContext) -> None:
        first = next(iter(_fetch(cinematheque_ctx)))

        assert first.name == "נינו"
        assert first.year == 2025
        assert first.kind is TitleKind.MOVIE
        assert first.offer_type is OfferType.RENT
        assert first.deep_link_url is not None
        assert first.deep_link_url.startswith("https://www.cinema.co.il/event/")
        assert first.poster_url is not None
        assert first.poster_url.startswith("https://www.cinema.co.il/wp-content/uploads/")
        assert first.extra["country"] == "צרפת"
        assert first.extra["runtime_minutes"] == 97
        assert first.extra["order_url"] == "https://cintlv.pres.global/order/132926"

    @respx.mock
    def test_each_offer_carries_the_price_that_title_costs(
        self, cinematheque_ctx: FetchContext
    ) -> None:
        """Prices are per film, not one house rate: 19.90 here, 24.90 there."""
        by_name = {item.name: item for item in _fetch(cinematheque_ctx)}

        assert by_name["נינו"].price_minor == 1990
        assert by_name["כן"].price_minor == 2490
        assert {item.price_currency for item in by_name.values()} <= {PRICE_CURRENCY, None}

    @respx.mock
    def test_a_title_given_away_is_a_free_offer(self, cinematheque_ctx: FetchContext) -> None:
        """Zero is the Cinematheque giving a film away, not a rental at ₪0.00."""
        by_name = {item.name: item for item in _fetch(cinematheque_ctx)}
        free = by_name["אלפרידה ילינק לשון משוחררת"]

        assert free.offer_type is OfferType.FREE
        assert free.price_minor is None
        assert free.price_currency is None

    @respx.mock
    def test_the_cheapest_ticket_type_is_the_price_shown(
        self, cinematheque_ctx: FetchContext
    ) -> None:
        """One type today; a future concession must not inflate what we show."""
        _mock_site()
        respx.get(PRICE_API.format(order_id="132926")).mock(
            return_value=httpx.Response(
                200,
                json={
                    "presentation": {
                        "priceLevels": [
                            {"name": "רגיל", "minPrice": 29.9, "maxPrice": 29.9},
                            {"name": "מנוי", "minPrice": 14.9, "maxPrice": 14.9},
                        ]
                    }
                },
            )
        )

        items = list(CinemathequeVodPlugin().fetch(cinematheque_ctx))

        assert next(item for item in items if item.name == "נינו").price_minor == 1490

    @respx.mock
    def test_an_unreadable_price_leaves_the_offer_unpriced_and_counted(
        self, cinematheque_ctx: FetchContext
    ) -> None:
        """Not knowing what it costs is not the same as it being free."""
        _mock_site()
        respx.get(PRICE_API.format(order_id="132926")).mock(return_value=httpx.Response(503))

        items = list(CinemathequeVodPlugin().fetch(cinematheque_ctx))
        unpriced = next(item for item in items if item.name == "נינו")

        assert unpriced.offer_type is OfferType.RENT
        assert unpriced.price_minor is None
        assert unpriced.price_currency is None
        assert any("without a readable price" in error for error in cinematheque_ctx.errors)

    @respx.mock
    def test_the_deep_link_shows_the_film_before_the_till(
        self, cinematheque_ctx: FetchContext
    ) -> None:
        """Nobody should land on a checkout for a film they have not been shown."""
        items = list(_fetch(cinematheque_ctx))

        assert all(
            item.deep_link_url is not None
            and item.deep_link_url.startswith("https://www.cinema.co.il/event/")
            for item in items
        )
        # The till is still recorded: it is what proves the film is on offer.
        assert all(item.extra["order_url"].startswith("https://cintlv.") for item in items)


class TestNames:
    @respx.mock
    def test_a_truncated_heading_is_recovered_from_the_slug(
        self, cinematheque_ctx: FetchContext
    ) -> None:
        """The theme cuts a long heading mid-title, losing the rest of the name."""
        names = [item.name for item in _fetch(cinematheque_ctx)]

        assert "אלפרידה ילינק לשון משוחררת" in names

    @respx.mock
    def test_the_vod_marker_and_its_marketing_tail_are_stripped(
        self, cinematheque_ctx: FetchContext
    ) -> None:
        """Headings read "הנחלמים | VOD לצפייה ללא הגבלה"; the title is the film."""
        names = [item.name for item in _fetch(cinematheque_ctx)]

        assert "הנחלמים" in names
        assert not any("VOD" in name for name in names)


class TestParseCatalog:
    def test_a_co_production_keeps_its_whole_country_field(self) -> None:
        """ "ישראל/צרפת / 2025 / אורך:149" - the year splits it, not the slash."""
        cards, _ = parse_catalog(load_fixture("cinematheque_vod", "vod_catalog.html"))

        israeli = next(card for card in cards if card.name == "כן")
        assert israeli.country == "ישראל/צרפת"
        assert israeli.year == 2025

    def test_a_cinema_screening_is_not_a_vod_offer(self) -> None:
        """The page also lists screenings; only /event/<slug>-vod/ is rentable."""
        cards, dropped = parse_catalog(
            "<div class='slid'><div class='text'><div class='title'><h3>"
            "<a href='https://www.cinema.co.il/event/הקרנה-מיוחדת/'>הקרנה מיוחדת</a></h3>"
            "<div class='content'><p>ישראל / 2024 / אורך:90</p></div></div>"
            "<div class='order-btnn'><a href='https://cintlv.pres.global/order/1'>להזמנה</a>"
            "</div></div></div>"
        )

        assert cards == []
        assert dropped == 0  # not an error: it is simply not a VOD title

    def test_a_vod_card_without_a_till_is_dropped_and_counted(self) -> None:
        """A retired title keeps its page but loses its ticketing link."""
        cards, dropped = parse_catalog(
            "<div class='slid'><div class='text'><div class='title'><h3>"
            "<a href='https://www.cinema.co.il/event/בומרנג-vod/'>בומרנג | VOD</a></h3>"
            "<div class='content'><p>ישראל / 2019 / אורך:90</p></div></div>"
            "<div class='order-btnn'><a href='https://www.cinema.co.il'>לרכישה</a>"
            "</div></div></div>"
        )

        assert cards == []
        assert dropped == 1

    def test_a_page_without_cards_fails_loudly(self) -> None:
        with pytest.raises(CinemathequeCatalogError, match="layout changed"):
            parse_catalog("<html><body><h1>Under maintenance</h1></body></html>")


class TestRobots:
    @respx.mock
    def test_a_disallowed_catalog_stops_the_sync(self, cinematheque_ctx: FetchContext) -> None:
        _mock_site(robots="User-Agent: *\nDisallow: /vod/\n")

        with pytest.raises(RobotsDisallowedError):
            list(CinemathequeVodPlugin().fetch(cinematheque_ctx))

    @respx.mock
    def test_the_check_happens_before_the_catalog_is_read(
        self, cinematheque_ctx: FetchContext
    ) -> None:
        _mock_site(robots="User-Agent: *\nDisallow: /vod/\n")

        with pytest.raises(RobotsDisallowedError):
            list(CinemathequeVodPlugin().fetch(cinematheque_ctx))

        assert [str(call.request.url) for call in respx.calls] == [ROBOTS_URL]


def _fetch(ctx: FetchContext, html: str | None = None) -> list:
    _mock_site(html)
    return list(CinemathequeVodPlugin().fetch(ctx))
