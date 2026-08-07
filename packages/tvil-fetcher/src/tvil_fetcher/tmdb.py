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

from tvil_core.enums import TitleKind
from tvil_fetcher.http import HttpClient

BASE_URL = "https://api.themoviedb.org/3"
IMAGE_BASE_URL = "https://image.tmdb.org/t/p"

HEBREW_LANGUAGE = "he-IL"
ENGLISH_LANGUAGE = "en-US"

#: TMDB refuses to paginate past this point regardless of total_pages.
MAX_PAGE = 500

logger = logging.getLogger("tvil.fetch.tmdb")

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

    def __init__(self, http: HttpClient, api_key: str, *, region: str = "IL") -> None:
        self._http = http
        self._api_key = api_key
        self.region = region

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

    def external_ids(self, kind: TitleKind, tmdb_id: int) -> dict[str, Any]:
        """External identifiers for a title, notably ``imdb_id``."""
        return self._get(f"/{_MEDIA_PATH[kind]}/{tmdb_id}/external_ids")

    def details(
        self,
        kind: TitleKind,
        tmdb_id: int,
        *,
        language: str = HEBREW_LANGUAGE,
    ) -> dict[str, Any]:
        """Full record for one title."""
        return self._get(f"/{_MEDIA_PATH[kind]}/{tmdb_id}", language=language)


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
