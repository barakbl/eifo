"""Tel Aviv Cinematheque VOD - Israeli arthouse films, rented per title.

The first source in Eifo that charges per title rather than per month, which is
why it is a :data:`SourceKind.RENT_BUY` source yielding :data:`OfferType.RENT`
offers carrying a price.

What makes it a cheap plugin:

* **No wall.** cinema.co.il serves everything to a plain HTTP client with our
  identifying User-Agent; its ``robots.txt`` disallows only ``/wp-admin``. No
  browser, unlike Kan and Reshet 13.
* **One request.** The whole current offering is server-rendered into
  ``/vod/`` as ``div.slid`` cards, each carrying the Hebrew title, country,
  year, runtime, poster and - crucially - the ticketing link that says the
  film is on offer right now.

Two things this plugin must get right:

* **Currently rentable, not ever published.** The site keeps a page per VOD
  title forever: 787 of them across its sitemaps against ~120 on ``/vod/``
  (measured 2026-08-18). A retired title's page still renders, but its
  purchase button falls back to the site homepage instead of a
  ``cintlv.pres.global`` order link. This plugin reads only ``/vod/`` and
  requires a real order link, so the catalog is what can be rented today.
* **The price is not on the page.** The checkout is a separate single-page
  app that loads its own prices, so nothing in the HTML quotes a figure. The
  Cinematheque charges one price for every VOD rental, recorded here as a
  constant and overridable from config when it moves (see
  :data:`DEFAULT_PRICE_MINOR`).

The cards carry no English title; the per-title pages do ("נינו | Nino"), at
the cost of one request each. Left out deliberately: ~120 extra requests per
sync is a poor trade against a matcher that already resolves Hebrew titles
carrying a year. If match quality disappoints, that is where to find them.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

from selectolax.parser import HTMLParser, Node

from eifo_core.enums import OfferType, SourceKind, TitleKind
from eifo_fetcher.http import USER_AGENT
from eifo_fetcher.robots import RobotsPolicy
from eifo_fetcher.sources.base import FetchContext, RawItem, SourceInfo, SourcePlugin

logger = logging.getLogger("eifo.fetch.source.cinematheque_vod")

SOURCE_KEY = "cinematheque_vod"
HOST = "www.cinema.co.il"
BASE_URL = f"https://{HOST}"
CATALOG_URL = f"{BASE_URL}/vod/"

#: One price for every rental, in the currency's minor unit: 19.90 ILS,
#: verified 2026-08-18. The pages do not publish it (see the module docstring),
#: so a change here is a manual edit - or `price` in the source's config, which
#: wins over this and needs no release.
DEFAULT_PRICE_MINOR = 1990
PRICE_CURRENCY = "ILS"

CARD_SELECTOR = "div.slid"
#: The ticketing host every live offer links to; a card without one is a
#: leftover rather than something that can be rented today.
ORDER_HOST = "cintlv.pres.global"

#: A VOD title's page lives at ``/event/<slug>-vod/``, sometimes with
#: WordPress's duplicate-slug tail ("-vod-2", "-vod-קבוע"). The slug decides
#: what is VOD, not the heading: the same page also lists cinema screenings,
#: and their cards are shaped identically.
_VOD_SLUG = re.compile(r"(?:^|-)vod(?:-|$)")
#: The card heading carries the VOD marker and sometimes a marketing tail after
#: it ("הנחלמים | VOD לצפייה ללא הגבלה"); the theme also truncates the whole
#: string, which is why "| V" and "|" both have to count as the marker.
_VOD_MARKER = re.compile(r"\s*\|\s*V(?:O(?:D)?)?\b.*$", re.IGNORECASE)
_TRAILING_YEAR = re.compile(r"\s+(?:19|20)\d{2}$")
#: The card's one line of metadata: "צרפת / 2025 / אורך:97". The country half
#: may itself carry slashes for a co-production ("ישראל/צרפת / 2025 / ..."), so
#: the year anchors the split rather than the first slash.
_META = re.compile(
    r"^(?P<country>.+?)\s*/\s*(?P<year>(?:19|20)\d{2})(?:\s*/\s*אורך:\s*(?P<runtime>\d+))?",
)


class CinemathequeCatalogError(RuntimeError):
    """The VOD page could not be read in the shape this plugin expects."""


@dataclass(frozen=True, slots=True)
class VodCard:
    """One film exactly as the VOD page presents it."""

    name: str
    #: Where it is rented - the ticketing link, which is also proof of an offer.
    order_url: str
    #: Its page on cinema.co.il, kept for debugging an unmatched item.
    event_url: str
    year: int | None = None
    country: str | None = None
    runtime_minutes: int | None = None
    poster_url: str | None = None


class CinemathequeVodPlugin(SourcePlugin):
    """Yields the Tel Aviv Cinematheque's rentable VOD catalog."""

    def sources(self) -> list[SourceInfo]:
        return [
            SourceInfo(
                key=SOURCE_KEY,
                name="Cinematheque VOD (Tel Aviv)",
                kind=SourceKind.RENT_BUY,
                website_url=CATALOG_URL,
            )
        ]

    def fetch(self, ctx: FetchContext) -> Iterator[RawItem]:
        ctx.apply_rate_limit(HOST)
        RobotsPolicy(user_agent=USER_AGENT).require_allowed(CATALOG_URL)

        cards, dropped = parse_catalog(ctx.http.get(CATALOG_URL).text)
        if dropped:
            # One counted error, not N: a redesign that breaks every card must
            # not trip the consecutive-error abort before the count is stored.
            ctx.record_error(f"{dropped} VOD cards were not usable; skipped")

        price_minor = _price_minor(ctx)
        for card in cards:
            ctx.record_success()
            yield _to_item(card, price_minor)


def _price_minor(ctx: FetchContext) -> int:
    """The rental price to record, config first, built-in constant second."""
    price = ctx.config.price
    if price is None:
        return DEFAULT_PRICE_MINOR
    return round(price * 100)


def parse_catalog(html: str) -> tuple[list[VodCard], int]:
    """Turn the VOD page into cards, deduped by film.

    A film appears in several of the page's rails (new releases, genres), so
    the same card is rendered more than once; it is yielded once.

    Returns:
        ``(cards, dropped)``: the films on offer, and how many *VOD* cards were
        unusable - the caller logs that count, because a malformed card must be
        loud rather than silently lost. Cards for cinema screenings, which the
        page renders identically, are not VOD offers and are not counted.

    Raises:
        CinemathequeCatalogError: if the page carries no cards at all, which
            means a layout change or a response that is not the VOD page.
    """
    nodes = HTMLParser(html).css(CARD_SELECTOR)
    if not nodes:
        raise CinemathequeCatalogError(
            f"no {CARD_SELECTOR!r} cards on the Cinematheque VOD page; "
            f"the layout changed or the response was not the catalog"
        )

    by_event_url: dict[str, VodCard] = {}
    dropped = 0
    for node in nodes:
        link = node.css_first("div.title h3 a[href]")
        if link is None:
            continue  # a banner slide rather than a catalog card

        event_url = (link.attributes.get("href") or "").strip()
        if not _is_vod_url(event_url):
            continue  # a cinema screening sharing the page, not a VOD title

        card = _to_card(node, link, event_url)
        if card is None:
            dropped += 1
            continue
        by_event_url.setdefault(card.event_url, card)
    return list(by_event_url.values()), dropped


def _to_card(node: Node, link: Node, event_url: str) -> VodCard | None:
    """Convert one VOD card, or None if it is not rentable right now."""
    order = node.css_first("a[href]:not([href*='cinema.co.il'])")
    order_url = (order.attributes.get("href") or "").strip() if order else ""
    name = _name(link.text(strip=True), event_url)
    if not name or ORDER_HOST not in order_url:
        return None

    year, country, runtime = _meta(node)
    return VodCard(
        name=name,
        order_url=order_url,
        event_url=event_url,
        year=year,
        country=country,
        runtime_minutes=runtime,
        poster_url=_poster(node),
    )


def _slug(event_url: str) -> str:
    """The page's slug, percent-decoded: ``נינו-vod``."""
    return unquote(urlsplit(event_url).path).strip("/").rsplit("/", 1)[-1]


def _is_vod_url(event_url: str) -> bool:
    """Whether this page is a VOD title rather than a cinema screening."""
    return bool(event_url) and bool(_VOD_SLUG.search(_slug(event_url)))


def _name(heading: str, event_url: str) -> str:
    """The film's title, from the heading or - when that is cut - the slug.

    The theme truncates a long heading mid-title, so "אלפרידה ילינק" arrives
    without the rest of its name. The slug holds every word but loses
    punctuation and sometimes gains a disambiguating year, so the two are
    compared and the fuller one wins.
    """
    displayed = _VOD_MARKER.sub("", heading).strip()

    words = [word for word in _slug(event_url).split("-") if not _VOD_SLUG.fullmatch(f"-{word}")]
    from_slug = _TRAILING_YEAR.sub("", " ".join(words).strip())

    return from_slug if len(from_slug) > len(displayed) else displayed


def _meta(node: Node) -> tuple[int | None, str | None, int | None]:
    """Read "country / year / runtime" off a card, tolerating a missing piece."""
    text = node.css_first("div.content p")
    if text is None:
        return None, None, None

    match = _META.match(" ".join(text.text(strip=True).split()))
    if match is None:
        return None, None, None

    runtime = match.group("runtime")
    return (
        int(match.group("year")),
        match.group("country").strip() or None,
        int(runtime) if runtime else None,
    )


def _poster(node: Node) -> str | None:
    """The card's artwork, which the theme lazy-loads via ``data-src``."""
    img = node.css_first("img")
    if img is None:
        return None
    url = (img.attributes.get("data-src") or img.attributes.get("src") or "").strip()
    return url if url.startswith(("http://", "https://")) else None


def _to_item(card: VodCard, price_minor: int) -> RawItem:
    """Convert one card into the item the pipeline stores."""
    return RawItem(
        source_key=SOURCE_KEY,
        kind=TitleKind.MOVIE,
        name=card.name,
        year=card.year,
        offer_type=OfferType.RENT,
        deep_link_url=card.order_url,
        poster_url=card.poster_url,
        price_minor=price_minor,
        price_currency=PRICE_CURRENCY,
        extra={
            "event_url": card.event_url,
            "country": card.country,
            "runtime_minutes": card.runtime_minutes,
        },
    )
