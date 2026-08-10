"""The Rotten Tomatoes enricher, parsed from a recorded page."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx
from recorded import load_fixture

from eifo_core.enums import RatingProvider, TitleKind
from eifo_core.settings import Settings
from eifo_fetcher.enrichers.base import TitleView
from eifo_fetcher.enrichers.rt import (
    BASE_URL,
    RottenTomatoesEnricher,
    candidate_urls,
    parse_scorecard,
    slugify,
)
from eifo_fetcher.http import HttpClient
from eifo_fetcher.sources.base import FetchContext


def view(**overrides: Any) -> TitleView:
    values: dict[str, Any] = {
        "id": 1,
        "kind": TitleKind.MOVIE,
        "name_he": "פוקסטרוט",
        "name_en": "Foxtrot",
        "year": 2017,
        "tmdb_id": None,
        "imdb_id": None,
    }
    values.update(overrides)
    return TitleView(**values)


@pytest.fixture
def ctx(http: HttpClient) -> FetchContext:
    return FetchContext(source_key="enrich", http=http, settings=Settings(_env_file=None))


class TestSlugify:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("Foxtrot", "foxtrot"),
            ("The Band's Visit", "the_band_s_visit"),
            ("Waltz with Bashir", "waltz_with_bashir"),
            ("Amélie", "amelie"),
            ("WALL·E", "wall_e"),
        ],
    )
    def test_produces_rt_style_slugs(self, name: str, expected: str) -> None:
        assert slugify(name) == expected


class TestCandidateUrls:
    def test_tries_the_bare_slug_then_the_dated_one(self) -> None:
        urls = candidate_urls(view())

        assert urls == [f"{BASE_URL}/m/foxtrot", f"{BASE_URL}/m/foxtrot_2017"]

    def test_series_use_the_tv_section(self) -> None:
        urls = candidate_urls(view(kind=TitleKind.SERIES, name_en="Fauda", year=None))

        assert urls == [f"{BASE_URL}/tv/fauda"]

    def test_a_hebrew_only_title_has_no_candidates(self) -> None:
        """RT slugs are ASCII, so there is nothing to build one from."""
        assert candidate_urls(view(name_en=None)) == []

    def test_a_hebrew_string_in_the_english_field_is_rejected(self) -> None:
        assert candidate_urls(view(name_en="פוקסטרוט")) == []

    def test_a_name_with_no_ascii_content_yields_nothing(self) -> None:
        assert candidate_urls(view(name_en="!!!")) == []


class TestParseScorecard:
    def test_reads_the_embedded_payload(self) -> None:
        scorecard = parse_scorecard(load_fixture("rt", "movie.html"))

        assert scorecard is not None
        assert scorecard["criticsScore"]["score"] == "94"

    def test_returns_none_when_absent(self) -> None:
        assert parse_scorecard("<html><body>nothing</body></html>") is None

    def test_ignores_unparsable_payloads(self) -> None:
        html = '<script id="media-scorecard-json" type="application/json">{oops</script>'

        assert parse_scorecard(html) is None


class TestEnrich:
    @respx.mock
    def test_returns_both_scores_as_percentages(self, ctx: FetchContext) -> None:
        respx.get(f"{BASE_URL}/m/foxtrot").mock(
            return_value=httpx.Response(200, text=load_fixture("rt", "movie.html"))
        )

        result = RottenTomatoesEnricher().enrich(view(), ctx)

        assert result is not None
        by_provider = {rating.provider: rating for rating in result.ratings}
        assert by_provider[RatingProvider.RT_CRITICS].score_raw == 94.0
        assert by_provider[RatingProvider.RT_CRITICS].vote_count == 141
        assert by_provider[RatingProvider.RT_AUDIENCE].score_raw == 76.0
        # ratingCount is null on audience blocks; reviewCount is the real figure.
        assert by_provider[RatingProvider.RT_AUDIENCE].vote_count == 35

    @respx.mock
    def test_falls_back_to_the_dated_slug(self, ctx: FetchContext) -> None:
        respx.get(f"{BASE_URL}/m/foxtrot").mock(return_value=httpx.Response(404))
        respx.get(f"{BASE_URL}/m/foxtrot_2017").mock(
            return_value=httpx.Response(200, text=load_fixture("rt", "movie.html"))
        )

        result = RottenTomatoesEnricher().enrich(view(), ctx)

        assert result is not None
        assert len(result.ratings) == 2

    @respx.mock
    def test_a_missing_film_is_not_an_error(self, ctx: FetchContext) -> None:
        """Israeli titles are routinely absent from RT; a 404 is a clean answer."""
        respx.get(url__startswith=f"{BASE_URL}/m/").mock(return_value=httpx.Response(404))

        assert RottenTomatoesEnricher().enrich(view(), ctx) is None
        assert ctx.error_count == 0

    @respx.mock
    def test_a_server_error_is_recorded(self, ctx: FetchContext) -> None:
        respx.get(url__startswith=f"{BASE_URL}/m/").mock(return_value=httpx.Response(500))

        assert RottenTomatoesEnricher().enrich(view(), ctx) is None
        assert ctx.error_count > 0

    @respx.mock
    def test_a_hidden_audience_score_is_respected(self, ctx: FetchContext) -> None:
        """If RT declines to show a figure, we do not publish one either."""
        payload = (
            '<script id="media-scorecard-json" type="application/json">'
            '{"criticsScore":{"score":"90","ratingCount":10},'
            '"audienceScore":{"score":"50","reviewCount":5},'
            '"hideAudienceScore":true}</script>'
        )
        respx.get(f"{BASE_URL}/m/foxtrot").mock(return_value=httpx.Response(200, text=payload))

        result = RottenTomatoesEnricher().enrich(view(), ctx)

        assert result is not None
        assert [rating.provider for rating in result.ratings] == [RatingProvider.RT_CRITICS]

    @respx.mock
    def test_a_page_without_scores_yields_nothing(self, ctx: FetchContext) -> None:
        respx.get(url__startswith=f"{BASE_URL}/m/").mock(
            return_value=httpx.Response(200, text="<html><body>no scores</body></html>")
        )

        assert RottenTomatoesEnricher().enrich(view(), ctx) is None

    def test_a_title_rt_cannot_address_is_skipped_without_a_request(
        self, ctx: FetchContext
    ) -> None:
        """No English name means no slug, so there is nothing to fetch."""
        assert RottenTomatoesEnricher().enrich(view(name_en=None), ctx) is None
