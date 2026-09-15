"""What Apple charges to rent or buy a title, and where its page is.

The provider harvester learns *that* a title is on the Apple TV store from
JustWatch, whose export carries neither a price nor a link - so those rows
arrive with the kind of deal and nothing else, and a viewer is told a film can
be bought without being told for how much or where. Apple publishes both
through the iTunes lookup endpoint, in the storefront's own currency.

**This enricher never says a title is on Apple.** It is told which services
already offer the one it is given and stays quiet unless the store is among
them; what it adds is the price and the link to an offer somebody else found.
A price arriving for something nobody is offering would be a matching mistake
rather than news, and the catalog drops it on that principle too.

**Matching is by id, not by name.** This used to search Apple by title and
accept a close name with an agreeing year. In September 2026 Apple's search
stopped returning films: "Inception" limited to movies came back empty on the
US storefront, and "Tokyo Drift" answered with podcasts and a song - from any
network, so not a block. Looking a film up by its iTunes id still works. The
ids come from Wikidata, which maps IMDb ids (which the catalog has) to iTunes
movie ids (property P6398). That is exact where the name search was a guess,
so the name and year checks went with it: Apple's release date is the date it
entered the store, not the film's year, and would have refused correct ones.

**About four in ten resolve.** Of 400 Apple films sampled from the catalog,
198 had an iTunes id on Wikidata and 174 of those were priced in the Israeli
store. The rest are left exactly as they were.

**Batched, because both ends allow it.** ``prepare`` is handed the worklist:
one Wikidata query per couple of hundred titles, one Apple lookup per 150 ids.
A thousand titles cost about fifteen requests, where the search cost a thousand
at Apple's twenty a minute.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator
from typing import Any

import httpx

from eifo_core.enums import OfferType, TitleKind
from eifo_core.findings import OfferFact
from eifo_fetcher.enrichers.base import Enricher, EnrichResult, TitleView
from eifo_fetcher.sources.base import FetchContext

logger = logging.getLogger("eifo.fetch.enrich.apple")

HOST = "itunes.apple.com"
LOOKUP_URL = f"https://{HOST}/lookup"
WIKIDATA_URL = "https://query.wikidata.org/sparql"

#: The source this speaks for. It says nothing about any other.
SOURCE_KEY = "apple_tv_store"

#: The storefront to price against. Israeli catalog, Israeli prices.
REGION = "IL"

#: IMDb ids per Wikidata query. Sent as a GET, so the list has to fit a URL.
WIKIDATA_CHUNK = 200

#: iTunes ids per lookup. A film carries several - one per regional listing -
#: and the one this storefront sells is found by asking about all of them.
LOOKUP_CHUNK = 150

#: What Apple calls a film, as opposed to a podcast, an album or an episode.
FEATURE = "feature-movie"


class LookupFailedError(RuntimeError):
    """Wikidata or Apple could not be asked about this title, which is not the
    same as being asked and having nothing - the catalog retries sooner."""


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

    def __init__(self) -> None:
        #: What Apple's storefront answered, by IMDb id.
        self._found: dict[str, dict[str, Any]] = {}
        #: IMDb ids already asked about, answered or not.
        self._asked: set[str] = set()
        #: IMDb ids that could not be asked because a request failed.
        self._failed: set[str] = set()

    def prepare(self, titles: list[TitleView], ctx: FetchContext) -> None:
        wanted = sorted(
            {
                title.imdb_id
                for title in titles
                if title.imdb_id and _worth_asking(title) and title.imdb_id not in self._asked
            }
        )
        if not wanted:
            return

        itunes_ids: dict[str, list[str]] = {}
        for chunk in _chunks(wanted, WIKIDATA_CHUNK):
            try:
                itunes_ids.update(_itunes_ids(chunk, ctx))
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                logger.warning("wikidata could not be asked about %d title(s): %s", len(chunk), exc)
                self._failed.update(chunk)

        by_track: dict[str, dict[str, Any]] = {}
        every_id = sorted({i for ids in itunes_ids.values() for i in ids})
        for chunk in _chunks(every_id, LOOKUP_CHUNK):
            try:
                by_track.update(_lookup(chunk, ctx))
            except (httpx.HTTPError, ValueError) as exc:
                logger.warning("apple could not be asked about %d id(s): %s", len(chunk), exc)
                asked = set(chunk)
                self._failed.update(imdb for imdb, ids in itunes_ids.items() if asked & set(ids))

        for imdb, ids in itunes_ids.items():
            best = _best([by_track[i] for i in ids if i in by_track])
            if best is not None:
                self._found[imdb] = best

        self._asked.update(wanted)
        logger.info(
            "apple: %d of %d title(s) have an iTunes id, %d priced in the %s store",
            len(itunes_ids),
            len(wanted),
            sum(1 for imdb in wanted if imdb in self._found),
            REGION,
        )

    def enrich(self, title: TitleView, ctx: FetchContext) -> EnrichResult | None:
        # Not on Apple, nothing to say. The price is for an offer somebody else
        # found; one for a title nobody says is there would be a mistake.
        if not _worth_asking(title):
            return None
        imdb = title.imdb_id
        assert imdb is not None  # _worth_asking

        # Handed a title the batch did not include - a repair naming its own
        # titles, or a test - so it is asked about on its own.
        if imdb not in self._asked:
            self.prepare([title], ctx)
        if imdb in self._failed:
            raise LookupFailedError(f"apple: {title.describe()}: lookup failed")

        match = self._found.get(imdb)
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


def _worth_asking(title: TitleView) -> bool:
    """On the store, a film, and with the id that leads to Apple's.

    Series are sold by season, which is a different shape of thing from the one
    offer per title this catalog keeps. Films only, for now.
    """
    return SOURCE_KEY in title.offered_by and title.kind is TitleKind.MOVIE and bool(title.imdb_id)


def _chunks(items: list[str], size: int) -> Iterator[list[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _itunes_ids(imdb_ids: Iterable[str], ctx: FetchContext) -> dict[str, list[str]]:
    """Every iTunes movie id Wikidata knows for these IMDb ids."""
    values = " ".join(f'"{imdb}"' for imdb in imdb_ids)
    # P345 is the IMDb id, P6398 the iTunes movie id.
    query = (
        f"SELECT ?imdb ?itunes WHERE {{ VALUES ?imdb {{ {values} }} "
        "?item wdt:P345 ?imdb ; wdt:P6398 ?itunes . }"
    )
    response = ctx.http.get(
        WIKIDATA_URL,
        params={"query": query, "format": "json"},
        headers={"Accept": "application/sparql-results+json"},
    )
    found: dict[str, list[str]] = {}
    for row in response.json()["results"]["bindings"]:
        itunes = row["itunes"]["value"]
        # The property is a string on Wikidata, and a stray non-number would
        # poison the whole lookup it is batched into.
        if itunes.isdigit():
            found.setdefault(row["imdb"]["value"], []).append(itunes)
    return found


def _lookup(itunes_ids: list[str], ctx: FetchContext) -> dict[str, dict[str, Any]]:
    """What this storefront says about each id it sells, by id."""
    response = ctx.http.get(
        LOOKUP_URL,
        params={"id": ",".join(itunes_ids), "country": REGION},
    )
    return {
        str(result["trackId"]): result
        for result in response.json().get("results") or []
        if result.get("kind") == FEATURE and result.get("trackId") is not None
    }


def _best(results: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The listing to quote, when the store sells a film under more than one id.

    The one that quotes the most wins - rental and sale over either alone - so a
    leftover listing with no price does not hide one that has both. Ties go to
    the lowest id, so a rerun quotes the same listing.
    """
    if not results:
        return None

    def quoted(result: dict[str, Any]) -> tuple[int, int]:
        count = sum(
            1 for key in ("trackPrice", "trackRentalPrice") if _minor_units(result.get(key))
        )
        return (-count, int(result["trackId"]))

    return min(results, key=quoted)


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
