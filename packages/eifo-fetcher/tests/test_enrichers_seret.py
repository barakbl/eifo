"""The Seret enricher, parsed from recorded pages.

The fixtures carry the real JSON-LD shape captured from seret.co.il, including
its windows-1255 encoding, so an encoding or schema change surfaces here. The
autocomplete fixture is the shape the site actually serves - lowercase ``mid=``
inside an ``onclick``, films and series mixed in one response - because a
pattern that only matched the tidier form is exactly the bug this replaced.
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
    AUTOCOMPLETE_URL,
    BASE_URL,
    HOST,
    MOVIE_URL,
    SERIES_URL,
    SeretEnricher,
    SeretEntry,
    autocomplete_url,
    critic_score,
    entry_from,
    imdb_id_of,
    matches,
    page_url,
    parse_links,
    parse_title_node,
    ratings_from,
    viewer_score,
)
from eifo_fetcher.enrichers.seret_index import SeretLookup
from eifo_fetcher.http import HttpClient
from eifo_fetcher.sources.base import FetchContext

ROBOTS_URL = f"{BASE_URL}/robots.txt"

#: What seret.co.il really serves, trimmed to the lines that matter here.
ROBOTS_TXT = """User-agent: *
Disallow: /admin/
Disallow: /ajax/getExtraMovieRatingsAjax.asp
Sitemap: https://www.seret.co.il/Sitemap.xml
"""


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


def entry(**overrides: Any) -> SeretEntry:
    values: dict[str, Any] = {
        "kind": TitleKind.MOVIE,
        "seret_id": 4242,
        "name_he": "פוקסטרוט",
        "name_en": "Foxtrot",
        "year": 2017,
        "viewers_score": 9.1,
        "viewers_votes": 42,
        "critics_score": 6.8,
    }
    values.update(overrides)
    return SeretEntry(**values)


@pytest.fixture
def ctx(http: HttpClient) -> FetchContext:
    return FetchContext(source_key="enrich", http=http, settings=Settings(_env_file=None))


def mock_robots(body: str = ROBOTS_TXT) -> None:
    respx.get(ROBOTS_URL).mock(return_value=httpx.Response(200, text=body))


def mock_page(url: str, fixture: str) -> respx.Route:
    return respx.get(url).mock(
        return_value=httpx.Response(
            200,
            content=fixture_bytes(fixture),
            headers={"Content-Type": "text/html; Charset=windows-1255"},
        )
    )


def mock_autocomplete(fixture: str = "autocomplete.html") -> respx.Route:
    return respx.get(url__startswith=AUTOCOMPLETE_URL).mock(
        return_value=httpx.Response(200, content=fixture_bytes(fixture))
    )


class TestParseTitleNode:
    def test_finds_the_movie_node_in_the_graph(self) -> None:
        node = parse_title_node(decoded("movie.html"))

        assert node is not None
        assert node["name"] == "פוקסטרוט"
        assert node["alternateName"] == "Foxtrot"

    def test_finds_a_series_node_too(self) -> None:
        """Accepting only "Movie" would silently lose every series."""
        node = parse_title_node(decoded("series.html"))

        assert node is not None
        assert node["@type"] == "TVSeries"
        assert node["name"] == "פאודה"

    def test_returns_none_without_json_ld(self) -> None:
        assert parse_title_node("<html><body>nothing</body></html>") is None

    def test_ignores_unparsable_json_ld(self) -> None:
        assert parse_title_node('<script type="application/ld+json">{not json</script>') is None

    def test_reads_a_bare_object_as_well_as_a_graph(self) -> None:
        html = '<script type="application/ld+json">{"@type":"Movie","name":"x"}</script>'

        node = parse_title_node(html)

        assert node is not None
        assert node["name"] == "x"


class TestScores:
    def test_reads_the_audience_score_and_its_vote_count(self) -> None:
        node = parse_title_node(decoded("movie.html"))
        assert node is not None

        assert viewer_score(node) == (9.1, 42)

    def test_reads_the_composite_editorial_score(self) -> None:
        node = parse_title_node(decoded("movie.html"))
        assert node is not None

        assert critic_score(node) == 6.8

    def test_a_zero_is_not_a_score(self) -> None:
        """An unreleased film publishes ratingValue 0 beside a real ratingCount.

        Stored as written it becomes nought out of ten for a film nobody has
        seen yet, which drags down the very aggregate it should not be in.
        """
        node = parse_title_node(decoded("unrated.html"))
        assert node is not None

        assert viewer_score(node) == (None, None)
        assert critic_score(node) is None
        assert ratings_from(entry_from(TitleKind.MOVIE, 8620, node)) == []

    def test_a_non_ten_best_rating_is_rescaled(self) -> None:
        """schema.org allows any scale; trust bestRating rather than assuming."""
        node = parse_title_node(
            '<script type="application/ld+json">'
            '{"@type":"Movie","name":"x","aggregateRating":'
            '{"ratingValue":4,"bestRating":5,"ratingCount":10}}'
            "</script>"
        )
        assert node is not None

        assert viewer_score(node) == (8.0, 10)

    def test_a_missing_aggregate_rating_yields_nothing(self) -> None:
        assert viewer_score({"name": "x"}) == (None, None)

    def test_a_missing_editorial_score_yields_nothing(self) -> None:
        assert critic_score({"name": "x", "additionalProperty": None}) is None

    def test_both_scores_link_back_to_the_page(self) -> None:
        """A score is never shown without a link to where it came from."""
        ratings = ratings_from(entry())

        assert len(ratings) == 2
        assert all("seret.co.il" in (rating.url or "") for rating in ratings)

    def test_the_audience_rating_carries_its_vote_count(self) -> None:
        by_provider = {rating.provider: rating for rating in ratings_from(entry())}

        assert by_provider[RatingProvider.SERET_VIEWERS].score_raw == 9.1
        assert by_provider[RatingProvider.SERET_VIEWERS].vote_count == 42
        assert by_provider[RatingProvider.SERET_CRITICS].score_raw == 6.8

    def test_one_score_without_the_other_is_still_a_result(self) -> None:
        ratings = ratings_from(entry(critics_score=None))

        assert [rating.provider for rating in ratings] == [RatingProvider.SERET_VIEWERS]


class TestPageLinks:
    def test_reads_lowercase_ids_the_site_actually_emits(self) -> None:
        """The autocomplete writes ``mid=``; the markup writes ``MID=``."""
        found = parse_links(decoded("autocomplete.html"))

        assert found == [(TitleKind.MOVIE, 4242), (TitleKind.SERIES, 268)]

    def test_keeps_films_and_series_apart(self) -> None:
        """Taking a film's number to the series endpoint asks about another title."""
        found = parse_links("s_movies.asp?MID=7 and s_series.asp?SID=7")

        assert found == [(TitleKind.MOVIE, 7), (TitleKind.SERIES, 7)]

    def test_drops_repeats_but_keeps_order(self) -> None:
        found = parse_links("MID=2 s_movies.asp?MID=1 s_movies.asp?mid=1 s_movies.asp?MID=3")

        assert found == [(TitleKind.MOVIE, 1), (TitleKind.MOVIE, 3)]

    def test_each_kind_goes_to_its_own_endpoint(self) -> None:
        assert page_url(TitleKind.MOVIE, 4242) == f"{MOVIE_URL}?MID=4242"
        assert page_url(TitleKind.SERIES, 268) == f"{SERIES_URL}?SID=268"


class TestAutocompleteUrl:
    def test_encodes_the_query_as_windows_1255(self) -> None:
        """UTF-8 reaches this endpoint as mojibake and it answers nothing.

        Which is indistinguishable from "no such film", and so is exactly the
        kind of breakage that looks like the site being unsearchable.
        """
        url = autocomplete_url("פאודה")

        assert url is not None
        # One windows-1255 byte per Hebrew letter, not the two UTF-8 would use.
        assert url.endswith("s=%F4%E0%E5%E3%E4&t=movie")

    def test_a_name_the_codepage_cannot_hold_is_not_asked_about(self) -> None:
        assert autocomplete_url("こんにちは") is None


class TestMatching:
    def test_reads_the_imdb_id_from_same_as(self) -> None:
        assert imdb_id_of({"sameAs": ["https://www.imdb.com/title/tt6896536/"]}) == "tt6896536"

    def test_tolerates_a_bare_string(self) -> None:
        assert imdb_id_of({"sameAs": "https://www.imdb.com/title/tt123456/"}) == "tt123456"

    def test_returns_none_without_a_link(self) -> None:
        assert imdb_id_of({"name": "x"}) is None

    def test_a_shared_imdb_id_settles_it(self) -> None:
        """Decisive, so no name comparison can get it wrong."""
        assert matches(
            view(imdb_id="tt6896536", name_he="שם אחר לגמרי", year=1990),
            entry(imdb_id="tt6896536"),
        )

    def test_a_conflicting_imdb_id_rejects_it(self) -> None:
        assert not matches(view(imdb_id="tt6896536"), entry(imdb_id="tt0000001"))

    def test_falls_back_to_name_and_year_without_an_imdb_id(self) -> None:
        assert matches(view(), entry())

    def test_rejects_an_unrelated_name(self) -> None:
        assert not matches(view(name_he="טהרן", name_en="Tehran"), entry())

    def test_rejects_a_film_from_the_wrong_year(self) -> None:
        assert not matches(view(year=1998), entry())

    def test_allows_for_a_late_israeli_release(self) -> None:
        """Seret dates a title by its Israeli release, not its production year.

        "The Big Short" is 2015 upstream and 2016-01-28 here, and a festival
        film can reach Israeli screens two years after it was made.
        """
        assert matches(
            view(name_he="מכונת הכסף", name_en="The Big Short", year=2015),
            entry(name_he="מכונת הכסף", name_en="The Big Short", year=2017),
        )

    def test_but_not_for_an_arbitrary_gap(self) -> None:
        assert not matches(view(year=2015), entry(name_he="פוקסטרוט", year=2021))


class TestEnrichFromTheIndex:
    def test_returns_both_israeli_scores_without_a_request(self, ctx: FetchContext) -> None:
        """The index already holds the scores, so this costs nothing at all."""
        enricher = SeretEnricher(SeretLookup([entry()]))

        with respx.mock:
            # No route registered: any request at all would fail the test.
            result = enricher.enrich(view(), ctx)

        assert result is not None
        by_provider = {rating.provider: rating for rating in result.ratings}
        assert by_provider[RatingProvider.SERET_VIEWERS].score_raw == 9.1
        assert by_provider[RatingProvider.SERET_VIEWERS].vote_count == 42
        assert by_provider[RatingProvider.SERET_CRITICS].score_raw == 6.8

    def test_a_title_the_index_does_not_have_is_not_an_error(self, ctx: FetchContext) -> None:
        """Foreign titles are routinely missing; that is ordinary."""
        enricher = SeretEnricher(SeretLookup([entry()]))

        with respx.mock:
            mock_robots()
            mock_autocomplete()
            mock_page(f"{MOVIE_URL}?MID=4242", "movie.html")
            mock_page(f"{SERIES_URL}?SID=268", "series.html")
            result = enricher.enrich(view(name_he="טהרן", name_en="Tehran", year=2020), ctx)

        assert result is None
        assert ctx.error_count == 0

    def test_an_empty_index_says_so_rather_than_guessing(
        self, ctx: FetchContext, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level("INFO", logger="eifo.fetch.enrich.seret"), respx.mock:
            mock_robots()
            mock_autocomplete()
            mock_page(f"{MOVIE_URL}?MID=4242", "movie.html")
            SeretEnricher(SeretLookup([])).enrich(view(), ctx)

        assert "eifo-fetch seret index" in caplog.text

    def test_a_title_with_no_name_is_skipped(self, ctx: FetchContext) -> None:
        enricher = SeretEnricher(SeretLookup([entry()]))

        with respx.mock:
            assert enricher.enrich(view(name_he=None, name_en=None), ctx) is None


class TestLiveFallback:
    @respx.mock
    def test_finds_a_title_released_since_the_last_crawl(self, ctx: FetchContext) -> None:
        mock_robots()
        auto = mock_autocomplete()
        mock_page(f"{MOVIE_URL}?MID=4242", "movie.html")

        result = SeretEnricher(SeretLookup([])).enrich(view(), ctx)

        assert auto.called
        assert result is not None
        assert len(result.ratings) == 2

    @respx.mock
    def test_a_series_is_fetched_from_the_series_endpoint(self, ctx: FetchContext) -> None:
        """Films and series live at different endpoints with different ids."""
        mock_robots()
        mock_autocomplete()
        mock_page(f"{MOVIE_URL}?MID=4242", "movie.html")
        series = mock_page(f"{SERIES_URL}?SID=268", "series.html")

        result = SeretEnricher(SeretLookup([])).enrich(
            view(kind=TitleKind.SERIES, name_he="פאודה", name_en="Fauda", year=2015), ctx
        )

        assert series.called
        assert result is not None
        assert {rating.score_raw for rating in result.ratings} == {8.4, 7.9}

    @respx.mock
    def test_a_latin_only_title_is_never_asked_about(self, ctx: FetchContext) -> None:
        """The autocomplete is indexed in Hebrew and answers nothing otherwise."""
        mock_robots()
        auto = mock_autocomplete()

        assert SeretEnricher(SeretLookup([])).enrich(view(name_he=None), ctx) is None
        assert not auto.called

    @respx.mock
    def test_configuration_can_switch_it_off(self, http: HttpClient) -> None:
        mock_robots()
        auto = mock_autocomplete()
        ctx = FetchContext(
            source_key="enrich",
            http=http,
            settings=Settings(_env_file=None, seret={"live_fallback": False}),
        )

        assert SeretEnricher(SeretLookup([])).enrich(view(), ctx) is None
        assert not auto.called

    def test_it_declares_its_host_and_pace_rather_than_setting_them(self) -> None:
        """The pipeline applies these; see enrich.apply_rate_limits.

        An enricher that reached for the rate limiter itself used to resolve it
        from ``[sources.enrich]``, a section that exists nowhere - so the call
        set nothing and this scraped at the client-wide default.
        """
        assert SeretEnricher.host == HOST
        assert SeretEnricher.default_rate_limit_rps == 0.5

    @respx.mock
    def test_a_failure_is_recorded(self, ctx: FetchContext) -> None:
        mock_robots()
        respx.get(url__startswith=AUTOCOMPLETE_URL).mock(return_value=httpx.Response(500))

        assert SeretEnricher(SeretLookup([])).enrich(view(), ctx) is None
        assert ctx.error_count == 1

    @respx.mock
    def test_an_empty_answer_is_not_an_error(self, ctx: FetchContext) -> None:
        mock_robots()
        respx.get(url__startswith=AUTOCOMPLETE_URL).mock(
            return_value=httpx.Response(200, content=b"")
        )

        assert SeretEnricher(SeretLookup([])).enrich(view(), ctx) is None
        assert ctx.error_count == 0

    @respx.mock
    def test_a_disallowed_endpoint_is_never_fetched(self, ctx: FetchContext) -> None:
        """robots.txt is asked rather than assumed."""
        mock_robots("User-agent: *\nDisallow: /searchAUAjax.asp\n")
        auto = mock_autocomplete()

        assert SeretEnricher(SeretLookup([])).enrich(view(), ctx) is None
        assert not auto.called
