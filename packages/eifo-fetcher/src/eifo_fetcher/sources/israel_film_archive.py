"""Israeli Film Archive (Jerusalem Cinematheque) - jfc.org.il.

Israel's national film archive, streaming its own digitised collection: about
940 films, roughly half of them free to watch and the rest sold per title. A
:data:`SourceKind.RENT_BUY` source, then, but one whose offers are as often
:data:`OfferType.FREE` as :data:`OfferType.RENT`.

Where the catalog comes from:

* **The sitemap lists the films.** The archive has no page that lists its whole
  collection - the browse UI pages out at ten screens and its "load more" runs
  through ``admin-ajax.php``, which ``robots.txt`` disallows - but the WordPress
  sitemap at :data:`SITEMAP_URL` carries every film page, and robots welcomes
  it.
* **Each film page states its own terms.** A paid film renders a price block
  ("מחיר : ₪15"); a free one renders none and offers "לצפייה ישירה" instead.
  That distinction is the whole point of this plugin: an archive film that
  costs nothing must reach the catalog as free to watch, not as a rental.

The cost is what it is: **one request per film**, ~940 per sync, because the
price and the year live on the film's own page and nowhere cheaper. Everything
else here exists to keep that sweep honest and gentle - the per-source rate
limit applies, and a page that fails is counted rather than retried.

What each page gives: Hebrew title, year, runtime, director, synopsis, poster,
genre, and the price when there is one. ``og:title`` reads "title | director |
year", which is where the year comes from when the header does not carry it.

One quirk worth knowing: some cards and links on the site point at
``stage2.jfc.org.il``, the archive's staging host. Every film page names its
real address in ``<link rel="canonical">``, so that is what gets stored - a
deep link into someone's staging server is not an offer.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from dataclasses import dataclass
from urllib.parse import urljoin

from selectolax.parser import HTMLParser

from eifo_core.enums import CreditRole, OfferType, SourceKind, TitleKind
from eifo_fetcher.http import USER_AGENT
from eifo_fetcher.robots import RobotsPolicy
from eifo_fetcher.sources.base import FetchContext, RawItem, SourceInfo, SourcePlugin

logger = logging.getLogger("eifo.fetch.source.israel_film_archive")

SOURCE_KEY = "israel_film_archive"
HOST = "jfc.org.il"
BASE_URL = f"https://{HOST}"
SITEMAP_URL = f"{BASE_URL}/movie-sitemap.xml"
CATALOG_URL = f"{BASE_URL}/movie/"

#: The archive sells in shekels; the price block prints the symbol, not a code.
PRICE_CURRENCY = "ILS"

_SITEMAP_LOC = re.compile(r"<loc>\s*(?P<url>[^<\s]+)\s*</loc>", re.IGNORECASE)
#: "94 דקות, 1997" under the title: runtime first, then the year.
_RUNTIME_AND_YEAR = re.compile(r"(?:(?P<runtime>\d{1,3})\s*דקות)?\s*,?\s*(?P<year>(?:19|20)\d{2})")
#: ``og:title`` reads "title | director | year"; the year closes it.
_OG_YEAR = re.compile(r"\|\s*(?P<year>(?:19|20)\d{2})\s*$")
#: "מחיר : ₪15" - the only price a film page quotes for itself.
_PRICE = re.compile(r"(?P<amount>\d+(?:\.\d{1,2})?)")
#: The button a film offers when it can be watched without paying.
_WATCH_NOW = "לצפייה ישירה"


class ArchiveCatalogError(RuntimeError):
    """The archive could not be read in the shape this plugin expects."""


@dataclass(frozen=True, slots=True)
class ArchiveFilm:
    """One film exactly as its own page presents it."""

    name: str
    url: str
    #: None for a film the archive streams for free.
    price_minor: int | None = None
    year: int | None = None
    runtime_minutes: int | None = None
    director: str | None = None
    genre: str | None = None
    description: str | None = None
    poster_url: str | None = None


class IsraelFilmArchivePlugin(SourcePlugin):
    """Yields the Israeli Film Archive's streamable collection."""

    def sources(self) -> list[SourceInfo]:
        return [
            SourceInfo(
                key=SOURCE_KEY,
                name="Israel Film Archive (Jerusalem)",
                kind=SourceKind.RENT_BUY,
                website_url=CATALOG_URL,
            )
        ]

    def fetch(self, ctx: FetchContext) -> Iterator[RawItem]:
        ctx.apply_rate_limit(HOST)
        robots = RobotsPolicy(user_agent=USER_AGENT)
        robots.require_allowed(SITEMAP_URL)
        robots.require_allowed(CATALOG_URL)

        urls = parse_sitemap(ctx.http.get(SITEMAP_URL).text)
        logger.info("archive sitemap lists %d film pages", len(urls))

        unreadable = 0
        for url in urls:
            film = self._film(ctx, url)
            if film is None:
                unreadable += 1
                continue
            ctx.record_success()
            yield _to_item(film)

        if unreadable:
            # One counted error rather than N: a sweep this wide must not trip
            # the consecutive-error abort over a handful of broken pages, and
            # must not hide a day when every page broke either.
            ctx.record_error(f"{unreadable} film pages could not be read; skipped")

    def _film(self, ctx: FetchContext, url: str) -> ArchiveFilm | None:
        """One film page, or None if it could not be read or is not streamable."""
        try:
            return parse_film(ctx.http.get(url).text, url)
        except Exception:
            logger.info("unreadable film page %s", url, exc_info=True)
            return None


def parse_sitemap(xml: str) -> list[str]:
    """Film page URLs from the archive's sitemap, index entry excluded.

    Raises:
        ArchiveCatalogError: if the document lists no film pages at all, which
            means the sitemap moved or the response was not the sitemap.
    """
    urls = [
        url
        for url in _SITEMAP_LOC.findall(xml)
        if _is_film_url(url) and url.rstrip("/") != f"{BASE_URL}/movie"
    ]
    if not urls:
        raise ArchiveCatalogError(
            f"no film pages in {SITEMAP_URL}; the sitemap moved or the response was not it"
        )
    return urls


def _is_film_url(url: str) -> bool:
    return "/movie/" in _canonical_host(url)


def _canonical_host(url: str) -> str:
    """The production address for a URL that may name the staging host."""
    return url.replace("stage2.jfc.org.il", HOST).replace("http://", "https://")


def parse_film(html: str, url: str) -> ArchiveFilm | None:
    """Read one film page, or None if the archive does not stream this film.

    Raises:
        ArchiveCatalogError: if the page carries no title at all - a layout
            change worth failing on rather than storing a nameless title.
    """
    tree = HTMLParser(html)

    heading = tree.css_first("h1.content_title")
    og_title = _meta(tree, "og:title")
    name = heading.text(strip=True) if heading else (og_title or "").split("|")[0].strip()
    if not name:
        raise ArchiveCatalogError(f"no title on {url}; the page layout changed")

    price_minor = _price_minor(tree)
    if price_minor is None and not _streams_free(tree):
        return None  # catalogued, but not something a visitor can watch here

    year, runtime = _year_and_runtime(tree, og_title)
    canonical = tree.css_first('link[rel="canonical"]')
    genre = tree.css_first("div.subjects")

    return ArchiveFilm(
        name=name,
        url=_canonical_host(canonical.attributes.get("href") or url if canonical else url),
        price_minor=price_minor,
        year=year,
        runtime_minutes=runtime,
        director=_director(og_title),
        genre=genre.text(strip=True) if genre else None,
        description=_meta(tree, "og:description"),
        poster_url=_absolute(_meta(tree, "og:image"), url),
    )


def _absolute(candidate: str | None, page_url: str) -> str | None:
    """Resolve an artwork URL against the page it was found on.

    Most of the archive's pages give an absolute ``og:image`` and two do not -
    ``/media/MOVIES/ZARIM_BALAYLA_32066_MAIN.jpg``. Stored as written, the image
    pipeline handed it to httpx, which refuses a URL with no scheme, and the
    nightly artwork phase reported itself failed every night for two posters it
    was never going to fetch.
    """
    # Stripped here rather than trusting the caller: urljoin("...", "  ")
    # answers with the page itself, which would store the film's own URL as
    # its artwork and fail in a way that looks like a working poster.
    text = (candidate or "").strip()
    if not text:
        return None
    resolved = urljoin(page_url, text)
    return resolved if resolved.startswith(("http://", "https://")) else None


def _meta(tree: HTMLParser, prop: str) -> str | None:
    node = tree.css_first(f'meta[property="{prop}"]')
    content = (node.attributes.get("content") or "").strip() if node else ""
    return content or None


def _price_minor(tree: HTMLParser) -> int | None:
    """What the film costs in agorot, or None when the page quotes no price."""
    block = tree.css_first("div.movie-price")
    if block is None:
        return None

    match = _PRICE.search(block.text(strip=True))
    if match is None:
        # A price block that quotes no number is a layout change, and guessing
        # "free" from it would put a paid film in the catalog as free.
        raise ArchiveCatalogError("a film page has a price block with no amount in it")
    return round(float(match.group("amount")) * 100)


def _streams_free(tree: HTMLParser) -> bool:
    """Whether the page offers the film for immediate, unpaid viewing."""
    return any(_WATCH_NOW in button.text(strip=True) for button in tree.css("a.btn_to_watch"))


def _year_and_runtime(tree: HTMLParser, og_title: str | None) -> tuple[int | None, int | None]:
    """Year and runtime from the header line, falling back to ``og:title``."""
    header = tree.css_first("div.title-and-subtitle") or tree.css_first("div.warp_title")
    if header is not None:
        match = _RUNTIME_AND_YEAR.search(" ".join(header.text(separator=" ", strip=True).split()))
        if match is not None:
            runtime = match.group("runtime")
            return int(match.group("year")), int(runtime) if runtime else None

    og_match = _OG_YEAR.search(og_title or "")
    return (int(og_match.group("year")) if og_match else None), None


def _director(og_title: str | None) -> str | None:
    """The middle of "title | director | year", when the page carries one."""
    parts = [part.strip() for part in (og_title or "").split("|")]
    return parts[1] if len(parts) >= 3 and parts[1] else None


def _credits(film: ArchiveFilm) -> tuple[dict[str, str], ...]:
    """The director, named the way the page names them.

    Hebrew, because that is what the archive publishes; a Latin name for the
    same person can only come from a source that has one.
    """
    if not film.director:
        return ()
    return ({"role": CreditRole.DIRECTOR, "name_he": film.director},)


def _to_item(film: ArchiveFilm) -> RawItem:
    """Convert one film into the item the pipeline stores.

    A film the archive streams for nothing is a free offer; one it sells is a
    rental carrying its price.
    """
    free = film.price_minor is None
    return RawItem(
        source_key=SOURCE_KEY,
        kind=TitleKind.MOVIE,
        name=film.name,
        year=film.year,
        offer_type=OfferType.FREE if free else OfferType.RENT,
        deep_link_url=film.url,
        poster_url=film.poster_url,
        price_minor=film.price_minor,
        price_currency=None if free else PRICE_CURRENCY,
        # TMDB carries little of this collection, so the archive's own credit
        # is the only one most of these films will ever have.
        credits=_credits(film),
        extra={
            "director": film.director,
            "genre": film.genre,
            "runtime_minutes": film.runtime_minutes,
            "description": film.description,
        },
    )
