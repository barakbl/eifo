"""Lev VOD ("לב בבית") - the Lev cinema chain's rental library.

Israeli and international arthouse film, rented per title, so a
:data:`SourceKind.RENT_BUY` source alongside the Tel Aviv Cinematheque and the
Israeli Film Archive.

It is the Cinematheque's sibling in a literal sense: both chains sell through
Pres Global, the same ticketing platform, so ``priceLevels`` here is the shape
:mod:`~eifo_fetcher.sources.cinematheque_vod` already reads. Lev runs its own
instance of it, and that instance answers an honest client in JSON - no
browser, unlike Kan and Reshet 13, and no HTML parsing, unlike either of the
other two rental sources.

Where the catalog comes from:

* **The listing: one request.** ``/api/presentations`` returns every
  presentation the chain sells, cinema screenings included. The VOD ones are
  the rows at the house's virtual venue (:data:`VOD_VENUE_TYPE_ID`, which the
  API names "VOD" and files under the location "לב בבית"), and they carry the
  Hebrew and English titles, runtime, category and the offer's own window.
* **The film: one request per title.** ``/api/vod/features/{id}`` is the
  document the film's page on the site is drawn from - release year, countries,
  director, cast and synopsis. None of it is in the listing, and the year in
  particular is what lets the matcher resolve a Hebrew title.
* **The till: one request per title.** ``/api/presentations/{id}`` quotes the
  price. A sample of 30 on 2026-09-22 found ₪19.90 for 29 of them and no price
  band at all for the thirtieth, so a flat house rate is close to true - but it
  is read rather than assumed, the same as the Cinematheque, because "close to
  true" is how a catalog starts quoting last year's prices.

Posters cost nothing at all: :data:`POSTER_URL` addresses artwork by film
rather than by a stored filename, so there is no lookup to make.

That is two requests per film - 362 films and ~725 requests on 2026-09-22.
Between the Cinematheque's ~135 and the Israeli Film Archive's ~970, and gentle
at the configured one request a second. The film documents are the bulk of the
bytes: most of them inline that same poster as base64, which is why a sync
moves tens of megabytes for a few hundred films.

Two things this plugin must get right, both learned next door:

* **Currently on offer, not ever listed.** Every presentation the chain has
  ever sold stays in the listing, VOD included, each with the window it is
  watchable in. A title outside its window - or one that never had a window,
  which is a row that was never put on sale - is a catalog entry rather than
  something to rent tonight, so :func:`_on_offer` is what decides. It is worth
  getting right in both directions: reading it loosely put two unbuyable
  leftovers in the catalog at no price, and reading it too tightly would drop
  real films the day the chain leaves an end date open.
* **The deep link shows the film before the till.** ``/feature/{id}`` carries
  the synopsis, the trailer and the same buy button; ``/order/{id}`` is a
  checkout. Nobody should land on a payment form for a film they have not been
  shown, so the order page is recorded in ``extra`` and never linked to.

Unlike the Cinematheque, almost every title here names itself in English too,
and almost every one carries a release year: 363 of 364 and 359 of 364 when the
whole catalog was read on 2026-09-22. Between them that is the difference
between a matcher guessing from a Hebrew title and one being told the answer.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

from eifo_core.enums import CreditRole, OfferType, SourceKind, TitleKind
from eifo_core.naming import split_by_script
from eifo_core.types import utcnow
from eifo_fetcher.countries import country_codes
from eifo_fetcher.http import USER_AGENT
from eifo_fetcher.robots import RobotsPolicy
from eifo_fetcher.sources.base import FetchContext, RawItem, SourceInfo, SourcePlugin

logger = logging.getLogger("eifo.fetch.source.lev_vod")

SOURCE_KEY = "lev_vod"
HOST = "ticket.lev.co.il"
BASE_URL = f"https://{HOST}"
API_URL = f"{BASE_URL}/api"

#: Every presentation the chain sells, screenings and rentals in one document.
CATALOG_API = f"{API_URL}/presentations"
#: The film's own record: year, countries, director, cast, synopsis.
FEATURE_API = f"{API_URL}/vod/features/{{feature_id}}"
#: The till, and so the price. ``referralMiniSiteId`` picks the house's own
#: pricing rather than a partner's; the site sends 0 and so does this.
PRICE_API = f"{API_URL}/presentations/{{presentation_id}}?referralMiniSiteId=0"
#: Artwork, addressed by film rather than by a stored filename - so a poster
#: costs no request of its own. ``raw=1`` asks for the image rather than a
#: document describing it.
POSTER_URL = f"{API_URL}/features/{{feature_id}}/image?raw=1"
#: Where a viewer is sent: the film's page, which is not the checkout.
FEATURE_PAGE = f"{BASE_URL}/feature/{{feature_id}}"
#: What the site calls the library, and what a visitor sees at the base URL.
CATALOG_URL = BASE_URL

#: The venue type the chain files home viewing under. Its rows name the venue
#: "VOD" and the location "לב בבית"; every other type is a physical screen.
VOD_VENUE_TYPE_ID = 101

#: The chain sells in shekels only, and the API quotes bare numbers.
PRICE_CURRENCY = "ILS"

#: The ticketing system stamps its datetimes in local time with no offset, so
#: they mean nothing until they are read as Israeli time. Everything else in
#: Eifo is UTC-aware, and comparing a naive Israeli timestamp against a UTC
#: clock is wrong by two or three hours depending on the season - which for a
#: window that opens at midnight is a title wrongly on or off the catalog.
_HOUSE_TIMEZONE = ZoneInfo("Asia/Jerusalem")
_TIMESTAMP_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M")

#: A marketing tail after the title: "מחוברים לחיים VOD צרפתי" is one film
#: called "מחוברים לחיים". Rarer than at the Cinematheque - one title in 364 -
#: but the same mistake if it is stored as written.
_VOD_MARKER = re.compile(r"\s*\|?\s*\bVOD\b.*$", re.IGNORECASE)


class LevCatalogError(RuntimeError):
    """The listing could not be read in the shape this plugin expects."""


@dataclass(frozen=True, slots=True)
class VodOffer:
    """One rentable film, as the chain's listing presents it."""

    feature_id: int
    #: The presentation this offer is sold as, and so where its price is read
    #: from. Not where a viewer is sent - see :data:`FEATURE_PAGE`.
    presentation_id: int
    name: str
    name_alt: str | None = None
    runtime_minutes: int | None = None
    category: str | None = None


@dataclass(frozen=True, slots=True)
class VodFeature:
    """What the film's own record adds to a listing."""

    year: int | None = None
    countries: str | None = None
    director: str | None = None
    actors: str | None = None
    synopsis: str | None = None
    trailer_url: str | None = None


class LevVodPlugin(SourcePlugin):
    """Yields the Lev chain's rentable VOD catalog."""

    def sources(self) -> list[SourceInfo]:
        return [
            SourceInfo(
                key=SOURCE_KEY,
                name="Lev VOD (לב בבית)",
                kind=SourceKind.RENT_BUY,
                website_url=CATALOG_URL,
            )
        ]

    def fetch(self, ctx: FetchContext) -> Iterator[RawItem]:
        ctx.apply_rate_limit(HOST)
        RobotsPolicy(user_agent=USER_AGENT).require_allowed(CATALOG_API)

        offers, dropped = parse_catalog(ctx.http.get_json(CATALOG_API))
        if dropped:
            # One counted error, not N: a listing that changes shape must not
            # trip the consecutive-error abort before the count is stored.
            ctx.record_error(f"{dropped} VOD listings were not usable; skipped")
        logger.info("Lev lists %d films on offer", len(offers))

        unpriced = 0
        undescribed = 0
        for offer in offers:
            feature = self._feature(ctx, offer.feature_id)
            undescribed += feature is None
            price_minor = _price_minor(ctx, offer.presentation_id)
            unpriced += price_minor is None
            ctx.record_success()
            yield _to_item(offer, feature or VodFeature(), price_minor)

        if unpriced:
            # Again counted rather than raised: the offers are real either way,
            # but a catalog that quietly lost every price must not look healthy.
            ctx.record_error(f"{unpriced} titles were listed without a readable price")
        if undescribed:
            ctx.record_error(f"{undescribed} titles were listed without their own record")

    def _feature(self, ctx: FetchContext, feature_id: int) -> VodFeature | None:
        """The film's own record, or None if it could not be read.

        A film that will not describe itself is still a film on offer, so this
        is thinner rather than absent: the title, the price and the link are
        all in the listing already.
        """
        try:
            payload = ctx.http.get_json(FEATURE_API.format(feature_id=feature_id))
            return parse_feature(payload)
        except Exception:
            logger.info("no record for feature %s; listing it thinner", feature_id, exc_info=True)
            return None


def _price_minor(ctx: FetchContext, presentation_id: int) -> int | None:
    """What this title costs, in agorot, or None if the till would not say.

    The document lists a price band per ticket type; the chain sells VOD as a
    single type today, and taking the cheapest keeps a future concession from
    inflating what Eifo displays.
    """
    try:
        payload = ctx.http.get_json(PRICE_API.format(presentation_id=presentation_id))
        levels = payload["presentation"]["priceLevels"]
        prices = [float(level["minPrice"]) for level in levels if level.get("minPrice") is not None]
    except Exception:
        logger.info("no price for presentation %s", presentation_id, exc_info=True)
        return None

    if not prices:
        return None
    return round(min(prices) * 100)


def parse_catalog(payload: Any, *, now: dt.datetime | None = None) -> tuple[list[VodOffer], int]:
    """Turn the chain's listing into the films rentable right now.

    A film can be listed more than once - the same title re-sold under a new
    presentation years later - and is yielded once, as the newest of them,
    because that is the one the site itself links to.

    Returns:
        ``(offers, dropped)``: the films on offer, and how many *VOD* rows were
        unusable. Only a row that fails to name a film counts as dropped.
        Cinema screenings share the document and are not VOD offers; a title
        outside its window, or one that never had a window, is correctly listed
        and simply not on sale. None of those is a fault to report.

    Raises:
        LevCatalogError: if the document holds no VOD rows at all, which means
            the API changed or the response was not the listing.
    """
    moment = now or utcnow()
    rows = payload.get("presentations") if isinstance(payload, Mapping) else None
    if not isinstance(rows, list):
        raise LevCatalogError(
            "no 'presentations' in the Lev listing; the API changed or the response was not it"
        )

    vod_rows = [
        row
        for row in rows
        if isinstance(row, Mapping) and row.get("venueTypeId") == VOD_VENUE_TYPE_ID
    ]
    if not vod_rows:
        raise LevCatalogError(
            f"no venueTypeId={VOD_VENUE_TYPE_ID} rows in the Lev listing; "
            f"the VOD venue was renumbered or the response was not the listing"
        )

    newest: dict[int, tuple[str, VodOffer]] = {}
    dropped = 0
    for row in vod_rows:
        if not _on_offer(row, moment):
            continue  # listed, but not watchable today
        offer = _to_offer(row)
        if offer is None:
            dropped += 1
            continue
        sold_on = str(row.get("dateTime") or "")
        current = newest.get(offer.feature_id)
        if current is None or sold_on > current[0]:
            newest[offer.feature_id] = (sold_on, offer)
    return [offer for _, offer in newest.values()], dropped


def _to_offer(row: Mapping[str, Any]) -> VodOffer | None:
    """Convert one listing row, or None if it does not identify a film.

    The two name fields are sorted by script rather than by position, because
    one title in the catalog gives the same Hebrew in both. A title written in
    neither script falls back to the listing's own first field: "1917" is
    spelled identically in both columns and has no letters to sort, and reading
    that as "no name" silently lost a film the chain really does sell.
    :func:`~eifo_core.match.fallback_name` is what finally places it.
    """
    feature_id = _int_or_none(row.get("featureId"))
    presentation_id = _int_or_none(row.get("id"))
    listed = _clean_name(row.get("featureName"))
    hebrew, english = split_by_script(listed, _clean_name(row.get("featureAdditionalName")))
    name = hebrew or english or listed
    if feature_id is None or presentation_id is None or not name:
        return None

    return VodOffer(
        feature_id=feature_id,
        presentation_id=presentation_id,
        name=name,
        # Only when it is a second name: one title gives the same Hebrew in
        # both fields, and an "alternate" identical to the name helps nobody.
        name_alt=english if english and english != name else None,
        runtime_minutes=_int_or_none(row.get("durationInMinutes")),
        category=_text_or_none(row.get("featureCategoryName")),
    )


def parse_feature(payload: Any) -> VodFeature:
    """Read the film's own record.

    Raises:
        LevCatalogError: if the document carries no feature. The API answers
            ``{"feature": null}`` rather than a 404 for a film it has never
            described, so this is a shape to expect rather than a failure to be
            loud about - one row was in that state on 2026-09-22, and it was one
            of the two leftovers :func:`_on_offer` already keeps out.
    """
    try:
        feature = payload["feature"]
    except (KeyError, TypeError) as exc:
        raise LevCatalogError("no 'feature' in the Lev film record") from exc
    if not isinstance(feature, Mapping):
        raise LevCatalogError("the Lev film record describes no film")

    return VodFeature(
        year=_int_or_none(feature.get("releaseYear")),
        countries=_text_or_none(feature.get("releaseCountries")),
        director=_text_or_none(feature.get("director")),
        actors=_text_or_none(feature.get("actors")),
        synopsis=_text_or_none(feature.get("synopsis")),
        trailer_url=_url_or_none(feature.get("trailer")),
    )


def _on_offer(row: Mapping[str, Any], now: dt.datetime) -> bool:
    """Whether this presentation can be watched at ``now``.

    A row with no window at all has never been put on sale, whatever else it
    looks like. Two of the 378 VOD rows were in that state on 2026-09-22, and
    both gave themselves away twice over: their tills quote an empty price band
    and neither is filed under a category, so neither is reachable from the
    library at all. Every one of the other 376 carries both dates. Reading a
    missing window as "sells forever" put two films in the catalog at no price,
    which is how a leftover row becomes an offer nobody can buy.

    An open end, by contrast, is taken at its word. No row has one today, so
    this is a guess either way - and the safe guess is that a chain which set a
    start and no close meant to keep selling.
    """
    start = _timestamp(row.get("vodStartDateTime"))
    if start is None or start > now:
        return False
    end = _timestamp(row.get("vodEndDateTime"))
    return end is None or end >= now


def _timestamp(value: Any) -> dt.datetime | None:
    """One of the API's local timestamps as an aware datetime, or None.

    Both widths the API uses are accepted: the listing writes "2030-06-30
    23:59:00" in one field and "2024-01-04 23:59" in another.
    """
    text = _text_or_none(value)
    if text is None:
        return None
    for fmt in _TIMESTAMP_FORMATS:
        try:
            return dt.datetime.strptime(text, fmt).replace(tzinfo=_HOUSE_TIMEZONE)
        except ValueError:
            continue
    logger.info("unreadable timestamp %r in the Lev listing", text)
    return None


def _clean_name(value: Any) -> str | None:
    """A title with any marketing tail taken off it."""
    text = _text_or_none(value)
    return _text_or_none(_VOD_MARKER.sub("", text)) if text else None


def _text_or_none(value: Any) -> str | None:
    if not isinstance(value, str):
        return str(value) if isinstance(value, int) else None
    return " ".join(value.split()) or None


def _url_or_none(value: Any) -> str | None:
    text = _text_or_none(value)
    return text if text and text.startswith(("http://", "https://")) else None


def _int_or_none(value: Any) -> int | None:
    """An integer the API may have written as a string, or None."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _credits(feature: VodFeature) -> tuple[dict[str, str], ...]:
    """Director and cast, named the way the chain names them.

    Hebrew, because that is what an Israeli distributor publishes; a Latin
    spelling for the same person can only come from a source that has one.
    Both fields list several people separated by commas.
    """
    entries: list[dict[str, str]] = [
        {"role": CreditRole.DIRECTOR, "name_he": name} for name in _people(feature.director)
    ]
    entries += [{"role": CreditRole.CAST, "name_he": name} for name in _people(feature.actors)]
    return tuple(entries)


def _people(names: str | None) -> list[str]:
    """ "דני פיליפו, מייקל פיליפו" as two people.

    The comma is the only separator trusted here. A directing pair is
    occasionally joined by the Hebrew conjunction instead - "אריק טולדנו
    ואוליבייה נקש" - and splitting on that would be a bad trade: the
    conjunction is glued to the following word and is spelled with the same
    letter many surnames begin with, so of the 28 directors whose names
    contain it, 26 are one person ("אנייס ורדה", "דני ווילנב", "מארק ווב").
    Two credits merged into one is a smaller wrong than 26 names cut in half.
    """
    return [name.strip() for name in (names or "").split(",") if name.strip()]


def _to_item(offer: VodOffer, feature: VodFeature, price_minor: int | None) -> RawItem:
    """Convert one offer into the item the pipeline stores.

    Everything here is rented: unlike the Cinematheque and the archive, the
    chain gives nothing away, and a price of zero would be a till that failed
    to answer rather than a gift.
    """
    return RawItem(
        source_key=SOURCE_KEY,
        kind=TitleKind.MOVIE,
        name=offer.name,
        name_alt=offer.name_alt,
        year=feature.year,
        offer_type=OfferType.RENT,
        deep_link_url=FEATURE_PAGE.format(feature_id=offer.feature_id),
        source_ref=str(offer.feature_id),
        poster_url=POSTER_URL.format(feature_id=offer.feature_id),
        price_minor=price_minor,
        price_currency=None if price_minor is None else PRICE_CURRENCY,
        # TMDB carries little Israeli distribution, so for a good part of this
        # catalog the chain's own credit is the only one there will ever be.
        credits=_credits(feature),
        origin_countries=country_codes(feature.countries),
        extra={
            "order_url": f"{BASE_URL}/order/{offer.presentation_id}",
            "country": feature.countries,
            "runtime_minutes": offer.runtime_minutes,
            "category": offer.category,
            "director": feature.director,
            "description": feature.synopsis,
            "trailer_url": feature.trailer_url,
        },
    )
