"""Mako VOD (Keshet 12) — free Israeli broadcaster catalog.

Mako's VOD section is a Next.js application, so the catalog is structured data
rather than markup to be scraped: the server embeds the page's React props in a
``__NEXT_DATA__`` script tag. This plugin reads the catalog page and parses that
object — no DOM traversal, no CSS selectors to rot.

Two things this plugin must get right:

* **It asks as itself.** Mako also exposes the same object at
  ``/_next/data/<buildId>/…json``, but that route answers only to
  browser-looking clients: with our identifying User-Agent it returns a
  bot-check interstitial instead of JSON. The rendered page has no such gate, so
  we read it and skip the endpoint rather than impersonate a browser.
* **robots.txt.** Mako disallows ``/AjaxPage`` and any ``/mako-vod-*`` URL
  carrying an ``sCh`` parameter; ``/mako-vod-index`` is permitted (the
  ``/vod-index/`` rule is a different path). This plugin touches only the
  permitted page.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator
from typing import Any

from tvil_core.enums import OfferType, SourceKind, TitleKind
from tvil_fetcher.sources.base import FetchContext, RawItem, SourceInfo, SourcePlugin

logger = logging.getLogger("tvil.fetch.source.mako")

SOURCE_KEY = "mako"
HOST = "www.mako.co.il"
BASE_URL = f"https://{HOST}"
CATALOG_PATH = "/mako-vod-index"

#: The build id lives in the Next.js bootstrap payload on any Mako VOD page.
_NEXT_DATA = re.compile(
    r'<script[^>]+id="__NEXT_DATA__"[^>]*>(?P<payload>.*?)</script>',
    re.DOTALL,
)


class MakoCatalogError(RuntimeError):
    """Mako's catalog could not be read in the shape this plugin expects."""


class MakoPlugin(SourcePlugin):
    """Yields Keshet 12's free VOD catalog."""

    def sources(self) -> list[SourceInfo]:
        return [
            SourceInfo(
                key=SOURCE_KEY,
                name="Mako VOD (Keshet 12)",
                kind=SourceKind.FREE,
                website_url=f"{BASE_URL}{CATALOG_PATH}",
            )
        ]

    def fetch(self, ctx: FetchContext) -> Iterator[RawItem]:
        ctx.apply_rate_limit(HOST)

        html = ctx.http.get(f"{BASE_URL}{CATALOG_PATH}").text
        yield from self._items(parse_next_data(html), ctx)

    def _items(self, payload: Any, ctx: FetchContext) -> Iterator[RawItem]:
        """Turn the catalog payload into items, one bad entry at a time."""
        programs = _programs(payload)
        if not programs:
            raise MakoCatalogError("catalog payload contained no programs")

        for entry in programs:
            item = _to_item(entry)
            if item is None:
                ctx.record_error(f"unparsable catalog entry: {_describe(entry)}")
                continue
            ctx.record_success()
            yield item


def parse_next_data(html: str) -> Any:
    """Pull the page's React props out of its ``__NEXT_DATA__`` script tag.

    Raises:
        MakoCatalogError: if the page is not the catalog page we expect — most
            likely a bot-check interstitial rather than the rendered catalog.
    """
    match = _NEXT_DATA.search(html)
    if match is None:
        raise MakoCatalogError(
            "no __NEXT_DATA__ script on the Mako VOD index page; "
            "the response was probably an interstitial rather than the catalog"
        )
    try:
        return json.loads(match.group("payload"))
    except json.JSONDecodeError as exc:
        raise MakoCatalogError("__NEXT_DATA__ did not contain valid JSON") from exc


def _programs(payload: Any) -> list[Any]:
    """Extract ``pageProps.programs.items`` from either payload shape.

    The ``_next/data`` endpoint and the inline ``__NEXT_DATA__`` script carry the
    same object at different depths, so both are accepted.
    """
    if not isinstance(payload, dict):
        raise MakoCatalogError(f"expected a JSON object, got {type(payload).__name__}")

    page_props = payload.get("pageProps")
    if page_props is None:
        props = payload.get("props")
        page_props = props.get("pageProps") if isinstance(props, dict) else None
    if not isinstance(page_props, dict):
        raise MakoCatalogError("payload has no pageProps")

    programs = page_props.get("programs")
    if not isinstance(programs, dict):
        raise MakoCatalogError("pageProps has no programs object")

    items = programs.get("items")
    if not isinstance(items, list):
        raise MakoCatalogError("programs has no items list")
    return items


def _to_item(entry: Any) -> RawItem | None:
    """Convert one catalog entry, or None if it is not usable.

    Mako's catalog carries no year and no English title — only a Hebrew name, a
    page URL and artwork. Everything else is filled in later by enrichment.
    """
    if not isinstance(entry, dict):
        return None

    title = str(entry.get("title") or "").strip()
    page_url = str(entry.get("pageUrl") or "").strip()
    if not title or not page_url:
        return None

    return RawItem(
        source_key=SOURCE_KEY,
        # Mako's VOD index is programmes and series; it lists no films.
        kind=TitleKind.SERIES,
        name=title,
        offer_type=OfferType.FREE,
        deep_link_url=_absolute(page_url),
        poster_url=str(entry.get("pic")).strip() or None if entry.get("pic") else None,
        extra={"vcm_id": entry.get("itemVcmId")},
    )


def _absolute(page_url: str) -> str:
    """Catalog URLs are site-relative."""
    if page_url.startswith(("http://", "https://")):
        return page_url
    return f"{BASE_URL}{page_url if page_url.startswith('/') else '/' + page_url}"


def _describe(entry: Any) -> str:
    """A short, safe description of a bad entry for the error log."""
    if isinstance(entry, dict):
        keys = ", ".join(sorted(map(str, entry))[:6])
        return f"dict with keys [{keys}]"
    return type(entry).__name__
