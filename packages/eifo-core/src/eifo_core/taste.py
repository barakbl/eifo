"""What a member likes, and what else is like a title.

Two questions a recommendation turns on, answered from data the catalog
already holds - a member's ratings, and the genres, people, years and
languages of every title - rather than from a model trained on anything.

**Taste** is read from ratings alone. An average over one film says nothing,
so every leaning is pulled toward the member's own mean by a couple of
imaginary ratings (:data:`PRIOR_WEIGHT`): a director rated 10 once sits below
one rated 9 four times, which is what a person would say too.

**Similarity** is a weighted overlap - shared genres, shared directors and
leads, era, language, kind - plus a little quality, so that of two equally
similar titles the better one comes first. Computed in SQL over the whole
catalog, because the candidates for a drama number in the thousands and are
filtered by service and availability before any of them is worth loading.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import ColumnElement, Integer, Select, and_, case, cast, func, literal, or_, select
from sqlalchemy.orm import Session

from eifo_core.enums import CreditRole, TitleKind
from eifo_core.models import (
    AggregateScore,
    Credit,
    Genre,
    Person,
    Title,
    TitleGenre,
    UserItem,
)

#: A rating at or above this is a title the member liked; at or below
#: :data:`DISLIKED`, one they did not.
LIKED = 8
DISLIKED = 4

#: Imaginary ratings at the member's own mean, added to every leaning. The
#: whole of the defence against one 10 making a favourite director.
PRIOR_WEIGHT = 2

#: Billed above this, an actor is a lead (billing starts at 0, so the first
#: six). Below it, a face in a crowd that two films sharing says nothing about.
LEAD_BILLING = 6

#: How many of a list to return. Enough for a model to see a pattern.
SHOWN = 8


@dataclass(frozen=True, slots=True)
class Leaning:
    """One thing the member has rated titles of, and how they rated them."""

    key: str
    name: str
    name_he: str | None
    titles: int
    average: float
    #: The average pulled toward the member's mean: what the lists are ranked
    #: by, so a single title cannot top one.
    weighted: float
    id: int | None = None


@dataclass(frozen=True, slots=True)
class RatedTitle:
    title_id: int
    name: str
    name_he: str | None
    year: int | None
    rating: int


@dataclass(slots=True)
class Taste:
    """A member's taste, as far as their ratings tell."""

    rated: int = 0
    average: float | None = None
    #: How many titles at each rating, 1 to 10.
    distribution: dict[int, int] = field(default_factory=dict)
    #: The member's rating against the catalog's, on the catalog's 0-100
    #: scale: positive rates kinder than the critics, negative harsher. None
    #: when nothing they rated has a score.
    against_consensus: float | None = None
    favourites: list[RatedTitle] = field(default_factory=list)
    dislikes: list[RatedTitle] = field(default_factory=list)
    liked: dict[str, list[Leaning]] = field(default_factory=dict)
    disliked: dict[str, list[Leaning]] = field(default_factory=dict)


def taste(session: Session, user_id: int) -> Taste:
    """Read a member's taste from what they rated."""
    rows = session.execute(
        select(UserItem.title_id, UserItem.rating, UserItem.updated_at).where(
            UserItem.user_id == user_id, UserItem.rating.is_not(None)
        )
    ).all()
    if not rows:
        return Taste()

    ratings = {row.title_id: int(row.rating) for row in rows}
    touched = {row.title_id: row.updated_at for row in rows}
    ids = list(ratings)
    titles = {title.id: title for title in session.scalars(select(Title).where(Title.id.in_(ids)))}
    mean = sum(ratings.values()) / len(ratings)

    found = Taste(
        rated=len(ratings),
        average=round(mean, 2),
        distribution=dict(sorted(_count(ratings.values()).items())),
        against_consensus=_against_consensus(session, ratings),
    )

    ranked = sorted(
        (title_id for title_id in ratings if title_id in titles),
        key=lambda title_id: (-ratings[title_id], -_epoch(touched[title_id]), title_id),
    )
    found.favourites = [
        _rated(titles[title_id], ratings[title_id])
        for title_id in ranked
        if ratings[title_id] >= LIKED
    ][:10]
    found.dislikes = [
        _rated(titles[title_id], ratings[title_id])
        for title_id in reversed(ranked)
        if ratings[title_id] <= DISLIKED
    ][:5]

    facets = _facets(session, titles.values())
    for facet, by_key in facets.items():
        leanings = [
            _leaning(key, names, [ratings[title_id] for title_id in title_ids], mean)
            for key, (names, title_ids) in by_key.items()
        ]
        minimum = 2 if facet in ("cast", "genres") else 1
        found.liked[facet] = sorted(
            (
                leaning
                for leaning in leanings
                if leaning.titles >= minimum
                and leaning.weighted > mean
                and leaning.average >= LIKED - 1
            ),
            key=lambda leaning: (-leaning.weighted, -leaning.titles, leaning.name),
        )[:SHOWN]
        found.disliked[facet] = sorted(
            (
                leaning
                for leaning in leanings
                if leaning.titles >= minimum
                and leaning.weighted < mean
                and leaning.average <= DISLIKED + 1
            ),
            key=lambda leaning: (leaning.weighted, -leaning.titles, leaning.name),
        )[: SHOWN // 2]
    return found


_Names = tuple[str, str | None, int | None]


def _facets(
    session: Session, titles: Iterable[Title]
) -> dict[str, dict[str, tuple[_Names, list[int]]]]:
    """Every facet of the rated titles, as ``key -> (names, title ids)``."""
    titles = list(titles)
    ids = [title.id for title in titles]
    facets: dict[str, dict[str, tuple[_Names, list[int]]]] = {
        name: {}
        for name in ("genres", "directors", "cast", "countries", "languages", "decades", "kinds")
    }

    def add(facet: str, key: str, names: _Names, title_id: int) -> None:
        held = facets[facet].setdefault(key, (names, []))
        if title_id not in held[1]:
            held[1].append(title_id)

    for title in titles:
        if title.year:
            decade = title.year // 10 * 10
            add("decades", str(decade), (f"{decade}s", None, None), title.id)
        if title.original_language:
            add(
                "languages",
                title.original_language,
                (title.original_language, None, None),
                title.id,
            )
        for code in (title.origin_countries or "").split(","):
            if code.strip():
                add("countries", code.strip(), (code.strip(), None, None), title.id)
        add("kinds", title.type.value, (title.type.value, None, None), title.id)

    for title_id, genre_id, name_en, name_he in session.execute(
        select(TitleGenre.title_id, Genre.id, Genre.name_en, Genre.name_he)
        .join(Genre, Genre.id == TitleGenre.genre_id)
        .where(TitleGenre.title_id.in_(ids))
    ):
        add("genres", str(genre_id), (name_en, name_he, genre_id), title_id)

    for title_id, role, person_id, name_en, name_he in session.execute(
        select(Credit.title_id, Credit.role, Person.id, Person.name_en, Person.name_he)
        .join(Person, Person.id == Credit.person_id)
        .where(
            Credit.title_id.in_(ids),
            or_(
                Credit.role == CreditRole.DIRECTOR,
                and_(Credit.role == CreditRole.CAST, Credit.billing_order < LEAD_BILLING),
            ),
        )
    ):
        facet = "directors" if role == CreditRole.DIRECTOR else "cast"
        add(facet, str(person_id), (name_en or name_he or "", name_he, person_id), title_id)

    return facets


def _leaning(key: str, names: _Names, ratings: Sequence[int], mean: float) -> Leaning:
    name, name_he, ident = names
    total = sum(ratings)
    return Leaning(
        key=key,
        name=name,
        name_he=name_he,
        titles=len(ratings),
        average=round(total / len(ratings), 2),
        weighted=round((total + PRIOR_WEIGHT * mean) / (len(ratings) + PRIOR_WEIGHT), 2),
        id=ident,
    )


def _against_consensus(session: Session, ratings: dict[int, int]) -> float | None:
    scores = dict(
        session.execute(
            select(AggregateScore.title_id, AggregateScore.score).where(
                AggregateScore.title_id.in_(list(ratings)), AggregateScore.score.is_not(None)
            )
        )
        .tuples()
        .all()
    )
    gaps = [
        ratings[title_id] * 10 - score for title_id, score in scores.items() if score is not None
    ]
    return round(sum(gaps) / len(gaps), 1) if gaps else None


def _rated(title: Title, rating: int) -> RatedTitle:
    return RatedTitle(
        title_id=title.id,
        name=title.name_en or title.name_he or "",
        name_he=title.name_he,
        year=title.year,
        rating=rating,
    )


def _count(values: Iterable[int]) -> dict[int, int]:
    counts: dict[int, int] = defaultdict(int)
    for value in values:
        counts[value] += 1
    return counts


def _epoch(value: dt.datetime | None) -> float:
    return value.timestamp() if value else 0.0


# -- similarity -------------------------------------------------------------

#: What each kind of overlap is worth, out of roughly a hundred. Genres lead -
#: they are what "like this" mostly means - but a shared director outweighs a
#: perfect genre match on its own: a director's other work is the likeliest
#: thing somebody who loved one of their films will love next, and the pure
#: genre match is one of thousands.
GENRE_WEIGHT = 40
DIRECTOR_WEIGHT = 25
LEAD_WEIGHT = 6
ERA_WEIGHT = 10
LANGUAGE_WEIGHT = 5
KIND_WEIGHT = 5
QUALITY_WEIGHT = 15

#: Years apart at which era stops counting at all.
ERA_SPAN = 30


@dataclass(frozen=True, slots=True)
class Seed:
    """What a title is, for comparing others with it."""

    title_id: int
    kind: TitleKind
    year: int | None
    language: str | None
    genres: frozenset[int]
    directors: frozenset[int]
    leads: frozenset[int]


@dataclass(frozen=True, slots=True)
class Similar:
    title_id: int
    #: Out of roughly a hundred; only meaningful against others for one seed.
    similarity: int
    shared_genres: tuple[int, ...]
    shared_people: tuple[int, ...]


def seed_of(session: Session, title: Title) -> Seed:
    genres = session.scalars(select(TitleGenre.genre_id).where(TitleGenre.title_id == title.id))
    directors = session.scalars(
        select(Credit.person_id).where(
            Credit.title_id == title.id, Credit.role == CreditRole.DIRECTOR
        )
    )
    leads = session.scalars(
        select(Credit.person_id)
        .where(
            Credit.title_id == title.id,
            Credit.role == CreditRole.CAST,
            Credit.billing_order.is_not(None),
        )
        .order_by(Credit.billing_order)
        .limit(LEAD_BILLING)
    )
    return Seed(
        title_id=title.id,
        kind=title.type,
        year=title.year,
        language=title.original_language,
        genres=frozenset(genres),
        directors=frozenset(directors),
        leads=frozenset(leads),
    )


def similar(
    session: Session,
    seed: Seed,
    *,
    within: Select[tuple[int]] | None = None,
    limit: int = 20,
    offset: int = 0,
) -> tuple[list[Similar], int]:
    """Titles most like the seed, best first, and how many there are.

    ``within`` narrows the candidates to a set of title ids - the catalog's
    own filters, so "like this, on my services, available now, unseen" is one
    question. A candidate has to share at least a genre, a director or a lead:
    a film from the same year in the same language is not "like" anything.
    """
    shared_genres = (
        select(TitleGenre.title_id, func.count().label("shared"))
        .where(TitleGenre.genre_id.in_(seed.genres or [-1]))
        .group_by(TitleGenre.title_id)
        .subquery()
    )
    genre_counts = (
        select(TitleGenre.title_id, func.count().label("count"))
        .group_by(TitleGenre.title_id)
        .subquery()
    )
    shared_directors = _shared_people(CreditRole.DIRECTOR, seed.directors)
    shared_leads = _shared_people(CreditRole.CAST, seed.leads, leads_only=True)

    genres = func.coalesce(shared_genres.c.shared, 0)
    directors = func.coalesce(shared_directors.c.shared, 0)
    leads = func.coalesce(shared_leads.c.shared, 0)

    score = (
        _genre_term(genres, func.coalesce(genre_counts.c.count, 0), len(seed.genres))
        + case((directors >= 2, 2 * DIRECTOR_WEIGHT), else_=directors * DIRECTOR_WEIGHT)
        + case((leads >= 3, 3 * LEAD_WEIGHT), else_=leads * LEAD_WEIGHT)
        + _era_term(seed.year)
        + (
            case((Title.original_language == seed.language, LANGUAGE_WEIGHT), else_=0)
            if seed.language
            else literal(0)
        )
        + case((Title.type == seed.kind, KIND_WEIGHT), else_=0)
        + func.coalesce(AggregateScore.score, 0) * QUALITY_WEIGHT / 100.0
    ).label("similarity")

    statement = (
        select(Title.id, score)
        .outerjoin(shared_genres, shared_genres.c.title_id == Title.id)
        .outerjoin(genre_counts, genre_counts.c.title_id == Title.id)
        .outerjoin(shared_directors, shared_directors.c.title_id == Title.id)
        .outerjoin(shared_leads, shared_leads.c.title_id == Title.id)
        .outerjoin(AggregateScore, AggregateScore.title_id == Title.id)
        .where(
            Title.id != seed.title_id,
            or_(genres > 0, directors > 0, leads > 0),
        )
    )
    if within is not None:
        statement = statement.where(Title.id.in_(within))

    total = session.scalar(select(func.count()).select_from(statement.subquery())) or 0
    rows = session.execute(
        statement.order_by(score.desc(), func.coalesce(AggregateScore.score, 0).desc(), Title.id)
        .limit(limit)
        .offset(offset)
    ).all()
    # Why, for the page only: what each shares with the seed, in two queries.
    ids = [row.id for row in rows]
    genres_of = _genres_of(session, ids)
    people_of = _people_of(session, ids)
    seed_people = seed.directors | seed.leads
    return [
        Similar(
            title_id=row.id,
            similarity=round(float(row.similarity)),
            shared_genres=tuple(sorted(genres_of.get(row.id, set()) & seed.genres)),
            shared_people=tuple(sorted(people_of.get(row.id, set()) & seed_people)),
        )
        for row in rows
    ], total


def _shared_people(role: CreditRole, people: frozenset[int], *, leads_only: bool = False) -> Any:
    conditions: list[ColumnElement[bool]] = [
        Credit.role == role,
        Credit.person_id.in_(people or [-1]),
    ]
    if leads_only:
        conditions.append(Credit.billing_order < LEAD_BILLING)
    return (
        select(Credit.title_id, func.count(func.distinct(Credit.person_id)).label("shared"))
        .where(*conditions)
        .group_by(Credit.title_id)
        .subquery()
    )


def _genre_term(shared: Any, count: Any, seed_count: int) -> Any:
    """Jaccard overlap of genres, times the weight: shared over their union."""
    if not seed_count:
        return literal(0)
    union = count + seed_count - shared
    return case((union > 0, shared * GENRE_WEIGHT * 1.0 / union), else_=0)


def _era_term(year: int | None) -> Any:
    """Full marks for the same year, nothing at :data:`ERA_SPAN` apart."""
    if year is None:
        return literal(0)
    gap = func.abs(Title.year - year)
    return case(
        (Title.year.is_(None), 0),
        (gap >= ERA_SPAN, 0),
        else_=ERA_WEIGHT * (1 - cast(gap, Integer) * 1.0 / ERA_SPAN),
    )


def _genres_of(session: Session, ids: list[int]) -> dict[int, set[int]]:
    found: dict[int, set[int]] = defaultdict(set)
    for title_id, genre_id in session.execute(
        select(TitleGenre.title_id, TitleGenre.genre_id).where(TitleGenre.title_id.in_(ids))
    ):
        found[title_id].add(genre_id)
    return found


def _people_of(session: Session, ids: list[int]) -> dict[int, set[int]]:
    found: dict[int, set[int]] = defaultdict(set)
    for title_id, person_id in session.execute(
        select(Credit.title_id, Credit.person_id).where(
            Credit.title_id.in_(ids),
            or_(
                Credit.role == CreditRole.DIRECTOR,
                and_(Credit.role == CreditRole.CAST, Credit.billing_order < LEAD_BILLING),
            ),
        )
    ):
        found[title_id].add(person_id)
    return found
