"""Reshet 13 (13tv.co.il) - free Israeli broadcaster VOD catalog.

Why a browser, and why this one needs no heroics:

* 13tv.co.il serves a 403 to any non-browser client (verified August 2026; see
  docs.internal.local/03-sources.md), so - like Kan - the catalog is read
  through a headless Chromium. Unlike Kan, the site places no per-session
  limit on page views: a sync is two ordinary page views plus one
  ``robots.txt`` fetch, all in one browser session.
* The site is a Next.js app, so the catalog is structured data rather than
  markup to be scraped: the server embeds the page's React props in a
  ``__NEXT_DATA__`` script tag (the same trick Mako uses over plain HTTP).
  This plugin parses that object - no DOM traversal, no styled-component CSS
  selectors to rot.

What the catalog gives us: two public screens carry the whole free VOD
catalog - ``/allshows/screen/1170108/`` ("כל התוכניות", every programme) and
``/allshows/screen/1170109/`` ("חדשות 13", the news programmes). Each screen's
``props.pageProps.leafs[].child[]`` mixes real programmes with dated news
clips and full-episode entries; only entries whose ``externalId`` is
``Show-<n>`` are programmes, and those are all the plugin yields. A programme
is a Hebrew name, a description and artwork (several crops; the ``2x3``
portrait is the poster). There is **no year and no English title** -
enrichment fills both later. Everything listed is a series/programme; these
screens carry no films. The news programmes also sit on the all-shows screen,
so a programme seen on both is yielded once (deduped by ``externalId``).

robots.txt is honoured the way Kan's plugin honours it: it is fetched through
the browser after the page views (one small in-page fetch), and a ``Disallow``
fails the sync before anything is stored. Today it explicitly
``Allow: /allshows/``.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Iterator
from typing import Any

from eifo_core.enums import OfferType, SourceKind, TitleKind
from eifo_fetcher.browser import BrowserSession
from eifo_fetcher.robots import RobotsPolicy
from eifo_fetcher.sources.base import FetchContext, RawItem, SourceInfo, SourcePlugin

logger = logging.getLogger("eifo.fetch.source.reshet13")

SOURCE_KEY = "reshet13"
HOST = "13tv.co.il"
BASE_URL = f"https://{HOST}"

#: The two public screens carrying the whole free VOD catalog (see the module
#: docstring): every programme, then the news programmes.
SCREEN_PATHS = ("/allshows/screen/1170108/", "/allshows/screen/1170109/")
SCREEN_URLS = tuple(f"{BASE_URL}{path}" for path in SCREEN_PATHS)
CATALOG_URL = SCREEN_URLS[0]

#: The build id lives in the Next.js bootstrap payload on any 13tv page.
_NEXT_DATA = re.compile(
    r'<script[^>]+id="__NEXT_DATA__"[^>]*>(?P<payload>.*?)</script>',
    re.DOTALL,
)

#: ``externalId`` prefixes a real programme; anything else on these screens is
#: a dated news clip or a full-episode entry, not a catalog title.
_SHOW_PREFIX = "Show-"

#: Artwork crops in order of preference for a poster.
_POSTER_RATIOS = ("2x3", "Portrait_9_16", "16x9")


class Reshet13CatalogError(RuntimeError):
    """Reshet 13's catalog could not be read in the shape this plugin expects."""


#: Lets tests drive ``fetch`` without a browser: build the session to use.
BrowserFactory = Callable[[FetchContext], BrowserSession]


class Reshet13Plugin(SourcePlugin):
    """Yields Reshet 13's free VOD catalog from its two public screens."""

    def __init__(self, browser_factory: BrowserFactory | None = None) -> None:
        self._browser_factory = browser_factory or (
            lambda ctx: BrowserSession(rate_limiter=ctx.http.rate_limiter)
        )

    def sources(self) -> list[SourceInfo]:
        return [
            SourceInfo(
                key=SOURCE_KEY,
                name="Reshet 13",
                kind=SourceKind.FREE,
                website_url=CATALOG_URL,
            )
        ]

    def fetch(self, ctx: FetchContext) -> Iterator[RawItem]:
        ctx.apply_rate_limit(HOST)

        pages: list[str] = []
        with self._browser_factory(ctx) as browser:
            for url in SCREEN_URLS:
                # div[data-id] is the programme rail item: waiting for it both
                # lets the screen render and proves it is content, not an
                # interstitial (the catalog itself is read from __NEXT_DATA__).
                pages.append(browser.get_html(url, ready_selector="div[data-id]"))
            self._check_robots(browser)

        entries = parse_catalog(pages)
        if not entries:
            raise Reshet13CatalogError("neither screen listed a single programme")

        for entry in entries:
            item = _to_item(entry)
            if item is None:
                ctx.record_error(f"unparsable catalog entry: {_describe(entry)}")
                continue
            ctx.record_success()
            yield item

    def _check_robots(self, browser: BrowserSession) -> None:
        """Enforce robots.txt, fetched through the browser after the page views.

        Today 13tv's robots.txt explicitly allows ``/allshows/``; the check
        runs after the loads so a future ``Disallow`` fails the sync before
        anything is stored rather than costing a third page view up front.
        """

        def fetch(url: str) -> str:
            status, text = browser.fetch_text(url)
            if status != 200:
                raise OSError(f"robots.txt answered HTTP {status}")
            return text

        policy = RobotsPolicy(user_agent=browser.user_agent, fetch=fetch)
        for url in SCREEN_URLS:
            policy.require_allowed(url)


def parse_catalog(pages: Iterator[str] | list[str]) -> list[dict[str, Any]]:
    """Merge the rendered screens into deduped programme entries.

    Only ``Show-*`` entries are programmes; dated news clips and full-episode
    entries sharing the screens are not catalog titles. A programme listed on
    both screens appears once.
    """
    by_external_id: dict[str, dict[str, Any]] = {}
    for html in pages:
        for entry in _show_entries(parse_next_data(html)):
            by_external_id.setdefault(str(entry["externalId"]), entry)
    return list(by_external_id.values())


def parse_next_data(html: str) -> Any:
    """Pull the page's React props out of its ``__NEXT_DATA__`` script tag.

    Raises:
        Reshet13CatalogError: if the page is not the screen we expect - most
            likely a bot-check interstitial rather than the rendered catalog.
    """
    match = _NEXT_DATA.search(html)
    if match is None:
        raise Reshet13CatalogError(
            "no __NEXT_DATA__ script on a 13tv screen page; "
            "the response was probably an interstitial rather than the catalog"
        )
    try:
        return json.loads(match.group("payload"))
    except json.JSONDecodeError as exc:
        raise Reshet13CatalogError("__NEXT_DATA__ did not contain valid JSON") from exc


def _show_entries(payload: Any) -> list[dict[str, Any]]:
    """Extract the ``Show-*`` entries from ``props.pageProps.leafs[].child[]``.

    Raises:
        Reshet13CatalogError: if the payload is not shaped like a screen page,
            or lists no programmes at all - a layout change, which must fail
            loudly rather than sync a partial catalog.
    """
    if not isinstance(payload, dict):
        raise Reshet13CatalogError(f"expected a JSON object, got {type(payload).__name__}")
    props = payload.get("props")
    page_props = props.get("pageProps") if isinstance(props, dict) else None
    if not isinstance(page_props, dict):
        raise Reshet13CatalogError("payload has no pageProps")
    leafs = page_props.get("leafs")
    if not isinstance(leafs, list):
        raise Reshet13CatalogError("pageProps has no leafs list")

    entries = [
        entry
        for leaf in leafs
        if isinstance(leaf, dict) and isinstance(leaf.get("child"), list)
        for entry in leaf["child"]
        if isinstance(entry, dict) and str(entry.get("externalId") or "").startswith(_SHOW_PREFIX)
    ]
    if not entries:
        raise Reshet13CatalogError(
            "a 13tv screen listed no programmes; the response was an "
            "interstitial or the layout changed"
        )
    return entries


def _to_item(entry: dict[str, Any]) -> RawItem | None:
    """Convert one programme entry, or None if it is not usable."""
    name = str(entry.get("name") or "").strip()
    series_id = str(entry.get("externalId") or "").removeprefix(_SHOW_PREFIX).strip()
    if not name or not series_id:
        return None

    return RawItem(
        source_key=SOURCE_KEY,
        # These screens list programmes only; no films.
        kind=TitleKind.SERIES,
        name=name,
        offer_type=OfferType.FREE,
        deep_link_url=f"{BASE_URL}/allshows/series/{series_id}/",
        poster_url=_poster_url(entry.get("images")),
        extra={
            "external_id": entry.get("externalId"),
            "description": str(entry.get("description") or "").strip() or None,
        },
    )


def _poster_url(images: Any) -> str | None:
    """The best poster crop among the entry's artwork, if any."""
    if not isinstance(images, list):
        return None
    urls = {
        str(image.get("imageTypeName")): str(image.get("url") or "").strip()
        for image in images
        if isinstance(image, dict) and image.get("url")
    }
    for ratio in _POSTER_RATIOS:
        if urls.get(ratio):
            return urls[ratio]
    return next(iter(urls.values()), None)


def _describe(entry: Any) -> str:
    """A short, safe description of a bad entry for the error log."""
    if isinstance(entry, dict):
        keys = ", ".join(sorted(map(str, entry))[:6])
        return f"dict with keys [{keys}]"
    return type(entry).__name__
