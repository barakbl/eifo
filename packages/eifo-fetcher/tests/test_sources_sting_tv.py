"""The Sting TV plugin, against section markup shaped like the live tiles."""

from __future__ import annotations

import httpx
import pytest
import respx

from eifo_core.enums import OfferType, SourceKind, TitleKind
from eifo_core.settings import Settings
from eifo_fetcher.robots import RobotsDisallowedError
from eifo_fetcher.sources.base import FetchContext, RawItem
from eifo_fetcher.sources.sting_tv import (
    SECTION_URLS,
    StingCatalogError,
    StingTvPlugin,
    parse_section,
)

ROBOTS_URL = "https://www.stingtv.co.il/robots.txt"
ROBOTS = "User-agent: *\nDisallow: /wp-admin/\n"
IMG = "https://www.stingtv.co.il/wp-content/themes/STINGTV/content_items_images"


def _tile(name: str, poster: str | None = "vp1", alt: str | None = None) -> str:
    img = (
        f'<img itemprop="image" class="lazyload" data-src="{IMG}/{poster}.jpg" '
        f'alt="{alt if alt is not None else name}">'
        if poster
        else ""
    )
    return (
        '<li class="item" itemscope itemtype="http://schema.org/Thing">'
        f'{img}<div class="spinner"><img src="/img/spinner.gif" alt="רק רגע"></div>'
        f'<h2 itemprop="name">{name}</h2><p itemprop="description"></p></li>'
    )


def _page(*tiles: str) -> str:
    return (
        '<html><body><footer><ul><li class="menu-item"><a href="/x/">תפריט</a></li></ul>'
        f'</footer><div class="items"><ul>{"".join(tiles)}</ul></div></body></html>'
    )


MOVIES = _page(
    _tile("אישה יפה", "vp000381437"),
    _tile("הצלפים (קלינט איסטווד)", "vp2"),
    _tile("הברוטליסט (2024)", "vp3"),
    _tile("", None, alt=""),
    _tile("", "vp4", alt="רק באלט"),
)
SERIES = _page(_tile("פלמח", "vp5"), _tile("אישה יפה", "vp000381437"))
EMPTY = _page()


@pytest.fixture
def sting_ctx(http: object, settings: Settings) -> FetchContext:
    return FetchContext(source_key="sting_tv", http=http, settings=settings)  # type: ignore[arg-type]


def _mock(
    movies: str = MOVIES, series: str = SERIES, other: str = EMPTY, robots: str = ROBOTS
) -> None:
    respx.get(ROBOTS_URL).mock(return_value=httpx.Response(200, text=robots))
    respx.get(SECTION_URLS[0]).mock(return_value=httpx.Response(200, text=movies))
    respx.get(SECTION_URLS[1]).mock(return_value=httpx.Response(200, text=series))
    for url in SECTION_URLS[2:]:
        respx.get(url).mock(return_value=httpx.Response(200, text=other))


def _run(ctx: FetchContext) -> list[RawItem]:
    return list(StingTvPlugin().fetch(ctx))


@respx.mock
def test_reads_every_section(sting_ctx: FetchContext) -> None:
    _mock()

    items = {i.source_ref: i for i in _run(sting_ctx)}

    assert set(items) == {"vp000381437", "vp2", "vp3", "vp4", "vp5"}
    film = items["vp000381437"]
    assert film.name == "אישה יפה"
    assert film.kind is TitleKind.MOVIE
    assert film.offer_type is OfferType.STREAM
    assert film.deep_link_url == SECTION_URLS[0]
    assert film.poster_url == f"{IMG}/vp000381437.jpg"
    assert items["vp5"].kind is TitleKind.SERIES
    assert items["vp5"].deep_link_url == SECTION_URLS[1]


@respx.mock
def test_strips_notes_and_reads_a_year(sting_ctx: FetchContext) -> None:
    _mock()

    items = {i.source_ref: i for i in _run(sting_ctx)}

    assert items["vp2"].name == "הצלפים"
    assert items["vp2"].extra == {"listed_as": "הצלפים (קלינט איסטווד)"}
    assert items["vp3"].name == "הברוטליסט"
    assert items["vp3"].year == 2024
    assert items["vp2"].year is None


@respx.mock
def test_a_title_in_two_sections_is_yielded_once(sting_ctx: FetchContext) -> None:
    _mock()

    refs = [i.source_ref for i in _run(sting_ctx)]

    assert refs.count("vp000381437") == 1


@respx.mock
def test_a_tile_without_any_name_is_skipped(sting_ctx: FetchContext) -> None:
    _mock()

    items = {i.source_ref: i for i in _run(sting_ctx)}

    assert "" not in {i.name for i in items.values()}
    assert items["vp4"].name == "רק באלט"  # empty heading: the image alt names it


def test_without_an_h2_the_alt_names_the_title() -> None:
    html = (
        '<ul><li class="item"><img itemprop="image" data-src="/a/vp9.png" alt="שם מאלט"></li></ul>'
    )

    (item,) = parse_section(html, SECTION_URLS[0], TitleKind.MOVIE)

    assert item.name == "שם מאלט"
    assert item.source_ref == "vp9"
    assert item.poster_url == "https://www.stingtv.co.il/a/vp9.png"


def test_a_tile_without_a_poster_falls_back_to_its_name() -> None:
    (item,) = parse_section(_page(_tile("בלי תמונה", None)), SECTION_URLS[0], TitleKind.SERIES)

    assert item.source_ref == "בלי תמונה"
    assert item.poster_url is None


@respx.mock
def test_no_tiles_anywhere_raises(sting_ctx: FetchContext) -> None:
    _mock(movies=EMPTY, series=EMPTY)

    with pytest.raises(StingCatalogError):
        _run(sting_ctx)


@respx.mock
def test_robots_disallow_raises(sting_ctx: FetchContext) -> None:
    _mock(robots="User-agent: *\nDisallow: /content-items/\n")

    with pytest.raises(RobotsDisallowedError):
        _run(sting_ctx)


@respx.mock
def test_an_http_error_raises(sting_ctx: FetchContext) -> None:
    _mock()
    respx.get(SECTION_URLS[1]).mock(return_value=httpx.Response(404))

    with pytest.raises(httpx.HTTPStatusError):
        _run(sting_ctx)


def test_declares_a_partial_subscription_source() -> None:
    (info,) = StingTvPlugin().sources()

    assert info.key == "sting_tv"
    assert info.kind is SourceKind.SUBSCRIPTION
    assert info.partial is True
