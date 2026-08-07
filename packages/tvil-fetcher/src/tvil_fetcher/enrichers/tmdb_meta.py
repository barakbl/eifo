"""TMDB: canonical metadata, plus TMDB's own rating.

TMDB is the metadata anchor for the whole catalog — bilingual names and
overviews, runtime, seasons, genres, artwork, and the ``imdb_id`` that lets the
IMDb dataset join find the title at all. Its own ``vote_average`` comes along
as one more rating.
"""

from __future__ import annotations

from typing import Any

from tvil_core.enums import RatingProvider, TitleKind
from tvil_fetcher.enrichers.base import Enricher, EnrichResult, Rating, TitleView
from tvil_fetcher.match import is_hebrew, similarity, years_match
from tvil_fetcher.sources.base import FetchContext
from tvil_fetcher.tmdb import (
    ENGLISH_LANGUAGE,
    HEBREW_LANGUAGE,
    TmdbClient,
    image_url,
)

#: Name similarity required before adopting a search hit as this title.
MATCH_THRESHOLD = 90.0

_WEB_BASE = "https://www.themoviedb.org"


class TmdbMetadataEnricher(Enricher):
    """Fills canonical metadata and TMDB's rating."""

    providers = (RatingProvider.TMDB,)

    def __init__(self, client: TmdbClient | None = None) -> None:
        self._client = client

    @property
    def key(self) -> str:
        return "tmdb"

    def enrich(self, title: TitleView, ctx: FetchContext) -> EnrichResult | None:
        client = self._client or _client_from(ctx)

        tmdb_id = title.tmdb_id or self._resolve(title, client, ctx)
        if tmdb_id is None:
            return None

        try:
            hebrew = client.details(title.kind, tmdb_id, language=HEBREW_LANGUAGE)
            english = client.details(title.kind, tmdb_id, language=ENGLISH_LANGUAGE)
            external = client.external_ids(title.kind, tmdb_id)
        except Exception as exc:
            ctx.record_error(f"TMDB lookup failed for title {title.id}", exc=exc)
            return None

        return EnrichResult(
            ratings=self._ratings(hebrew, title.kind, tmdb_id),
            metadata_patch=self._metadata(hebrew, english, external, title.kind, tmdb_id),
        )

    def _resolve(self, title: TitleView, client: TmdbClient, ctx: FetchContext) -> int | None:
        """Find this title on TMDB by name and year."""
        for name in title.names():
            try:
                candidates = client.search(title.kind, name, year=title.year)
            except Exception as exc:
                ctx.record_error(f"TMDB search failed for {name!r}", exc=exc)
                return None

            for candidate in candidates:
                if not years_match(title.year, candidate.year):
                    continue
                names = (candidate.name, candidate.original_name)
                if any(other and similarity(name, other) >= MATCH_THRESHOLD for other in names):
                    return candidate.tmdb_id
        return None

    def _ratings(self, details: dict[str, Any], kind: TitleKind, tmdb_id: int) -> list[Rating]:
        average = details.get("vote_average")
        if not isinstance(average, int | float) or average <= 0:
            return []

        return [
            Rating(
                provider=RatingProvider.TMDB,
                score_raw=float(average),
                vote_count=_int_or_none(details.get("vote_count")),
                url=web_url(kind, tmdb_id),
            )
        ]

    def _metadata(
        self,
        hebrew: dict[str, Any],
        english: dict[str, Any],
        external: dict[str, Any],
        kind: TitleKind,
        tmdb_id: int,
    ) -> dict[str, Any]:
        """Build the patch. Only non-empty values are offered."""
        patch: dict[str, Any] = {"tmdb_id": tmdb_id}

        he_name, en_name = _names(hebrew, english, kind)
        patch["name_he"] = he_name
        patch["name_en"] = en_name
        patch["overview_he"] = _clean(hebrew.get("overview"))
        patch["overview_en"] = _clean(english.get("overview"))
        patch["year"] = _year(english, kind)
        patch["imdb_id"] = _clean(external.get("imdb_id"))
        patch["runtime_minutes"] = _runtime(english, kind)
        patch["status"] = _clean(english.get("status"))
        patch["genres"] = _genres(hebrew, english)

        poster = _clean(english.get("poster_path")) or _clean(hebrew.get("poster_path"))
        if poster:
            patch["poster_source_url"] = image_url(poster)

        if kind is TitleKind.SERIES:
            patch["seasons"] = _int_or_none(english.get("number_of_seasons"))

        return {key: value for key, value in patch.items() if value is not None}


def web_url(kind: TitleKind, tmdb_id: int) -> str:
    """The public TMDB page for a title."""
    path = "movie" if kind is TitleKind.MOVIE else "tv"
    return f"{_WEB_BASE}/{path}/{tmdb_id}"


def _names(
    hebrew: dict[str, Any],
    english: dict[str, Any],
    kind: TitleKind,
) -> tuple[str | None, str | None]:
    """Split TMDB's localised titles into Hebrew and English.

    TMDB falls back to the original title when a translation is missing, so the
    "Hebrew" response may hold a Latin name; the script decides, not the request.
    """
    field = "title" if kind is TitleKind.MOVIE else "name"
    he_value = _clean(hebrew.get(field))
    en_value = _clean(english.get(field))

    he_name = he_value if he_value and is_hebrew(he_value) else None
    en_name = en_value if en_value and not is_hebrew(en_value) else None
    return he_name, en_name


def _year(details: dict[str, Any], kind: TitleKind) -> int | None:
    field = "release_date" if kind is TitleKind.MOVIE else "first_air_date"
    date = _clean(details.get(field)) or ""
    if len(date) < 4 or not date[:4].isdigit():
        return None
    return int(date[:4])


def _runtime(details: dict[str, Any], kind: TitleKind) -> int | None:
    if kind is TitleKind.MOVIE:
        return _int_or_none(details.get("runtime"))
    runtimes = details.get("episode_run_time")
    if isinstance(runtimes, list) and runtimes:
        return _int_or_none(runtimes[0])
    return None


def _genres(hebrew: dict[str, Any], english: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Genres as (tmdb_id, name_en, name_he) triples."""
    localised = {
        genre["id"]: _clean(genre.get("name"))
        for genre in hebrew.get("genres", [])
        if isinstance(genre, dict) and "id" in genre
    }

    genres = [
        {
            "tmdb_id": genre["id"],
            "name_en": _clean(genre.get("name")) or "",
            "name_he": localised.get(genre["id"]),
        }
        for genre in english.get("genres", [])
        if isinstance(genre, dict) and "id" in genre
    ]
    return genres or None


def _clean(value: Any) -> str | None:
    """A non-empty trimmed string, or None."""
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _int_or_none(value: Any) -> int | None:
    return int(value) if isinstance(value, int | float) and value > 0 else None


def _client_from(ctx: FetchContext) -> TmdbClient:
    ctx.settings.require("tmdb_api_key")
    assert ctx.settings.tmdb_api_key is not None
    return TmdbClient(ctx.http, ctx.settings.tmdb_api_key.get_secret_value())
