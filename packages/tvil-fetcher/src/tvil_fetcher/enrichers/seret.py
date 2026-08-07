"""Seret (seret.co.il) — the primary Israeli ratings provider.

Seret publishes schema.org JSON-LD on every film page, so the scores come from
structured data rather than CSS selectors that rot on the next redesign:

* ``aggregateRating.ratingValue`` — the viewer score, 0-10.
* an ``additionalProperty`` entry holding the site's composite editorial score,
  which is the critic figure.

Site-specific details this must get right: pages are **windows-1255**; films and
series live at different endpoints (``s_movies.asp?MID=`` vs
``s_series.asp?SID=``) and declare different JSON-LD types; and ``sameAs``
carries an IMDb link, which settles identity far more reliably than comparing
names. robots.txt disallows ``/ajax/getExtraMovieRatingsAjax.asp``, which this
never touches; title and listing pages are permitted.

**Known limitation — this enricher is disabled by default.** Seret publishes no
working title search: its advertised ``SearchAction`` endpoint, the real form
POST, and the autocomplete endpoint all return a generic current-releases
listing rather than results, so a title cannot reliably be resolved to a page
id. Everything downstream of an id is implemented and tested; resolving one
needs sitemap-based indexing, which is left to a follow-up. Enable this provider
only once that exists.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from tvil_core.enums import RatingProvider, TitleKind
from tvil_fetcher.enrichers.base import Enricher, EnrichResult, Rating, TitleView
from tvil_fetcher.match import similarity, years_match
from tvil_fetcher.sources.base import FetchContext

logger = logging.getLogger("tvil.fetch.enrich.seret")

HOST = "www.seret.co.il"
BASE_URL = f"https://{HOST}"
SEARCH_URL = f"{BASE_URL}/movies/l_movies.asp"
MOVIE_URL = f"{BASE_URL}/movies/s_movies.asp"
SERIES_URL = f"{BASE_URL}/series/s_series.asp"

#: Seret serves legacy Hebrew encoding, declared in the Content-Type header.
ENCODING = "windows-1255"

#: Name similarity required before accepting a search result as this title.
MATCH_THRESHOLD = 85.0
#: Detail pages fetched per lookup; search returns dozens of loose matches.
MAX_CANDIDATES = 3

#: JSON-LD types Seret uses for a title page.
_TITLE_TYPES = frozenset({"Movie", "TVSeries"})

#: The additionalProperty entry carrying the editorial score.
_CRITIC_PROPERTY = "seret score"

_JSON_LD = re.compile(
    r'<script[^>]+type="application/ld\+json"[^>]*>(?P<payload>.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)
_MOVIE_LINK = re.compile(r"s_movies\.asp\?MID=(?P<mid>\d+)")


class SeretEnricher(Enricher):
    """Israeli critic and viewer scores."""

    providers = (RatingProvider.SERET_CRITICS, RatingProvider.SERET_VIEWERS)

    @property
    def key(self) -> str:
        return "seret"

    def enrich(self, title: TitleView, ctx: FetchContext) -> EnrichResult | None:
        ctx.apply_rate_limit(HOST)

        for mid in self._candidates(title, ctx):
            movie = self._movie(mid, ctx, title.kind)
            if movie is None:
                continue
            if not _matches(title, movie):
                continue
            ratings = _ratings_from(movie, mid)
            if ratings:
                return EnrichResult(ratings=ratings)

        # Foreign titles frequently are not on Seret; that is not a failure.
        return None

    def _candidates(self, title: TitleView, ctx: FetchContext) -> list[str]:
        """Film ids the search page offers for this title, best-effort."""
        query = title.name_he or title.name_en
        if not query:
            return []

        try:
            html = _decode(ctx.http.get(SEARCH_URL, params={"searchbar": query}).content)
        except Exception as exc:
            ctx.record_error(f"Seret search failed for {query!r}", exc=exc)
            return []

        seen: list[str] = []
        for match in _MOVIE_LINK.finditer(html):
            mid = match.group("mid")
            if mid not in seen:
                seen.append(mid)
            if len(seen) >= MAX_CANDIDATES:
                break
        return seen

    def _movie(self, mid: str, ctx: FetchContext, kind: TitleKind) -> dict[str, Any] | None:
        """The JSON-LD title node for one page.

        Films and series live at different endpoints under different id
        parameters, so the kind decides which is requested.
        """
        url, param = (MOVIE_URL, "MID") if kind is TitleKind.MOVIE else (SERIES_URL, "SID")
        try:
            html = _decode(ctx.http.get(url, params={param: mid}).content)
        except Exception as exc:
            ctx.record_error(f"Seret page {param}={mid} failed", exc=exc)
            return None
        return parse_movie(html)


def parse_movie(html: str) -> dict[str, Any] | None:
    """Pull the ``Movie`` node out of a film page's JSON-LD graph."""
    for match in _JSON_LD.finditer(html):
        try:
            payload = json.loads(match.group("payload"))
        except json.JSONDecodeError:
            continue

        for node in _nodes(payload):
            if node.get("@type") in _TITLE_TYPES:
                return node
    return None


def _nodes(payload: Any) -> list[dict[str, Any]]:
    """Flatten the shapes JSON-LD is legitimately served in."""
    if isinstance(payload, dict):
        graph = payload.get("@graph")
        if isinstance(graph, list):
            return [node for node in graph if isinstance(node, dict)]
        return [payload]
    if isinstance(payload, list):
        return [node for node in payload if isinstance(node, dict)]
    return []


def imdb_id_of(movie: dict[str, Any]) -> str | None:
    """The IMDb id Seret links to, when it publishes one."""
    same_as = movie.get("sameAs")
    values = same_as if isinstance(same_as, list) else [same_as]
    for value in values:
        if isinstance(value, str):
            found = re.search(r"(tt\d{6,})", value)
            if found:
                return found.group(1)
    return None


def _matches(title: TitleView, movie: dict[str, Any]) -> bool:
    """Whether this Seret title is the one we were asked about.

    Seret publishes an IMDb link in ``sameAs``; when both sides have one that
    settles it outright, with no name comparison to get wrong.
    """
    if title.imdb_id:
        published = imdb_id_of(movie)
        if published:
            return published == title.imdb_id

    year = _year(movie)
    if not years_match(title.year, year):
        return False

    candidates = [
        value
        for value in (movie.get("name"), movie.get("alternateName"))
        if isinstance(value, str) and value
    ]
    return any(
        similarity(name, candidate) >= MATCH_THRESHOLD
        for name in title.names()
        for candidate in candidates
    )


def _ratings_from(movie: dict[str, Any], mid: str) -> list[Rating]:
    """Both Seret scores, when the page carries them."""
    url = _url(movie, mid)
    ratings: list[Rating] = []

    viewers = _viewer_rating(movie, url)
    if viewers is not None:
        ratings.append(viewers)

    critics = _critic_rating(movie, url)
    if critics is not None:
        ratings.append(critics)

    return ratings


def _viewer_rating(movie: dict[str, Any], url: str) -> Rating | None:
    aggregate = movie.get("aggregateRating")
    if not isinstance(aggregate, dict):
        return None

    value = _number(aggregate.get("ratingValue"))
    if value is None:
        return None

    # Seret publishes 0-10, but the field is generic schema.org: trust the
    # declared bestRating rather than assuming.
    best = _number(aggregate.get("bestRating")) or 10.0
    if best != 10.0:
        value = value * 10.0 / best

    return Rating(
        provider=RatingProvider.SERET_VIEWERS,
        score_raw=value,
        vote_count=_int(aggregate.get("ratingCount")),
        url=url,
    )


def _critic_rating(movie: dict[str, Any], url: str) -> Rating | None:
    properties = movie.get("additionalProperty")
    if not isinstance(properties, list):
        return None

    for entry in properties:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").casefold()
        if _CRITIC_PROPERTY not in name:
            continue
        value = _number(entry.get("value"))
        if value is not None:
            return Rating(
                provider=RatingProvider.SERET_CRITICS,
                score_raw=value,
                url=url,
            )
    return None


def _url(movie: dict[str, Any], mid: str) -> str:
    url = movie.get("url")
    if isinstance(url, str) and url.startswith("http"):
        return url
    return f"{MOVIE_URL}?MID={mid}"


def _year(movie: dict[str, Any]) -> int | None:
    published = movie.get("datePublished")
    if isinstance(published, str) and len(published) >= 4 and published[:4].isdigit():
        return int(published[:4])
    return None


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _int(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None else None


def _decode(body: bytes) -> str:
    """Decode a Seret page, tolerating the odd stray byte."""
    return body.decode(ENCODING, errors="replace")
