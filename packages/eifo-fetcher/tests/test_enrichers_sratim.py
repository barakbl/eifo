"""Sratim: user rating, resolved through search and confirmed by IMDb id."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

from eifo_core.enums import RatingProvider, TitleKind
from eifo_core.settings import Settings
from eifo_fetcher.enrichers import DISABLED_BY_DEFAULT, discover_enrichers
from eifo_fetcher.enrichers.base import TitleView
from eifo_fetcher.enrichers.sratim import (
    BASE_URL,
    SratimEnricher,
    parse_candidates,
    parse_page,
)
from eifo_fetcher.http import HttpClient, RetryableStatusError
from eifo_fetcher.robots import RobotsPolicy
from eifo_fetcher.sources.base import FetchContext

SEARCH = f"{BASE_URL}/search.php"

SEARCH_HTML = """
<div><a href="/tt1172880/"><b>חולית: חלק שלישי</b></a> (2026)</div>
<div><a href="/tt151248/"><b>חולית</b></a> (2021)</div>
<div><a href="/nm2302/">Somebody</a></div>
"""


def page_html(imdb: str = "tt1160419", score: str = "6.7", votes: str = "21") -> str:
    return f"""
<html><body><script>var x = "10/ 1 5 הצבעות";</script>
<div class="rate"><span>10/</span> <b>{score}</b> <small>{votes} הצבעות</small></div>
<a href="https://www.imdb.com/title/{imdb}">חולית באתר IMDb</a>
<div><span>10/</span> <b>8.0</b> <small>647.3K הצבעות</small></div>
</body></html>
"""


def view(**overrides: Any) -> TitleView:
    values: dict[str, Any] = {
        "id": 1,
        "kind": TitleKind.MOVIE,
        "name_he": "חולית",
        "name_en": "Dune",
        "year": 2021,
        "tmdb_id": None,
        "imdb_id": "tt1160419",
    }
    values.update(overrides)
    return TitleView(**values)


@pytest.fixture
def ctx(http: HttpClient) -> FetchContext:
    return FetchContext(source_key="enrich", http=http, settings=Settings(_env_file=None))


def enricher(robots: str = "") -> SratimEnricher:
    result = SratimEnricher()
    result._robots = RobotsPolicy(user_agent="test", fetch=lambda _url: robots)
    return result


def test_is_off_by_default() -> None:
    assert "sratim" in DISABLED_BY_DEFAULT
    keys = [e.key for e in discover_enrichers(Settings(_env_file=None))]
    assert "sratim" not in keys
    enabled = Settings(_env_file=None, enrich={"enabled": ["sratim"]})
    assert "sratim" in [e.key for e in discover_enrichers(enabled)]


class TestParsing:
    def test_candidates_are_filtered_by_year(self) -> None:
        assert parse_candidates(SEARCH_HTML, 2021) == ["/tt151248/"]

    def test_candidates_keep_everything_without_a_year(self) -> None:
        assert parse_candidates(SEARCH_HTML, None) == ["/tt1172880/", "/tt151248/"]

    def test_rating_and_votes(self) -> None:
        rating = parse_page(page_html(), "tt1160419", "u")
        assert rating is not None
        assert (rating.provider, rating.score_raw, rating.vote_count) == (
            RatingProvider.SRATIM,
            6.7,
            21,
        )

    def test_k_suffix_votes(self) -> None:
        rating = parse_page(page_html(votes="1.5K"), "tt1160419", "u")
        assert rating is not None
        assert rating.vote_count == 1500

    def test_no_rating_yet(self) -> None:
        assert parse_page(page_html(score="?", votes="0"), "tt1160419", "u") is None

    def test_imdb_mismatch_is_rejected(self) -> None:
        assert parse_page(page_html(imdb="tt9999999"), "tt1160419", "u") is None

    def test_page_without_imdb_link_is_rejected(self) -> None:
        assert parse_page("<p>10/ 6.7 21 הצבעות</p>", "tt1160419", "u") is None


class TestEnrich:
    @respx.mock
    def test_finds_and_rates(self, ctx: FetchContext) -> None:
        respx.get(SEARCH).mock(return_value=httpx.Response(200, text=SEARCH_HTML))
        respx.get(f"{BASE_URL}/tt151248/").mock(return_value=httpx.Response(200, text=page_html()))

        result = enricher().enrich(view(), ctx)

        assert result is not None
        assert result.ratings[0].score_raw == 6.7
        assert result.ratings[0].url == f"{BASE_URL}/tt151248/"

    @respx.mock
    def test_wrong_film_yields_nothing(self, ctx: FetchContext) -> None:
        respx.get(SEARCH).mock(return_value=httpx.Response(200, text=SEARCH_HTML))
        respx.get(f"{BASE_URL}/tt151248/").mock(
            return_value=httpx.Response(200, text=page_html(imdb="tt1"))
        )
        assert enricher().enrich(view(), ctx) is None

    def test_no_imdb_id_means_no_request(self, ctx: FetchContext) -> None:
        with respx.mock:  # any request would raise
            assert enricher().enrich(view(imdb_id=None), ctx) is None

    @respx.mock
    def test_server_error_propagates(self, ctx: FetchContext) -> None:
        respx.get(SEARCH).mock(return_value=httpx.Response(500))
        with pytest.raises(RetryableStatusError):
            enricher().enrich(view(), ctx)

    def test_robots_disallow_means_no_request(self, ctx: FetchContext) -> None:
        with respx.mock:
            assert enricher("User-agent: *\nDisallow: /\n").enrich(view(), ctx) is None
