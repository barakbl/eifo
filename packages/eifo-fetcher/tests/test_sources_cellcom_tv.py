"""The Cellcom TV plugin, against payloads shaped like the content API's."""

from __future__ import annotations

import httpx
import pytest
import respx

from eifo_core.enums import TitleKind
from eifo_core.settings import Settings
from eifo_fetcher.robots import RobotsDisallowedError
from eifo_fetcher.sources.base import FetchContext, RawItem
from eifo_fetcher.sources.cellcom_tv import (
    API_URL,
    CATALOG_URL,
    FALLBACK_PAGE_ID,
    PARENT_ID,
    CellcomCatalogError,
    CellcomTvPlugin,
    parse_block_ids,
)

ROBOTS_URL = "https://contentepi.cellcom.co.il/robots.txt"
ITEMS_TYPE = ["Block", "InternalTvContentsItemsBlock"]


@pytest.fixture
def cellcom_ctx(http: object, settings: Settings) -> FetchContext:
    return FetchContext(source_key="cellcom_tv", http=http, settings=settings)  # type: ignore[arg-type]


def _item(
    item_id: int, title: str, image: str | None = "/globalassets/a.jpg", name: str = ""
) -> dict:
    payload: dict = {
        "contentLink": {"id": item_id},
        "contentType": ["Block", "InternalTvContentBlock"],
        "title": title,
    }
    if image:
        payload["image"] = {"value": {"url": image}, "expandedValue": {"name": name}}
    return payload


def _page(*ids: int) -> dict:
    return {"mainContentArea": [{"contentLink": {"id": i}} for i in ids]}


def _mock(page_id: int = 1158, children: object = None) -> None:
    respx.get(ROBOTS_URL).mock(return_value=httpx.Response(404))
    if children is None:
        children = [{"routeSegment": "content", "contentLink": {"id": page_id}}]
    respx.get(f"{API_URL}/{PARENT_ID}/children").mock(
        return_value=httpx.Response(200, json=children)
    )


def _run(ctx: FetchContext) -> list[RawItem]:
    return list(CellcomTvPlugin().fetch(ctx))


@respx.mock
def test_reads_expanded_items_and_skips_other_blocks(cellcom_ctx: FetchContext) -> None:
    _mock()
    respx.get(f"{API_URL}/1158").mock(return_value=httpx.Response(200, json=_page(10, 20, 30)))
    respx.get(f"{API_URL}/10", params={"expand": "*"}).mock(
        return_value=httpx.Response(
            200,
            json={
                "contentType": ITEMS_TYPE,
                "items": [
                    {"expandedValue": _item(1, "האשליה 3", name="סרטים_האשליה-3.jpg")},
                    {"expandedValue": _item(2, "סדרה", name="סדרות_x.jpg")},
                    {"expandedValue": _item(1, "האשליה 3")},  # duplicate
                    {"expandedValue": {"contentLink": {"id": 3}}},  # names nothing
                ],
            },
        )
    )
    respx.get(f"{API_URL}/20", params={"expand": "*"}).mock(
        return_value=httpx.Response(
            200, json={"contentType": ["Block", "TVChannelsListBlock"], "items": [_item(9, "ערוץ")]}
        )
    )
    respx.get(f"{API_URL}/30", params={"expand": "*"}).mock(
        return_value=httpx.Response(200, json={"contentType": ITEMS_TYPE, "items": []})
    )

    items = _run(cellcom_ctx)

    assert [i.name for i in items] == ["האשליה 3", "סדרה"]
    assert items[0].kind is TitleKind.MOVIE
    assert items[1].kind is TitleKind.SERIES
    assert items[0].deep_link_url == CATALOG_URL
    # Relative poster paths are made absolute against the API host.
    assert items[0].poster_url == "https://contentepi.cellcom.co.il/globalassets/a.jpg"


@respx.mock
def test_bare_references_are_fetched_one_by_one(cellcom_ctx: FetchContext) -> None:
    _mock()
    respx.get(f"{API_URL}/1158").mock(return_value=httpx.Response(200, json=_page(10)))
    respx.get(f"{API_URL}/10", params={"expand": "*"}).mock(
        return_value=httpx.Response(
            200, json={"contentType": ITEMS_TYPE, "items": [{"contentLink": {"id": 7}}]}
        )
    )
    respx.get(f"{API_URL}/7").mock(return_value=httpx.Response(200, json=_item(7, "בלואי")))

    assert [i.name for i in _run(cellcom_ctx)] == ["בלואי"]


@respx.mock
def test_falls_back_to_the_known_page_id(cellcom_ctx: FetchContext) -> None:
    respx.get(ROBOTS_URL).mock(return_value=httpx.Response(404))
    respx.get(f"{API_URL}/{PARENT_ID}/children").mock(return_value=httpx.Response(500))
    page = respx.get(f"{API_URL}/{FALLBACK_PAGE_ID}").mock(
        return_value=httpx.Response(200, json=_page())
    )

    assert _run(cellcom_ctx) == []
    assert page.called


@respx.mock
def test_uses_the_page_found_by_route_segment(cellcom_ctx: FetchContext) -> None:
    _mock(page_id=555)
    page = respx.get(f"{API_URL}/555").mock(return_value=httpx.Response(200, json=_page()))

    _run(cellcom_ctx)

    assert page.called


@respx.mock
def test_an_unreadable_item_is_counted_not_fatal(cellcom_ctx: FetchContext) -> None:
    _mock()
    respx.get(f"{API_URL}/1158").mock(return_value=httpx.Response(200, json=_page(10)))
    respx.get(f"{API_URL}/10", params={"expand": "*"}).mock(
        return_value=httpx.Response(
            200,
            json={"contentType": ITEMS_TYPE, "items": [{"nothing": 1}, {"title": "מובלנד"}]},
        )
    )

    items = _run(cellcom_ctx)

    assert [i.name for i in items] == ["מובלנד"]
    assert cellcom_ctx.error_count == 1


@respx.mock
def test_wrong_shape_raises(cellcom_ctx: FetchContext) -> None:
    _mock()
    respx.get(f"{API_URL}/1158").mock(return_value=httpx.Response(200, json={"oops": 1}))

    with pytest.raises(CellcomCatalogError):
        _run(cellcom_ctx)


@respx.mock
def test_robots_block_is_respected(cellcom_ctx: FetchContext) -> None:
    respx.get(ROBOTS_URL).mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /api/\n")
    )

    with pytest.raises(RobotsDisallowedError):
        _run(cellcom_ctx)


def test_parse_block_ids_skips_malformed_references() -> None:
    page = {"mainContentArea": [{"contentLink": {"id": "5"}}, {"contentLink": {}}, "x"]}

    assert parse_block_ids(page) == [5]


def test_source_is_a_partial_subscription() -> None:
    (info,) = CellcomTvPlugin().sources()

    assert info.key == "cellcom_tv"
    assert info.partial is True
