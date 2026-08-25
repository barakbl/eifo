"""TMDB API client.

TMDB is both a source of availability data (its watch-providers dataset is
powered by JustWatch) and the canonical metadata anchor the matcher resolves
against. One free API key covers both.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from eifo_core.enums import TitleKind
from eifo_fetcher.http import HttpClient

BASE_URL = "https://api.themoviedb.org/3"
IMAGE_BASE_URL = "https://image.tmdb.org/t/p"

API_HOST = "api.themoviedb.org"
IMAGE_HOST = "image.tmdb.org"

#: TMDB is an API built to be called, not a site being scraped, and it is the
#: one host in this system that every phase leans on: the matcher asks it about
#: every unmatched item, the metadata enricher about every due title. At the
#: fetcher's default of one request a second that arithmetic sets the pace of
#: the whole nightly run - a 3,479-item sync took 3,479 seconds, which is one
#: item per second and no coincidence.
#:
#: Twenty is well inside what TMDB serves happily and still far short of asking
#: for trouble. If they ever disagree, the client retries on 429 and honours
#: Retry-After, so the floor is a slower run rather than a failed one.
DEFAULT_RATE_LIMIT_RPS = 20.0

HEBREW_LANGUAGE = "he-IL"
ENGLISH_LANGUAGE = "en-US"

#: TMDB refuses to paginate past this point regardless of total_pages.
MAX_PAGE = 500

logger = logging.getLogger("eifo.fetch.tmdb")

_MEDIA_PATH = {TitleKind.MOVIE: "movie", TitleKind.SERIES: "tv"}


@dataclass(frozen=True, slots=True)
class TmdbTitle:
    """A search or discover result, normalised across the movie/tv split."""

    tmdb_id: int
    kind: TitleKind
    name: str
    original_name: str | None
    year: int | None
    overview: str | None
    poster_path: str | None

    @property
    def media_type(self) -> str:
        return _MEDIA_PATH[self.kind]


class TmdbClient:
    """Thin, typed wrapper over the endpoints the fetcher actually uses."""

    def __init__(
        self,
        http: HttpClient,
        api_key: str,
        *,
        region: str = "IL",
        rate_limit_rps: float = DEFAULT_RATE_LIMIT_RPS,
    ) -> None:
        self._http = http
        self._api_key = api_key
        self.region = region
        # Here rather than at each of the places that build a client: the host
        # is this class's business, and a fourth caller added later should not
        # have to know that forgetting one line costs an hour a night.
        http.rate_limiter.set_host_rate(API_HOST, rate_limit_rps)

    def _get(self, path: str, **params: Any) -> dict[str, Any]:
        payload = self._http.get_json(
            f"{BASE_URL}{path}",
            params={"api_key": self._api_key, **params},
        )
        assert isinstance(payload, dict)
        return payload

    def watch_providers(self, kind: TitleKind) -> list[dict[str, Any]]:
        """Providers JustWatch tracks in the configured region."""
        payload = self._get(
            f"/watch/providers/{_MEDIA_PATH[kind]}",
            watch_region=self.region,
        )
        results = payload.get("results", [])
        return [item for item in results if isinstance(item, dict)]

    def discover_by_provider(
        self,
        kind: TitleKind,
        provider_id: int,
        *,
        language: str = HEBREW_LANGUAGE,
        max_pages: int = MAX_PAGE,
    ) -> Iterator[TmdbTitle]:
        """Every title a provider offers in the region, page by page.

        Stops at ``max_pages`` or TMDB's own hard limit, whichever comes first;
        the caller logs when a cap truncates a catalog.
        """
        page = 1
        total_pages = 1
        while page <= min(total_pages, max_pages, MAX_PAGE):
            payload = self._get(
                f"/discover/{_MEDIA_PATH[kind]}",
                language=language,
                watch_region=self.region,
                with_watch_providers=provider_id,
                page=page,
            )
            total_pages = int(payload.get("total_pages") or 1)
            results = payload.get("results") or []
            if not results:
                return
            for result in results:
                if isinstance(result, dict):
                    yield _parse_title(result, kind)
            page += 1

    def search(
        self,
        kind: TitleKind,
        query: str,
        *,
        year: int | None = None,
        language: str = HEBREW_LANGUAGE,
    ) -> list[TmdbTitle]:
        """Search one media type by name, optionally constrained by year."""
        params: dict[str, Any] = {"query": query, "language": language}
        if year is not None:
            params["year" if kind is TitleKind.MOVIE else "first_air_date_year"] = year

        payload = self._get(f"/search/{_MEDIA_PATH[kind]}", **params)
        results = payload.get("results") or []
        return [_parse_title(result, kind) for result in results if isinstance(result, dict)]

    def title_watch_providers(self, kind: TitleKind, tmdb_id: int) -> dict[str, Any]:
        """How one title is offered in the region, split by monetisation.

        Returns TMDB's region block - ``flatrate``, ``rent``, ``buy``, ``free``,
        ``ads``, each a list of providers - or an empty dict when the title is
        offered nowhere in the region.

        This exists because ``discover`` cannot answer the same question.
        Combining ``with_watch_providers`` with ``with_watch_monetization_types``
        reads as "on this provider AND rentable somewhere", not "rentable on
        this provider": asking it for rentals on Apple TV, a subscription that
        rents nothing, returns films whose rental is on the Apple TV Store. One
        request per title is the price of an offer type that is actually true.
        """
        payload = self._get(f"/{_MEDIA_PATH[kind]}/{tmdb_id}/watch/providers")
        region = (payload.get("results") or {}).get(self.region)
        return region if isinstance(region, dict) else {}

    def external_ids(self, kind: TitleKind, tmdb_id: int) -> dict[str, Any]:
        """External identifiers for a title, notably ``imdb_id``."""
        return self._get(f"/{_MEDIA_PATH[kind]}/{tmdb_id}/external_ids")

    def details(
        self,
        kind: TitleKind,
        tmdb_id: int,
        *,
        language: str = HEBREW_LANGUAGE,
        append: str | None = None,
    ) -> dict[str, Any]:
        """Full record for one title.

        Args:
            append: TMDB sub-resources to fold into the same response
                (``"credits"``). These cost no extra request, which is the
                whole reason to ask for them here rather than separately.
        """
        params: dict[str, Any] = {"language": language}
        if append:
            params["append_to_response"] = append
        return self._get(f"/{_MEDIA_PATH[kind]}/{tmdb_id}", **params)


def _parse_title(payload: dict[str, Any], kind: TitleKind) -> TmdbTitle:
    """Normalise TMDB's movie/tv field split into one shape."""
    if kind is TitleKind.MOVIE:
        name = payload.get("title") or payload.get("original_title") or ""
        original = payload.get("original_title")
        date = payload.get("release_date") or ""
    else:
        name = payload.get("name") or payload.get("original_name") or ""
        original = payload.get("original_name")
        date = payload.get("first_air_date") or ""

    return TmdbTitle(
        tmdb_id=int(payload["id"]),
        kind=kind,
        name=name,
        original_name=original,
        year=_year_from(date),
        overview=payload.get("overview") or None,
        poster_path=payload.get("poster_path") or None,
    )


def _year_from(date: str) -> int | None:
    """Year out of a TMDB ``YYYY-MM-DD``, tolerating blanks and junk."""
    if len(date) < 4 or not date[:4].isdigit():
        return None
    return int(date[:4])


def image_url(path: str, *, size: str = "w500") -> str:
    """Absolute URL for a TMDB image path."""
    return f"{IMAGE_BASE_URL}/{size}{path}"
