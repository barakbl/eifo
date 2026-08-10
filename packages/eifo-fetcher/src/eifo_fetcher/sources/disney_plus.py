"""Disney+ Israel - read from the sitemaps Disney publishes for the region.

Disney+ is available in Israel but JustWatch does not track it for region ``IL``
(verified August 2026 against TMDB's republished provider list: 22 TV and 33
movie providers, no Disney entry, while ``GB`` has one). So it cannot come from
the ``tmdb-providers`` harvester and needs a plugin of its own.

Disney's own ``robots.txt`` advertises a per-region sitemap index, and the
Israeli one is real: separate children for films and for series, regenerated
daily. That is a surface published *for* automated readers, which makes it the
politest catalog in the project - two requests per sync and nothing scraped.

What the sitemap does and does not carry:

* **Does:** the title's English slug, Disney's stable content id, and which of
  the two catalogs it came from (film or series).
* **Does not:** year, Hebrew title, artwork, synopsis. Title pages are a
  JavaScript shell whose ``<title>`` and OpenGraph tags are the site-wide
  Disney+ Israel strings, so fetching 3,500 of them would cost 3,500 requests
  and return nothing. Enrichment fills these in from TMDB instead.

The slug is therefore a *matching seed*, not a final name - and a lossy one:
non-ASCII is stripped, so "Shōgun" arrives as ``sh-gun``. Titles the matcher
cannot resolve confidently land in ``match_reviews`` by design.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ElementTree
from collections.abc import Iterator

from eifo_core.enums import OfferType, SourceKind, TitleKind
from eifo_fetcher.sources.base import FetchContext, RawItem, SourceInfo, SourcePlugin

logger = logging.getLogger("eifo.fetch.source.disney_plus")

SOURCE_KEY = "disney_plus_il"
HOST = "www.apps.disneyplus.com"
REGION = "il"
BASE_URL = f"https://{HOST}/{REGION}"
SITEMAP_INDEX = f"{BASE_URL}/new-sitemap.xml"

#: The two catalog sitemaps worth following, and what each one lists. The index
#: also offers EPISODE, MOVIE_WATCH_PAGES and FIRST_LEVEL_LANDING_PAGES: episodes
#: are below the granularity Eifo tracks, and the other two are duplicate routes
#: to titles already covered here.
CATALOG_KINDS = {"MOVIE": TitleKind.MOVIE, "SHOWS": TitleKind.SERIES}

#: Matches only the numbered catalog children. Anchored on the segment because
#: "MOVIE_WATCH_PAGES" would otherwise match a naive "MOVIE" test.
_CHILD = re.compile(r"/new-sitemap-(?P<kind>[A-Z_]+)-\d+\.xml$")

#: `/il/movies/<slug>/<content-id>` or `/il/shows/<slug>/<content-id>`.
_TITLE_URL = re.compile(r"/(?P<section>movies|shows)/(?P<slug>[^/]+)/(?P<content_id>\d+)/?$")

#: Sequel numerals worth restoring to uppercase. Deliberately not "every word of
#: roman-numeral letters": that rule turns "civil" into "CIVIL" and "mix" into
#: "MIX". These are the forms that actually appear in film titles.
_NUMERALS = frozenset({"ii", "iii", "iv", "vi", "vii", "viii", "ix", "xi", "xii", "xiii"})


class DisneyCatalogError(RuntimeError):
    """Disney's sitemaps could not be read in the shape this plugin expects."""


class DisneyPlusPlugin(SourcePlugin):
    """Yields the Disney+ Israel catalog from the region's published sitemaps."""

    def sources(self) -> list[SourceInfo]:
        return [
            SourceInfo(
                key=SOURCE_KEY,
                name="Disney+",
                kind=SourceKind.SUBSCRIPTION,
                website_url=f"https://www.disneyplus.com/{REGION}",
            )
        ]

    def fetch(self, ctx: FetchContext) -> Iterator[RawItem]:
        ctx.apply_rate_limit(HOST)
        children = self._catalog_children(ctx)

        for url, kind in children:
            ctx.apply_rate_limit(HOST)
            yield from self._items(ctx.http.get(url).text, kind, ctx)

    def _catalog_children(self, ctx: FetchContext) -> list[tuple[str, TitleKind]]:
        """The film and series sitemaps, discovered rather than hard-coded.

        Following the index means a second page appearing as the catalog grows
        (``-MOVIE-2.xml``) is picked up without a code change.
        """
        index = ctx.http.get(SITEMAP_INDEX).text

        children: list[tuple[str, TitleKind]] = []
        for location in locations(index):
            match = _CHILD.search(location)
            kind = CATALOG_KINDS.get(match.group("kind")) if match else None
            if kind is not None and (location, kind) not in children:
                children.append((location, kind))

        if not children:
            raise DisneyCatalogError(
                f"the {REGION} sitemap index listed no MOVIE or SHOWS children; "
                f"Disney has changed its layout"
            )
        return children

    def _items(self, xml: str, kind: TitleKind, ctx: FetchContext) -> Iterator[RawItem]:
        """Turn one catalog sitemap into items, one bad entry at a time."""
        entries = locations(xml)
        if not entries:
            raise DisneyCatalogError(f"the {kind.value} sitemap contained no URLs")

        for location in entries:
            item = to_item(location, kind)
            if item is None:
                ctx.record_error(f"unparsable catalog URL: {location}")
                continue
            ctx.record_success()
            yield item


def locations(xml: str) -> list[str]:
    """Every ``<loc>`` in a sitemap, whatever namespace it declares.

    Raises:
        DisneyCatalogError: if the response is not the XML we expect - most
            likely an error page served with a 200.
    """
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError as exc:
        raise DisneyCatalogError("sitemap response was not valid XML") from exc

    # Sitemaps are namespaced; match on the local name so a namespace change
    # does not silently yield an empty catalog.
    return [
        element.text.strip()
        for element in root.iter()
        if element.tag.rpartition("}")[2] == "loc" and element.text and element.text.strip()
    ]


def to_item(location: str, kind: TitleKind) -> RawItem | None:
    """Convert one catalog URL, or None if it is not a title URL."""
    match = _TITLE_URL.search(location)
    if match is None:
        return None

    name = title_from_slug(match.group("slug"))
    if not name:
        return None

    return RawItem(
        source_key=SOURCE_KEY,
        kind=kind,
        name=name,
        offer_type=OfferType.STREAM,
        deep_link_url=location,
        extra={"content_id": match.group("content_id"), "slug": match.group("slug")},
    )


def title_from_slug(slug: str) -> str:
    """A readable title from a URL slug, for the matcher to work with.

    Only ever a seed: enrichment replaces it with TMDB's canonical name once the
    title is matched, which is also what supplies the Hebrew name and the year.
    """
    words = [word for word in slug.replace("-", " ").split() if word]
    return " ".join(
        word.upper() if word in _NUMERALS else word[:1].upper() + word[1:] for word in words
    )
