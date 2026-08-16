"""Kan Box (Kan 11 / IPBC) - free Israeli public-broadcaster VOD catalog.

Why a browser, and why it is gentle:

* kan.org.il sits behind Cloudflare bot management that serves a 403 to any
  non-browser client on its TLS fingerprint - including ``robots.txt`` and the
  ``pipes2`` JSON API the Kodi addon uses (verified August 2026; see
  docs.internal.local/13-israeli-vod-acquisition-research.md). A headless
  Chromium is the only honest transport that gets in.
* The VOD catalog is server-rendered into **three public lobby pages** -
  ``/lobby/kan-box/`` (the main catalog), ``/lobby/series/`` (archive series)
  and ``/lobby/digital-lobby/`` (Kan Digital web series). No crawling, no
  pagination, no API enumeration: a sync is three page views plus one
  ``robots.txt`` fetch. Kan's WAF serves **one HTML document per cleared
  browser session** (a second navigation is blocked no matter the delay, and
  in-page ``fetch`` of HTML is blocked outright - both verified August 2026),
  so each lobby is read in a fresh browser context, spaced politely. A
  ``Disallow`` in robots.txt is therefore honoured before anything is stored,
  not before the pages are fetched - the pragmatic trade for staying under the
  rate rule.

What the catalog gives us: a Hebrew title (the card's ``img alt``), an absolute
deep link, poster artwork, a free-text description and the genre section the
card sits under. There is **no year and no English title** - enrichment fills
both later. Cards in the ``סרטים`` ("movies") section are films; everything
else is a series/programme. A program can appear under several sections and on
several lobbies; it is yielded once, and it counts as a film if *any* of its
sections is ``סרטים``.

One CMS quirk the parser must clean: ~20% of cards leak the image *filename*
into ``alt`` instead of a title - "Poster Image Small 239X360 בהסתורה",
"6 Poster Image Big 1200X1800 (3)", "שומרי הסף לוגו". The filename boilerplate
is stripped (``_clean_name``); a card whose cleaned name is empty is dropped
and counted as an error, never stored as a "title".

The featured "hero" promos at the top of the kan-box lobby are skipped: they
duplicate grid items in all but a couple of cases, and their markup carries no
poster.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

from selectolax.parser import HTMLParser, Node

from eifo_core.enums import OfferType, SourceKind, TitleKind
from eifo_fetcher.browser import BrowserSession
from eifo_fetcher.robots import RobotsPolicy
from eifo_fetcher.sources.base import FetchContext, RawItem, SourceInfo, SourcePlugin

logger = logging.getLogger("eifo.fetch.source.kan")

SOURCE_KEY = "kan"
HOST = "www.kan.org.il"
BASE_URL = f"https://{HOST}"

#: The three public lobbies carrying the whole free VOD catalog (see the module
#: docstring). Order matters only for politeness: kan-box first.
LOBBY_PATHS = ("/lobby/kan-box/", "/lobby/series/", "/lobby/digital-lobby/")
LOBBY_URLS = tuple(f"{BASE_URL}{path}" for path in LOBBY_PATHS)
CATALOG_URL = LOBBY_URLS[0]

CARD_SELECTOR = "div.card.vods-by-category"
#: The digital lobby lists its programmes in a different component.
PODCAST_ITEM_SELECTOR = "a.podcast-program__item"
SECTION_CONTAINER_CLASS = "block-list-item"
SECTION_HEADER_SELECTOR = "h2.title-elem"

_READY_SELECTOR_BY_PATH = {
    "/lobby/kan-box/": CARD_SELECTOR,
    "/lobby/series/": CARD_SELECTOR,
    "/lobby/digital-lobby/": PODCAST_ITEM_SELECTOR,
}

#: Kan's WAF serves one HTML document per cleared browser session, so each
#: lobby is read in a fresh context - this far apart, to stay gentle.
INTER_LOBBY_DELAY_SECONDS = 12.0

#: The one genre section whose cards are films rather than programmes.
MOVIES_SECTION = "סרטים"

#: How far a card may sit from its section header before we give up on
#: attributing it - the real layout nests three levels, this is headroom.
_SECTION_ANCESTOR_LIMIT = 8

# --- Filename junk the CMS leaks into ``alt`` (see the module docstring) -----
# Real examples: "Poster Image Small 239X360 בהסתורה", "5 Poster Image Small
# 239X360 (2)", "4_Poster image_small_239X360", "Poster 239 360 ...", "1800X1200
# דפיקה בדלת", "אולפן פתוח Poster Image Small 239X360", "פוסטר קטן רגע עם
# דודלי", "שומרי הסף לוגו" (test_sources_kan.py keeps them verbatim).
_DIM_X = r"\d{3,4}\s*[xX]\s*\d{3,4}"  # 239X360
_DIM_S = r"\d{3,4}\s+\d{3,4}"  # 1200 1800
_WORD = r"(?:poster|image|small|big|share)(?![a-zA-Z])"  # English boilerplate words
_BOILER_TOKEN = rf"(?:{_WORD}|{_DIM_X}|{_DIM_S}|@\d+[xX]|\(\d+\))"

#: Two or more boilerplate tokens open the alt. Requiring two protects a real
#: title that happens to start with one ("Big Brother" would survive).
_FILENAME_PREFIX = re.compile(
    rf"^(?:\d+\s*)?_?\s*(?:{_BOILER_TOKEN}[\s_]*){{2,}}",
    re.IGNORECASE,
)
#: A lone dimensions blob opens the alt: "1800X1200 דפיקה בדלת", "1200 1800 במאי".
_DIMENSIONS_PREFIX = re.compile(rf"^(?:{_DIM_X}|{_DIM_S})(?:\s*\(\d+\))?\s*")
#: Boilerplate closes the alt: "אולפן פתוח Poster Image Small 239X360".
_FILENAME_SUFFIX = re.compile(
    rf"[\s_]+(?:{_WORD}[\s_]*){{2,}}(?:{_DIM_X})?(?:\s*\(\d+\))?\s*$",
    re.IGNORECASE,
)
#: Hebrew artwork notes, either end: "פוסטר קטן רגע עם דודלי", "גרוסמן פוסטר
#: קטן", "שומרי הסף לוגו", "אורות וצללים רכיבים" - and the sandwich form
#: "פוסטר מה הרעש קטן", where the title sits between the two words.
_HE_ARTWORK_SANDWICH = re.compile(r"^פוסטר\s+(.+?)\s*קטן\s*$")
_HE_ARTWORK_PREFIX = re.compile(r"^פוסטר\s+קטן(?![א-ת])")
_ARTWORK_SUFFIX = re.compile(r"\s+(?:לוגו|פוסטר\s+קטן|פוסטר|רכיבים)\s*$")
#: A usable title has a Hebrew letter or a real Latin word; "-", "239X360"
#: and "@1X 1" do not.
_HAS_LETTER = re.compile(r"[א-ת]|[a-zA-Z]{2,}")
#: Whole-name junk observed on the page: alt texts that name the asset, not the show.
_KNOWN_JUNK_NAMES = frozenset({"תמונת אורך פרק"})


class KanCatalogError(RuntimeError):
    """Kan's catalog could not be read in the shape this plugin expects."""


@dataclass(frozen=True)
class CatalogCard:
    """One program card exactly as the lobby page presents it."""

    name: str
    url: str
    poster_url: str | None
    description: str | None
    genre: str | None
    #: Every genre section this program appeared under (deduped across them).
    sections: tuple[str, ...] = field(default=())


#: Lets tests drive ``fetch`` without a browser: build the session to use.
BrowserFactory = Callable[[FetchContext], BrowserSession]


class KanPlugin(SourcePlugin):
    """Yields Kan Box's free VOD catalog from its single lobby page."""

    def __init__(self, browser_factory: BrowserFactory | None = None) -> None:
        self._browser_factory = browser_factory or (
            lambda ctx: BrowserSession(
                rate_limiter=ctx.http.rate_limiter,
                reset_delay_seconds=INTER_LOBBY_DELAY_SECONDS,
            )
        )

    def sources(self) -> list[SourceInfo]:
        return [
            SourceInfo(
                key=SOURCE_KEY,
                name="Kan Box (Kan 11)",
                kind=SourceKind.FREE,
                website_url=CATALOG_URL,
            )
        ]

    def fetch(self, ctx: FetchContext) -> Iterator[RawItem]:
        ctx.apply_rate_limit(HOST)

        pages: list[str] = []
        with self._browser_factory(ctx) as browser:
            for index, (path, url) in enumerate(zip(LOBBY_PATHS, LOBBY_URLS, strict=True)):
                if index:
                    # One document per cleared session: the next lobby needs a
                    # fresh context (reset() also waits the polite delay).
                    browser.reset()
                pages.append(browser.get_html(url, ready_selector=_READY_SELECTOR_BY_PATH[path]))
            self._check_robots(browser)

        cards, dropped = parse_catalog(pages)
        if dropped:
            # One counted, loud error - not N, which would trip the
            # consecutive-error abort on a page with many title-less cards.
            ctx.record_error(f"{dropped} catalog cards had no usable title; skipped")
        yield from self._items(cards, ctx)

    def _check_robots(self, browser: BrowserSession) -> None:
        """Enforce robots.txt whenever Kan chooses to serve one.

        Today robots.txt answers 403 even to browsers; RFC 9309 (and
        :class:`RobotsPolicy`) treat an unservable robots.txt as "no
        restrictions", so this costs one small fetch and honours the file the
        day it exists. It runs after the single page load (see ``fetch``), so
        a ``Disallow`` fails the sync before anything is stored.
        """

        def fetch(url: str) -> str:
            status, text = browser.fetch_text(url)
            if status != 200:
                raise OSError(f"robots.txt answered HTTP {status}")
            return text

        policy = RobotsPolicy(user_agent=browser.user_agent, fetch=fetch)
        for url in LOBBY_URLS:
            policy.require_allowed(url)

    def _items(self, cards: list[CatalogCard], ctx: FetchContext) -> Iterator[RawItem]:
        if not cards:
            raise KanCatalogError("catalog page contained no program cards")

        for card in cards:
            item = to_item(card)
            if item is None:
                ctx.record_error(f"unparsable catalog card: {card.url or card.name!r}")
                continue
            ctx.record_success()
            yield item


def parse_catalog(pages: Iterator[str] | list[str]) -> tuple[list[CatalogCard], int]:
    """Merge the rendered lobby pages into deduped program cards.

    A program listed on several lobbies (or under several genre sections of
    one lobby) appears once, with every section it was seen under.

    Returns:
        ``(cards, dropped)``: the unique programs, and how many card elements
        were dropped for lacking a title or link (the caller logs the count -
        a malformed item must be loud, never silently stored or silently lost).
    """
    by_url: dict[str, CatalogCard] = {}
    sections_by_url: dict[str, list[str]] = {}
    dropped = 0
    for html in pages:
        cards, page_dropped = parse_lobby(html)
        dropped += page_dropped
        for card in cards:
            if card.url not in by_url:
                by_url[card.url] = card
                sections_by_url[card.url] = []
            for section in card.sections:
                if section not in sections_by_url[card.url]:
                    sections_by_url[card.url].append(section)

    items = [
        CatalogCard(
            name=card.name,
            url=url,
            poster_url=card.poster_url,
            description=card.description,
            genre=card.genre,
            sections=tuple(sections_by_url[url]),
        )
        for url, card in by_url.items()
    ]
    return items, dropped


def parse_lobby(html: str) -> tuple[list[CatalogCard], int]:
    """Turn one rendered lobby page into program cards, in whichever markup
    that lobby uses (genre cards on kan-box/series, program items on digital).

    Raises:
        KanCatalogError: if the page carries no catalog cards at all - a
            bot-check interstitial or a layout change, both of which must fail
            loudly rather than sync a partial catalog.
    """
    tree = HTMLParser(html)
    nodes = tree.css(CARD_SELECTOR)
    convert = _to_card
    if not nodes:
        nodes = tree.css(PODCAST_ITEM_SELECTOR)
        convert = _to_podcast_item
    if not nodes:
        raise KanCatalogError(
            f"no catalog cards on a Kan lobby page ({CARD_SELECTOR!r} or "
            f"{PODCAST_ITEM_SELECTOR!r}); the response was an interstitial "
            f"or the layout changed"
        )

    cards: list[CatalogCard] = []
    dropped = 0
    for node in nodes:
        card = convert(node)
        if card is None:
            dropped += 1
        else:
            cards.append(card)
    return cards, dropped


def _to_card(node: Node) -> CatalogCard | None:
    """Convert one card element, or None if it lacks a title or link."""
    link = node.css_first("a.card-link")
    img = node.css_first("img")
    if link is None or img is None:
        return None

    url = (link.attributes.get("href") or "").strip()
    name = _clean_name(img.attributes.get("alt") or "")
    if not url or not name:
        return None

    return CatalogCard(
        name=name,
        url=url,
        poster_url=_absolute(img.attributes.get("src") or ""),
        description=_text(node.css_first("div.details h3")),
        genre=_text(node.css_first("div.details p")),
        sections=(section,) if (section := _section_of(node)) else (),
    )


def _to_podcast_item(node: Node) -> CatalogCard | None:
    """Convert one ``a.podcast-program__item`` - the digital lobby's markup.

    Umbraco reuses the "podcast program" component for Kan Digital's web
    series: the link is the program page, the ``img alt`` the title, and the
    hidden overlay carries a description and the genre ("דיגיטל"). There are
    no genre sections here, so the genre doubles as the section.
    """
    url = (node.attributes.get("href") or "").strip()
    img = node.css_first("img")
    if img is None:
        return None

    name = _clean_name(img.attributes.get("alt") or "")
    if not url or not name:
        return None

    genre = _text(node.css_first(".digital-series-genres"))
    return CatalogCard(
        name=name,
        url=url,
        poster_url=_absolute(img.attributes.get("src") or ""),
        description=_text(node.css_first(".podcast-program__item-hidden-text")),
        genre=genre,
        sections=(genre,) if genre else (),
    )


def _section_of(node: Node) -> str | None:
    """The genre section header owning this card, when identifiable."""
    ancestor = node.parent
    for _ in range(_SECTION_ANCESTOR_LIMIT):
        if ancestor is None:
            return None
        if SECTION_CONTAINER_CLASS in (ancestor.attributes.get("class") or ""):
            header = ancestor.css_first(SECTION_HEADER_SELECTOR)
            return header.text(strip=True) if header else None
        ancestor = ancestor.parent
    return None


def to_item(card: CatalogCard) -> RawItem | None:
    """Convert one catalog card, or None if it is not usable."""
    if not card.name.strip() or not card.url.strip():
        return None

    return RawItem(
        source_key=SOURCE_KEY,
        kind=TitleKind.MOVIE if MOVIES_SECTION in card.sections else TitleKind.SERIES,
        name=card.name,
        offer_type=OfferType.FREE,
        deep_link_url=card.url,
        poster_url=card.poster_url,
        extra={
            "sections": list(card.sections),
            "genre": card.genre,
            "description": card.description,
        },
    )


def _clean_name(raw_alt: str) -> str:
    """Recover the program title from an ``alt`` that may be an image filename.

    Strips the CMS's filename boilerplate (see the patterns above). Returns ""
    when nothing title-like remains - the caller drops and logs that card.
    """
    name = raw_alt.strip()
    name = _FILENAME_PREFIX.sub("", name)
    name = _DIMENSIONS_PREFIX.sub("", name)
    if sandwich := _HE_ARTWORK_SANDWICH.match(name):
        name = sandwich.group(1)
    name = _HE_ARTWORK_PREFIX.sub("", name)
    name = _FILENAME_SUFFIX.sub("", name)
    previous = None
    while previous != name:
        previous = name
        name = _ARTWORK_SUFFIX.sub("", name).strip()
    if not _HAS_LETTER.search(name) or name in _KNOWN_JUNK_NAMES:
        return ""
    return name


def _text(node: Node | None) -> str | None:
    if node is None:
        return None
    return node.text(strip=True) or None


def _absolute(url: str) -> str | None:
    """Artwork URLs are site-relative; the image pipeline needs absolute."""
    if not url:
        return None
    if url.startswith(("http://", "https://")):
        return url
    return f"{BASE_URL}{url if url.startswith('/') else '/' + url}"
