"""Sting TV (+STING, yes's budget streamer) - the titles on its public section pages.

``www.stingtv.co.il`` is a server-rendered WordPress site (robots.txt: ``Allow: /``).
Each section, ``/content-items/{movies,series,kids,docu,israeli}/``, is one page
holding every title as a tile::

    <li class="item" itemscope itemtype="http://schema.org/Thing">
      <img itemprop="image" class="lazyload" data-src=".../vp000381437.jpg" alt="...">
      <h2 itemprop="name">אישה יפה</h2>

Limits, stated plainly: the tiles have no link to a per-title page, so the deep link
is the section page; the Hebrew name is all there is (no year, no English title, and
sometimes a note in parentheses such as an actor's name); and the movie/series
distinction comes from the section - ``movies`` is movies, the rest are series. A
series tile may name its season (``הבוזגלוס - עונה 4``); the season is dropped from the
name, which is what matches a title, and kept in ``extra``. The poster's file id
(``vp000381437``) identifies a title across sections, except for a season tile: each
season has its own poster, so the show's name is its reference. Nothing says
which yes product carries a title, so every offer is a plain subscription stream.
Hence :attr:`~eifo_core.items.SourceInfo.partial`.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from eifo_core.enums import OfferType, SourceKind, TitleKind
from eifo_fetcher.http import USER_AGENT
from eifo_fetcher.robots import RobotsPolicy
from eifo_fetcher.sources.base import FetchContext, RawItem, SourceInfo, SourcePlugin

logger = logging.getLogger("eifo.fetch.source.sting_tv")

SOURCE_KEY = "sting_tv"
HOST = "www.stingtv.co.il"
BASE_URL = f"https://{HOST}"
CATALOG_URL = f"{BASE_URL}/content-items/"
#: (section path, the kind of every title listed there)
SECTIONS: tuple[tuple[str, TitleKind], ...] = (
    ("movies", TitleKind.MOVIE),
    ("series", TitleKind.SERIES),
    ("kids", TitleKind.SERIES),
    ("docu", TitleKind.SERIES),
    ("israeli", TitleKind.SERIES),
)
SECTION_URLS = tuple(f"{CATALOG_URL}{section}/" for section, _ in SECTIONS)

_TRAILING_YEAR = re.compile(r"\s*\((\d{4})\)\s*$")
#: "(קלינט איסטווד)" - a note after the title, not part of it.
_TRAILING_NOTE = re.compile(r"\s*\([^()]*\)\s*$")
_POSTER_ID = re.compile(r"([^/?#]+?)\.[A-Za-z0-9]+(?:[?#].*)?$")
#: "הבוזגלוס - עונה 4", and once "... הפסיכולוגית עונה" with the number missing.
_TRAILING_SEASON = re.compile(r"\s+(?:[-\u2013\u2014]\s*)?עונה(?:\s+(\d{1,2}))?\s*$")


class StingCatalogError(RuntimeError):
    """The section pages held no title tiles; the site changed or was not a section."""


class _Tile:
    __slots__ = ("alt", "name", "poster")

    def __init__(self) -> None:
        self.name: list[str] = []
        self.alt: str = ""
        self.poster: str | None = None


class _SectionParser(HTMLParser):
    """Collects every ``li.item`` tile: its name, and the picture marked as its image."""

    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self._base_url = base_url
        self._tile: _Tile | None = None
        self._in_name = False
        self.tiles: list[_Tile] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "li" and "item" in (values.get("class") or "").split():
            self._tile = _Tile()
            self.tiles.append(self._tile)
        elif self._tile is None:
            return
        elif tag == "img" and values.get("itemprop") == "image":
            self._tile.alt = " ".join((values.get("alt") or "").split())
            src = values.get("data-src") or values.get("src")
            if src and self._tile.poster is None:
                self._tile.poster = _own_poster(urljoin(self._base_url, src))
        elif tag == "h2" and values.get("itemprop") == "name":
            self._in_name = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "h2":
            self._in_name = False
        elif tag == "li":
            self._tile = None

    def handle_data(self, data: str) -> None:
        if self._in_name and self._tile is not None and data.strip():
            self._tile.name.append(" ".join(data.split()))


class StingTvPlugin(SourcePlugin):
    """Yields the titles on Sting TV's section pages."""

    def sources(self) -> list[SourceInfo]:
        return [
            SourceInfo(
                key=SOURCE_KEY,
                name="Sting TV",
                kind=SourceKind.SUBSCRIPTION,
                website_url=CATALOG_URL,
                partial=True,
            )
        ]

    def fetch(self, ctx: FetchContext) -> Iterator[RawItem]:
        robots = RobotsPolicy(user_agent=USER_AGENT)
        seen: set[str] = set()
        found = 0
        for (_, kind), url in zip(SECTIONS, SECTION_URLS, strict=True):
            ctx.apply_rate_limit(HOST)
            robots.require_allowed(url)
            # Deliberately not caught: a section that still fails after the client's
            # retries must fail the sync. A sync that skipped it would end OK, and the
            # sweep would retire that section's offers after two such nights - one
            # section of five is too small a drop to trip the volume guard. A failed
            # sync keeps everything (CLAUDE.md, "Catalog safety").
            html = ctx.http.get(url).text
            for item in parse_section(html, url, kind):
                found += 1
                key = item.source_ref or item.name
                if key in seen:
                    continue
                seen.add(key)
                ctx.record_success()
                yield item
        if not found:
            raise StingCatalogError("no title tiles on the Sting TV section pages")
        logger.info("Sting TV sections list %d titles", len(seen))


def parse_section(html: str, page_url: str, kind: TitleKind) -> list[RawItem]:
    """Every title tile on one section page, deduplicated by poster id (or name)."""
    parser = _SectionParser(page_url)
    parser.feed(html)
    parser.close()

    items: dict[str, RawItem] = {}
    for tile in parser.tiles:
        item = _to_item(kind, page_url, tile)
        if item is not None:
            items.setdefault(item.source_ref or item.name, item)
    return list(items.values())


def _own_poster(url: str) -> str | None:
    """The poster URL when it is on Sting's own site, as https; None for any other host.

    The page is remote input: a changed or compromised page must not be able to point the
    image downloader at an arbitrary host.
    """
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or not (
        host == "stingtv.co.il" or host.endswith(".stingtv.co.il")
    ):
        return None
    return parts._replace(scheme="https").geturl()


def _to_item(kind: TitleKind, page_url: str, tile: _Tile) -> RawItem | None:
    text = " ".join(tile.name) or tile.alt
    year_match = _TRAILING_YEAR.search(text)
    year = int(year_match.group(1)) if year_match else None
    name = _TRAILING_NOTE.sub("", text).strip()
    season_match = _TRAILING_SEASON.search(name) if kind is not TitleKind.MOVIE else None
    if season_match:
        name = name[: season_match.start()].strip()
    if not name:
        return None
    poster_match = _POSTER_ID.search(tile.poster) if tile.poster else None
    poster_id = poster_match.group(1) if poster_match else None
    extra: dict[str, object] = {"listed_as": text}
    if season_match:
        poster_id = None  # every season has its own poster; the show is one title
        if season_match.group(1):
            extra["season"] = int(season_match.group(1))
    return RawItem(
        source_key=SOURCE_KEY,
        kind=kind,
        name=name,
        offer_type=OfferType.STREAM,
        year=year,
        deep_link_url=page_url,
        source_ref=poster_id or name,
        poster_url=tile.poster,
        extra=extra,
    )
