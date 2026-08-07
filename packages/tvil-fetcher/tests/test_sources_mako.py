"""The Mako VOD plugin, parsed entirely from a recorded fixture.

The fixture is a trimmed copy of a real rendered catalog page from mako.co.il,
so a change in Mako's payload shape shows up here rather than in production.
"""

from __future__ import annotations

import httpx
import pytest
import respx
from recorded import load_fixture

from tvil_core.enums import OfferType, SourceKind, TitleKind
from tvil_fetcher.sources.base import FetchContext
from tvil_fetcher.sources.mako import (
    BASE_URL,
    CATALOG_PATH,
    MakoCatalogError,
    MakoPlugin,
    _programs,
    _to_item,
    parse_next_data,
)

INDEX_PAGE = f"{BASE_URL}{CATALOG_PATH}"


@pytest.fixture
def mako_ctx(http: object, settings: object) -> FetchContext:
    return FetchContext(source_key="mako", http=http, settings=settings)  # type: ignore[arg-type]


def _mock_catalog(html: str | None = None) -> None:
    respx.get(INDEX_PAGE).mock(
        return_value=httpx.Response(
            200, text=html if html is not None else load_fixture("mako", "vod_index_page.html")
        )
    )


class TestSourceDeclaration:
    def test_declares_one_free_source(self) -> None:
        sources = MakoPlugin().sources()

        assert len(sources) == 1
        assert sources[0].key == "mako"
        assert sources[0].kind is SourceKind.FREE


class TestFetch:
    @respx.mock
    def test_yields_every_catalog_entry(self, mako_ctx: FetchContext) -> None:
        _mock_catalog()

        items = list(MakoPlugin().fetch(mako_ctx))

        assert len(items) == 5
        assert all(item.source_key == "mako" for item in items)

    @respx.mock
    def test_reads_the_catalog_in_a_single_request(self, mako_ctx: FetchContext) -> None:
        """The JSON endpoint answers only browser-looking clients; the page does not."""
        _mock_catalog()

        list(MakoPlugin().fetch(mako_ctx))

        assert len(respx.calls) == 1
        assert respx.calls[0].request.url == INDEX_PAGE

    @respx.mock
    def test_maps_the_real_field_names(self, mako_ctx: FetchContext) -> None:
        _mock_catalog()

        first = next(iter(MakoPlugin().fetch(mako_ctx)))

        assert first.name == "חתונה ממבט ראשון"
        assert first.kind is TitleKind.SERIES
        assert first.offer_type is OfferType.FREE
        assert first.deep_link_url == f"{BASE_URL}/mako-vod-keshet/wedding-at-first-sight"
        assert first.poster_url is not None
        assert first.poster_url.startswith("https://img.mako.co.il/")
        assert first.extra["vcm_id"]

    @respx.mock
    def test_catalog_has_no_years(self, mako_ctx: FetchContext) -> None:
        """Verified against the real payload; enrichment fills this in later."""
        _mock_catalog()

        assert all(item.year is None for item in MakoPlugin().fetch(mako_ctx))

    @respx.mock
    def test_an_interstitial_fails_with_an_explanation(self, mako_ctx: FetchContext) -> None:
        """A bot check returns HTML without __NEXT_DATA__; say so, don't guess."""
        _mock_catalog("<html><head><script>challenge()</script></head></html>")

        with pytest.raises(MakoCatalogError, match="interstitial"):
            list(MakoPlugin().fetch(mako_ctx))

    @respx.mock
    def test_unreadable_next_data_fails_loudly(self, mako_ctx: FetchContext) -> None:
        _mock_catalog('<script id="__NEXT_DATA__" type="application/json">{not json</script>')

        with pytest.raises(MakoCatalogError, match="valid JSON"):
            list(MakoPlugin().fetch(mako_ctx))

    @respx.mock
    def test_a_malformed_entry_is_recorded_and_skipped(self, mako_ctx: FetchContext) -> None:
        _mock_catalog(
            '<script id="__NEXT_DATA__" type="application/json">'
            '{"props":{"pageProps":{"programs":{"items":['
            '{"title":"תקין","pageUrl":"/mako-vod-keshet/ok"},'
            '{"title":"","pageUrl":"/mako-vod-keshet/blank"},'
            '"not-an-object"]}}}}'
            "</script>"
        )

        items = list(MakoPlugin().fetch(mako_ctx))

        assert [item.name for item in items] == ["תקין"]
        assert mako_ctx.error_count == 2


class TestPayloadShapes:
    def test_reads_the_rendered_page_shape(self) -> None:
        payload = parse_next_data(load_fixture("mako", "vod_index_page.html"))

        assert len(_programs(payload)) == 5

    def test_also_reads_the_data_endpoint_shape(self) -> None:
        """Both shapes wrap the same object at different depths."""
        items = _programs(parse_next_data(load_fixture("mako", "vod_index_page.html")))

        assert _programs({"pageProps": {"programs": {"items": items}}}) == items

    @pytest.mark.parametrize(
        ("payload", "message"),
        [
            ([], "expected a JSON object"),
            ({}, "no pageProps"),
            ({"pageProps": {}}, "no programs"),
            ({"pageProps": {"programs": {}}}, "no items"),
        ],
    )
    def test_rejects_an_unrecognised_payload(self, payload: object, message: str) -> None:
        with pytest.raises(MakoCatalogError, match=message):
            _programs(payload)

    def test_a_page_without_next_data_is_rejected(self) -> None:
        with pytest.raises(MakoCatalogError, match="interstitial"):
            parse_next_data("<html><body>nothing here</body></html>")


class TestEntryConversion:
    def test_requires_a_title_and_a_url(self) -> None:
        assert _to_item({"title": "", "pageUrl": "/x"}) is None
        assert _to_item({"title": "x", "pageUrl": ""}) is None
        assert _to_item("nonsense") is None

    def test_makes_relative_urls_absolute(self) -> None:
        item = _to_item({"title": "x", "pageUrl": "/mako-vod-keshet/y"})

        assert item is not None
        assert item.deep_link_url == f"{BASE_URL}/mako-vod-keshet/y"

    def test_leaves_absolute_urls_alone(self) -> None:
        item = _to_item({"title": "x", "pageUrl": f"{BASE_URL}/z"})

        assert item is not None
        assert item.deep_link_url == f"{BASE_URL}/z"

    def test_tolerates_missing_artwork(self) -> None:
        item = _to_item({"title": "x", "pageUrl": "/y"})

        assert item is not None
        assert item.poster_url is None
