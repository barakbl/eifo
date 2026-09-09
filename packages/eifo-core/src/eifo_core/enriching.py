"""Writing down what an enricher found.

The other half of :mod:`eifo_core.catalog`, for the other phase. An enricher is
a pure reader - it is handed a :class:`~eifo_core.findings.TitleView` and
returns what a ratings site says - and everything about turning that into rows
is here: which titles are due, what may be written over what, how a score is
normalised, and when the title next falls due.

Here rather than in the fetcher because the fetcher no longer writes it. An
enrich run ships its findings to the API and the API stores them, which is what
lets the run happen on a machine the catalog is not on.

**Metadata fills gaps only.** A patch never overwrites a field that already has
a value, so a scraped guess cannot displace TMDB's canonical answer - the one
rule in here that everything else is arranged around.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from eifo_core.enums import EnrichOutcome, RatingProvider
from eifo_core.findings import EnrichResult, Rating, TitleView
from eifo_core.items import plausible_year
from eifo_core.models import (
    AggregateScore,
    Availability,
    EnrichAttempt,
    ExternalRating,
    Genre,
    Source,
    Title,
    TitleGenre,
)
from eifo_core.naming import is_hebrew, latin_script
from eifo_core.people import apply_credits
from eifo_core.scores import RatingInput, aggregate, normalise
from eifo_core.settings import Settings
from eifo_core.types import utcnow

logger = logging.getLogger("eifo.enriching")

#: Told a rejected score and why. The fetcher passed a FetchContext in for
#: this; the API has no such thing, and neither needs the other's idea of what
#: to do about it.
RejectionSink = Callable[[str, Exception], None]

#: Aggregates recomputed between commits. Cheaper per row than enrichment - no
#: network - so a larger batch still keeps each write lock short.
AGGREGATE_COMMIT_EVERY = 200

#: Patchable fields the schema keeps unique, so a second writer collides.
_UNIQUE_FIELDS = frozenset({"tmdb_id", "imdb_id"})

#: Metadata fields an enricher may fill. Anything else in a patch is ignored,
#: so a provider cannot quietly write to columns it has no business setting.
PATCHABLE_FIELDS = frozenset(
    {
        "tmdb_id",
        "imdb_id",
        "name_he",
        "name_en",
        "overview_he",
        "overview_en",
        "year",
        "runtime_minutes",
        "seasons",
        "status",
        "poster_source_url",
        "original_language",
        "origin_countries",
    }
)


def titles_due(
    session: Session,
    settings: Settings,
    *,
    force: bool = False,
    limit: int | None = None,
) -> list[Title]:
    """Titles it is worth putting through the enrichers now.

    Least recently attempted first, with never attempted counting as infinitely
    long ago, so a run always advances instead of re-reading the head of the
    catalog. When a title next falls due is decided at the end of its last
    attempt: a rated one comes back on the refresh schedule, and one nobody
    could rate waits progressively longer.
    """
    batch = limit if limit is not None else settings.enrich.batch_size

    if force:
        return list(session.scalars(select(Title).order_by(Title.id).limit(batch)).all())

    statement = (
        select(Title)
        .outerjoin(EnrichAttempt, EnrichAttempt.title_id == Title.id)
        .where(or_(EnrichAttempt.title_id.is_(None), EnrichAttempt.due_at <= utcnow()))
        # Never attempted sorts first: is_(None) is true there, and true is the
        # high value, so descending puts it at the front. Spelled out rather
        # than left to the dialect, which may sort NULLs either way.
        .order_by(
            EnrichAttempt.attempted_at.is_(None).desc(),
            EnrichAttempt.attempted_at,
            Title.id,
        )
        .limit(batch)
    )
    return list(session.scalars(statement).all())


def record_attempt(
    session: Session,
    title: Title,
    settings: Settings,
    *,
    outcome: EnrichOutcome,
) -> None:
    """Write down that this title was tried, and when it is worth trying again."""
    now = utcnow()
    attempt = session.get(EnrichAttempt, title.id)
    previous = attempt.fruitless if attempt is not None else 0
    fruitless = 0 if outcome is EnrichOutcome.OK else previous + 1
    due_at = now + _wait_after(session, title, settings, outcome=outcome, fruitless=fruitless)

    if attempt is None:
        session.add(
            EnrichAttempt(
                title_id=title.id,
                attempted_at=now,
                outcome=outcome,
                fruitless=fruitless,
                due_at=due_at,
            )
        )
        return

    attempt.attempted_at = now
    attempt.outcome = outcome
    attempt.fruitless = fruitless
    attempt.due_at = due_at


def _wait_after(
    session: Session,
    title: Title,
    settings: Settings,
    *,
    outcome: EnrichOutcome,
    fruitless: int,
) -> dt.timedelta:
    """How long to leave a title alone after this outcome.

    A title that was rated comes back on the ordinary refresh schedule, sooner
    if some service currently carries it, since that is the one somebody may be
    looking at tonight. Everything else backs off, doubling with each
    consecutive empty-handed attempt, so the titles no provider covers cannot
    crowd out the ones worth asking about.
    """
    enrich = settings.enrich
    if outcome is EnrichOutcome.OK:
        fresher = _is_available(session, title.id)
        return dt.timedelta(days=enrich.hot_refresh_days if fresher else enrich.refresh_days)

    base = enrich.retry_error_days if outcome is EnrichOutcome.ERROR else enrich.retry_days
    return dt.timedelta(days=min(base * 2 ** max(fruitless - 1, 0), enrich.retry_max_days))


def _is_available(session: Session, title_id: int) -> bool:
    """Whether any service currently carries this title."""
    found = session.scalar(
        select(Availability.title_id)
        .where(Availability.title_id == title_id, Availability.is_current.is_(True))
        .limit(1)
    )
    return found is not None


def mislabelled_names(session: Session, *, limit: int | None = None) -> list[Title]:
    """Titles whose English name is not written in Latin script.

    These are the ones an en-US request can still fix: a title with no TMDB id
    has nobody to ask. Filtered in Python rather than SQL because "is this
    Latin" is a question about scripts, not about bytes, and a maintenance
    command can afford to read the column.
    """
    stored = session.scalars(
        select(Title)
        .where(Title.name_en.is_not(None), Title.tmdb_id.is_not(None))
        .order_by(Title.id)
    ).all()
    broken = [title for title in stored if title.name_en and not latin_script(title.name_en)]
    return broken[:limit] if limit is not None else broken


def outcome_of(title: Title, *, written: int, errored: bool) -> EnrichOutcome:
    """Read the outcome off what one title's pass through the enrichers produced.

    The order matters: a rating written is a success whatever else went wrong,
    and a provider failure says nothing about whether the title is rateable, so
    it outranks the two empty-handed verdicts below it.
    """
    if written:
        return EnrichOutcome.OK
    if errored:
        return EnrichOutcome.ERROR
    if title.tmdb_id is not None or title.imdb_id is not None:
        return EnrichOutcome.NO_DATA
    return EnrichOutcome.NO_MATCH


def store_ratings(
    session: Session,
    title: Title,
    ratings: list[Rating],
    rejected: RejectionSink,
) -> int:
    """Upsert one row per (title, provider)."""
    stored = {rating.provider: rating for rating in title.ratings}
    written = 0
    now = utcnow()

    for rating in ratings:
        try:
            normalized = normalise(rating.provider, rating.score_raw)
        except ValueError as exc:
            # An out-of-scale score means the parser is wrong; storing it would
            # quietly skew the aggregate.
            rejected(f"rejected {rating.provider} score for title {title.id}", exc)
            continue

        existing = stored.get(rating.provider)
        if existing is None:
            session.add(
                ExternalRating(
                    title_id=title.id,
                    provider=rating.provider,
                    score_raw=rating.score_raw,
                    score_normalized=normalized,
                    vote_count=rating.vote_count,
                    url=rating.url,
                    fetched_at=now,
                )
            )
        else:
            existing.score_raw = rating.score_raw
            existing.score_normalized = normalized
            existing.vote_count = rating.vote_count
            existing.url = rating.url or existing.url
            existing.fetched_at = now
        written += 1

    return written


def apply_offer_facts(session: Session, title: Title, result: EnrichResult) -> int:
    """Attach prices and links to offers this title already has.

    Never creates an offer and never revives a retired one. An enricher knows
    what a service charges, not what it carries - the harvester decides that,
    and a price arriving for something nobody is offering is a matching mistake
    rather than news.

    Fills rather than overwrites, on the same principle as ``metadata_patch``:
    a source that scrapes its own storefront knows its price better than a
    search API does, so the search only speaks where nothing else has.
    """
    if not result.offers:
        return 0

    rows = session.scalars(
        select(Availability)
        .join(Source, Source.id == Availability.source_id)
        .where(Availability.title_id == title.id, Availability.is_current.is_(True))
    ).all()
    by_offer = {(row.source.key, row.offer_type): row for row in rows}

    changed = 0
    for fact in result.offers:
        row = by_offer.get((fact.source_key, fact.offer_type))
        if row is None:
            continue
        touched = False
        if fact.price_minor is not None and row.price_minor is None:
            row.price_minor = fact.price_minor
            row.price_currency = fact.price_currency
            touched = True
        if fact.deep_link_url and not row.deep_link_url:
            row.deep_link_url = fact.deep_link_url
            touched = True
        changed += int(touched)
    return changed


def apply_patch(session: Session, title: Title, result: EnrichResult, *, source: str) -> bool:
    """Fill empty fields, and correct a name stored in the wrong script."""
    changed = False

    for field_name, value in result.metadata_patch.items():
        if field_name == "genres":
            changed |= _apply_genres(session, title, value)
            continue
        if field_name == "credits":
            changed |= _apply_credits(session, title, value, source=source)
            continue
        if field_name == "year":
            value = plausible_year(value)
        if field_name not in PATCHABLE_FIELDS or value in (None, ""):
            continue
        if field_name in _UNIQUE_FIELDS and _already_taken(session, field_name, value, title):
            continue
        if _may_write(title, field_name, value):
            setattr(title, field_name, value)
            changed = True

    return changed


def _may_write(title: Title, field_name: str, value: Any) -> bool:
    """Whether a patch may set this field.

    Fill empty fields only, with one exception: a name in the wrong script is
    not a name we have, it is one we mislabelled, and it stays mislabelled for
    ever if only emptiness can be overwritten. Every enrichment pass fetched the
    right English title for "千と千尋の神隠し" and threw it away because the
    column already held something.

    The replacement has to be in the right script itself, so this can only ever
    improve a name and never trade one wrong answer for another.
    """
    current = getattr(title, field_name, None)
    if current in (None, ""):
        return True
    if field_name == "name_en":
        return not latin_script(str(current)) and latin_script(str(value))
    if field_name == "name_he":
        return not is_hebrew(str(current)) and is_hebrew(str(value))
    return False


def _already_taken(session: Session, field_name: str, value: Any, title: Title) -> bool:
    """Whether another title already holds this external id.

    Writing one another title owns raises on the next flush and takes the whole
    run's remaining work with it. It also means something worth knowing: two
    titles the enricher believes are the same work. Recording that is the dedupe
    tool's job, so this only declines to write and says so.

    A TMDB id is only taken by a title of the same kind. TMDB numbers films and
    series separately, so movie 105 and series 105 are two works that share a
    number and the schema keys them as ``(type, tmdb_id)``. Compared without the
    kind, every film whose number some unrelated series already held was refused
    its own id - Back to the Future turned away because Sex and the City is
    series 105. IMDb ids need no such qualifier; those really are global.
    """
    column = getattr(Title, field_name)
    taken = select(Title.id).where(column == value, Title.id != title.id)
    if field_name == "tmdb_id":
        taken = taken.where(Title.type == title.type)

    owner = session.scalar(taken)
    if owner is None:
        return False

    logger.warning(
        "title %s and title %s both look like %s=%r; leaving it on %s",
        title.id,
        owner,
        field_name,
        value,
        owner,
    )
    return True


def _apply_credits(session: Session, title: Title, entries: Any, *, source: str) -> bool:
    """Attach who made this, crediting whoever said so."""
    if not isinstance(entries, list):
        return False
    return apply_credits(session, title, entries, source=source) > 0


def _apply_genres(session: Session, title: Title, genres: Any) -> bool:
    """Attach genres, creating any the catalog has not seen before."""
    if not isinstance(genres, list) or title.genres:
        return False

    changed = False
    # TMDB lists the same genre twice for the occasional title, and the join
    # table will not have it: the insert fails, the flush raises, and the whole
    # enrich run ends on one bad payload - twenty-two titles into a backlog of
    # thirty thousand. Deduplicated on the row rather than on the id TMDB sent,
    # so two of its ids resolving to one genre here is covered too.
    attached: set[int] = set()
    for entry in genres:
        if not isinstance(entry, dict) or not entry.get("tmdb_id"):
            continue
        genre = _get_or_create_genre(session, entry)
        if genre is None or genre.id in attached:
            continue
        attached.add(genre.id)
        session.add(TitleGenre(title_id=title.id, genre_id=genre.id))
        changed = True
    return changed


def _get_or_create_genre(session: Session, entry: dict[str, Any]) -> Genre | None:
    tmdb_id = int(entry["tmdb_id"])
    genre = session.scalar(select(Genre).where(Genre.tmdb_id == tmdb_id))
    if genre is not None:
        if not genre.name_he and entry.get("name_he"):
            genre.name_he = entry["name_he"]
        return genre

    name_en = entry.get("name_en")
    if not name_en:
        return None

    genre = Genre(tmdb_id=tmdb_id, name_en=name_en, name_he=entry.get("name_he"))
    session.add(genre)
    session.flush()
    return genre


def recompute(session: Session, title: Title, settings: Settings) -> bool:
    """Recompute a title's aggregate from whatever ratings it now has.

    Returns whether one was written, so the caller can count them. It used to
    take the tally and increment it, which meant this needed the fetcher's idea
    of what a run's totals look like in order to do arithmetic.
    """
    session.flush()
    ratings = session.scalars(
        select(ExternalRating).where(ExternalRating.title_id == title.id)
    ).all()
    if not ratings:
        return False

    computed = aggregate(
        [
            RatingInput(
                provider=RatingProvider(rating.provider),
                score_normalized=rating.score_normalized,
                vote_count=rating.vote_count,
            )
            for rating in ratings
        ],
        settings.scores,
    )

    stored = session.get(AggregateScore, title.id)
    if stored is None:
        session.add(
            AggregateScore(
                title_id=title.id,
                score=computed.score,
                score_israeli=computed.score_israeli,
                components=computed.components,
            )
        )
    else:
        stored.score = computed.score
        stored.score_israeli = computed.score_israeli
        stored.components = computed.components
        stored.computed_at = utcnow()

    return True


def recompute_all_aggregates(session: Session, settings: Settings) -> int:
    """Rescore every title that has ratings.

    Needed after the IMDb bulk pass, which writes ratings without going through
    the per-title path that would otherwise rescore as it goes.
    """
    computed = 0
    rated_ids = [
        title_id
        for (title_id,) in session.execute(select(ExternalRating.title_id).distinct()).all()
    ]
    # Cheap per row and there are tens of thousands of them, so this is minutes
    # of a phase that has already run for an hour - and the last minutes of a
    # long run are exactly when somebody is wondering whether to kill it.
    logger.info("rescoring %d rated title(s)", len(rated_ids))

    for index, title_id in enumerate(rated_ids, 1):
        title = session.get(Title, title_id)
        if title is not None and recompute(session, title, settings):
            computed += 1
        # Thousands of titles in one transaction is the same write-lock problem
        # as the enrich loop, just after the IMDb pass rather than during it.
        if index % AGGREGATE_COMMIT_EVERY == 0:
            session.commit()
            logger.info("rescoring: %d of %d", index, len(rated_ids))

    session.commit()
    return computed


def _describe(title: Title) -> str:
    """A title as a person would recognise it, with the id to look it up by."""
    return view_of(title).describe()


def view_of(title: Title) -> TitleView:
    return TitleView(
        id=title.id,
        kind=title.type,
        name_he=title.name_he,
        name_en=title.name_en,
        year=title.year,
        tmdb_id=title.tmdb_id,
        imdb_id=title.imdb_id,
        offered_by=frozenset(row.source.key for row in title.availability if row.is_current),
    )
