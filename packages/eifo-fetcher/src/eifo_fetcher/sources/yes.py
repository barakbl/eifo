"""yes - the titles on its public "movies" and "series" lobby pages.

yes has no public JSON API (``/api/`` is disallowed by its robots.txt) and the
yes+ app host resets honest clients. What is public is the marketing site
``www.yes.co.il``: server-rendered lobby pages whose tiles link to one page per
title, ``/content/tv/movies/<slug>/`` or ``/content/tv/series/<slug>/``. That
URL pattern is also where the movie/series distinction comes from.

Two requests read about 300 titles. The sitemap lists thousands more, but
reading them costs a request per title and also includes titles that are not on
offer; the lobbies are what yes itself is showing. Hence
:attr:`~eifo_core.items.SourceInfo.partial`.

Limits, stated plainly: the tiles carry a Hebrew name, sometimes a year in
parentheses, and a poster - no English title - and nothing says which yes
product (yes VOD, yes+, STING) carries a title, so every offer is a plain
subscription stream. The parser is tolerant of markup (it reads anchors by their
href) because it was written from a text rendering of the pages.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from html.parser import HTMLParser
from urllib.parse import urljoin

from eifo_core.enums import OfferType, SourceKind, TitleKind
from eifo_fetcher.http import USER_AGENT
from eifo_fetcher.robots import RobotsPolicy
from eifo_fetcher.sources.base import FetchContext, RawItem, SourceInfo, SourcePlugin

logger = logging.getLogger("eifo.fetch.source.yes")

SOURCE_KEY = "yes"
HOST = "www.yes.co.il"
BASE_URL = f"https://{HOST}"
LOBBY_URLS = (f"{BASE_URL}/content/movies/", f"{BASE_URL}/content/series/")
CATALOG_URL = f"{BASE_URL}/content/"

_TITLE_HREF = re.compile(r"^/content/tv/(movies|series)/([^/?#]+)/?(?:[?#].*)?$")
_TRAILING_YEAR = re.compile(r"\s*\((\d{4})\)\s*$")
#: "(מדובב)" and "(ג'רארד באטלר)" - a note after the title, not part of it.
_TRAILING_NOTE = re.compile(r"\s*\([^()]*\)\s*$")


class YesCatalogError(RuntimeError):
    """The lobby pages held no title links; the site changed or was not the lobby."""


class _Tile:
    __slots__ = ("kind", "poster", "slug", "texts")

    def __init__(self, kind: TitleKind, slug: str) -> None:
        self.kind = kind
        self.slug = slug
        self.texts: list[str] = []
        self.poster: str | None = None


class _LobbyParser(HTMLParser):
    """Collects every anchor that links to a title page, with its text and picture."""

    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self._base_url = base_url
        self._open: _Tile | None = None
        self.tiles: list[_Tile] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "a":
            match = _TITLE_HREF.match(values.get("href") or "")
            if match:
                kind = TitleKind.MOVIE if match.group(1) == "movies" else TitleKind.SERIES
                self._open = _Tile(kind, match.group(2))
                self.tiles.append(self._open)
        elif tag == "img" and self._open is not None:
            alt = (values.get("alt") or "").strip()
            if alt:
                self._open.texts.append(alt)
            src = values.get("src") or values.get("data-src")
            if src and self._open.poster is None:
                self._open.poster = urljoin(self._base_url, src)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a":
            self._open = None

    def handle_data(self, data: str) -> None:
        if self._open is not None and data.strip():
            self._open.texts.append(" ".join(data.split()))


class YesPlugin(SourcePlugin):
    """Yields the titles on yes's movies and series lobby pages."""

    def sources(self) -> list[SourceInfo]:
        return [
            SourceInfo(
                key=SOURCE_KEY,
                name="yes",
                kind=SourceKind.SUBSCRIPTION,
                website_url=CATALOG_URL,
                partial=True,
            )
        ]

    def fetch(self, ctx: FetchContext) -> Iterator[RawItem]:
        robots = RobotsPolicy(user_agent=USER_AGENT)
        seen: set[tuple[TitleKind, str]] = set()
        found = 0
        for url in LOBBY_URLS:
            ctx.apply_rate_limit(HOST)
            robots.require_allowed(url)
            html = ctx.http.get(url).text
            for item in parse_lobby(html, url):
                found += 1
                key = (item.kind, item.source_ref or item.name)
                if key in seen:
                    continue
                seen.add(key)
                ctx.record_success()
                yield item
        if not found:
            raise YesCatalogError("no title links on the yes lobby pages")
        logger.info("yes lobbies list %d titles", len(seen))


def parse_lobby(html: str, page_url: str) -> list[RawItem]:
    """Every title tile on one lobby page, deduplicated by slug."""
    parser = _LobbyParser(page_url)
    parser.feed(html)
    parser.close()

    best: dict[tuple[TitleKind, str], _Tile] = {}
    for tile in parser.tiles:
        key = (tile.kind, tile.slug)
        current = best.get(key)
        if current is None or (not current.texts and tile.texts):
            best[key] = tile
        elif current.poster is None and tile.poster:
            current.poster = tile.poster

    items: list[RawItem] = []
    for (kind, slug), tile in best.items():
        item = _to_item(kind, slug, tile)
        if item is not None:
            items.append(item)
    return items


def _to_item(kind: TitleKind, slug: str, tile: _Tile) -> RawItem | None:
    text = tile.texts[0] if tile.texts else ""
    year_match = _TRAILING_YEAR.search(text)
    year = int(year_match.group(1)) if year_match else None
    section = "movies" if kind is TitleKind.MOVIE else "series"
    name = _TRAILING_NOTE.sub("", text).strip()
    if not name:
        return None
    return RawItem(
        source_key=SOURCE_KEY,
        kind=kind,
        name=name,
        offer_type=OfferType.STREAM,
        year=year,
        deep_link_url=f"{BASE_URL}/content/tv/{section}/{slug}/",
        source_ref=slug,
        poster_url=tile.poster,
        extra={"slug": slug, "listed_as": text},
    )
