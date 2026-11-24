"""Time Out Tel Aviv: Hebrew review pages with a star rating."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx
import pytest
import respx

from eifo_core.enums import RatingProvider, TitleKind
from eifo_core.settings import Settings
from eifo_fetcher.enrichers import discover_enrichers
from eifo_fetcher.enrichers.base import TitleView
from eifo_fetcher.enrichers.timeout_tlv import (
    BASE_URL,
    TimeOutTelAvivEnricher,
    parse_candidates,
    parse_page,
)
from eifo_fetcher.http import HttpClient, RetryableStatusError
from eifo_fetcher.robots import RobotsPolicy
from eifo_fetcher.sources.base import FetchContext

SLUG = "חולית-ביקורת"
REVIEW_URL = f"{BASE_URL}/{quote(SLUG)}/"
SEARCH_HTML = f"""
<a href="{REVIEW_URL}">חולית</a>
<a href="{BASE_URL}/{quote("משהו-אחר-ביקורת")}/">other</a>
<a href="{BASE_URL}/{quote("חולית-כתבה")}/">not a review</a>
"""
JSON_LD = (
    '<script type="application/ld+json">'
    '{"@graph": [{"@type": "Review", "reviewRating": {"ratingValue": "4", "bestRating": "5"}}]}'
    "</script>"
)


def view(**overrides: Any) -> TitleView:
    values: dict[str, Any] = {
        "id": 1,
        "kind": TitleKind.MOVIE,
        "name_he": "חולית",
        "name_en": "Dune",
        "year": 2021,
        "tmdb_id": None,
        "imdb_id": None,
    }
    values.update(overrides)
    return TitleView(**values)


@pytest.fixture
def ctx(http: HttpClient) -> FetchContext:
    return FetchContext(source_key="enrich", http=http, settings=Settings(_env_file=None))


def enricher(robots: str = "") -> TimeOutTelAvivEnricher:
    result = TimeOutTelAvivEnricher()
    result._robots = RobotsPolicy(user_agent="test", fetch=lambda _url: robots)
    return result


def test_is_on_by_default() -> None:
    assert "timeout_tlv" in [e.key for e in discover_enrichers(Settings(_env_file=None))]


class TestParsing:
    def test_only_matching_review_links_are_candidates(self) -> None:
        assert parse_candidates(SEARCH_HTML, "חולית") == [REVIEW_URL]

    def test_json_ld_stars_are_doubled_onto_ten(self) -> None:
        rating = parse_page(JSON_LD, "u")
        assert rating is not None
        assert rating.provider is RatingProvider.TIMEOUT_TLV
        assert rating.score_raw == 8.0

    def test_star_elements_are_counted_as_a_fallback(self) -> None:
        html = '<i class="star star-full"></i>' * 3 + '<i class="star star-empty"></i>' * 2
        rating = parse_page(html, "u")
        assert rating is not None
        assert rating.score_raw == 6.0

    def test_unreadable_page_has_no_rating(self) -> None:
        assert parse_page("<p>nothing</p>", "u") is None
        assert parse_page('<script type="application/ld+json">{oops</script>', "u") is None

    def test_out_of_range_rating_is_ignored(self) -> None:
        bad = JSON_LD.replace('"4"', '"9"')
        assert parse_page(bad, "u") is None


class TestEnrich:
    @respx.mock
    def test_finds_and_rates(self, ctx: FetchContext) -> None:
        respx.get(url__startswith=f"{BASE_URL}/?s=").mock(
            return_value=httpx.Response(200, text=SEARCH_HTML)
        )
        respx.get(REVIEW_URL).mock(return_value=httpx.Response(200, text=JSON_LD))

        result = enricher().enrich(view(), ctx)

        assert result is not None
        assert result.ratings[0].score_raw == 8.0
        assert result.ratings[0].url == REVIEW_URL

    @respx.mock
    def test_no_review_found(self, ctx: FetchContext) -> None:
        respx.get(url__startswith=f"{BASE_URL}/?s=").mock(
            return_value=httpx.Response(200, text="<p>nothing</p>")
        )
        assert enricher().enrich(view(), ctx) is None

    def test_title_without_hebrew_name_is_skipped(self, ctx: FetchContext) -> None:
        with respx.mock:
            assert enricher().enrich(view(name_he=None), ctx) is None

    @respx.mock
    def test_server_error_propagates(self, ctx: FetchContext) -> None:
        respx.get(url__startswith=f"{BASE_URL}/?s=").mock(return_value=httpx.Response(500))
        with pytest.raises(RetryableStatusError):
            enricher().enrich(view(), ctx)

    def test_robots_disallow_means_no_request(self, ctx: FetchContext) -> None:
        with respx.mock:
            assert enricher("User-agent: *\nDisallow: /\n").enrich(view(), ctx) is None
