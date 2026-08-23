"""Turning ORM rows into response models.

Kept apart from the routers so the mapping is testable on its own and the
handlers stay short.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from eifo_api.schemas import (
    AggregateOut,
    AvailabilityOut,
    CreditOut,
    GenreOut,
    PersonCredit,
    PersonDetail,
    PersonRef,
    RatingOut,
    SourceOut,
    TitleCard,
    TitleDetail,
    UserItemOut,
    UserOut,
)
from eifo_core.enums import CreditRole, RatingProvider
from eifo_core.models import (
    AggregateScore,
    Availability,
    Credit,
    ExternalRating,
    Genre,
    Person,
    Source,
    Title,
    User,
    UserItem,
)

IMAGES_PREFIX = "/images"

#: How each provider is credited in the UI. A score without an attributed
#: source is a rumour, so every rating carries one.
PROVIDER_NAMES = {
    RatingProvider.IMDB: "IMDb",
    RatingProvider.TMDB: "TMDB",
    RatingProvider.RT_CRITICS: "Rotten Tomatoes - Tomatometer",
    RatingProvider.RT_AUDIENCE: "Rotten Tomatoes - Audience",
    RatingProvider.SERET_CRITICS: "סרט - מבקרים",
    RatingProvider.SERET_VIEWERS: "סרט - צופים",
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
        price_minor=availability.price_minor,
        price_currency=availability.price_currency,
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
        original_language=title.original_language,
        origin_countries=split_codes(title.origin_countries),
        credits=[
            to_credit(credit) for credit in sort_credits(preferred_credits(list(title.credits)))
        ],
        ratings=[to_rating(rating) for rating in title.ratings],
        aggregate=to_aggregate(title.aggregate),
    )


#: How credits read on a page: who made it, then who shot it, then who is in
#: it. Storage order is alphabetical by role, which would open on the cast.
_ROLE_ORDER = {
    CreditRole.DIRECTOR: 0,
    CreditRole.CINEMATOGRAPHER: 1,
    CreditRole.CAST: 2,
}


#: Who to believe when two sources credit the same role. A Hebrew name scraped
#: from an archive and TMDB's Latin one are the same human, and listing both
#: would show one director twice under two spellings - so where TMDB has spoken
#: about a role, it wins. A role TMDB says nothing about keeps what the
#: catalogue knew, which for most Israeli cinema is the only credit there is.
TMDB_SOURCE = "tmdb"


def preferred_credits(credits: list[Credit]) -> list[Credit]:
    """One source per role, TMDB first (see :data:`TMDB_SOURCE`)."""
    by_role: dict[CreditRole, list[Credit]] = {}
    for credit in credits:
        by_role.setdefault(credit.role, []).append(credit)

    kept: list[Credit] = []
    for entries in by_role.values():
        canonical = [credit for credit in entries if credit.source == TMDB_SOURCE]
        kept.extend(canonical or entries)
    return kept


def sort_credits(credits: list[Credit]) -> list[Credit]:
    """Crew first, then cast in billing order."""
    return sorted(
        credits,
        key=lambda credit: (
            _ROLE_ORDER.get(credit.role, len(_ROLE_ORDER)),
            credit.billing_order if credit.billing_order is not None else 1_000,
            credit.id,
        ),
    )


def split_codes(codes: str | None) -> list[str]:
    """ "IL,FR" as ["IL", "FR"], and an absent value as no countries at all."""
    return [code.strip() for code in (codes or "").split(",") if code.strip()]


def to_person_ref(person: Person) -> PersonRef:
    return PersonRef(
        id=person.id,
        name_he=person.name_he,
        name_en=person.name_en,
        profile_url=person.profile_source_url,
    )


def to_credit(credit: Credit) -> CreditOut:
    return CreditOut(
        role=credit.role,
        person=to_person_ref(credit.person),
        character=credit.character,
    )


def to_person_detail(person: Person, credits: list[Credit]) -> PersonDetail:
    """A person and their whole body of work.

    Ordered by role - director, cinematographer, then cast - so the page opens
    on what they are best known for making rather than on a bit part. The
    caller has already ordered each role's titles newest first.
    """
    ordered = sorted(credits, key=lambda credit: _ROLE_ORDER.get(credit.role, len(_ROLE_ORDER)))
    return PersonDetail(
        id=person.id,
        name_he=person.name_he,
        name_en=person.name_en,
        profile_url=person.profile_source_url,
        tmdb_id=person.tmdb_id,
        credits=[
            PersonCredit(role=credit.role, character=credit.character, title=to_card(credit.title))
            for credit in ordered
        ],
    )


def to_user(user: User) -> UserOut:
    """A user for their own eyes.

    Building this from named fields rather than the ORM object is what keeps
    ``email`` and the provider identity out of every response by construction.
    """
    return UserOut(
        id=user.id,
        display_name=user.display_name,
        handle=user.handle,
        avatar_url=user.avatar_url,
        is_public=user.is_public,
        my_source_ids=list(user.my_source_ids or []),
        created_at=user.created_at,
    )


def to_user_item(item: UserItem, *, title: Title | None = None) -> UserItemOut:
    return UserItemOut(
        title_id=item.title_id,
        status=item.status,
        rating=item.rating,
        note=item.note,
        updated_at=item.updated_at,
        title=to_card(title) if title is not None else None,
    )


def hydrate_titles(session: Session, title_ids: list[int]) -> list[Title]:
    """Load titles with everything a card or detail page needs, in order.

    One query with eager loads rather than lazy relationships: a 24-card grid
    should cost a couple of round trips, not dozens. Results are re-ordered to
    match ``title_ids``, since ``IN`` does not preserve the ordering a sort
    query established.
    """
    if not title_ids:
        return []

    titles = session.scalars(
        select(Title)
        .where(Title.id.in_(title_ids))
        .options(
            selectinload(Title.availability).selectinload(Availability.source),
            selectinload(Title.genres),
            selectinload(Title.ratings),
            selectinload(Title.aggregate),
            selectinload(Title.credits).selectinload(Credit.person),
        )
    ).all()

    by_id = {title.id: title for title in titles}
    return [by_id[title_id] for title_id in title_ids if title_id in by_id]


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
