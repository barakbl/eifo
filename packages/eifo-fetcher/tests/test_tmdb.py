"""The TMDB client and the provider harvester."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx
from pydantic import SecretStr

from eifo_core.db import create_engine_from_settings, make_session_factory
from eifo_core.enums import OfferType, TitleKind
from eifo_core.models import Base
from eifo_core.settings import Settings, SourceConfig
from eifo_fetcher.enrichers.tmdb_meta import _client_from as _enricher_client_from
from eifo_fetcher.http import HttpClient, RateLimiter
from eifo_fetcher.runner import _tmdb_client, fetch_images
from eifo_fetcher.sources.base import FetchContext
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
