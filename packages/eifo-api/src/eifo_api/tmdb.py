"""The two TMDB questions the API asks itself: find a film, and describe one.

Everything else TMDB is used for - matching listings, enriching titles - is the
fetcher's, and stays there. This exists for one person typing the name of a film
they watched, so it is small, answers in a few seconds or not at all, and never
runs more than a member's keystrokes can ask of it:

* searches are remembered for a while, so the same prefix typed again, or by
  somebody else, does not go back to TMDB;
* each member has a budget per minute (:class:`RateLimit`), which a person never
  meets and a stuck client or a loop meets at once.

Both live in this process's memory. That is enough for the one worker an
instance runs; with several, each keeps its own, which loosens the limit by the
number of workers rather than breaking anything.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import HTTPException, Request

from eifo_core.additions import MovieRecord, movie_from_tmdb, tmdb_poster_path
from eifo_core.items import plausible_year

BASE_URL = "https://api.themoviedb.org/3"
THUMBNAIL_BASE = "https://image.tmdb.org/t/p/w92"
HEBREW = "he-IL"
ENGLISH = "en-US"

#: Long enough that nobody waiting on a dropdown is left wondering, short enough
#: that a TMDB outage costs a member a moment rather than a worker a minute.
TIMEOUT_SECONDS = 4.0

#: How long a search answer is reused. Films do not appear on TMDB by the
#: minute, and a member refining "matr" to "matrix" asks for both.
SEARCH_TTL_SECONDS = 600.0
SEARCH_CACHE_SIZE = 512

#: Results shown per search. Past this the right film is not going to be
#: found by scrolling; it is going to be found by typing more.
MAX_RESULTS = 8

logger = logging.getLogger("eifo.api.tmdb")


class TmdbUnavailableError(Exception):
    """TMDB did not answer, or answered with something unusable."""


@dataclass(frozen=True, slots=True)
class FoundMovie:
    """One search result, as the dropdown shows it."""

    tmdb_id: int
    name: str
    original_name: str | None
    year: int | None
    thumbnail_url: str | None


class TmdbLookup:
    """Search and details for films, over TMDB's v3 API."""

    def __init__(
        self,
        api_key: str,
        *,
        transport: httpx.BaseTransport | None = None,
        clock: Any = time.monotonic,
    ) -> None:
        self._client = httpx.Client(
            base_url=BASE_URL,
            timeout=TIMEOUT_SECONDS,
            transport=transport,
            headers={"Accept": "application/json"},
        )
        self._api_key = api_key
        self._clock = clock
        self._searches: OrderedDict[str, tuple[float, list[FoundMovie]]] = OrderedDict()
        self._lock = threading.Lock()

    def close(self) -> None:
        self._client.close()

    def search(self, query: str) -> list[FoundMovie]:
        """Films whose name matches, best first, in Hebrew where TMDB has it."""
        key = " ".join(query.casefold().split())
        now = self._clock()
        with self._lock:
            cached = self._searches.get(key)
            if cached is not None and now - cached[0] < SEARCH_TTL_SECONDS:
                self._searches.move_to_end(key)
                return cached[1]

        payload = self._get(
            "/search/movie",
            query=query,
            language=HEBREW,
            include_adult="false",
        )
        results = payload.get("results")
        found = [
            movie
            for raw in (results if isinstance(results, list) else [])
            if (movie := _found(raw)) is not None
        ][:MAX_RESULTS]

        with self._lock:
            self._searches[key] = (now, found)
            self._searches.move_to_end(key)
            while len(self._searches) > SEARCH_CACHE_SIZE:
                self._searches.popitem(last=False)
        return found

    def movie(self, tmdb_id: int) -> MovieRecord | None:
        """One film in full, or None when TMDB has no film by that id.

        Two requests, because a name and an overview are per language and the
        catalog keeps both: Hebrew for the reader, English for the matcher and
        for everybody searching by the name the world knows it by.
        """
        try:
            english = self._get(f"/movie/{tmdb_id}", language=ENGLISH)
        except _NotFoundError:
            return None
        if english.get("adult") is True:
            return None
        try:
            hebrew = self._get(f"/movie/{tmdb_id}", language=HEBREW)
        except _NotFoundError:
            hebrew = {}
        return movie_from_tmdb(hebrew, english)

    def _get(self, path: str, **params: str) -> dict[str, Any]:
        try:
            response = self._client.get(path, params={"api_key": self._api_key, **params})
        except httpx.HTTPError as cause:
            logger.warning("TMDB unreachable: %s", type(cause).__name__)
            raise TmdbUnavailableError from cause

        if response.status_code == 404:
            raise _NotFoundError
        if response.status_code != 200:
            # The status alone: TMDB echoes the request, key included, in some
            # error bodies, and this line ends up in a log somebody reads.
            logger.warning("TMDB answered %s for %s", response.status_code, path.split("/")[1])
            raise TmdbUnavailableError
        try:
            payload = response.json()
        except ValueError as cause:
            raise TmdbUnavailableError from cause
        if not isinstance(payload, dict):
            raise TmdbUnavailableError
        return payload


class _NotFoundError(Exception):
    pass


def _found(raw: Any) -> FoundMovie | None:
    if not isinstance(raw, dict) or raw.get("adult") is True:
        return None
    tmdb_id = raw.get("id")
    name = raw.get("title") or raw.get("original_title")
    if not isinstance(tmdb_id, int) or tmdb_id <= 0 or not isinstance(name, str) or not name:
        return None
    original = raw.get("original_title")
    date = raw.get("release_date")
    year = int(date[:4]) if isinstance(date, str) and date[:4].isdigit() else None
    poster = raw.get("poster_path")
    return FoundMovie(
        tmdb_id=tmdb_id,
        name=name,
        # Only when it says something the name does not.
        original_name=original if isinstance(original, str) and original != name else None,
        year=plausible_year(year),
        thumbnail_url=_thumbnail(poster),
    )


def _thumbnail(poster: Any) -> str | None:
    # Held to the same shape the catalog accepts for a poster, so a result can
    # never point the browser anywhere but TMDB's own image host.
    path = tmdb_poster_path(poster)
    return f"{THUMBNAIL_BASE}{path}" if path else None


class RateLimit:
    """At most ``limit`` calls per member in any ``window`` seconds."""

    def __init__(self, limit: int, window: float, *, clock: Any = time.monotonic) -> None:
        self._limit = limit
        self._window = window
        self._clock = clock
        self._calls: dict[int, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, user_id: int) -> bool:
        now = self._clock()
        with self._lock:
            calls = self._calls.setdefault(user_id, deque())
            while calls and now - calls[0] >= self._window:
                calls.popleft()
            if len(calls) >= self._limit:
                return False
            calls.append(now)
            return True


#: Typing at a normal pace with the dropdown's debounce is a request every few
#: hundred milliseconds at most, for a few seconds. Sixty a minute leaves that
#: untouched.
SEARCHES_PER_MINUTE = 60


def get_tmdb(request: Request) -> TmdbLookup:
    """The instance's TMDB client, or 503 when none is configured."""
    lookup: TmdbLookup | None = getattr(request.app.state, "tmdb", None)
    if lookup is None:
        raise HTTPException(
            status_code=503,
            detail="Adding films is off: this instance has no TMDB key (EIFO_TMDB_API_KEY).",
        )
    return lookup
