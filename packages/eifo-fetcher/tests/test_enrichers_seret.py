"""The Seret enricher, parsed from a recorded page.

The fixture carries the real JSON-LD shape captured from seret.co.il, including
its windows-1255 encoding, so an encoding or schema change surfaces here.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx
from recorded import FIXTURES

from eifo_core.enums import RatingProvider, TitleKind
from eifo_core.settings import Settings
from eifo_fetcher.enrichers.base import TitleView
from eifo_fetcher.enrichers.seret import (
    MOVIE_URL,
    SEARCH_URL,
    SERIES_URL,
    SeretEnricher,
    imdb_id_of,
    parse_movie,
)
from eifo_fetcher.http import HttpClient
from eifo_fetcher.sources.base import FetchContext


def fixture_bytes(name: str) -> bytes:
    """Seret pages are windows-1255, so they are read as bytes."""
    return (FIXTURES / "seret" / name).read_bytes()


def decoded(name: str) -> str:
    return fixture_bytes(name).decode("windows-1255")


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


def _mock_seret() -> None:
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            content=fixture_bytes("search.html"),
            headers={"Content-Type": "text/html; Charset=windows-1255"},
        )
    )
    respx.get(MOVIE_URL).mock(
        return_value=httpx.Response(
            200,
            content=fixture_bytes("movie.html"),
            headers={"Content-Type": "text/html; Charset=windows-1255"},
        )
    )


class TestParseMovie:
    def test_finds_the_movie_node_in_the_graph(self) -> None:
        movie = parse_movie(decoded("movie.html"))

        assert movie is not None
        assert movie["name"] == "פוקסטרוט"
        assert movie["alternateName"] == "Foxtrot"

    def test_returns_none_without_json_ld(self) -> None:
        assert parse_movie("<html><body>nothing</body></html>") is None

    def test_ignores_unparsable_json_ld(self) -> None:
        html = '<script type="application/ld+json">{not json</script>'

        assert parse_movie(html) is None

    def test_reads_a_bare_object_as_well_as_a_graph(self) -> None:
        html = '<script type="application/ld+json">{"@type":"Movie","name":"x"}</script>'

        movie = parse_movie(html)

        assert movie is not None
        assert movie["name"] == "x"


class TestEnrich:
    @respx.mock
    def test_returns_both_israeli_scores(self, ctx: FetchContext) -> None:
        _mock_seret()

        result = SeretEnricher().enrich(view(), ctx)

        assert result is not None
        by_provider = {rating.provider: rating for rating in result.ratings}
        assert by_provider[RatingProvider.SERET_VIEWERS].score_raw == 9.1
        assert by_provider[RatingProvider.SERET_VIEWERS].vote_count == 42
        assert by_provider[RatingProvider.SERET_CRITICS].score_raw == 6.8

    @respx.mock
    def test_links_back_to_the_seret_page(self, ctx: FetchContext) -> None:
        """A score is never shown without a link to where it came from."""
        _mock_seret()

        result = SeretEnricher().enrich(view(), ctx)

        assert result is not None
        assert all("seret.co.il" in (rating.url or "") for rating in result.ratings)

    @respx.mock
    def test_decodes_hebrew_from_windows_1255(self, ctx: FetchContext) -> None:
        """The site predates UTF-8; a wrong decode would break every match."""
        _mock_seret()

        assert SeretEnricher().enrich(view(name_en=None), ctx) is not None

    @respx.mock
    def test_rejects_a_film_from_the_wrong_year(self, ctx: FetchContext) -> None:
        _mock_seret()

        assert SeretEnricher().enrich(view(year=1998), ctx) is None

    @respx.mock
    def test_rejects_a_film_with_an_unrelated_name(self, ctx: FetchContext) -> None:
        _mock_seret()

        assert SeretEnricher().enrich(view(name_he="טהרן", name_en="Tehran"), ctx) is None

    @respx.mock
    def test_a_title_absent_from_seret_is_not_an_error(self, ctx: FetchContext) -> None:
        """Foreign titles are routinely missing; that is ordinary."""
        respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, content=b"<html></html>"))

        assert SeretEnricher().enrich(view(), ctx) is None
        assert ctx.error_count == 0

    @respx.mock
    def test_a_search_failure_is_recorded(self, ctx: FetchContext) -> None:
        respx.get(SEARCH_URL).mock(return_value=httpx.Response(500))

        assert SeretEnricher().enrich(view(), ctx) is None
        assert ctx.error_count == 1

    def test_a_title_with_no_name_is_skipped(self, ctx: FetchContext) -> None:
        assert SeretEnricher().enrich(view(name_he=None, name_en=None), ctx) is None


class TestScoreScaling:
    def test_a_non_ten_best_rating_is_rescaled(self) -> None:
        """schema.org allows any scale; trust bestRating rather than assuming."""
        html = (
            '<script type="application/ld+json">'
            '{"@type":"Movie","name":"x","aggregateRating":'
            '{"ratingValue":4,"bestRating":5,"ratingCount":10}}'
            "</script>"
        )
        movie = parse_movie(html)

        assert movie is not None
        from eifo_fetcher.enrichers.seret import _viewer_rating

        rating = _viewer_rating(movie, "https://example.com")
        assert rating is not None
        assert rating.score_raw == 8.0

    def test_a_missing_aggregate_rating_yields_nothing(self) -> None:
        from eifo_fetcher.enrichers.seret import _viewer_rating

        assert _viewer_rating({"name": "x"}, "https://example.com") is None


class TestSeriesSupport:
    def test_series_pages_declare_a_different_type(self) -> None:
        """Accepting only "Movie" would silently lose every series."""
        html = '<script type="application/ld+json">{"@type":"TVSeries","name":"פאודה"}</script>'

        movie = parse_movie(html)

        assert movie is not None
        assert movie["name"] == "פאודה"

    @respx.mock
    def test_a_series_is_fetched_from_the_series_endpoint(self, ctx: FetchContext) -> None:
        """Films and series live at different endpoints with different ids."""
        respx.get(SEARCH_URL).mock(
            return_value=httpx.Response(200, content=fixture_bytes("search.html"))
        )
        series = respx.get(SERIES_URL).mock(
            return_value=httpx.Response(200, content=fixture_bytes("movie.html"))
        )

        SeretEnricher().enrich(view(kind=TitleKind.SERIES), ctx)

        assert series.called
        assert "SID" in str(series.calls.last.request.url)


class TestImdbMatching:
    def test_reads_the_imdb_id_from_same_as(self) -> None:
        movie = {"sameAs": ["https://www.imdb.com/title/tt6896536/"]}

        assert imdb_id_of(movie) == "tt6896536"

    def test_tolerates_a_bare_string(self) -> None:
        assert imdb_id_of({"sameAs": "https://www.imdb.com/title/tt123456/"}) == "tt123456"

    def test_returns_none_without_a_link(self) -> None:
        assert imdb_id_of({"name": "x"}) is None

    @respx.mock
    def test_a_matching_imdb_id_settles_identity(self, ctx: FetchContext) -> None:
        """A shared id is decisive, so no name comparison can get it wrong."""
        page = (
            '<script type="application/ld+json">'
            '{"@type":"Movie","name":"שם אחר לגמרי","datePublished":"1990",'
            '"sameAs":["https://www.imdb.com/title/tt6896536/"],'
            '"aggregateRating":{"ratingValue":8.0,"bestRating":10,"ratingCount":5}}'
            "</script>"
        ).encode("windows-1255")
        respx.get(SEARCH_URL).mock(
            return_value=httpx.Response(200, content=fixture_bytes("search.html"))
        )
        respx.get(MOVIE_URL).mock(return_value=httpx.Response(200, content=page))

        result = SeretEnricher().enrich(view(imdb_id="tt6896536"), ctx)

        assert result is not None

    @respx.mock
    def test_a_conflicting_imdb_id_is_rejected(self, ctx: FetchContext) -> None:
        page = (
            '<script type="application/ld+json">'
            '{"@type":"Movie","name":"פוקסטרוט","datePublished":"2017",'
            '"sameAs":["https://www.imdb.com/title/tt0000001/"],'
            '"aggregateRating":{"ratingValue":8.0,"bestRating":10}}'
            "</script>"
        ).encode("windows-1255")
        respx.get(SEARCH_URL).mock(
            return_value=httpx.Response(200, content=fixture_bytes("search.html"))
        )
        respx.get(MOVIE_URL).mock(return_value=httpx.Response(200, content=page))

        assert SeretEnricher().enrich(view(imdb_id="tt6896536"), ctx) is None
