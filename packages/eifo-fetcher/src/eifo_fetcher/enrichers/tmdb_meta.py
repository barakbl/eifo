"""TMDB: canonical metadata, plus TMDB's own rating.

TMDB is the metadata anchor for the whole catalog - bilingual names and
overviews, runtime, seasons, genres, artwork, and the ``imdb_id`` that lets the
IMDb dataset join find the title at all. Its own ``vote_average`` comes along
as one more rating.
"""

from __future__ import annotations

from typing import Any

from eifo_core.enums import CreditRole, RatingProvider, TitleKind
from eifo_fetcher.enrichers.base import Enricher, EnrichResult, Rating, TitleView
from eifo_fetcher.match import is_hebrew, latin_script, similarity, years_match
from eifo_fetcher.sources.base import FetchContext
from eifo_fetcher.tmdb import (
    ENGLISH_LANGUAGE,
    HEBREW_LANGUAGE,
    TmdbClient,
    image_url,
)

#: Name similarity required before adopting a search hit as this title.
MATCH_THRESHOLD = 90.0

#: The crew jobs worth showing. TMDB spells the camera credit two ways.
_CREW_ROLES = {
    "Director": CreditRole.DIRECTOR,
    "Director of Photography": CreditRole.CINEMATOGRAPHER,
    "Cinematography": CreditRole.CINEMATOGRAPHER,
}
#: Cast kept per title. Deep enough that the page can offer "show all" and mean
#: it, shallow enough that a blockbuster does not add a hundred rows.
MAX_CAST = 20
#: Where TMDB serves headshots, for a person page that wants a face.
_PROFILE_SIZE = "w185"

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
            # Credits ride along on the English call: same request, and TMDB
            # writes character names in the requested language.
            english = client.details(
                title.kind, tmdb_id, language=ENGLISH_LANGUAGE, append="credits"
            )
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
        patch["original_language"] = _clean(english.get("original_language"))
        patch["origin_countries"] = _countries(english)
        patch["credits"] = _credits(english)

        poster = _clean(english.get("poster_path")) or _clean(hebrew.get("poster_path"))
        if poster:
            patch["poster_source_url"] = image_url(poster)

        if kind is TitleKind.SERIES:
            patch["seasons"] = _int_or_none(english.get("number_of_seasons"))

        return {key: value for key, value in patch.items() if value is not None}


def _countries(details: dict[str, Any]) -> str | None:
    """Where it was made, as ISO 3166-1 codes: "IL", "IL,FR".

    Codes rather than names so the client can render them in whichever
    language the reader chose. Movies carry ``production_countries``; series
    carry ``origin_country`` as bare codes.
    """
    codes: list[str] = []
    for entry in details.get("production_countries") or []:
        code = _clean(entry.get("iso_3166_1")) if isinstance(entry, dict) else None
        if code and code not in codes:
            codes.append(code.upper())
    for code in details.get("origin_country") or []:
        cleaned = _clean(code)
        if cleaned and cleaned.upper() not in codes:
            codes.append(cleaned.upper())
    return ",".join(codes) or None


def _credits(details: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Director, cinematographer and billed cast, in TMDB's own order.

    None rather than an empty list when TMDB credits nobody, so the patch
    offers only what it actually found.
    """
    payload = details.get("credits")
    if not isinstance(payload, dict):
        return None

    entries: list[dict[str, Any]] = []
    for member in payload.get("crew") or []:
        if not isinstance(member, dict):
            continue
        role = _CREW_ROLES.get(str(member.get("job") or ""))
        if role is not None:
            entries.append(_person(member, role))

    cast = [member for member in payload.get("cast") or [] if isinstance(member, dict)]
    cast.sort(key=lambda member: _billing_order(member.get("order")) or 0)
    for member in cast[:MAX_CAST]:
        entry = _person(member, CreditRole.CAST)
        entry["character"] = _clean(member.get("character"))
        entry["billing_order"] = _billing_order(member.get("order"))
        entries.append(entry)

    return entries or None


def _person(member: dict[str, Any], role: CreditRole) -> dict[str, Any]:
    profile = _clean(member.get("profile_path"))
    return {
        "role": role,
        "tmdb_id": _int_or_none(member.get("id")),
        "name_en": _clean(member.get("name")) or _clean(member.get("original_name")),
        "profile_source_url": image_url(profile, size=_PROFILE_SIZE) if profile else None,
    }


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
    # Not "anything that is not Hebrew": TMDB answers an en-US request with the
    # original title when it has no English one, so this field arrives in
    # Japanese, Tamil or Korean as readily as in English.
    en_name = en_value if en_value and latin_script(en_value) else None
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
    """A positive number, or None. Zero runtime means "unknown", not "instant"."""
    return int(value) if isinstance(value, int | float) and value > 0 else None


def _billing_order(value: Any) -> int | None:
    """Billing position, where zero is the lead rather than a missing value."""
    return int(value) if isinstance(value, int | float) and value >= 0 else None


def _client_from(ctx: FetchContext) -> TmdbClient:
    ctx.settings.require("tmdb_api_key")
    assert ctx.settings.tmdb_api_key is not None
    return TmdbClient(
        ctx.http,
        ctx.settings.tmdb_api_key.get_secret_value(),
        rate_limit_rps=ctx.settings.tmdb.rate_limit_rps,
    )
