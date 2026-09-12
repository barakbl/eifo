"""What Apple charges to rent or buy a title, and where its page is.

The provider harvester learns *that* a title is on the Apple TV store from
JustWatch, whose export carries neither a price nor a link - so those rows
arrive with the kind of deal and nothing else, and a viewer is told a film can
be bought without being told for how much or where. Apple publishes both
through the same public search endpoint the iTunes affiliate API has always
offered, in the storefront's own currency.

**This enricher never says a title is on Apple.** It is told which services
already offer the one it is given and stays quiet unless the store is among
them; what it adds is the price and the link to an offer somebody else found.
A price arriving for something nobody is offering would be a matching mistake
rather than news, and the catalog drops it on that principle too.

**Roughly half of them resolve.** Measured against the deployed catalog: of
twenty Apple TV titles sampled at random, nine matched confidently and every
one of those nine carried both a price and a working ``tv.apple.com`` link; ten
returned no film at all and one was too close to call. The failures are real
rather than a bug here - Apple's search answers with podcasts for "Grizzly
Man", and returns no film for "Ted" even on the US storefront - so this fills
in what it can and leaves the rest exactly as they were. Half a catalog with
real prices is worth more than none, and a row that says only "Rent" is what
every one of them says today.

**Matching is by name, because there is nothing else.** These offers carry no
source reference, so the title text is the only handle - which is the same
fuzzy problem that once let a ten-out-of-ten from a single voter top the
catalog. It is treated with the same suspicion: the catalog's own normalising
and similarity, a high bar, a year that has to agree, and silence when unsure.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from eifo_core.enums import OfferType, TitleKind
from eifo_core.findings import OfferFact
from eifo_core.match import similarity, years_match
from eifo_fetcher.enrichers.base import Enricher, EnrichResult, TitleView
from eifo_fetcher.sources.base import FetchContext

logger = logging.getLogger("eifo.fetch.enrich.apple")

HOST = "itunes.apple.com"
SEARCH_URL = f"https://{HOST}/search"

#: The source this speaks for. It says nothing about any other.
SOURCE_KEY = "apple_tv_store"

#: The storefront to price against. Israeli catalog, Israeli prices.
REGION = "IL"

#: How sure the name has to be before a price is attached to a title.
#:
#: The same bar the catalog uses to accept a fuzzy match, and for the same
#: reason: a price on the wrong film is worse than no price, because it looks
#: exactly as authoritative as a right one. Apple's search answers "the
#: godfather" with "The Godfathers of Hardcore", which scores far below this.
NAME_THRESHOLD = 90.0

#: Results to consider. Apple orders by its own relevance, which puts podcasts
#: above films for some queries, so the film may not be first.
RESULTS = 12

#: What Apple calls a film, as opposed to a podcast, an album or an episode.
FEATURE = "feature-movie"


class ApplePricesEnricher(Enricher):
    """Prices and store links for titles the Apple TV store carries."""

    key = "apple_prices"
    name = "Apple TV prices"
    host = HOST
    # Not a rating provider, so the ratings queue is not its worklist: asked on
    # its own, it walks the store's offers that carry no figure yet.
    prices_for = SOURCE_KEY
    # Apple documents roughly twenty calls a minute for this endpoint, so a
    # third of a request a second with a little room. The pipeline applies it
    # from here; nothing in this file has to remember to be polite.
    default_rate_limit_rps = 0.3

    def enrich(self, title: TitleView, ctx: FetchContext) -> EnrichResult | None:
        # Not on Apple, nothing to say. Checked first because the budget here
        # is requests per minute, and half the catalog is not on the store.
        if SOURCE_KEY not in title.offered_by:
            return None

        # Series are sold by season, which is a different shape of thing from
        # the one offer per title this catalog keeps. Films only, for now.
        if title.kind is not TitleKind.MOVIE:
            return None

        term = title.name_en or title.name_he
        if not term:
            return None

        # A year on both sides, and they have to agree. The catalog's ordinary
        # rule is that a missing year is not evidence of a mismatch, which is
        # right when the question is "are these the same work" and wrong when
        # the answer becomes a price: "Toy Story" scores 90 against "Toy Story
        # 5", so without a year the 1995 film would be sold for what the 2026
        # one costs. Two of the 17,799 Apple films here have no year, which is
        # a cheap price for never quoting the wrong one.
        if title.year is None:
            return None

        match = self._best(term, title, ctx)
        if match is None:
            return None

        facts = [
            fact
            for fact in (
                _fact(OfferType.RENT, match.get("trackRentalPrice"), match),
                _fact(OfferType.BUY, match.get("trackPrice"), match),
            )
            if fact is not None
        ]
        return EnrichResult(offers=facts) if facts else None

    def _best(self, term: str, title: TitleView, ctx: FetchContext) -> dict[str, Any] | None:
        """The film Apple returned that is this one, if it returned it.

        Deliberately not "the first result": Apple ranks by its own relevance
        and will happily lead with a podcast, or with a different film whose
        name merely contains the words.
        """
        try:
            response = ctx.http.get(
                SEARCH_URL,
                params={"term": term, "country": REGION, "limit": RESULTS},
            )
            response.raise_for_status()
            results = response.json().get("results") or []
        except (httpx.HTTPError, ValueError) as exc:
            ctx.record_error(f"apple: {title.describe()}: {exc}")
            return None

        best: dict[str, Any] | None = None
        best_score = 0.0
        for result in results:
            if result.get("kind") != FEATURE:
                continue
            score = similarity(term, result.get("trackName") or "")
            if score <= best_score:
                continue
            # A year that disagrees is a different film with a shared name, and
            # there are a great many of those. Apple's date is the release into
            # its own store for some titles, so the catalog's ordinary tolerance
            # applies rather than an exact match - but it has to be there.
            year = _year_of(result)
            if year is None or not years_match(title.year, year):
                continue
            best, best_score = result, score

        if best is None or best_score < NAME_THRESHOLD:
            return None
        return best


def _fact(kind: OfferType, price: Any, result: dict[str, Any]) -> OfferFact | None:
    """One deal, if Apple quoted it.

    A missing price is ordinary - plenty of films are sold but not rented - and
    means this deal simply has nothing to add. The link is worth carrying even
    then, because it is the same page either way.
    """
    minor = _minor_units(price)
    link = result.get("trackViewUrl")
    if minor is None and not link:
        return None
    return OfferFact(
        source_key=SOURCE_KEY,
        offer_type=kind,
        price_minor=minor,
        price_currency=result.get("currency") if minor is not None else None,
        deep_link_url=link or None,
    )


def _minor_units(price: Any) -> int | None:
    """A price in the currency's minor unit, the way the catalog stores it.

    Apple quotes 16.9 for what is written ₪16.90. Rounded rather than
    truncated: floating point renders 16.9 as 16.899999999999999, and int()
    would quietly shave an agora off every rental in the catalog.
    """
    if price is None:
        return None
    try:
        value = float(price)
    except (TypeError, ValueError):
        return None
    # Apple uses 0.0 for "free with this app", which is not a price for a
    # rental and would read as one.
    if value <= 0:
        return None
    return round(value * 100)


def _year_of(result: dict[str, Any]) -> int | None:
    raw = str(result.get("releaseDate") or "")[:4]
    return int(raw) if raw.isdigit() else None
