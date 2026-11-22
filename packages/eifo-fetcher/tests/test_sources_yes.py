"""The yes plugin, against lobby markup shaped like its title tiles."""

from __future__ import annotations

import httpx
import pytest
import respx

from eifo_core.enums import TitleKind
from eifo_core.settings import Settings
from eifo_fetcher.robots import RobotsDisallowedError
from eifo_fetcher.sources.base import FetchContext, RawItem
from eifo_fetcher.sources.yes import (
    LOBBY_URLS,
    YesCatalogError,
    YesPlugin,
    parse_lobby,
)

ROBOTS_URL = "https://www.yes.co.il/robots.txt"
ROBOTS = "User-agent: *\nDisallow: /api/\nDisallow: /content/tv/ganres/\n"

MOVIES = """
<html><body>
<a href="/content/tv/movies/the-brutalist/"><img src="/media/abc/vp1.jpg?quality=80" alt="">
  <span>הברוטליסט (2024)</span></a>
<a href="/content/tv/movies/smurfs/">
  <img src="https://www.yes.co.il/media/x/vp2.jpg" alt="דרדסים (מדובב)"></a>
<a href="/content/tv/movies/the-brutalist/">הברוטליסט (2024)</a>
<a href="/content/tv/movies/empty/"></a>
<a href="/content/tv/ganres/comedy/">קומדיה</a>
<a href="/deals/sale/">הצטרפו</a>
</body></html>
"""
SERIES = (
    '<a href="/content/tv/series/fargo/">פארגו</a><a href="/content/tv/movies/smurfs/">דרדסים</a>'
)


@pytest.fixture
def yes_ctx(http: object, settings: Settings) -> FetchContext:
    return FetchContext(source_key="yes", http=http, settings=settings)  # type: ignore[arg-type]


def _mock(movies: str = MOVIES, series: str = SERIES, robots: str = ROBOTS) -> None:
    respx.get(ROBOTS_URL).mock(return_value=httpx.Response(200, text=robots))
    respx.get(LOBBY_URLS[0]).mock(return_value=httpx.Response(200, text=movies))
    respx.get(LOBBY_URLS[1]).mock(return_value=httpx.Response(200, text=series))


def _run(ctx: FetchContext) -> list[RawItem]:
    return list(YesPlugin().fetch(ctx))


@respx.mock
def test_reads_both_lobbies(yes_ctx: FetchContext) -> None:
    _mock()

    items = {i.source_ref: i for i in _run(yes_ctx)}

    assert set(items) == {"the-brutalist", "smurfs", "fargo"}
    brutalist = items["the-brutalist"]
    assert (brutalist.name, brutalist.year, brutalist.kind) == ("הברוטליסט", 2024, TitleKind.MOVIE)
    assert brutalist.poster_url == "https://www.yes.co.il/media/abc/vp1.jpg?quality=80"
    assert brutalist.deep_link_url == "https://www.yes.co.il/content/tv/movies/the-brutalist/"
    # A trailing note is not part of the title; the image alt is a name fallback.
    assert items["smurfs"].name == "דרדסים"
    assert items["fargo"].kind is TitleKind.SERIES


def test_parse_lobby_dedupes_and_skips_nameless_and_other_pages() -> None:
    items = parse_lobby(MOVIES, LOBBY_URLS[0])

    assert sorted(i.source_ref or "" for i in items) == ["smurfs", "the-brutalist"]


@respx.mock
def test_no_title_links_raises(yes_ctx: FetchContext) -> None:
    _mock(movies="<html></html>", series="<html></html>")

    with pytest.raises(YesCatalogError):
        _run(yes_ctx)


@respx.mock
def test_robots_block_is_respected(yes_ctx: FetchContext) -> None:
    _mock(robots="User-agent: *\nDisallow: /content/\n")

    with pytest.raises(RobotsDisallowedError):
        _run(yes_ctx)


@respx.mock
def test_a_failing_lobby_raises(yes_ctx: FetchContext) -> None:
    respx.get(ROBOTS_URL).mock(return_value=httpx.Response(200, text=ROBOTS))
    respx.get(LOBBY_URLS[0]).mock(return_value=httpx.Response(403))

    with pytest.raises(httpx.HTTPError):
        _run(yes_ctx)


def test_source_is_a_partial_subscription() -> None:
    (info,) = YesPlugin().sources()

    assert info.key == "yes"
    assert info.partial is True
