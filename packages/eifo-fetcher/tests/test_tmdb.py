"""The TMDB client and the provider harvester."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import httpx
import pytest
import respx
from pydantic import SecretStr

from eifo_core.db import create_engine_from_settings, make_session_factory
from eifo_core.enums import OfferType, SourceKind, TitleKind
from eifo_core.models import Base
from eifo_core.settings import Settings, SourceConfig
from eifo_fetcher.enrichers.tmdb_meta import _client_from as _enricher_client_from
from eifo_fetcher.http import HttpClient, RateLimiter
from eifo_fetcher.runner import _tmdb_client, fetch_images
from eifo_fetcher.sources.base import FetchContext, TooManyErrorsError
from eifo_fetcher.sources.tmdb_providers import (
    PROVIDER_SOURCES,
    TmdbProvidersPlugin,
)
from eifo_fetcher.sources.tmdb_providers import _client_from as _provider_client_from
from eifo_fetcher.tmdb import (
    API_HOST,
    BASE_URL,
    DEFAULT_RATE_LIMIT_RPS,
    IMAGE_HOST,
    TmdbClient,
    image_url,
)

API_KEY = "test-key"


@pytest.fixture
def tmdb(http: HttpClient) -> TmdbClient:
    return TmdbClient(http, API_KEY)


def movie_result(tmdb_id: int, title: str, date: str = "2015-06-01") -> dict[str, Any]:
    return {
        "id": tmdb_id,
        "title": title,
        "original_title": title,
        "release_date": date,
        "overview": "An overview.",
        "poster_path": "/p.jpg",
    }


def tv_result(tmdb_id: int, name: str, date: str = "2015-06-01") -> dict[str, Any]:
    return {
        "id": tmdb_id,
        "name": name,
        "original_name": name,
        "first_air_date": date,
        "overview": "An overview.",
        "poster_path": "/p.jpg",
    }


class TestParsing:
    @respx.mock
    def test_normalises_the_movie_shape(self, tmdb: TmdbClient) -> None:
        respx.get(f"{BASE_URL}/search/movie").mock(
            return_value=httpx.Response(200, json={"results": [movie_result(1, "Foxtrot")]})
        )

        [hit] = tmdb.search(TitleKind.MOVIE, "Foxtrot")

        assert hit.tmdb_id == 1
        assert hit.name == "Foxtrot"
        assert hit.year == 2015
        assert hit.media_type == "movie"

    @respx.mock
    def test_normalises_the_tv_shape(self, tmdb: TmdbClient) -> None:
        respx.get(f"{BASE_URL}/search/tv").mock(
            return_value=httpx.Response(200, json={"results": [tv_result(2, "Fauda")]})
        )

        [hit] = tmdb.search(TitleKind.SERIES, "Fauda")

        assert hit.name == "Fauda"
        assert hit.media_type == "tv"

    @respx.mock
    @pytest.mark.parametrize("date", ["", "not-a-date", None])
    def test_tolerates_a_missing_or_junk_date(self, tmdb: TmdbClient, date: Any) -> None:
        respx.get(f"{BASE_URL}/search/movie").mock(
            return_value=httpx.Response(
                200, json={"results": [{"id": 3, "title": "X", "release_date": date}]}
            )
        )

        assert tmdb.search(TitleKind.MOVIE, "X")[0].year is None

    @respx.mock
    def test_sends_the_api_key_and_language(self, tmdb: TmdbClient) -> None:
        route = respx.get(f"{BASE_URL}/search/tv").mock(
            return_value=httpx.Response(200, json={"results": []})
        )

        tmdb.search(TitleKind.SERIES, "Fauda", year=2015)

        params = route.calls.last.request.url.params
        assert params["api_key"] == API_KEY
        assert params["language"] == "he-IL"
        assert params["first_air_date_year"] == "2015"


class TestDiscover:
    @respx.mock
    def test_walks_every_page(self, tmdb: TmdbClient) -> None:
        respx.get(f"{BASE_URL}/discover/tv").mock(
            side_effect=[
                httpx.Response(200, json={"total_pages": 2, "results": [tv_result(1, "A")]}),
                httpx.Response(200, json={"total_pages": 2, "results": [tv_result(2, "B")]}),
            ]
        )

        hits = list(tmdb.discover_by_provider(TitleKind.SERIES, 8))

        assert [hit.tmdb_id for hit in hits] == [1, 2]

    @respx.mock
    def test_stops_at_the_page_cap(self, tmdb: TmdbClient) -> None:
        route = respx.get(f"{BASE_URL}/discover/tv").mock(
            return_value=httpx.Response(
                200, json={"total_pages": 500, "results": [tv_result(1, "A")]}
            )
        )

        list(tmdb.discover_by_provider(TitleKind.SERIES, 8, max_pages=3))

        assert route.call_count == 3

    @respx.mock
    def test_stops_on_an_empty_page(self, tmdb: TmdbClient) -> None:
        respx.get(f"{BASE_URL}/discover/tv").mock(
            return_value=httpx.Response(200, json={"total_pages": 10, "results": []})
        )

        assert list(tmdb.discover_by_provider(TitleKind.SERIES, 8)) == []

    @respx.mock
    def test_filters_by_region_and_provider(self, tmdb: TmdbClient) -> None:
        route = respx.get(f"{BASE_URL}/discover/movie").mock(
            return_value=httpx.Response(200, json={"total_pages": 1, "results": []})
        )

        list(tmdb.discover_by_provider(TitleKind.MOVIE, 337))

        params = route.calls.last.request.url.params
        assert params["watch_region"] == "IL"
        assert params["with_watch_providers"] == "337"


def test_image_url_is_absolute() -> None:
    assert image_url("/abc.jpg", size="w500") == "https://image.tmdb.org/t/p/w500/abc.jpg"


class TestProviderHarvester:
    def _settings(self, **overrides: Any) -> Settings:
        return Settings(_env_file=None, tmdb_api_key="key", **overrides)

    def _ctx(self, http: HttpClient, key: str, **overrides: Any) -> FetchContext:
        return FetchContext(source_key=key, http=http, settings=self._settings(**overrides))

    def test_declares_only_services_tmdb_actually_carries_in_israel(self) -> None:
        """JustWatch does not track the Israeli operators, so they are not here.

        Declaring yes+, HOT, Cellcom TV, Partner TV or Sting TV would produce a
        source that syncs cleanly and stays permanently empty, which reads as a
        working feature with no content rather than as a missing one.
        """
        keys = {source.key for source in TmdbProvidersPlugin().sources()}

        assert {"netflix_il", "prime_video_il", "apple_tv_plus"} <= keys
        assert not keys & {"yes_plus", "hot", "cellcom_tv", "partner_tv", "sting_tv"}
        assert len(keys) == len(PROVIDER_SOURCES)

    @respx.mock
    def test_resolves_the_provider_by_name_then_yields_items(self, http: HttpClient) -> None:
        """Provider ids are resolved at runtime: TMDB renumbers them."""
        respx.get(f"{BASE_URL}/watch/providers/movie").mock(
            return_value=httpx.Response(
                200, json={"results": [{"provider_id": 8, "provider_name": "Netflix"}]}
            )
        )
        respx.get(f"{BASE_URL}/watch/providers/tv").mock(
            return_value=httpx.Response(200, json={"results": []})
        )
        respx.get(f"{BASE_URL}/discover/movie").mock(
            return_value=httpx.Response(
                200, json={"total_pages": 1, "results": [movie_result(11, "Foxtrot")]}
            )
        )

        items = list(TmdbProvidersPlugin().fetch(self._ctx(http, "netflix_il")))

        assert len(items) == 1
        assert items[0].source_key == "netflix_il"
        assert items[0].tmdb_id == 11
        assert items[0].kind is TitleKind.MOVIE
        assert items[0].offer_type is OfferType.STREAM
        assert items[0].poster_url == "https://image.tmdb.org/t/p/w500/p.jpg"

    @respx.mock
    def test_matches_a_provider_whose_tmdb_name_differs_from_ours(self, http: HttpClient) -> None:
        """We call it Prime Video; TMDB calls it Amazon Prime Video."""
        respx.get(f"{BASE_URL}/watch/providers/movie").mock(
            return_value=httpx.Response(
                200,
                json={"results": [{"provider_id": 119, "provider_name": "Amazon Prime Video"}]},
            )
        )
        respx.get(f"{BASE_URL}/watch/providers/tv").mock(
            return_value=httpx.Response(200, json={"results": []})
        )
        route = respx.get(f"{BASE_URL}/discover/movie").mock(
            return_value=httpx.Response(200, json={"total_pages": 1, "results": []})
        )

        list(TmdbProvidersPlugin().fetch(self._ctx(http, "prime_video_il")))

        assert route.calls.last.request.url.params["with_watch_providers"] == "119"

    @respx.mock
    def test_an_unmatched_provider_yields_nothing_without_failing(self, http: HttpClient) -> None:
        """A service may simply carry no titles of a kind in this region."""
        respx.get(f"{BASE_URL}/watch/providers/movie").mock(
            return_value=httpx.Response(200, json={"results": []})
        )
        respx.get(f"{BASE_URL}/watch/providers/tv").mock(
            return_value=httpx.Response(200, json={"results": []})
        )
        ctx = self._ctx(http, "mubi_il")

        assert list(TmdbProvidersPlugin().fetch(ctx)) == []
        assert ctx.error_count == 0

    @respx.mock
    def test_an_unknown_source_key_is_recorded_as_an_error(self, http: HttpClient) -> None:
        ctx = self._ctx(http, "not_a_provider_source")

        assert list(TmdbProvidersPlugin().fetch(ctx)) == []
        assert ctx.error_count == 1

    @respx.mock
    def test_honours_the_configured_page_cap(self, http: HttpClient) -> None:
        respx.get(f"{BASE_URL}/watch/providers/movie").mock(
            return_value=httpx.Response(
                200, json={"results": [{"provider_id": 8, "provider_name": "Netflix"}]}
            )
        )
        respx.get(f"{BASE_URL}/watch/providers/tv").mock(
            return_value=httpx.Response(200, json={"results": []})
        )
        route = respx.get(f"{BASE_URL}/discover/movie").mock(
            return_value=httpx.Response(
                200, json={"total_pages": 99, "results": [movie_result(1, "A")]}
            )
        )
        ctx = self._ctx(http, "netflix_il", sources={"netflix_il": SourceConfig(max_pages=2)})

        list(TmdbProvidersPlugin().fetch(ctx))

        assert route.call_count == 2

    @respx.mock
    def test_a_catalog_larger_than_a_thousand_titles_is_read_whole(self, http: HttpClient) -> None:
        """Netflix reports 4,240 films for Israel and the catalog held 1,000.

        The old default stopped at 50 pages on the reasoning that a cap keeps a
        sync sane. It never did: the walk stops at the catalog's own total_pages
        either way, so all the cap bounded was how much of a big service went
        missing without anybody being told twice.
        """
        respx.get(f"{BASE_URL}/watch/providers/movie").mock(
            return_value=httpx.Response(
                200, json={"results": [{"provider_id": 8, "provider_name": "Netflix"}]}
            )
        )
        respx.get(f"{BASE_URL}/watch/providers/tv").mock(
            return_value=httpx.Response(200, json={"results": []})
        )
        route = respx.get(f"{BASE_URL}/discover/movie").mock(
            return_value=httpx.Response(
                200,
                json={"total_pages": 212, "total_results": 4240, "results": [movie_result(1, "A")]},
            )
        )

        list(TmdbProvidersPlugin().fetch(self._ctx(http, "netflix_il")))

        assert route.call_count == 212

    @respx.mock
    def test_a_small_catalog_costs_only_the_pages_it_has(self, http: HttpClient) -> None:
        """Which is why raising the ceiling is free for everybody else."""
        respx.get(f"{BASE_URL}/watch/providers/movie").mock(
            return_value=httpx.Response(
                200, json={"results": [{"provider_id": 8, "provider_name": "Netflix"}]}
            )
        )
        respx.get(f"{BASE_URL}/watch/providers/tv").mock(
            return_value=httpx.Response(200, json={"results": []})
        )
        route = respx.get(f"{BASE_URL}/discover/movie").mock(
            return_value=httpx.Response(
                200, json={"total_pages": 3, "total_results": 60, "results": [movie_result(1, "A")]}
            )
        )

        list(TmdbProvidersPlugin().fetch(self._ctx(http, "netflix_il")))

        assert route.call_count == 3

    def test_a_missing_api_key_fails_fast(self, http: HttpClient) -> None:
        ctx = FetchContext(source_key="netflix_il", http=http, settings=Settings(_env_file=None))

        with pytest.raises(Exception, match="EIFO_TMDB_API_KEY"):
            list(TmdbProvidersPlugin().fetch(ctx))


class TestTheRateItIsAskedAt:
    """TMDB is the one host every phase leans on, so its pace is the run's pace.

    Measured the way the limiter is measured elsewhere: ask twice, and see how
    long the second call is made to wait.
    """

    @pytest.fixture
    def limited(self) -> Iterator[HttpClient]:
        """A client with the fetcher's real default limit, but a costless sleep."""
        client = HttpClient(rate_limiter=RateLimiter(), sleep=lambda _seconds: None)
        yield client
        client.close()

    def _spacing(self, http: HttpClient, host: str) -> float:
        http.rate_limiter.wait(host, sleep=lambda _s: None, now=lambda: 0.0)
        return http.rate_limiter.wait(host, sleep=lambda _s: None, now=lambda: 0.0)

    def test_a_client_raises_the_api_host_off_the_scraping_default(
        self, limited: HttpClient
    ) -> None:
        TmdbClient(limited, API_KEY)

        assert self._spacing(limited, API_HOST) == pytest.approx(1 / DEFAULT_RATE_LIMIT_RPS)

    def test_a_configured_rate_is_honoured(self, limited: HttpClient) -> None:
        TmdbClient(limited, API_KEY, rate_limit_rps=5.0)

        assert self._spacing(limited, API_HOST) == pytest.approx(0.2)

    def test_other_hosts_keep_the_polite_default(self, limited: HttpClient) -> None:
        """The limit belongs to TMDB, not to every site the fetcher reads."""
        TmdbClient(limited, API_KEY)

        assert self._spacing(limited, "www.mako.co.il") == pytest.approx(1.0)

    def test_the_image_cdn_is_raised_for_the_artwork_phase(
        self, limited: HttpClient, tmp_path: Any
    ) -> None:
        """A static CDN was being asked for one poster a second."""
        settings = Settings(
            _env_file=None,
            db_url="sqlite:///:memory:",
            images_dir=tmp_path,
            tmdb={"rate_limit_rps": 10.0},
        )
        engine = create_engine_from_settings(settings)
        Base.metadata.create_all(engine)
        try:
            fetch_images(make_session_factory(engine), settings, http=limited)
        finally:
            engine.dispose()

        assert self._spacing(limited, IMAGE_HOST) == pytest.approx(0.1)

    @pytest.mark.parametrize(
        "build",
        [
            lambda settings, http: _tmdb_client(http, settings),
            lambda settings, http: _enricher_client_from(
                FetchContext(source_key="enrich", http=http, settings=settings)
            ),
            lambda settings, http: _provider_client_from(
                FetchContext(source_key="netflix_il", http=http, settings=settings)
            ),
        ],
        ids=["runner", "enricher", "provider-harvester"],
    )
    def test_every_place_a_client_is_built_carries_the_setting(
        self, limited: HttpClient, build: Any, tmp_path: Any
    ) -> None:
        """Three call sites today; any one that forgot would cost an hour a night."""
        settings = Settings(
            _env_file=None,
            db_url="sqlite:///:memory:",
            images_dir=tmp_path,
            tmdb_api_key=SecretStr(API_KEY),
            tmdb={"rate_limit_rps": 7.0},
        )

        build(settings, limited)

        assert self._spacing(limited, API_HOST) == pytest.approx(1 / 7.0)


@contextmanager
def caplog_at_warning() -> Iterator[list[str]]:
    """The fetcher's own warnings, for a run that must not fail silently."""
    records: list[str] = []

    class _Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            # getMessage already interpolates the args.
            records.append(record.getMessage())

    handler = _Collect(level=logging.WARNING)
    logger = logging.getLogger("eifo")
    previous = logger.level
    logger.setLevel(logging.WARNING)
    logger.addHandler(handler)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)


class TestAStorefrontIsNotASubscription:
    """Apple TV Store: the same film is rented, sold, or both, and a discover
    listing says which of those it is - nothing.

    The subscription beside it (`apple_tv_plus`, provider "Apple TV") carries
    110 films in Israel; the store carries 17,799. Getting the offer type wrong
    on a catalog that size is the difference between "watch it" and "buy it"
    on every card in the product.
    """

    def _settings(self, **overrides: Any) -> Settings:
        return Settings(_env_file=None, tmdb_api_key="k", db_url="sqlite:///:memory:", **overrides)

    def _ctx(self, http: HttpClient, key: str = "apple_tv_store") -> FetchContext:
        return FetchContext(source_key=key, http=http, settings=self._settings())

    def _providers(self, movie_id: int = 2) -> None:
        respx.get(f"{BASE_URL}/watch/providers/movie").mock(
            return_value=httpx.Response(
                200,
                json={"results": [{"provider_id": movie_id, "provider_name": "Apple TV Store"}]},
            )
        )
        # TMDB lists no series provider for the store in Israel.
        respx.get(f"{BASE_URL}/watch/providers/tv").mock(
            return_value=httpx.Response(200, json={"results": []})
        )

    def _discover(self, *results: dict[str, Any]) -> None:
        respx.get(f"{BASE_URL}/discover/movie").mock(
            return_value=httpx.Response(200, json={"total_pages": 1, "results": list(results)})
        )

    def _offers(self, tmdb_id: int, **buckets: list[dict[str, Any]]) -> None:
        respx.get(f"{BASE_URL}/movie/{tmdb_id}/watch/providers").mock(
            return_value=httpx.Response(200, json={"results": {"IL": buckets}})
        )

    def test_the_store_is_declared_as_a_rent_buy_source(self) -> None:
        store = next(s for s in TmdbProvidersPlugin().sources() if s.key == "apple_tv_store")

        assert store.kind is SourceKind.RENT_BUY
        assert store.name == "Apple TV Store"

    @respx.mock
    def test_a_film_that_is_both_rented_and_sold_yields_both(self, http: HttpClient) -> None:
        self._providers()
        self._discover(movie_result(693134, "Dune: Part Two"))
        self._offers(
            693134,
            rent=[{"provider_id": 2, "provider_name": "Apple TV Store"}],
            buy=[{"provider_id": 2, "provider_name": "Apple TV Store"}],
        )

        items = list(TmdbProvidersPlugin().fetch(self._ctx(http)))

        assert [item.offer_type for item in items] == [OfferType.RENT, OfferType.BUY]
        assert {item.tmdb_id for item in items} == {693134}

    @respx.mock
    def test_a_film_only_sold_is_not_reported_as_rentable(self, http: HttpClient) -> None:
        """The first Dune sells in Israel and does not rent. Saying otherwise
        sends somebody to a shop that will not rent it to them."""
        self._providers()
        self._discover(movie_result(438631, "Dune"))
        self._offers(438631, buy=[{"provider_id": 2, "provider_name": "Apple TV Store"}])

        items = list(TmdbProvidersPlugin().fetch(self._ctx(http)))

        assert [item.offer_type for item in items] == [OfferType.BUY]

    @respx.mock
    def test_another_shops_rental_is_not_this_shops_rental(self, http: HttpClient) -> None:
        """The whole reason for the per-title request.

        TMDB's discover filter reads as "on this provider AND rentable
        somewhere", so a title rented by a different storefront would come back
        as ours. Matching on the resolved provider id is what stops it.
        """
        self._providers()
        self._discover(movie_result(77, "Somebody Else's Rental"))
        self._offers(77, rent=[{"provider_id": 350, "provider_name": "Apple TV"}])

        assert list(TmdbProvidersPlugin().fetch(self._ctx(http))) == []

    @respx.mock
    def test_a_film_offered_no_way_we_sell_yields_nothing(self, http: HttpClient) -> None:
        self._providers()
        self._discover(movie_result(78, "Streaming Only"))
        self._offers(78, flatrate=[{"provider_id": 2, "provider_name": "Apple TV Store"}])

        assert list(TmdbProvidersPlugin().fetch(self._ctx(http))) == []

    @respx.mock
    def test_a_title_offered_nowhere_in_the_region_yields_nothing(self, http: HttpClient) -> None:
        self._providers()
        self._discover(movie_result(79, "Not In Israel"))
        respx.get(f"{BASE_URL}/movie/79/watch/providers").mock(
            return_value=httpx.Response(200, json={"results": {"US": {"buy": []}}})
        )

        assert list(TmdbProvidersPlugin().fetch(self._ctx(http))) == []

    @respx.mock
    def test_an_unreadable_title_is_recorded_and_skipped_not_guessed(
        self, http: HttpClient
    ) -> None:
        """An invented offer is worse than a missing one."""
        self._providers()
        self._discover(movie_result(80, "Unreadable"))
        respx.get(f"{BASE_URL}/movie/80/watch/providers").mock(return_value=httpx.Response(500))
        ctx = self._ctx(http)

        assert list(TmdbProvidersPlugin().fetch(ctx)) == []
        assert ctx.error_count == 1

    @respx.mock
    def test_a_subscription_still_costs_no_extra_request(self, http: HttpClient) -> None:
        """Being on a subscription provider already means it streams there."""
        respx.get(f"{BASE_URL}/watch/providers/movie").mock(
            return_value=httpx.Response(
                200, json={"results": [{"provider_id": 8, "provider_name": "Netflix"}]}
            )
        )
        respx.get(f"{BASE_URL}/watch/providers/tv").mock(
            return_value=httpx.Response(200, json={"results": []})
        )
        self._discover(movie_result(11, "Foxtrot"))
        per_title = respx.get(f"{BASE_URL}/movie/11/watch/providers").mock(
            return_value=httpx.Response(200, json={"results": {}})
        )

        items = list(TmdbProvidersPlugin().fetch(self._ctx(http, "netflix_il")))

        assert [item.offer_type for item in items] == [OfferType.STREAM]
        assert per_title.call_count == 0


class TestACatalogBiggerThanOneListing:
    """TMDB stops paging at 500 pages - 10,000 titles - and that is a limit on
    the query, not on the provider.

    The Apple TV Store is 17,799 films, so one listing reaches the popular half
    and stops. Asked a release year at a time the biggest slice is about a
    thousand, and every film is reachable. A first run against the real API read
    1,000 films and called that the whole store; it was 5.6% of it.
    """

    def _settings(self) -> Settings:
        return Settings(_env_file=None, tmdb_api_key="k", db_url="sqlite:///:memory:")

    def _ctx(self, http: HttpClient, key: str = "apple_tv_store") -> FetchContext:
        return FetchContext(source_key=key, http=http, settings=self._settings())

    def _providers(self) -> None:
        respx.get(f"{BASE_URL}/watch/providers/movie").mock(
            return_value=httpx.Response(
                200, json={"results": [{"provider_id": 2, "provider_name": "Apple TV Store"}]}
            )
        )
        respx.get(f"{BASE_URL}/watch/providers/tv").mock(
            return_value=httpx.Response(200, json={"results": []})
        )

    @respx.mock
    def test_it_asks_for_one_release_year_at_a_time(self, http: HttpClient) -> None:
        self._providers()
        route = respx.get(f"{BASE_URL}/discover/movie").mock(
            return_value=httpx.Response(
                200, json={"total_pages": 1, "total_results": 0, "results": []}
            )
        )

        list(TmdbProvidersPlugin().fetch(self._ctx(http)))

        asked = [call.request.url.params for call in route.calls]
        years = {p["primary_release_year"] for p in asked if "primary_release_year" in p}
        assert len(years) > 60, "expected a slice per year, not one listing"
        # And one bucket for everything before the years start, or the early
        # films would be in no slice at all.
        assert any("primary_release_date.lte" in p for p in asked)

    @respx.mock
    def test_a_subscription_still_asks_once(self, http: HttpClient) -> None:
        """Slicing is for a catalog that cannot be finished, not for every one."""
        respx.get(f"{BASE_URL}/watch/providers/movie").mock(
            return_value=httpx.Response(
                200, json={"results": [{"provider_id": 8, "provider_name": "Netflix"}]}
            )
        )
        respx.get(f"{BASE_URL}/watch/providers/tv").mock(
            return_value=httpx.Response(200, json={"results": []})
        )
        route = respx.get(f"{BASE_URL}/discover/movie").mock(
            return_value=httpx.Response(
                200, json={"total_pages": 1, "total_results": 0, "results": []}
            )
        )

        list(TmdbProvidersPlugin().fetch(self._ctx(http, "netflix_il")))

        assert route.call_count == 1
        assert "primary_release_year" not in route.calls.last.request.url.params

    @respx.mock
    def test_a_film_in_two_slices_costs_one_verification(self, http: HttpClient) -> None:
        """A per-title request is the expensive part; a boundary is not worth one."""
        self._providers()
        respx.get(f"{BASE_URL}/discover/movie").mock(
            return_value=httpx.Response(
                200,
                json={"total_pages": 1, "total_results": 1, "results": [movie_result(5, "Twice")]},
            )
        )
        verify = respx.get(f"{BASE_URL}/movie/5/watch/providers").mock(
            return_value=httpx.Response(
                200,
                json={"results": {"IL": {"buy": [{"provider_id": 2}]}}},
            )
        )

        items = list(TmdbProvidersPlugin().fetch(self._ctx(http)))

        assert len(items) == 1
        assert verify.call_count == 1

    @respx.mock
    def test_a_slice_too_big_for_its_pages_says_so(self, http: HttpClient) -> None:
        """Silence here is a catalog quietly missing its tail."""
        self._providers()
        respx.get(f"{BASE_URL}/discover/movie").mock(
            return_value=httpx.Response(
                200, json={"total_pages": 900, "total_results": 18_000, "results": []}
            )
        )
        ctx = self._ctx(http)

        with caplog_at_warning() as records:
            list(TmdbProvidersPlugin().fetch(ctx))

        assert any("more than" in message for message in records)


class TestAShopThatCannotBeAskedIsNotAnEmptyShop:
    """A title that yields no offer is a title the sweep counts as missing, and
    after two such nights its availability is retired.

    So "we could not ask" must not look like "it is not sold". A run whose
    lookups are all failing has to give up rather than report an empty shop -
    otherwise a bad hour at TMDB retires a storefront two nights later.
    """

    def _settings(self) -> Settings:
        return Settings(_env_file=None, tmdb_api_key="k", db_url="sqlite:///:memory:")

    def _ctx(self, http: HttpClient) -> FetchContext:
        return FetchContext(source_key="apple_tv_store", http=http, settings=self._settings())

    def _providers(self) -> None:
        respx.get(f"{BASE_URL}/watch/providers/movie").mock(
            return_value=httpx.Response(
                200, json={"results": [{"provider_id": 2, "provider_name": "Apple TV Store"}]}
            )
        )
        respx.get(f"{BASE_URL}/watch/providers/tv").mock(
            return_value=httpx.Response(200, json={"results": []})
        )

    @respx.mock
    def test_lookups_that_all_fail_stop_the_run(self, http: HttpClient) -> None:
        """Rather than quietly yielding nothing for every film in the shop."""
        self._providers()
        many = [movie_result(i, f"Film {i}") for i in range(1, 41)]
        respx.get(f"{BASE_URL}/discover/movie").mock(
            return_value=httpx.Response(
                200, json={"total_pages": 1, "total_results": len(many), "results": many}
            )
        )
        respx.get(url__regex=rf"{BASE_URL}/movie/\d+/watch/providers").mock(
            return_value=httpx.Response(503)
        )
        ctx = self._ctx(http)

        with pytest.raises(TooManyErrorsError):
            list(TmdbProvidersPlugin().fetch(ctx))

    @respx.mock
    def test_one_bad_lookup_among_good_ones_does_not(self, http: HttpClient) -> None:
        """A scattered failure is incidental; the streak resets on the next read."""
        self._providers()
        respx.get(f"{BASE_URL}/discover/movie").mock(
            return_value=httpx.Response(
                200,
                json={
                    "total_pages": 1,
                    "total_results": 2,
                    "results": [movie_result(1, "Fine"), movie_result(2, "Broken")],
                },
            )
        )
        sold = {"results": {"IL": {"buy": [{"provider_id": 2}]}}}
        respx.get(f"{BASE_URL}/movie/1/watch/providers").mock(
            return_value=httpx.Response(200, json=sold)
        )
        respx.get(f"{BASE_URL}/movie/2/watch/providers").mock(return_value=httpx.Response(503))
        ctx = self._ctx(http)

        items = list(TmdbProvidersPlugin().fetch(ctx))

        assert [item.tmdb_id for item in items] == [1]
        assert ctx.error_count == 1
