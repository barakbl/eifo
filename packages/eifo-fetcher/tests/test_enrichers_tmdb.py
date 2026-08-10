"""The TMDB metadata enricher."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

from eifo_core.enums import RatingProvider, TitleKind
from eifo_core.settings import Settings
from eifo_fetcher.enrichers.base import TitleView
from eifo_fetcher.enrichers.tmdb_meta import TmdbMetadataEnricher, web_url
from eifo_fetcher.http import HttpClient
from eifo_fetcher.sources.base import FetchContext
from eifo_fetcher.tmdb import BASE_URL, TmdbClient

TMDB_ID = 66119


def view(**overrides: Any) -> TitleView:
    values: dict[str, Any] = {
        "id": 1,
        "kind": TitleKind.SERIES,
        "name_he": "פאודה",
        "name_en": None,
        "year": 2015,
        "tmdb_id": TMDB_ID,
        "imdb_id": None,
    }
    values.update(overrides)
    return TitleView(**values)


def tv_details(language: str) -> dict[str, Any]:
    """Shaped like TMDB's /tv/{id} response for each language."""
    hebrew = language.startswith("he")
    return {
        "id": TMDB_ID,
        "name": "פאודה" if hebrew else "Fauda",
        "original_name": "פאודה",
        "overview": "יחידה מסתערבת." if hebrew else "An undercover unit.",
        "first_air_date": "2015-02-15",
        "number_of_seasons": 4,
        "episode_run_time": [45],
        "status": "Returning Series",
        "vote_average": 7.8,
        "vote_count": 512,
        "poster_path": "/poster.jpg",
        "genres": [
            {"id": 18, "name": "דרמה" if hebrew else "Drama"},
            {"id": 10759, "name": "אקשן והרפתקאות" if hebrew else "Action & Adventure"},
        ],
    }


@pytest.fixture
def ctx(http: HttpClient) -> FetchContext:
    return FetchContext(
        source_key="enrich",
        http=http,
        settings=Settings(_env_file=None, tmdb_api_key="key"),
    )


@pytest.fixture
def enricher(http: HttpClient) -> TmdbMetadataEnricher:
    return TmdbMetadataEnricher(TmdbClient(http, "key"))


def _mock_details() -> None:
    respx.get(f"{BASE_URL}/tv/{TMDB_ID}").mock(
        side_effect=lambda request: httpx.Response(
            200, json=tv_details(request.url.params.get("language", "en-US"))
        )
    )
    respx.get(f"{BASE_URL}/tv/{TMDB_ID}/external_ids").mock(
        return_value=httpx.Response(200, json={"imdb_id": "tt4565380"})
    )


class TestRatings:
    @respx.mock
    def test_returns_tmdbs_own_score(
        self, enricher: TmdbMetadataEnricher, ctx: FetchContext
    ) -> None:
        _mock_details()

        result = enricher.enrich(view(), ctx)

        assert result is not None
        [rating] = result.ratings
        assert rating.provider is RatingProvider.TMDB
        assert rating.score_raw == 7.8
        assert rating.vote_count == 512
        assert rating.url == web_url(TitleKind.SERIES, TMDB_ID)

    @respx.mock
    def test_an_unrated_title_yields_no_rating(
        self, enricher: TmdbMetadataEnricher, ctx: FetchContext
    ) -> None:
        """TMDB reports 0 for titles nobody has voted on; that is not a score."""
        payload = tv_details("en-US") | {"vote_average": 0}
        respx.get(f"{BASE_URL}/tv/{TMDB_ID}").mock(return_value=httpx.Response(200, json=payload))
        respx.get(f"{BASE_URL}/tv/{TMDB_ID}/external_ids").mock(
            return_value=httpx.Response(200, json={})
        )

        result = enricher.enrich(view(), ctx)

        assert result is not None
        assert result.ratings == []


class TestMetadata:
    @respx.mock
    def test_fills_both_languages(self, enricher: TmdbMetadataEnricher, ctx: FetchContext) -> None:
        _mock_details()

        result = enricher.enrich(view(), ctx)

        assert result is not None
        patch = result.metadata_patch
        assert patch["name_he"] == "פאודה"
        assert patch["name_en"] == "Fauda"
        assert patch["overview_he"] == "יחידה מסתערבת."
        assert patch["overview_en"] == "An undercover unit."

    @respx.mock
    def test_fills_the_imdb_id_the_dataset_join_needs(
        self, enricher: TmdbMetadataEnricher, ctx: FetchContext
    ) -> None:
        _mock_details()

        result = enricher.enrich(view(), ctx)

        assert result is not None
        assert result.metadata_patch["imdb_id"] == "tt4565380"

    @respx.mock
    def test_fills_series_shape(self, enricher: TmdbMetadataEnricher, ctx: FetchContext) -> None:
        _mock_details()

        result = enricher.enrich(view(), ctx)

        assert result is not None
        patch = result.metadata_patch
        assert patch["year"] == 2015
        assert patch["seasons"] == 4
        assert patch["runtime_minutes"] == 45
        assert patch["status"] == "Returning Series"

    @respx.mock
    def test_offers_localised_genres(
        self, enricher: TmdbMetadataEnricher, ctx: FetchContext
    ) -> None:
        _mock_details()

        result = enricher.enrich(view(), ctx)

        assert result is not None
        genres = result.metadata_patch["genres"]
        assert {"tmdb_id": 18, "name_en": "Drama", "name_he": "דרמה"} in genres

    @respx.mock
    def test_offers_an_artwork_url(self, enricher: TmdbMetadataEnricher, ctx: FetchContext) -> None:
        _mock_details()

        result = enricher.enrich(view(), ctx)

        assert result is not None
        assert result.metadata_patch["poster_source_url"].endswith("/poster.jpg")

    @respx.mock
    def test_a_latin_name_is_never_stored_as_hebrew(
        self, enricher: TmdbMetadataEnricher, ctx: FetchContext
    ) -> None:
        """TMDB falls back to the original title when a translation is missing."""
        payload = tv_details("en-US")
        respx.get(f"{BASE_URL}/tv/{TMDB_ID}").mock(return_value=httpx.Response(200, json=payload))
        respx.get(f"{BASE_URL}/tv/{TMDB_ID}/external_ids").mock(
            return_value=httpx.Response(200, json={})
        )

        result = enricher.enrich(view(), ctx)

        assert result is not None
        assert "name_he" not in result.metadata_patch
        assert result.metadata_patch["name_en"] == "Fauda"


class TestResolution:
    @respx.mock
    def test_searches_when_the_title_has_no_tmdb_id(
        self, enricher: TmdbMetadataEnricher, ctx: FetchContext
    ) -> None:
        respx.get(f"{BASE_URL}/search/tv").mock(
            return_value=httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "id": TMDB_ID,
                            "name": "פאודה",
                            "original_name": "פאודה",
                            "first_air_date": "2015-02-15",
                        }
                    ]
                },
            )
        )
        _mock_details()

        result = enricher.enrich(view(tmdb_id=None), ctx)

        assert result is not None
        assert result.metadata_patch["tmdb_id"] == TMDB_ID

    @respx.mock
    def test_an_unconvincing_search_hit_is_rejected(
        self, enricher: TmdbMetadataEnricher, ctx: FetchContext
    ) -> None:
        respx.get(f"{BASE_URL}/search/tv").mock(
            return_value=httpx.Response(
                200,
                json={"results": [{"id": 999, "name": "Tehran", "first_air_date": "2020-06-25"}]},
            )
        )

        assert enricher.enrich(view(tmdb_id=None), ctx) is None

    @respx.mock
    def test_a_failed_lookup_is_recorded_not_raised(
        self, enricher: TmdbMetadataEnricher, ctx: FetchContext
    ) -> None:
        respx.get(f"{BASE_URL}/tv/{TMDB_ID}").mock(return_value=httpx.Response(500))

        assert enricher.enrich(view(), ctx) is None
        assert ctx.error_count == 1


def test_web_url_points_at_the_public_page() -> None:
    assert web_url(TitleKind.MOVIE, 5) == "https://www.themoviedb.org/movie/5"
    assert web_url(TitleKind.SERIES, 5) == "https://www.themoviedb.org/tv/5"
