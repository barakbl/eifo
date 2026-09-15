"""Apple TV prices: what it will attach to an offer, and what it refuses to.

The handle is an id now - IMDb to iTunes through Wikidata, then Apple's lookup
- so the tests that matter are about staying quiet when the chain breaks, and
about asking in batches rather than a title at a time.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

from eifo_core.enums import OfferType, TitleKind
from eifo_core.settings import Settings
from eifo_fetcher.enrichers.apple_prices import (
    LOOKUP_URL,
    SOURCE_KEY,
    WIKIDATA_URL,
    ApplePricesEnricher,
    LookupFailedError,
)
from eifo_fetcher.enrichers.base import TitleView
from eifo_fetcher.http import HttpClient
from eifo_fetcher.sources.base import FetchContext


def view(**overrides: Any) -> TitleView:
    values: dict[str, Any] = {
        "id": 1,
        "kind": TitleKind.MOVIE,
        "name_he": None,
        "name_en": "Raging Bull",
        "year": 1980,
        "tmdb_id": None,
        "imdb_id": "tt0081398",
        "offered_by": frozenset({SOURCE_KEY}),
    }
    values.update(overrides)
    return TitleView(**values)


def film(track_id: int = 123, **overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "kind": "feature-movie",
        "trackId": track_id,
        "trackName": "Raging Bull",
        # Apple's date is when it entered the store, not the film's year.
        "releaseDate": "2012-11-14T08:00:00Z",
        "trackPrice": 34.9,
        "trackRentalPrice": 16.9,
        "currency": "ILS",
        "trackViewUrl": f"https://itunes.apple.com/il/movie/raging-bull/id{track_id}?uo=4",
    }
    values.update(overrides)
    return values


@pytest.fixture
def ctx() -> FetchContext:
    return FetchContext(
        source_key="enrich", http=HttpClient(attempts=1), settings=Settings(_env_file=None)
    )


def wikidata(*pairs: tuple[str, str]) -> respx.Route:
    rows = [{"imdb": {"value": imdb}, "itunes": {"value": itunes}} for imdb, itunes in pairs]
    return respx.get(WIKIDATA_URL).mock(
        return_value=httpx.Response(200, json={"results": {"bindings": rows}})
    )


def apple(*results: dict[str, Any]) -> respx.Route:
    return respx.get(LOOKUP_URL).mock(
        return_value=httpx.Response(
            200, json={"resultCount": len(results), "results": list(results)}
        )
    )


class TestWhenItStaysQuiet:
    """It speaks for one store, about offers somebody else found."""

    @respx.mock
    def test_a_title_nobody_says_is_on_apple_is_not_looked_up(self, ctx: FetchContext) -> None:
        """A price found for a title Apple does not carry is a matching error,
        not a discovery."""
        route = wikidata()

        result = ApplePricesEnricher().enrich(view(offered_by=frozenset({"netflix_il"})), ctx)

        assert result is None
        assert not route.called

    @respx.mock
    def test_a_series_is_left_alone(self, ctx: FetchContext) -> None:
        """Apple sells those by season, which is not the shape kept here."""
        route = wikidata()

        result = ApplePricesEnricher().enrich(view(kind=TitleKind.SERIES), ctx)

        assert result is None
        assert not route.called

    @respx.mock
    def test_a_title_with_no_imdb_id_is_not_looked_up(self, ctx: FetchContext) -> None:
        """The IMDb id is the whole chain; without it there is nothing to follow."""
        route = wikidata()

        result = ApplePricesEnricher().enrich(view(imdb_id=None), ctx)

        assert result is None
        assert not route.called

    @respx.mock
    def test_no_itunes_id_on_wikidata_means_no_apple_request(self, ctx: FetchContext) -> None:
        wikidata()
        store = apple()

        result = ApplePricesEnricher().enrich(view(), ctx)

        assert result is None
        assert not store.called

    @respx.mock
    def test_an_id_the_israeli_store_does_not_sell(self, ctx: FetchContext) -> None:
        """Wikidata lists ids from every storefront; most answer nothing here."""
        wikidata(("tt0081398", "123"))
        apple()

        assert ApplePricesEnricher().enrich(view(), ctx) is None

    @respx.mock
    def test_something_that_is_not_a_film(self, ctx: FetchContext) -> None:
        wikidata(("tt0081398", "123"))
        apple(film(kind="tv-episode"))

        assert ApplePricesEnricher().enrich(view(), ctx) is None


class TestWhenAskingFails:
    """A failed request is not "Apple has nothing", and must not read as it."""

    @respx.mock
    def test_wikidata_down_is_an_error_for_the_title(self, ctx: FetchContext) -> None:
        respx.get(WIKIDATA_URL).mock(return_value=httpx.Response(500))

        with pytest.raises(LookupFailedError):
            ApplePricesEnricher().enrich(view(), ctx)

    @respx.mock
    def test_apple_down_is_an_error_for_the_title(self, ctx: FetchContext) -> None:
        wikidata(("tt0081398", "123"))
        respx.get(LOOKUP_URL).mock(return_value=httpx.Response(500))

        with pytest.raises(LookupFailedError):
            ApplePricesEnricher().enrich(view(), ctx)


class TestBatching:
    @respx.mock
    def test_the_worklist_is_asked_about_once(self, ctx: FetchContext) -> None:
        """A thousand titles used to be a thousand searches at twenty a minute."""
        titles = [view(id=n, imdb_id=f"tt{n:07d}") for n in range(1, 6)]
        ids = wikidata(*[(t.imdb_id or "", str(100 + t.id)) for t in titles])
        store = apple(*[film(track_id=100 + t.id) for t in titles])
        enricher = ApplePricesEnricher()

        enricher.prepare(titles, ctx)
        results = [enricher.enrich(title, ctx) for title in titles]

        assert ids.call_count == 1
        assert store.call_count == 1
        assert all(result is not None and result.offers for result in results)

    @respx.mock
    def test_every_id_is_asked_about_in_the_storefront(self, ctx: FetchContext) -> None:
        wikidata(("tt0081398", "111"), ("tt0081398", "222"))
        store = apple(film(track_id=222))

        ApplePricesEnricher().enrich(view(), ctx)

        request = store.calls.last.request
        assert request.url.params["country"] == "IL"
        assert set(request.url.params["id"].split(",")) == {"111", "222"}


class TestWhatItAttaches:
    @respx.mock
    def test_a_rental_and_a_sale_are_two_facts(self, ctx: FetchContext) -> None:
        wikidata(("tt0081398", "123"))
        apple(film())

        result = ApplePricesEnricher().enrich(view(), ctx)

        assert result is not None
        assert {(o.offer_type, o.price_minor) for o in result.offers} == {
            (OfferType.RENT, 1690),
            (OfferType.BUY, 3490),
        }
        assert all(o.source_key == SOURCE_KEY for o in result.offers)
        assert all(o.price_currency == "ILS" for o in result.offers)

    @respx.mock
    def test_the_store_year_is_not_held_against_it(self, ctx: FetchContext) -> None:
        """Bye Bye Birdie is 1963; Apple dates its listing 2012. The id is the
        match, and a year check would refuse a correct one."""
        wikidata(("tt0081398", "123"))
        apple(film(releaseDate="2012-01-01T00:00:00Z"))

        assert ApplePricesEnricher().enrich(view(year=1963), ctx) is not None

    @respx.mock
    def test_the_listing_that_quotes_more_wins(self, ctx: FetchContext) -> None:
        wikidata(("tt0081398", "111"), ("tt0081398", "222"))
        apple(
            film(track_id=111, trackRentalPrice=None, trackPrice=None),
            film(track_id=222),
        )

        result = ApplePricesEnricher().enrich(view(), ctx)

        assert result is not None
        assert {o.price_minor for o in result.offers} == {1690, 3490}
        assert all("id222" in (o.deep_link_url or "") for o in result.offers)

    @respx.mock
    def test_prices_are_stored_in_the_minor_unit(self, ctx: FetchContext) -> None:
        """16.9 is 1690 agorot, not 1689 - the float is 16.899999999999999."""
        wikidata(("tt0081398", "123"))
        apple(film(trackPrice=16.9, trackRentalPrice=None))

        result = ApplePricesEnricher().enrich(view(), ctx)

        assert result is not None
        priced = [offer.price_minor for offer in result.offers if offer.price_minor is not None]
        assert priced == [1690]

    @respx.mock
    def test_a_film_sold_but_not_rented_quotes_only_the_sale(self, ctx: FetchContext) -> None:
        """The rental keeps the link and gains no price: it is the same page."""
        wikidata(("tt0081398", "123"))
        apple(film(trackRentalPrice=None))

        result = ApplePricesEnricher().enrich(view(), ctx)

        assert result is not None
        priced = {offer.offer_type for offer in result.offers if offer.price_minor is not None}
        linked = {offer.offer_type for offer in result.offers if offer.deep_link_url}
        assert priced == {OfferType.BUY}
        assert linked == {OfferType.RENT, OfferType.BUY}

    @respx.mock
    def test_free_is_not_a_price(self, ctx: FetchContext) -> None:
        """Apple quotes 0.0 for "included with the app"."""
        wikidata(("tt0081398", "123"))
        apple(film(trackPrice=0.0, trackRentalPrice=0.0))

        result = ApplePricesEnricher().enrich(view(), ctx)

        assert result is not None
        assert all(offer.price_minor is None for offer in result.offers)
        assert all(offer.deep_link_url for offer in result.offers)
