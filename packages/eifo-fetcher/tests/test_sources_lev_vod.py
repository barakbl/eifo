"""The Lev VOD plugin, parsed entirely from recorded fixtures.

``presentations.json`` is a trimmed copy of the chain's real listing, keeping
one row of each awkward shape the live API serves: a plain film, the same film
listed twice under two presentations, a title whose English field repeats the
Hebrew, the two leftovers that carry no window and no price, a cinema screening
that shares the document, and a VOD row that names no film.

Three rows are edited, and each one is here rather than hidden. The expired
title had its window moved into the past, because every row the live listing
served was inside its own. The nameless row had its ``featureId`` removed and
its close pushed out to the listing's furthest date, so the count it proves
does not start passing for the wrong reason in 2028. Everything else is
verbatim - including the marketing tail on "מחוברים לחיים VOD צרפתי", which is
why that title is tested through an edited row instead: the one film that
really carries a tail is also one of the two that are not on sale.

``features.json`` holds the real film records those rows point at, including
the ``{"feature": null}`` the API answers for a film it sells and has never
described. Their inline base64 posters are blanked - megabytes each, and
nothing here reads them.
"""

from __future__ import annotations

import datetime as dt
import json

import httpx
import pytest
import respx
from recorded import load_json_fixture

from eifo_core.enums import CreditRole, OfferType, SourceKind, TitleKind
from eifo_core.settings import Settings
from eifo_fetcher.robots import RobotsDisallowedError
from eifo_fetcher.sources.base import FetchContext, RawItem
from eifo_fetcher.sources.lev_vod import (
    CATALOG_API,
    FEATURE_API,
    PRICE_API,
    PRICE_CURRENCY,
    LevCatalogError,
    LevVodPlugin,
    parse_catalog,
    parse_feature,
)

ROBOTS_URL = "https://ticket.lev.co.il/robots.txt"
#: What the host really serves: a page of comment and not one directive, which
#: RFC 9309 reads as no restrictions.
PERMISSIVE_ROBOTS = "# content-signal preamble, and no rules at all\n"
#: The presentation ids the recorded listing actually offers, and what each
#: costs. The two leftovers are absent on purpose: nothing should ask.
FIXTURE_PRICES = {574964: 19.9, 531590: 19.9, 549702: 19.9}
#: A moment inside every open window in the fixture and outside the closed one.
NOW = dt.datetime(2026, 9, 22, 12, 0, tzinfo=dt.UTC)


@pytest.fixture
def lev_ctx(http: object, settings: Settings) -> FetchContext:
    return FetchContext(source_key="lev_vod", http=http, settings=settings)  # type: ignore[arg-type]


def _price_payload(price: float | None, name: str = "רגיל") -> dict:
    """The slice of the ticketing document this plugin reads."""
    levels = [] if price is None else [{"ticketGroupId": 1, "name": name, "minPrice": price}]
    return {"presentation": {"id": 1, "priceLevels": levels}, "serverTime": "2026-09-22T12:00:00"}


def _mock_site(robots: str = PERMISSIVE_ROBOTS) -> None:
    respx.get(ROBOTS_URL).mock(return_value=httpx.Response(200, text=robots))
    respx.get(CATALOG_API).mock(
        return_value=httpx.Response(200, json=load_json_fixture("lev_vod", "presentations.json"))
    )
    features = load_json_fixture("lev_vod", "features.json")
    for feature_id, payload in features.items():
        respx.get(FEATURE_API.format(feature_id=feature_id)).mock(
            return_value=httpx.Response(200, json=payload)
        )
    for presentation_id, price in FIXTURE_PRICES.items():
        respx.get(PRICE_API.format(presentation_id=presentation_id)).mock(
            return_value=httpx.Response(200, json=_price_payload(price))
        )


def _fetch(ctx: FetchContext) -> list[RawItem]:
    _mock_site()
    return list(LevVodPlugin().fetch(ctx))


#: The row every ``_catalog`` edit lands on unless told otherwise: the newer of
#: the two rows for Portrait, and so the one that survives the dedupe. Editing
#: the older one would prove nothing - its twin replaces it.
EDITABLE_ROW = 574964


def _catalog(row_id: int = EDITABLE_ROW, **edits: object) -> dict:
    """The recorded listing, with one row's fields overridden by ``edits``."""
    payload = load_json_fixture("lev_vod", "presentations.json")
    row = next(r for r in payload["presentations"] if r["id"] == row_id)
    row.update(edits)
    return payload


def _only(row_id: int = EDITABLE_ROW, **edits: object) -> dict:
    """One edited row on its own, for what a second row would mask."""
    payload = _catalog(row_id, **edits)
    payload["presentations"] = [r for r in payload["presentations"] if r["id"] == row_id]
    return payload


class TestSourceDeclaration:
    def test_declares_one_rent_buy_source(self) -> None:
        sources = LevVodPlugin().sources()

        assert len(sources) == 1
        assert sources[0].key == "lev_vod"
        assert sources[0].kind is SourceKind.RENT_BUY

    def test_is_on_unless_an_operator_turns_it_off(self) -> None:
        """~730 requests a sync is well inside what a nightly run is for."""
        assert LevVodPlugin().sources()[0].default_enabled is True


class TestFetch:
    @respx.mock
    def test_yields_every_film_once(self, lev_ctx: FetchContext) -> None:
        """A film re-sold under a second presentation is one offer, not two."""
        items = _fetch(lev_ctx)

        assert len(items) == 3
        assert len({item.deep_link_url for item in items}) == 3

    @respx.mock
    def test_reads_the_whole_listing_in_one_request(self, lev_ctx: FetchContext) -> None:
        _fetch(lev_ctx)

        catalog_calls = [c for c in respx.calls if str(c.request.url) == CATALOG_API]
        assert len(catalog_calls) == 1

    @respx.mock
    def test_maps_the_real_fields(self, lev_ctx: FetchContext) -> None:
        by_name = {item.name: item for item in _fetch(lev_ctx)}
        portrait = by_name["דיוקן של נערה עולה באש"]

        assert portrait.name_alt == "Portrait of a Lady on Fire"
        assert portrait.year == 2019
        assert portrait.kind is TitleKind.MOVIE
        assert portrait.offer_type is OfferType.RENT
        assert portrait.deep_link_url == "https://ticket.lev.co.il/feature/11"
        assert portrait.source_ref == "11"
        assert portrait.origin_countries == "FR"
        assert portrait.extra["runtime_minutes"] == 120
        assert portrait.extra["category"] == "ממיטב הפסטיבלים"
        assert portrait.extra["trailer_url"].startswith("https://")

    @respx.mock
    def test_a_poster_costs_no_request_of_its_own(self, lev_ctx: FetchContext) -> None:
        """Artwork is addressed by film, so there is nothing to look up."""
        items = _fetch(lev_ctx)

        assert all(
            item.poster_url
            == f"https://ticket.lev.co.il/api/features/{item.source_ref}/image?raw=1"
            for item in items
        )
        assert not [c for c in respx.calls if "/image" in str(c.request.url)]

    @respx.mock
    def test_each_offer_carries_the_price_the_till_quotes(self, lev_ctx: FetchContext) -> None:
        items = _fetch(lev_ctx)

        assert all(item.price_minor == 1990 for item in items)
        assert {item.price_currency for item in items} == {PRICE_CURRENCY}

    @respx.mock
    def test_the_cheapest_ticket_type_is_the_price_shown(self, lev_ctx: FetchContext) -> None:
        """One type today; a future concession must not inflate what we show."""
        _mock_site()
        respx.get(PRICE_API.format(presentation_id=574964)).mock(
            return_value=httpx.Response(
                200,
                json={
                    "presentation": {
                        "priceLevels": [
                            {"name": "רגיל", "minPrice": 29.9},
                            {"name": "מנוי", "minPrice": 14.9},
                        ]
                    }
                },
            )
        )

        items = list(LevVodPlugin().fetch(lev_ctx))

        assert next(i for i in items if i.source_ref == "11").price_minor == 1490

    @respx.mock
    def test_an_unreadable_price_leaves_the_offer_unpriced_and_counted(
        self, lev_ctx: FetchContext
    ) -> None:
        """Not knowing what it costs is not the same as it being free."""
        _mock_site()
        respx.get(PRICE_API.format(presentation_id=574964)).mock(return_value=httpx.Response(503))

        items = list(LevVodPlugin().fetch(lev_ctx))
        unpriced = next(item for item in items if item.source_ref == "11")

        assert unpriced.offer_type is OfferType.RENT
        assert unpriced.price_minor is None
        assert unpriced.price_currency is None
        assert any("without a readable price" in error for error in lev_ctx.errors)

    @respx.mock
    def test_a_till_quoting_nothing_is_not_a_free_film(self, lev_ctx: FetchContext) -> None:
        """The chain gives nothing away: an empty price band is a silent till."""
        _mock_site()
        respx.get(PRICE_API.format(presentation_id=574964)).mock(
            return_value=httpx.Response(200, json=_price_payload(None))
        )

        items = list(LevVodPlugin().fetch(lev_ctx))
        unpriced = next(item for item in items if item.source_ref == "11")

        assert unpriced.offer_type is OfferType.RENT
        assert unpriced.price_minor is None

    @respx.mock
    def test_the_deep_link_shows_the_film_before_the_till(self, lev_ctx: FetchContext) -> None:
        """Nobody should land on a checkout for a film they have not been shown."""
        items = _fetch(lev_ctx)

        assert all(
            item.deep_link_url is not None
            and item.deep_link_url.startswith("https://ticket.lev.co.il/feature/")
            for item in items
        )
        # The till is still recorded: it is what the price was read from.
        tills = {item.extra["order_url"] for item in items}
        assert all(till.startswith("https://ticket.lev.co.il/order/") for till in tills)


class TestFilmRecords:
    @respx.mock
    def test_credits_the_director_and_the_cast_in_hebrew(self, lev_ctx: FetchContext) -> None:
        """TMDB carries little Israeli distribution; this is what there is."""
        by_name = {item.name: item for item in _fetch(lev_ctx)}
        portrait = by_name["דיוקן של נערה עולה באש"]

        assert dict(portrait.credits[0]) == {
            "role": CreditRole.DIRECTOR,
            "name_he": "סלין סיאמה",
        }
        assert [dict(c)["name_he"] for c in portrait.credits[1:]] == ["אדל האנל", "נעמי מרלן"]

    @respx.mock
    def test_a_cast_list_is_split_into_people(self, lev_ctx: FetchContext) -> None:
        by_name = {item.name: item for item in _fetch(lev_ctx)}
        dreams = by_name["חלומות גדולים"]
        cast = [dict(c)["name_he"] for c in dreams.credits if dict(c)["role"] is CreditRole.CAST]

        assert cast == ["קייסי אפלק", "זואי דשנל", "וולטון גוגינס", "כריס מסינה"]

    @respx.mock
    def test_a_film_the_chain_never_described_is_still_on_offer(
        self, lev_ctx: FetchContext
    ) -> None:
        """The API answers {"feature": null} for one film it sells today.

        The offer is real regardless: the title, the price and the link all
        come from the listing, and only the description is missing.
        """
        _mock_site()
        respx.get(FEATURE_API.format(feature_id=11)).mock(
            return_value=httpx.Response(200, json={"feature": None})
        )

        items = list(LevVodPlugin().fetch(lev_ctx))
        undescribed = next(item for item in items if item.source_ref == "11")

        assert undescribed.name == "דיוקן של נערה עולה באש"
        assert undescribed.price_minor == 1990
        assert undescribed.year is None
        assert undescribed.origin_countries is None
        assert undescribed.credits == ()
        assert any("without their own record" in error for error in lev_ctx.errors)

    def test_a_record_describing_no_film_is_refused(self) -> None:
        with pytest.raises(LevCatalogError, match="describes no film"):
            parse_feature({"feature": None})

    def test_a_document_that_is_not_a_record_is_refused(self) -> None:
        with pytest.raises(LevCatalogError, match="no 'feature'"):
            parse_feature({"presentations": []})


class TestNames:
    def test_a_marketing_tail_is_not_part_of_the_title(self) -> None:
        """ "מחוברים לחיים VOD צרפתי" is one film, called "מחוברים לחיים".

        The real string, on a row that is really on sale - the film that
        carries it is one of the two the chain no longer sells.
        """
        offers, _ = parse_catalog(_catalog(featureName="מחוברים לחיים VOD צרפתי"), now=NOW)

        names = {offer.name for offer in offers}
        assert "מחוברים לחיים" in names
        assert not any("VOD" in name for name in names)

    @respx.mock
    def test_an_english_field_repeating_the_hebrew_is_not_a_second_name(
        self, lev_ctx: FetchContext
    ) -> None:
        """An "alternate" identical to the name helps nobody find anything."""
        by_name = {item.name: item for item in _fetch(lev_ctx)}

        assert by_name["חלומות גדולים"].name_alt is None
        assert by_name["דיוקן של נערה עולה באש"].name_alt == "Portrait of a Lady on Fire"

    def test_a_film_with_no_english_name_keeps_its_hebrew_one(self) -> None:
        offers, _ = parse_catalog(_catalog(featureAdditionalName=""), now=NOW)

        portrait = next(offer for offer in offers if offer.feature_id == 11)
        assert portrait.name == "דיוקן של נערה עולה באש"
        assert portrait.name_alt is None

    def test_a_title_written_in_neither_script_is_still_a_title(self) -> None:
        """ "1917" is spelled the same in both fields and has no letters.

        Sorting the two names by script answers "neither", which is correct and
        is not the same as "nameless" - reading it that way dropped a film the
        chain really sells.
        """
        offers, dropped = parse_catalog(
            _catalog(featureName="1917", featureAdditionalName="1917"), now=NOW
        )

        assert next(o for o in offers if o.feature_id == 11).name == "1917"
        assert dropped == 1  # only the row that names nothing at all


class TestParseCatalog:
    def test_a_cinema_screening_is_not_a_vod_offer(self) -> None:
        """The listing sells seats and rentals from one document."""
        offers, dropped = parse_catalog(load_json_fixture("lev_vod", "presentations.json"), now=NOW)

        assert "ילד המדבר מדובב" not in {offer.name for offer in offers}
        assert dropped == 1  # the nameless row, not the screening

    def test_a_title_outside_its_window_is_not_on_offer(self) -> None:
        """Listed forever, watchable for a while: the window decides."""
        offers, _ = parse_catalog(load_json_fixture("lev_vod", "presentations.json"), now=NOW)

        assert "הארטיסט" not in {offer.name for offer in offers}

    def test_a_closed_window_reopens_nothing_when_the_clock_moves_back(self) -> None:
        """The same recorded row, read at a moment inside its window."""
        offers, _ = parse_catalog(
            load_json_fixture("lev_vod", "presentations.json"),
            now=dt.datetime(2020, 6, 1, tzinfo=dt.UTC),
        )

        assert "הארטיסט" in {offer.name for offer in offers}

    def test_a_row_that_was_never_put_on_sale_is_not_an_offer(self) -> None:
        """Two of the chain's 378 VOD rows carry no window at all.

        Both give themselves away twice over - an empty price band and no
        category, so neither is reachable from the library. Reading a missing
        window as "sells forever" put both in the catalog at no price.
        """
        offers, dropped = parse_catalog(load_json_fixture("lev_vod", "presentations.json"), now=NOW)

        assert {"הדוכסית", "מחוברים לחיים"} & {offer.name for offer in offers} == set()
        assert dropped == 1  # not counted as broken: they are correctly listed

    def test_an_open_ended_window_is_taken_at_its_word(self) -> None:
        """No row has one today, so the safe guess is that it keeps selling."""
        offers, _ = parse_catalog(
            _catalog(vodEndDateTime=None), now=dt.datetime(2099, 1, 1, tzinfo=dt.UTC)
        )

        assert {offer.name for offer in offers} == {"דיוקן של נערה עולה באש"}

    def test_a_window_that_has_not_opened_yet_is_not_an_offer(self) -> None:
        """A title the chain has scheduled is not one it is selling."""
        offers, _ = parse_catalog(_only(vodStartDateTime="2030-01-01 00:00:00"), now=NOW)

        assert offers == []

    def test_the_newest_presentation_is_the_one_kept(self) -> None:
        """It is the one the site itself links to, and so the one to price."""
        offers, _ = parse_catalog(load_json_fixture("lev_vod", "presentations.json"), now=NOW)

        portrait = next(offer for offer in offers if offer.feature_id == 11)
        assert portrait.presentation_id == 574964

    def test_a_row_naming_no_film_is_dropped_and_counted(self) -> None:
        offers, dropped = parse_catalog(_catalog(featureId=None, id=None), now=NOW)

        assert dropped == 2  # the recorded nameless row, and this one
        assert all(offer.feature_id is not None for offer in offers)

    def test_a_listing_with_no_vod_rows_fails_loudly(self) -> None:
        """An empty catalog and a renumbered venue look identical from here."""
        payload = load_json_fixture("lev_vod", "presentations.json")
        payload["presentations"] = [
            row for row in payload["presentations"] if row["venueTypeId"] != 101
        ]

        with pytest.raises(LevCatalogError, match="renumbered"):
            parse_catalog(payload, now=NOW)

    def test_a_response_that_is_not_the_listing_fails_loudly(self) -> None:
        with pytest.raises(LevCatalogError, match="the API changed"):
            parse_catalog({"error": "maintenance"}, now=NOW)

    def test_an_unreadable_timestamp_does_not_lose_the_film(self) -> None:
        """A window nobody can read is not a window that has closed."""
        offers, _ = parse_catalog(_catalog(vodEndDateTime="next Tuesday"), now=NOW)

        assert 11 in {offer.feature_id for offer in offers}


class TestWindowsAreIsraeliTime:
    def test_a_window_closes_on_the_chains_clock_not_utc(self) -> None:
        """23:59 in Tel Aviv is 20:59 UTC; read as UTC it would still be open."""
        payload = _only(vodEndDateTime="2026-09-22 23:59:00")

        still_open = dt.datetime(2026, 9, 22, 20, 55, tzinfo=dt.UTC)
        closed = dt.datetime(2026, 9, 22, 21, 5, tzinfo=dt.UTC)

        assert parse_catalog(payload, now=still_open)[0]
        assert not parse_catalog(payload, now=closed)[0]


class TestRobots:
    @respx.mock
    def test_a_disallowed_listing_stops_the_sync(self, lev_ctx: FetchContext) -> None:
        _mock_site(robots="User-Agent: *\nDisallow: /api/\n")

        with pytest.raises(RobotsDisallowedError):
            list(LevVodPlugin().fetch(lev_ctx))

    @respx.mock
    def test_the_check_happens_before_the_listing_is_read(self, lev_ctx: FetchContext) -> None:
        _mock_site(robots="User-Agent: *\nDisallow: /api/\n")

        with pytest.raises(RobotsDisallowedError):
            list(LevVodPlugin().fetch(lev_ctx))

        assert [str(call.request.url) for call in respx.calls] == [ROBOTS_URL]


class TestRecordedShapes:
    def test_the_fixture_is_the_shape_the_api_really_serves(self) -> None:
        """Guards the fixture against being tidied into something unreal."""
        payload = load_json_fixture("lev_vod", "presentations.json")
        rows = payload["presentations"]

        assert {"presentations", "hasReserved", "hasGA"} <= set(payload)
        assert {row["venueTypeId"] for row in rows} == {1, 101}
        assert {row["venueName"] for row in rows if row["venueTypeId"] == 101} == {"VOD"}
        assert {row["locationName"] for row in rows if row["venueTypeId"] == 101} == {"לב בבית"}

    def test_the_feature_fixture_carries_no_base64_poster(self) -> None:
        """Real records inline megabytes of it, and nothing here reads one."""
        raw = json.dumps(load_json_fixture("lev_vod", "features.json"))

        assert "data:image" not in raw
