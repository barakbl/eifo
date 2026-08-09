"""The FreeTV plugin, parsed entirely from a recorded API page.

The fixture is a trimmed copy of a real ``/api/products/vods`` response, so a
change in FreeTV's payload shape shows up here rather than as a silently empty
catalog.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx
from recorded import load_fixture

from tvil_core.enums import OfferType, SourceKind, TitleKind
from tvil_fetcher.sources.base import FetchContext
from tvil_fetcher.sources.freetv import (
    CATALOG_PATH,
    HOST,
    FreetvCatalogError,
    FreetvPlugin,
    to_item,
)

CATALOG_URL = f"https://{HOST}{CATALOG_PATH}"


def page(items: list[dict], *, total: int | None = None, first: int = 0) -> str:
    """Build a product-list response body around some items."""
    body = {
        "items": items,
        "meta": {
            "totalCount": len(items) if total is None else total,
            "firstResult": first,
            "maxResults": 100,
        },
    }
    return json.dumps(body, ensure_ascii=False)


@pytest.fixture
def freetv_ctx(http: object, settings: object) -> FetchContext:
    return FetchContext(source_key="freetv", http=http, settings=settings)  # type: ignore[arg-type]


def mock_catalog(body: str | None = None) -> None:
    respx.get(CATALOG_URL).mock(
        return_value=httpx.Response(
            200, text=body if body is not None else load_fixture("freetv", "vods_page.json")
        )
    )


class TestSourceDeclaration:
    def test_declares_one_subscription_source(self) -> None:
        sources = FreetvPlugin().sources()

        assert len(sources) == 1
        assert sources[0].key == "freetv"
        assert sources[0].kind is SourceKind.SUBSCRIPTION


class TestFetch:
    @respx.mock
    def test_yields_movies_and_series_from_one_list(self, freetv_ctx: FetchContext) -> None:
        mock_catalog()

        items = list(FreetvPlugin().fetch(freetv_ctx))

        assert len(items) == 5
        assert sum(i.kind is TitleKind.MOVIE for i in items) == 4
        assert sum(i.kind is TitleKind.SERIES for i in items) == 1
        assert all(i.source_key == "freetv" for i in items)
        assert all(i.offer_type is OfferType.STREAM for i in items)

    @respx.mock
    def test_sends_the_mandatory_platform_parameter(self, freetv_ctx: FetchContext) -> None:
        """Omitting platform=BROWSER is an HTTP 400 from the portal."""
        mock_catalog()

        list(FreetvPlugin().fetch(freetv_ctx))

        assert respx.calls[0].request.url.params["platform"] == "BROWSER"

    @respx.mock
    def test_uses_the_ready_made_deep_link(self, freetv_ctx: FetchContext) -> None:
        mock_catalog()

        item = next(i for i in FreetvPlugin().fetch(freetv_ctx) if i.name == "100% זאב")

        assert item.deep_link_url.startswith("https://web.freetv.tv/")
        assert item.extra["content_id"] is not None

    @respx.mock
    def test_prefers_a_portrait_poster_and_makes_it_absolute(
        self, freetv_ctx: FetchContext
    ) -> None:
        mock_catalog()

        by_name = {i.name: i for i in FreetvPlugin().fetch(freetv_ctx)}
        # This fixture item carries 2x3, 3x4 and 16x9 - the 2x3 must win.
        poster = by_name["10 דברים להספיק לפני שנפרד"].poster_url

        assert poster is not None
        assert poster.startswith("https://")
        assert "3x4" not in poster and "169" not in poster

    @respx.mock
    def test_falls_back_to_widescreen_when_no_portrait_exists(
        self, freetv_ctx: FetchContext
    ) -> None:
        """The last fixture item has only a 16x9 image; it must still get a poster."""
        mock_catalog()

        posters = [i.poster_url for i in FreetvPlugin().fetch(freetv_ctx)]

        assert all(p is None or p.startswith("https://") for p in posters)
        assert any(p and "169" in p for p in posters)

    @respx.mock
    def test_keeps_a_missing_year_null(self, freetv_ctx: FetchContext) -> None:
        """6% of the catalog has no year; enrichment fills it, we do not invent it."""
        mock_catalog()

        years = [i.year for i in FreetvPlugin().fetch(freetv_ctx)]

        assert None in years
        assert 2024 in years


class TestPagination:
    @respx.mock
    def test_walks_every_page_until_the_total_is_reached(self, freetv_ctx: FetchContext) -> None:
        movie = _movie(1)
        first = page([dict(movie, id=n) for n in range(100)], total=150, first=0)
        second = page([dict(movie, id=n) for n in range(100, 150)], total=150, first=100)
        route = respx.get(CATALOG_URL).mock(
            side_effect=[httpx.Response(200, text=first), httpx.Response(200, text=second)]
        )

        items = list(FreetvPlugin().fetch(freetv_ctx))

        assert len(items) == 150
        assert route.call_count == 2
        assert route.calls[0].request.url.params["firstResult"] == "0"
        assert route.calls[1].request.url.params["firstResult"] == "100"

    @respx.mock
    def test_stops_when_a_page_comes_back_empty(self, freetv_ctx: FetchContext) -> None:
        """A total that overstates the catalog must not loop forever."""
        full = page([dict(_movie(1), id=n) for n in range(100)], total=9999, first=0)
        empty = page([], total=9999, first=100)
        respx.get(CATALOG_URL).mock(
            side_effect=[httpx.Response(200, text=full), httpx.Response(200, text=empty)]
        )

        items = list(FreetvPlugin().fetch(freetv_ctx))

        assert len(items) == 100

    @respx.mock
    def test_honours_a_configured_page_cap(self, freetv_ctx: FetchContext) -> None:
        """max_pages bounds a sweep; the plugin logs that coverage was capped."""
        freetv_ctx.settings.sources["freetv"] = _source_config(max_pages=1)  # type: ignore[attr-defined]
        body = page([dict(_movie(1), id=n) for n in range(100)], total=9999, first=0)
        route = respx.get(CATALOG_URL).mock(return_value=httpx.Response(200, text=body))

        items = list(FreetvPlugin().fetch(freetv_ctx))

        assert len(items) == 100
        assert route.call_count == 1


class TestBadResponses:
    @respx.mock
    def test_a_response_without_items_fails_loudly(self, freetv_ctx: FetchContext) -> None:
        mock_catalog(body=json.dumps({"error": "gone"}))

        with pytest.raises(FreetvCatalogError, match="product API has changed"):
            list(FreetvPlugin().fetch(freetv_ctx))

    @respx.mock
    def test_a_non_object_body_fails_loudly(self, freetv_ctx: FetchContext) -> None:
        mock_catalog(body=json.dumps([1, 2, 3]))

        with pytest.raises(FreetvCatalogError, match="expected a JSON object"):
            list(FreetvPlugin().fetch(freetv_ctx))

    @respx.mock
    def test_a_missing_total_count_fails_loudly(self, freetv_ctx: FetchContext) -> None:
        mock_catalog(body=json.dumps({"items": [], "meta": {"firstResult": 0}}))

        with pytest.raises(FreetvCatalogError, match="totalCount"):
            list(FreetvPlugin().fetch(freetv_ctx))

    @respx.mock
    def test_a_bad_entry_is_recorded_not_stored(self, freetv_ctx: FetchContext) -> None:
        body = page(
            [
                _movie(1),
                {"type": "VOD", "title": "", "webUrl": "https://x"},  # blank title
                {"type": "CHANNEL", "title": "Live", "webUrl": "https://x"},  # unknown type
                {"type": "SERIAL", "title": "No link", "id": 9},  # no webUrl
            ]
        )
        mock_catalog(body=body)

        items = list(FreetvPlugin().fetch(freetv_ctx))

        assert len(items) == 1
        assert freetv_ctx.error_count == 3


class TestToItem:
    def test_maps_vod_to_movie_and_serial_to_series(self) -> None:
        assert to_item(_movie(1)).kind is TitleKind.MOVIE
        assert to_item(dict(_movie(1), type="SERIAL")).kind is TitleKind.SERIES

    def test_an_unknown_type_is_rejected(self) -> None:
        """Guessing the medium is worse than skipping the item."""
        assert to_item(dict(_movie(1), type="EPISODE")) is None

    def test_a_non_dict_is_rejected(self) -> None:
        assert to_item("nope") is None


def _movie(content_id: int) -> dict:
    return {
        "id": content_id,
        "publicUid": "uid-" + str(content_id),
        "title": "Some Film",
        "type": "VOD",
        "year": 2020,
        "webUrl": f"https://web.freetv.tv/movie,2039/some-film,{content_id}",
        "images": {"3x4": [{"url": "//img.example/p34.jpg"}]},
    }


def _source_config(**kwargs: object) -> object:
    from tvil_core.settings import SourceConfig

    return SourceConfig(**kwargs)  # type: ignore[arg-type]
