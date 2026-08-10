"""FreeTV - read the public RedGalaxy catalog API.

FreeTV is a Keshet-owned Israeli OTT service. JustWatch does not track it for
region IL, so it cannot come from the ``tmdb-providers`` harvester - but unlike
the other Israeli operators it needs no plugin heroics either: its web app is a
RedGalaxy portal whose product API answers an honest client with no login, no
cookie and no device token (see docs.internal.local/13-israeli-vod-acquisition-
research.md for how this was established and re-verified).

One paginated endpoint carries the whole VOD catalogue - movies (``type: VOD``)
and series (``type: SERIAL``) together - and each item already includes a
canonical deep link and poster artwork, so a full sweep is a few dozen requests
and nothing is scraped.

What the API does and does not give us:

* **Does:** ``title`` (Hebrew), ``type``, ``year`` (usually), a ready-made
  absolute ``webUrl`` deep link, and poster artwork in several aspect ratios.
* **Does not:** a reliable English title - ``originalTitle`` is present but is
  simply a copy of the Hebrew ``title``, so it is ignored. Enrichment supplies
  the English name and fills a missing year from TMDB.

The ``platform=BROWSER`` query parameter is the portal's own convention rather
than a documented contract: omitting it is an HTTP 400, and a portal upgrade
could change it. The plugin therefore fails loudly on an unrecognised payload
rather than silently syncing an empty catalogue.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

from eifo_core.enums import OfferType, SourceKind, TitleKind
from eifo_fetcher.sources.base import FetchContext, RawItem, SourceInfo, SourcePlugin

logger = logging.getLogger("eifo.fetch.source.freetv")

SOURCE_KEY = "freetv"
HOST = "web.freetv.tv"
BASE_URL = f"https://{HOST}"
CATALOG_PATH = "/api/products/vods"

#: The portal caps a page at this many items; proven live to return a full page.
PAGE_SIZE = 100
#: A backstop against a pathological ``totalCount`` - 5,725 titles today, so this
#: is far above any real catalogue while still bounding a runaway loop.
MAX_PAGES = 200

#: ``type`` values the API uses for the two things Eifo tracks.
_KIND_BY_TYPE = {"VOD": TitleKind.MOVIE, "SERIAL": TitleKind.SERIES}

#: Poster preference: the closest to Eifo's 2:3 card first, widescreen last.
#: Every item carries 16x9 and 3x4; 2x3 exists for a minority and is ideal.
_POSTER_RATIOS = ("2x3", "3x4", "16x9")


class FreetvCatalogError(RuntimeError):
    """FreeTV's catalog could not be read in the shape this plugin expects."""


class FreetvPlugin(SourcePlugin):
    """Yields FreeTV's VOD catalogue from its public product API."""

    def sources(self) -> list[SourceInfo]:
        return [
            SourceInfo(
                key=SOURCE_KEY,
                name="FreeTV",
                kind=SourceKind.SUBSCRIPTION,
                website_url="https://www.freetv.tv/",
            )
        ]

    def fetch(self, ctx: FetchContext) -> Iterator[RawItem]:
        max_pages = _max_pages(ctx)
        seen = 0
        total: int | None = None

        for _ in range(max_pages):
            ctx.apply_rate_limit(HOST)
            page = _page(ctx.http.get_json(f"{BASE_URL}{CATALOG_PATH}", params=_params(seen)))
            items, total = page

            if not items:
                break

            for entry in items:
                item = to_item(entry)
                if item is None:
                    ctx.record_error(f"unparsable catalog entry: {_describe(entry)}")
                    continue
                ctx.record_success()
                yield item

            seen += len(items)
            if seen >= total:
                break
        else:
            logger.warning(
                "freetv hit the %d-page cap at %d/%s items; catalog may be truncated",
                max_pages,
                seen,
                total,
            )


def _params(first_result: int) -> dict[str, str]:
    """Query for one page. ``platform`` is mandatory - omitting it is a 400."""
    return {
        "platform": "BROWSER",
        "firstResult": str(first_result),
        "maxResults": str(PAGE_SIZE),
    }


def _page(payload: Any) -> tuple[list[Any], int]:
    """Pull ``(items, totalCount)`` out of one response.

    Raises:
        FreetvCatalogError: if the payload is not the product-list shape - most
            likely a portal change or an error page served with a 200.
    """
    if not isinstance(payload, dict):
        raise FreetvCatalogError(f"expected a JSON object, got {type(payload).__name__}")

    items = payload.get("items")
    meta = payload.get("meta")
    if not isinstance(items, list) or not isinstance(meta, dict):
        raise FreetvCatalogError("response has no items/meta; the product API has changed")

    total = meta.get("totalCount")
    if not isinstance(total, int):
        raise FreetvCatalogError("response meta carries no totalCount")

    return items, total


def to_item(entry: Any) -> RawItem | None:
    """Convert one catalog entry, or None if it is not a usable title.

    An unknown ``type`` returns None rather than a guessed kind: filing a title
    under the wrong medium is worse than parking it for a human to look at.
    """
    if not isinstance(entry, dict):
        return None

    title = str(entry.get("title") or "").strip()
    web_url = str(entry.get("webUrl") or "").strip()
    kind = _KIND_BY_TYPE.get(str(entry.get("type") or ""))
    if not title or not web_url or kind is None:
        return None

    return RawItem(
        source_key=SOURCE_KEY,
        kind=kind,
        name=title,
        year=_year(entry.get("year")),
        offer_type=OfferType.STREAM,
        deep_link_url=web_url,
        poster_url=_poster(entry.get("images")),
        extra={"content_id": entry.get("id"), "public_uid": entry.get("publicUid")},
    )


def _year(value: Any) -> int | None:
    """A production year, when the catalog carries one - 6% of items do not."""
    return value if isinstance(value, int) else None


def _poster(images: Any) -> str | None:
    """The best available poster URL, made absolute.

    Artwork URLs are protocol-relative (``//host/...``); the image pipeline
    needs a scheme to fetch them.
    """
    if not isinstance(images, dict):
        return None

    for ratio in _POSTER_RATIOS:
        variants = images.get(ratio)
        if isinstance(variants, list) and variants:
            url = str((variants[0] or {}).get("url") or "").strip()
            if url:
                return f"https:{url}" if url.startswith("//") else url
    return None


def _max_pages(ctx: FetchContext) -> int:
    configured = ctx.config.max_pages
    return configured if configured is not None else MAX_PAGES


def _describe(entry: Any) -> str:
    """A short, safe description of a bad entry for the error log."""
    if isinstance(entry, dict):
        keys = ", ".join(sorted(map(str, entry))[:6])
        return f"dict with keys [{keys}]"
    return type(entry).__name__
