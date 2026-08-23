"""The TMDB metadata enricher."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

from eifo_core.enums import CreditRole, RatingProvider, TitleKind
from eifo_core.settings import Settings
from eifo_fetcher.enrichers.base import TitleView
from eifo_fetcher.enrichers.tmdb_meta import TmdbMetadataEnricher, _countries, web_url
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
        "original_language": "he",
        "origin_country": ["IL"],
        "credits": {
            "crew": [
                {"id": 11, "name": "Assaf Bernstein", "job": "Director"},
                {"id": 12, "name": "Boaz Yehonatan Yacov", "job": "Director of Photography"},
                {"id": 13, "name": "Someone Else", "job": "Best Boy"},
            ],
            "cast": [
                {"id": 21, "name": "Lior Raz", "character": "Doron", "order": 0},
                {"id": 22, "name": "Itzik Cohen", "character": "Gabi", "order": 1},
            ],
        },
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


class TestCredits:
    """Who made it rides along on the details call, at no extra request."""

    @respx.mock
    def test_reads_director_cinematographer_and_cast(
        self, enricher: TmdbMetadataEnricher, ctx: FetchContext
    ) -> None:
        _mock_details()

        result = enricher.enrich(view(), ctx)

        assert result is not None
        credits = result.metadata_patch["credits"]
        by_role: dict[CreditRole, list[str]] = {}
        for entry in credits:
            by_role.setdefault(entry["role"], []).append(entry["name_en"])
        assert by_role[CreditRole.DIRECTOR] == ["Assaf Bernstein"]
        assert by_role[CreditRole.CINEMATOGRAPHER] == ["Boaz Yehonatan Yacov"]
        assert by_role[CreditRole.CAST] == ["Lior Raz", "Itzik Cohen"]

    @respx.mock
    def test_ignores_crew_jobs_nobody_scans_a_page_for(
        self, enricher: TmdbMetadataEnricher, ctx: FetchContext
    ) -> None:
        _mock_details()

        result = enricher.enrich(view(), ctx)

        assert result is not None
        names = {entry["name_en"] for entry in result.metadata_patch["credits"]}
        assert "Someone Else" not in names

    @respx.mock
    def test_keeps_the_lead_at_the_top_of_the_bill(
        self, enricher: TmdbMetadataEnricher, ctx: FetchContext
    ) -> None:
        """Billing zero is the lead, not a missing value."""
        _mock_details()

        result = enricher.enrich(view(), ctx)

        assert result is not None
        cast = [e for e in result.metadata_patch["credits"] if e["role"] is CreditRole.CAST]
        assert [(e["name_en"], e["billing_order"], e["character"]) for e in cast] == [
            ("Lior Raz", 0, "Doron"),
            ("Itzik Cohen", 1, "Gabi"),
        ]

    @respx.mock
    def test_asks_for_credits_on_the_details_request_it_already_makes(
        self, enricher: TmdbMetadataEnricher, ctx: FetchContext
    ) -> None:
        _mock_details()

        enricher.enrich(view(), ctx)

        appended = [
            call.request.url.params.get("append_to_response")
            for call in respx.calls
            if str(call.request.url).startswith(f"{BASE_URL}/tv/{TMDB_ID}?")
        ]
        assert "credits" in appended

    @respx.mock
    def test_reads_language_and_origin_country(
        self, enricher: TmdbMetadataEnricher, ctx: FetchContext
    ) -> None:
        _mock_details()

        result = enricher.enrich(view(), ctx)

        assert result is not None
        assert result.metadata_patch["original_language"] == "he"
        assert result.metadata_patch["origin_countries"] == "IL"

    @respx.mock
    def test_a_title_without_credits_offers_none(
        self, enricher: TmdbMetadataEnricher, ctx: FetchContext
    ) -> None:
        payload = tv_details("en-US")
        payload.pop("credits")
        respx.get(f"{BASE_URL}/tv/{TMDB_ID}").mock(return_value=httpx.Response(200, json=payload))
        respx.get(f"{BASE_URL}/tv/{TMDB_ID}/external_ids").mock(
            return_value=httpx.Response(200, json={})
        )

        result = enricher.enrich(view(), ctx)

        assert result is not None
        assert "credits" not in result.metadata_patch


class TestProductionCountries:
    def test_a_co_production_keeps_every_country_in_order(self) -> None:
        assert (
            _countries({"production_countries": [{"iso_3166_1": "IL"}, {"iso_3166_1": "FR"}]})
            == "IL,FR"
        )

    def test_a_country_listed_twice_appears_once(self) -> None:
        assert (
            _countries({"production_countries": [{"iso_3166_1": "IL"}], "origin_country": ["IL"]})
            == "IL"
        )

    def test_no_country_is_no_claim(self) -> None:
        assert _countries({}) is None
