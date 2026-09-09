"""Apple TV prices: what it will attach to an offer, and what it refuses to.

Most of these are refusals. The matching handle is a title string with no id
behind it, which is the same fuzzy problem that once put a ten-out-of-ten from
a single voter at the top of the catalog - so the tests that matter are the
ones about staying quiet.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

from eifo_core.enums import OfferType, TitleKind
from eifo_core.settings import Settings
from eifo_fetcher.enrichers.apple_prices import (
    SEARCH_URL,
    SOURCE_KEY,
    ApplePricesEnricher,
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
        "imdb_id": None,
        "offered_by": frozenset({SOURCE_KEY}),
    }
    values.update(overrides)
    return TitleView(**values)


def film(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "kind": "feature-movie",
        "trackName": "Raging Bull",
        "releaseDate": "1980-11-14T08:00:00Z",
        "trackPrice": 34.9,
        "trackRentalPrice": 16.9,
        "currency": "ILS",
        "trackViewUrl": "https://itunes.apple.com/il/movie/raging-bull/id123?uo=4",
    }
    values.update(overrides)
    return values


@pytest.fixture
def ctx() -> FetchContext:
    return FetchContext(source_key="enrich", http=HttpClient(), settings=Settings(_env_file=None))


def answer(*results: dict[str, Any]) -> None:
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(
            200, json={"resultCount": len(results), "results": list(results)}
        )
    )


class TestWhenItStaysQuiet:
    """It speaks for one store, about offers somebody else found."""

    @respx.mock
    def test_a_title_nobody_says_is_on_apple_is_not_looked_up(self, ctx: FetchContext) -> None:
        """The budget is requests a minute, and half the catalog is not there.

        Asking anyway would also invite the opposite mistake: a price found for
        a title Apple does not carry is a matching error, not a discovery.
        """
        route = respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json={"results": []}))

        result = ApplePricesEnricher().enrich(view(offered_by=frozenset({"netflix_il"})), ctx)

        assert result is None
        assert not route.called

    @respx.mock
    def test_a_series_is_left_alone(self, ctx: FetchContext) -> None:
        """Apple sells those by season, which is not the shape kept here."""
        route = respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json={"results": []}))

        result = ApplePricesEnricher().enrich(view(kind=TitleKind.SERIES), ctx)

        assert result is None
        assert not route.called

    @respx.mock
    def test_a_title_with_no_name_is_not_searched_for(self, ctx: FetchContext) -> None:
        route = respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json={"results": []}))

        result = ApplePricesEnricher().enrich(view(name_en=None, name_he=None), ctx)

        assert result is None
        assert not route.called


class TestWhatItRefusesToMatch:
    @respx.mock
    def test_a_name_that_merely_contains_the_words(self, ctx: FetchContext) -> None:
        """Apple answers "the godfather" with "The Godfathers of Hardcore"."""
        answer(film(trackName="The Godfathers of Hardcore", releaseDate="2017-01-01T00:00:00Z"))

        result = ApplePricesEnricher().enrich(view(name_en="The Godfather", year=1972), ctx)

        assert result is None

    @respx.mock
    def test_the_same_name_a_different_film(self, ctx: FetchContext) -> None:
        """Remakes share a title and nothing else; the year is what separates."""
        answer(film(trackName="Raging Bull", releaseDate="2015-01-01T00:00:00Z"))

        result = ApplePricesEnricher().enrich(view(year=1980), ctx)

        assert result is None

    @respx.mock
    def test_a_podcast_that_happens_to_be_named_right(self, ctx: FetchContext) -> None:
        """Apple's search leads with podcasts for a good many film titles."""
        answer(film(kind="podcast"))

        result = ApplePricesEnricher().enrich(view(), ctx)

        assert result is None

    @respx.mock
    def test_a_search_that_fails_costs_one_title_not_the_run(self, ctx: FetchContext) -> None:
        respx.get(SEARCH_URL).mock(return_value=httpx.Response(503))

        result = ApplePricesEnricher().enrich(view(), ctx)

        assert result is None
        assert ctx.error_count == 1

    @respx.mock
    def test_it_takes_the_right_film_rather_than_the_first(self, ctx: FetchContext) -> None:
        """Relevance is Apple's opinion; the name and the year are the test."""
        answer(
            film(trackName="Raging Bull II", releaseDate="1980-01-01T00:00:00Z", trackPrice=9.9),
            film(trackName="Raging Bull", trackPrice=34.9),
        )

        result = ApplePricesEnricher().enrich(view(), ctx)

        assert result is not None
        assert {offer.price_minor for offer in result.offers} == {1690, 3490}


class TestTheYearIsNotOptional:
    """The catalog treats a missing year as "no evidence either way".

    That is right when the question is whether two records are the same work,
    and wrong the moment the answer becomes a price. "Toy Story" scores exactly
    the accepting threshold against "Toy Story 5", so with the year absent the
    1995 film would be quoted what the 2026 one costs.
    """

    @respx.mock
    def test_a_title_with_no_year_is_not_priced(self, ctx: FetchContext) -> None:
        route = respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json={"results": []}))

        result = ApplePricesEnricher().enrich(view(name_en="Toy Story", year=None), ctx)

        assert result is None
        assert not route.called

    @respx.mock
    def test_a_result_with_no_year_is_not_believed(self, ctx: FetchContext) -> None:
        answer(film(releaseDate=None))

        result = ApplePricesEnricher().enrich(view(), ctx)

        assert result is None

    @respx.mock
    def test_a_sequel_in_another_year_is_a_different_film(self, ctx: FetchContext) -> None:
        """The real case: Apple's Israeli store answers "Toy Story" with only
        "Toy Story 5", which scores 90 - exactly the bar."""
        answer(film(trackName="Toy Story 5", releaseDate="2026-06-19T00:00:00Z", trackPrice=49.9))

        result = ApplePricesEnricher().enrich(view(name_en="Toy Story", year=1995), ctx)

        assert result is None


class TestWhatItAttaches:
    @respx.mock
    def test_a_rental_and_a_sale_are_two_facts(self, ctx: FetchContext) -> None:
        answer(film())

        result = ApplePricesEnricher().enrich(view(), ctx)

        assert result is not None
        assert {(o.offer_type, o.price_minor) for o in result.offers} == {
            (OfferType.RENT, 1690),
            (OfferType.BUY, 3490),
        }
        assert all(o.source_key == SOURCE_KEY for o in result.offers)
        assert all(o.price_currency == "ILS" for o in result.offers)

    @respx.mock
    def test_prices_are_stored_in_the_minor_unit(self, ctx: FetchContext) -> None:
        """16.9 is 1690 agorot, not 1689.

        Apple quotes 16.9 for what is written 16.90, and the float is really
        16.899999999999999 - truncating would shave an agora off every rental
        in the catalog.
        """
        answer(film(trackPrice=16.9, trackRentalPrice=None))

        result = ApplePricesEnricher().enrich(view(), ctx)

        assert result is not None
        priced = [offer for offer in result.offers if offer.price_minor is not None]
        assert [offer.price_minor for offer in priced] == [1690]

    @respx.mock
    def test_a_film_sold_but_not_rented_quotes_only_the_sale(self, ctx: FetchContext) -> None:
        """The rental keeps the link and gains no price.

        It is the same Apple page whichever way you would pay, and these offers
        have had no link at all since the TMDB watch page was taken off them -
        so the row still earns its button. What it does not earn is a figure
        Apple never quoted.
        """
        answer(film(trackRentalPrice=None))

        result = ApplePricesEnricher().enrich(view(), ctx)

        assert result is not None
        priced = {offer.offer_type for offer in result.offers if offer.price_minor is not None}
        linked = {offer.offer_type for offer in result.offers if offer.deep_link_url}
        assert priced == {OfferType.BUY}
        assert linked == {OfferType.RENT, OfferType.BUY}

    @respx.mock
    def test_free_is_not_a_price(self, ctx: FetchContext) -> None:
        """Apple quotes 0.0 for "included with the app", which is not a rental
        anybody is being charged for - and would read on the page as one."""
        answer(film(trackPrice=0.0, trackRentalPrice=0.0))

        result = ApplePricesEnricher().enrich(view(), ctx)

        # The link is still worth having; the prices are not.
        assert result is not None
        assert all(offer.price_minor is None for offer in result.offers)
        assert all(offer.deep_link_url for offer in result.offers)

    @respx.mock
    def test_the_link_is_carried_even_with_no_price_at_all(self, ctx: FetchContext) -> None:
        """It is the page either way, and these offers have had no link since
        the TMDB watch page was taken off them."""
        answer(film(trackPrice=None, trackRentalPrice=None))

        result = ApplePricesEnricher().enrich(view(), ctx)

        assert result is not None
        assert result.offers
        assert all("itunes.apple.com" in (offer.deep_link_url or "") for offer in result.offers)
