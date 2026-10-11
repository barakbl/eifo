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
import math
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
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

#: How much shared genre - in rarity, see :func:`genre_rarity` - makes a
#: match certain. Drama alone is about one, so two titles that are only both
#: dramas share a third of what this asks; crime and drama, or science
#: fiction, share all of it.
GENRE_EVIDENCE = 3.0


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
    count: bool = True,
    rarity: dict[int, float] | None = None,
) -> tuple[list[Similar], int]:
    """Titles most like the seed, best first, and how many there are.

    ``within`` narrows the candidates to a set of title ids - the catalog's
    own filters, so "like this, on my services, available now, unseen" is one
    question. A candidate has to share at least a genre, a director or a lead:
    a film from the same year in the same language is not "like" anything.
    ``rarity`` is :func:`genre_rarity`, for a caller asking about many seeds.
    """
    if rarity is None:
        rarity = genre_rarity(session)
    weight = _rarity_of(rarity)
    shared_genres = (
        select(
            TitleGenre.title_id,
            func.count().label("shared"),
            func.sum(weight).label("rarity"),
        )
        .where(TitleGenre.genre_id.in_(seed.genres or [-1]))
        .group_by(TitleGenre.title_id)
        .subquery()
    )
    genre_counts = (
        select(TitleGenre.title_id, func.sum(weight).label("rarity"))
        .group_by(TitleGenre.title_id)
        .subquery()
    )
    shared_directors = _shared_people(CreditRole.DIRECTOR, seed.directors)
    shared_leads = _shared_people(CreditRole.CAST, seed.leads, leads_only=True)

    genres = func.coalesce(shared_genres.c.shared, 0)
    directors = func.coalesce(shared_directors.c.shared, 0)
    leads = func.coalesce(shared_leads.c.shared, 0)

    score = (
        _genre_term(
            func.coalesce(shared_genres.c.rarity, 0),
            func.coalesce(genre_counts.c.rarity, 0),
            sum(rarity.get(genre, 0.0) for genre in seed.genres),
        )
        # One director's worth however many are shared: co-directors are one
        # hand, and so is a person the catalog holds twice under two names.
        + case((directors > 0, DIRECTOR_WEIGHT), else_=0)
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

    # The count is a second pass over every candidate; a caller merging
    # several seeds' best few has no use for it.
    total = (
        session.scalar(select(func.count()).select_from(statement.subquery())) or 0 if count else -1
    )
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


def genre_rarity(session: Session) -> dict[int, float]:
    """How telling each genre is: the log of how few titles carry it.

    Half the catalog is drama, so two titles both being dramas says little;
    two westerns say a lot. Inverse document frequency, in other words.
    """
    titles = session.scalar(select(func.count(func.distinct(TitleGenre.title_id)))) or 0
    return {
        genre_id: math.log(titles / carrying)
        for genre_id, carrying in session.execute(
            select(TitleGenre.genre_id, func.count()).group_by(TitleGenre.genre_id)
        ).tuples()
        if carrying
    }


def _rarity_of(rarity: dict[int, float]) -> Any:
    if not rarity:
        return literal(0.0)
    return case(rarity, value=TitleGenre.genre_id, else_=0.0)


def _genre_term(shared: Any, candidate: Any, seed_total: float) -> Any:
    """Genre overlap, times the weight: rarity shared over rarity in either,
    and scaled down when what is shared is too common to tell much
    (:data:`GENRE_EVIDENCE`).
    """
    if seed_total <= 0:
        return literal(0)
    union = candidate + seed_total - shared
    certainty = case((shared >= GENRE_EVIDENCE, 1.0), else_=shared * 1.0 / GENRE_EVIDENCE)
    return case((union > 0, shared * GENRE_WEIGHT * certainty / union), else_=0)


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


# -- recommendations --------------------------------------------------------

#: Favourites a recommendation starts from: the best rated, newest first.
#: Enough that a member's every 10 has a say - a favourite director whose
#: films all sat ninth in line used to recommend nothing.
SEEDS = 16
#: Candidates read per favourite. Enough that the merge has a choice; past
#: this a seed's suggestions are the genre rather than the film.
PER_SEED = 40
#: The most picks one favourite may account for. Without it a member who
#: loved one film of a franchise gets the franchise.
PER_SEED_PICKS = 3
#: What a second, third... favourite agreeing on a candidate adds, as a share
#: of its own match. Agreement is a signal; it is not a vote to be counted.
AGREEMENT = 0.3
#: The most agreement can add. With a dozen favourites, half of them crime,
#: every crime drama is proposed six times over and would fill the row on
#: votes alone; what the member leans toward is :class:`Profile`'s to say.
AGREEMENT_CAP = 15

#: How far the member's whole taste moves a pick, in match points per unit of
#: lean (see :class:`Profile`). Leans are small - a member's favourite
#: director is about half a spread above their average - so a pick by them,
#: in genres and a decade they favour, rises by a director's worth of match;
#: an animated film, to somebody who rates animation lowest, sinks as far.
PROFILE_WEIGHT = 20
#: What each facet of a candidate counts for in its lean. Directors and
#: genres say most; a decade or a country is a hint.
FACET_WEIGHTS = {
    "directors": 1.0,
    "genres": 1.0,
    "cast": 0.5,
    "decades": 0.5,
    "kinds": 0.5,
    "countries": 0.3,
    "languages": 0.3,
}
#: A title on the watchlist, unrated, counts as this share of a rating at the
#: member's favourite floor: wanting to see something is a mild "like".
WATCHLIST_WEIGHT = 0.5
#: The least spread a member's ratings are taken to have. Somebody who gave
#: three films a 9 has no spread, and every difference would be infinite.
MIN_SPREAD = 1.0


@dataclass(frozen=True, slots=True)
class Pick:
    """One recommendation, and the favourite it came from."""

    title_id: int
    score: float
    seed_id: int
    seed_rating: int
    shared_genres: tuple[int, ...]
    shared_people: tuple[int, ...]


def favourite_floor(session: Session, user_id: int) -> int:
    """The lowest rating that makes a title a member's favourite.

    :data:`LIKED`, or the member's own average if that is higher: to somebody
    who gives most films a 9, an 8 is a disappointment. Never above one more
    than :data:`LIKED`, though - a 9 is a favourite whoever gave it.
    """
    mean = session.scalar(
        select(func.avg(UserItem.rating)).where(
            UserItem.user_id == user_id, UserItem.rating.is_not(None)
        )
    )
    return max(LIKED, min(round(mean), LIKED + 1)) if mean is not None else LIKED


@dataclass(frozen=True, slots=True)
class Profile:
    """How a member leans on every facet of what they rated or want to see.

    A lean is how far their ratings of a facet's titles sit from their own
    average, pulled toward it by :data:`PRIOR_WEIGHT` imaginary ratings, in
    units of their own spread: +1 is "a spread above what I usually give".
    Only facets they have rated anything of are here; the rest lean nothing.
    """

    leans: dict[str, dict[str, float]] = field(default_factory=dict)

    def lean(self, facets: dict[str, set[str]], rarity: dict[int, float]) -> float:
        """How the member leans toward a title with these facets."""
        total = 0.0
        for facet, weight in FACET_WEIGHTS.items():
            leans = self.leans.get(facet, {})
            known = [key for key in facets.get(facet, ()) if key in leans]
            if not known:
                continue
            if facet == "genres":
                # Weighed by rarity: a disliked animation says more about a
                # title than a liked drama does.
                weights = [rarity.get(int(key), 0.0) for key in known]
                if sum(weights) <= 0:
                    continue
                value = sum(leans[key] * w for key, w in zip(known, weights, strict=True)) / sum(
                    weights
                )
            elif facet == "cast":
                # Leads add up - two loved actors are more than one - up to a
                # director's worth.
                value = max(-2.0, min(2.0, sum(leans[key] for key in known)))
            else:
                value = sum(leans[key] for key in known) / len(known)
            total += weight * value
        return total


def profile(session: Session, user_id: int) -> Profile:
    """Read a member's leanings from their ratings and their watchlist."""
    rows = session.execute(
        select(UserItem.title_id, UserItem.rating, UserItem.want_to_watch).where(
            UserItem.user_id == user_id,
            or_(UserItem.rating.is_not(None), UserItem.want_to_watch),
        )
    ).all()
    rated = [int(row.rating) for row in rows if row.rating is not None]
    if not rated:
        return Profile()
    mean = sum(rated) / len(rated)
    spread = max(MIN_SPREAD, (sum((r - mean) ** 2 for r in rated) / len(rated)) ** 0.5)
    floor = favourite_floor(session, user_id)

    # Every title as (rating, how much it counts).
    counted: dict[int, tuple[float, float]] = {
        row.title_id: (
            (float(row.rating), 1.0) if row.rating is not None else (float(floor), WATCHLIST_WEIGHT)
        )
        for row in rows
    }
    titles = session.scalars(select(Title).where(Title.id.in_(list(counted)))).all()
    leans: dict[str, dict[str, float]] = {}
    for facet, by_key in _facets(session, titles).items():
        leans[facet] = {}
        for key, (_, title_ids) in by_key.items():
            total = sum(counted[t][0] * counted[t][1] for t in title_ids)
            weight = sum(counted[t][1] for t in title_ids)
            weighted = (total + PRIOR_WEIGHT * mean) / (weight + PRIOR_WEIGHT)
            leans[facet][key] = (weighted - mean) / spread
    return Profile(leans=leans)


def _facets_by_title(session: Session, title_ids: Sequence[int]) -> dict[int, dict[str, set[str]]]:
    """Every facet of each title, as ``title id -> facet -> keys``."""
    titles = session.scalars(select(Title).where(Title.id.in_(list(title_ids)))).all()
    found: dict[int, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for facet, by_key in _facets(session, titles).items():
        for key, (_, held) in by_key.items():
            for title_id in held:
                found[title_id][facet].add(key)
    return found


def for_you(
    session: Session,
    user_id: int,
    *,
    within: Select[tuple[int]] | None = None,
    limit: int = 12,
) -> list[Pick]:
    """Titles a member is likely to enjoy, from what they rated highly.

    Each favourite (see :func:`favourite_floor`) proposes the titles most
    like it; a candidate keeps its best proposal, weighted by how much the
    member liked the favourite, plus a little for every other favourite that
    proposed it too, plus how the member leans toward everything the
    candidate is (:class:`Profile`) - its director, leads, genres, decade -
    across all they rated, not only the one favourite. ``within`` is where
    to look - the member's services, available now, nothing they already
    have - and is the caller's to decide.
    """
    seeds = session.execute(
        select(Title, UserItem.rating)
        .join(UserItem, UserItem.title_id == Title.id)
        .where(
            UserItem.user_id == user_id,
            UserItem.rating >= favourite_floor(session, user_id),
            # A favourite with no genres and no credits - a film a member
            # added by hand, say - is like nothing, and would waste a seat.
            or_(
                select(TitleGenre.title_id).where(TitleGenre.title_id == Title.id).exists(),
                select(Credit.title_id).where(Credit.title_id == Title.id).exists(),
            ),
        )
        .order_by(UserItem.rating.desc(), UserItem.updated_at.desc(), Title.id)
        .limit(SEEDS)
    ).all()

    # Never what they rated: their favourites would otherwise recommend each
    # other, whatever the caller narrowed to.
    unrated = select(Title.id).where(
        Title.id.not_in(
            select(UserItem.title_id).where(
                UserItem.user_id == user_id, UserItem.rating.is_not(None)
            )
        )
    )
    if within is not None:
        unrated = unrated.where(Title.id.in_(within))

    rarity = genre_rarity(session)
    best: dict[int, Pick] = {}
    agreeing: dict[int, float] = defaultdict(float)
    for seed_title, rating in seeds:
        found, _ = similar(
            session,
            seed_of(session, seed_title),
            within=unrated,
            limit=PER_SEED,
            count=False,
            rarity=rarity,
        )
        weight = rating / 10
        for candidate in found:
            score = candidate.similarity * weight
            agreeing[candidate.title_id] += score
            held = best.get(candidate.title_id)
            if held is None or score > held.score:
                best[candidate.title_id] = Pick(
                    title_id=candidate.title_id,
                    score=score,
                    seed_id=seed_title.id,
                    seed_rating=int(rating),
                    shared_genres=candidate.shared_genres,
                    shared_people=candidate.shared_people,
                )

    taste_of = profile(session, user_id)
    facets = _facets_by_title(session, list(best))
    ranked = sorted(
        (
            replace(
                pick,
                score=pick.score
                + min(AGREEMENT_CAP, AGREEMENT * (agreeing[pick.title_id] - pick.score))
                + PROFILE_WEIGHT * taste_of.lean(facets.get(pick.title_id, {}), rarity),
            )
            for pick in best.values()
        ),
        key=lambda pick: (-pick.score, pick.title_id),
    )
    picks: list[Pick] = []
    per_seed: dict[int, int] = defaultdict(int)
    for pick in ranked:
        if per_seed[pick.seed_id] >= PER_SEED_PICKS:
            continue
        per_seed[pick.seed_id] += 1
        picks.append(pick)
        if len(picks) >= limit:
            break
    return picks
