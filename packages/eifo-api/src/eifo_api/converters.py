"""Turning ORM rows into response models.

Kept apart from the routers so the mapping is testable on its own and the
handlers stay short.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable

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
    RatingGroupOut,
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
    RatingProviderInfo,
    Source,
    Title,
    User,
    UserItem,
)

IMAGES_PREFIX = "/images"

#: Providers reported as percentages rather than out of ten.
_PERCENT_PROVIDERS = frozenset({RatingProvider.RT_CRITICS, RatingProvider.RT_AUDIENCE})


class ProviderRegistry:
    """How each ratings provider credits itself, as its plugin declared it.

    This used to be a dictionary here - names for seven providers, kept by hand
    in a package that has never met an enricher. It could disagree with the
    plugin that produced the score, it had to be edited to add a provider, and
    it had nowhere to say which figures belong to the same service or where
    that service's mark is.

    Now the fetcher writes what the plugins declare into ``rating_providers``
    and this reads it. A provider with no row is still shown - its key is a
    worse name than "Tomatometer" but a better one than nothing - because the
    alternative is a score on the page with no source against it, and a rating
    without its source is a rumour.
    """

    def __init__(self, rows: Iterable[RatingProviderInfo]) -> None:
        self._rows = {RatingProvider(row.provider): row for row in rows}

    @classmethod
    def load(cls, session: Session) -> ProviderRegistry:
        return cls(session.scalars(select(RatingProviderInfo)).all())

    @classmethod
    def empty(cls) -> ProviderRegistry:
        """A registry that knows nothing, for callers without a session."""
        return cls([])

    def label(self, provider: RatingProvider) -> str:
        """What to call this particular figure."""
        row = self._rows.get(provider)
        return row.label if row else provider.value

    def row(self, provider: RatingProvider) -> RatingProviderInfo | None:
        return self._rows.get(provider)

    def group_key(self, provider: RatingProvider) -> str:
        """Which chip this figure belongs in.

        Its own provider key when nothing has been declared, which puts an
        unknown provider in a chip of its own - the shape the page had before
        grouping existed, and the right guess when nobody has said otherwise.
        """
        row = self._rows.get(provider)
        return row.group_key if row else provider.value


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


def to_rating(rating: ExternalRating, registry: ProviderRegistry) -> RatingOut:
    provider = RatingProvider(rating.provider)
    return RatingOut(
        provider=provider,
        provider_name=registry.label(provider),
        score_raw=rating.score_raw,
        score_display=score_display(provider, rating.score_raw),
        score_normalized=rating.score_normalized,
        vote_count=rating.vote_count,
        url=rating.url,
    )


def to_rating_groups(
    ratings: list[ExternalRating],
    registry: ProviderRegistry,
) -> list[RatingGroupOut]:
    """The same ratings, gathered into one chip per service.

    Order is the order the providers are declared in :class:`RatingProvider`,
    and within a chip the order the plugin gave - critics before the crowd,
    which is how both sites that report two figures print them. Neither is a
    judgement made here: a page that reshuffled its raters between two loads
    would be unreadable, so it needs *an* order, and the schema's own is the
    one thing every deployment already agrees on.
    """
    order = {provider: index for index, provider in enumerate(RatingProvider)}
    grouped: dict[str, list[ExternalRating]] = {}
    for rating in sorted(ratings, key=lambda r: order.get(RatingProvider(r.provider), 99)):
        grouped.setdefault(registry.group_key(RatingProvider(rating.provider)), []).append(rating)

    groups = []
    for key, members in grouped.items():
        members.sort(key=lambda r: _position(registry, RatingProvider(r.provider)))
        first = registry.row(RatingProvider(members[0].provider))
        groups.append(
            RatingGroupOut(
                key=key,
                # The service's name where one is recorded, and this figure's
                # own name where none is: a chip has to say something, and an
                # undeclared provider's key is the only true thing available.
                name=first.group_name
                if first
                else registry.label(RatingProvider(members[0].provider)),
                logo_url=image_url(first.logo_path) if first else None,
                # The title's page on that service, which both of a pair carry
                # and which is the link worth having. The service's front page
                # only when no score brought one.
                url=next((r.url for r in members if r.url), first.website_url if first else None),
                scores=[to_rating(rating, registry) for rating in members],
            )
        )
    return groups


def _position(registry: ProviderRegistry, provider: RatingProvider) -> int:
    row = registry.row(provider)
    return row.position if row else 0


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


def to_detail(title: Title, registry: ProviderRegistry | None = None) -> TitleDetail:
    """A full title, including availability that has lapsed.

    ``registry`` is how the ratings are credited. It defaults to an empty one
    so a caller with no session still gets a title back - every score then
    carries its provider key rather than its name, which is the honest thing to
    show when nothing has said what the name is.
    """
    registry = registry or ProviderRegistry.empty()
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
        ratings=[to_rating(rating, registry) for rating in title.ratings],
        rating_groups=to_rating_groups(list(title.ratings), registry),
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
        want_to_watch=item.want_to_watch,
        watched=item.watched,
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
