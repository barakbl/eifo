"""Turning ORM rows into response models.

Kept apart from the routers so the mapping is testable on its own and the
handlers stay short.
"""

from __future__ import annotations

import datetime as dt

from tvil_api.schemas import (
    AggregateOut,
    AvailabilityOut,
    GenreOut,
    RatingOut,
    SourceOut,
    TitleCard,
    TitleDetail,
)
from tvil_core.enums import RatingProvider
from tvil_core.models import AggregateScore, Availability, ExternalRating, Genre, Source, Title

IMAGES_PREFIX = "/images"

#: How each provider is credited in the UI. A score without an attributed
#: source is a rumour, so every rating carries one.
PROVIDER_NAMES = {
    RatingProvider.IMDB: "IMDb",
    RatingProvider.TMDB: "TMDB",
    RatingProvider.RT_CRITICS: "Rotten Tomatoes — Tomatometer",
    RatingProvider.RT_AUDIENCE: "Rotten Tomatoes — Audience",
    RatingProvider.SERET_CRITICS: "סרט — מבקרים",
    RatingProvider.SERET_VIEWERS: "סרט — צופים",
    RatingProvider.EDB: "EDB",
}

#: Providers reported as percentages rather than out of ten.
_PERCENT_PROVIDERS = frozenset({RatingProvider.RT_CRITICS, RatingProvider.RT_AUDIENCE})


def image_url(path: str | None) -> str | None:
    """Public URL for a stored image path."""
    return f"{IMAGES_PREFIX}/{path}" if path else None


def score_display(provider: RatingProvider, score_raw: float) -> str:
    """The score as its provider would print it."""
    if provider in _PERCENT_PROVIDERS:
        return f"{round(score_raw)}%"
    return f"{score_raw:.1f}"


def to_genre(genre: Genre) -> GenreOut:
    return GenreOut(id=genre.id, name_en=genre.name_en, name_he=genre.name_he)


def to_availability(availability: Availability) -> AvailabilityOut:
    source = availability.source
    return AvailabilityOut(
        source_key=source.key,
        source_name=source.name,
        source_kind=source.kind,
        source_active=source.active,
        offer_type=availability.offer_type,
        is_current=availability.is_current,
        deep_link_url=availability.deep_link_url,
        last_seen=availability.last_seen,
        gone_since=availability.gone_since,
    )


def to_rating(rating: ExternalRating) -> RatingOut:
    provider = RatingProvider(rating.provider)
    return RatingOut(
        provider=provider,
        provider_name=PROVIDER_NAMES.get(provider, provider.value),
        score_raw=rating.score_raw,
        score_display=score_display(provider, rating.score_raw),
        score_normalized=rating.score_normalized,
        vote_count=rating.vote_count,
        url=rating.url,
    )


def to_aggregate(aggregate: AggregateScore | None) -> AggregateOut:
    if aggregate is None:
        return AggregateOut()
    return AggregateOut(
        score=aggregate.score,
        score_israeli=aggregate.score_israeli,
        components=aggregate.components or {},
    )


def to_card(title: Title) -> TitleCard:
    """A grid card. Only current availability is shown here.

    The full history, including what has gone away, belongs on the detail page
    rather than cluttering a card.
    """
    return TitleCard(
        id=title.id,
        type=title.type,
        name_he=title.name_he,
        name_en=title.name_en,
        year=title.year,
        poster_url=image_url(title.poster_path),
        score=title.aggregate.score if title.aggregate else None,
        score_israeli=title.aggregate.score_israeli if title.aggregate else None,
        genres=[to_genre(genre) for genre in title.genres],
        availability=[
            to_availability(availability)
            for availability in title.availability
            if availability.is_current
        ],
    )


def to_detail(title: Title) -> TitleDetail:
    """A full title, including availability that has lapsed."""
    card = to_card(title)
    return TitleDetail(
        **card.model_dump(exclude={"availability"}),
        # Everything, so the page can say "was on X until Y".
        availability=[to_availability(entry) for entry in title.availability],
        overview_he=title.overview_he,
        overview_en=title.overview_en,
        runtime_minutes=title.runtime_minutes,
        seasons=title.seasons,
        status=title.status,
        backdrop_url=image_url(title.backdrop_path),
        ratings=[to_rating(rating) for rating in title.ratings],
        aggregate=to_aggregate(title.aggregate),
    )


def to_source(
    source: Source,
    *,
    title_count: int = 0,
    last_synced_at: dt.datetime | None = None,
) -> SourceOut:
    return SourceOut(
        id=source.id,
        key=source.key,
        name=source.name,
        kind=source.kind,
        website_url=source.website_url,
        logo_url=image_url(source.logo_path),
        active=source.active,
        deactivated_at=source.deactivated_at,
        title_count=title_count,
        last_synced_at=last_synced_at,
    )
