"""Cellcom TV - the showcase on the operator's "world of content" page.

Cellcom's site is a headless Optimizely (EPiServer) CMS, and its Content
Delivery API is public JSON: no login, no cookies, no browser. The HTML pages
render nothing without JavaScript, which is why this reads the API the page is
drawn from instead.

**This is a showcase, not the library.** The page lists a dozen or so
hand-picked titles (a Hebrew name and a picture each) - no year, no English
title, no movie/series flag and no page per title. That is why the source is
:attr:`~eifo_core.items.SourceInfo.partial`: a title missing from it proves
nothing. TMDB enrichment settles year and kind later; the kind here is only a
guess from the picture's file name ("סרטים_..." is "movies_...").

How it is read:

* ``content/46495/children`` finds the page by its route segment, so an editor
  moving it does not break the plugin; id 1158 is the fallback.
* The page's ``mainContentArea`` references blocks. Only
  ``InternalTvContentsItemsBlock`` carries titles (channel lists and the like are
  skipped), and each of its ``items`` is an ``InternalTvContentBlock``.
* Items are accepted expanded or as bare references, the latter fetched one by one.

The deep link is the content page itself, which is allowed: the site's
``Disallow: /content`` is anchored at the root and does not cover
``/production/Private/cellcomtv/content/``. The API host serves no robots.txt,
which RFC 9309 reads as no restrictions.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping
from typing import Any

from eifo_core.enums import OfferType, SourceKind, TitleKind
from eifo_fetcher.http import USER_AGENT
from eifo_fetcher.robots import RobotsPolicy
from eifo_fetcher.sources.base import FetchContext, RawItem, SourceInfo, SourcePlugin

logger = logging.getLogger("eifo.fetch.source.cellcom_tv")

SOURCE_KEY = "cellcom_tv"
HOST = "contentepi.cellcom.co.il"
API_URL = f"https://{HOST}/api/episerver/v2.0/content"
#: ``/production/Private/cellcomtv/``, the parent of the showcase page.
PARENT_ID = 46495
#: The showcase page's id on 2026-11-22, used when the lookup finds nothing.
FALLBACK_PAGE_ID = 1158
PAGE_ROUTE_SEGMENT = "content"
ITEMS_BLOCK_TYPE = "InternalTvContentsItemsBlock"
CATALOG_URL = "https://cellcom.co.il/production/Private/cellcomtv/content/"

_SERIES_HINTS = ("סדרות", "סדרה")


class CellcomCatalogError(RuntimeError):
    """The content API did not answer in the shape this plugin expects."""


class CellcomTvPlugin(SourcePlugin):
    """Yields the titles on Cellcom TV's content page."""

    def sources(self) -> list[SourceInfo]:
        return [
            SourceInfo(
                key=SOURCE_KEY,
                name="Cellcom TV",
                kind=SourceKind.SUBSCRIPTION,
                website_url=CATALOG_URL,
                partial=True,
            )
        ]

    def fetch(self, ctx: FetchContext) -> Iterator[RawItem]:
        robots = RobotsPolicy(user_agent=USER_AGENT)
        ctx.apply_rate_limit(HOST)
        robots.require_allowed(f"{API_URL}/{FALLBACK_PAGE_ID}")

        page_id = self._page_id(ctx)
        page = _get(ctx, f"{API_URL}/{page_id}")
        block_ids = parse_block_ids(page)

        seen: set[str] = set()
        for block_id in block_ids:
            block = _get(ctx, f"{API_URL}/{block_id}", params={"expand": "*"})
            if not _is_items_block(block):
                continue
            for entry in _entries(block):
                try:
                    content = self._content(ctx, entry)
                    item = _to_item(content)
                except Exception as exc:
                    ctx.record_error(f"unreadable Cellcom item in block {block_id}", exc=exc)
                    continue
                if item is None or item.name in seen:
                    continue
                seen.add(item.name)
                ctx.record_success()
                yield item
        logger.info("Cellcom TV showcase lists %d titles", len(seen))

    def _page_id(self, ctx: FetchContext) -> int:
        """The showcase page's id, found by route segment rather than assumed."""
        try:
            children = _get(ctx, f"{API_URL}/{PARENT_ID}/children")
            for child in children if isinstance(children, list) else []:
                if isinstance(child, Mapping) and child.get("routeSegment") == PAGE_ROUTE_SEGMENT:
                    found = _int_or_none((child.get("contentLink") or {}).get("id"))
                    if found is not None:
                        return found
        except Exception:
            logger.info("could not list the Cellcom TV page's siblings", exc_info=True)
        return FALLBACK_PAGE_ID

    def _content(self, ctx: FetchContext, entry: Mapping[str, Any]) -> Mapping[str, Any]:
        """One item's content, whether the block expanded it or only referenced it."""
        expanded = entry.get("expandedValue")
        if isinstance(expanded, Mapping):
            return expanded
        if "title" in entry:
            return entry
        item_id = _int_or_none((entry.get("contentLink") or {}).get("id"))
        if item_id is None:
            raise CellcomCatalogError("an item with neither content nor an id")
        fetched = _get(ctx, f"{API_URL}/{item_id}")
        if not isinstance(fetched, Mapping):
            raise CellcomCatalogError(f"item {item_id} is not an object")
        return fetched


def _get(ctx: FetchContext, url: str, *, params: Mapping[str, str] | None = None) -> Any:
    ctx.apply_rate_limit(HOST)
    return ctx.http.get_json(url, params=params)


def parse_block_ids(page: Any) -> list[int]:
    """The ids of the blocks in the page's main content area, in order.

    Raises:
        CellcomCatalogError: if the page has no ``mainContentArea`` list.
    """
    area = page.get("mainContentArea") if isinstance(page, Mapping) else None
    if not isinstance(area, list):
        raise CellcomCatalogError("no 'mainContentArea' on the Cellcom page; the API changed")
    ids = [
        _int_or_none((ref.get("contentLink") or {}).get("id"))
        for ref in area
        if isinstance(ref, Mapping)
    ]
    return [block_id for block_id in ids if block_id is not None]


def _is_items_block(block: Any) -> bool:
    types = block.get("contentType") if isinstance(block, Mapping) else None
    return isinstance(types, list) and ITEMS_BLOCK_TYPE in types


def _entries(block: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    items = block.get("items")
    return (
        [entry for entry in items if isinstance(entry, Mapping)] if isinstance(items, list) else []
    )


def _to_item(content: Mapping[str, Any]) -> RawItem | None:
    """Convert one ``InternalTvContentBlock``, or None if it names nothing."""
    title = _text(content.get("title")) or _text(content.get("name"))
    if not title:
        return None
    image = content.get("image")
    value = image.get("value") if isinstance(image, Mapping) else None
    path = _text(value.get("url")) if isinstance(value, Mapping) else None
    expanded = image.get("expandedValue") if isinstance(image, Mapping) else None
    file_name = _text(expanded.get("name")) if isinstance(expanded, Mapping) else None
    kind = (
        TitleKind.SERIES if file_name and file_name.startswith(_SERIES_HINTS) else TitleKind.MOVIE
    )
    return RawItem(
        source_key=SOURCE_KEY,
        kind=kind,
        name=title,
        offer_type=OfferType.STREAM,
        deep_link_url=CATALOG_URL,
        source_ref=str((content.get("contentLink") or {}).get("id") or "") or None,
        poster_url=_absolute(path),
        # The kind is a guess from a file name; TMDB gets the last word.
        extra={
            "kind_guessed": True,
            "image_name": file_name,
            "summary": _text(content.get("summary")),
        },
    )


def _absolute(path: str | None) -> str | None:
    if not path:
        return None
    if path.startswith(("http://", "https://")):
        return path
    return f"https://{HOST}{path if path.startswith('/') else '/' + path}"


def _text(value: Any) -> str | None:
    return " ".join(value.split()) or None if isinstance(value, str) else None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None
