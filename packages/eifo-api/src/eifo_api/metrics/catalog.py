"""What the catalog holds right now, counted at each scrape.

Gauges only: a title count goes down when a service drops something, and that
is part of what is worth watching. Each one is a single grouped count, so a
scrape costs a handful of indexed queries however large the catalog grows.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from eifo_api import __version__
from eifo_api.metrics import PREFIX, Family
from eifo_api.routers.admin import source_switched_on
from eifo_core.enums import EnrichOutcome, RatingProvider, TitleKind
from eifo_core.models import (
    AggregateScore,
    Availability,
    EnrichAttempt,
    ExternalRating,
    MatchReview,
    Source,
    Title,
)
from eifo_core.settings import Settings

KINDS = tuple(kind.value for kind in TitleKind)


def catalog_families(session: Session, settings: Settings, now: dt.datetime) -> list[Family]:
    """The catalog's figures as they stand."""
    sources = session.scalars(select(Source).order_by(Source.key)).all()
    active = [source.key for source in sources if source.active]

    info = Family(
        f"{PREFIX}source_info",
        "gauge",
        "Always 1; carries each source's display name and kind for dashboards to join on.",
        ("source", "name", "kind"),
    )
    enabled = Family(
        f"{PREFIX}source_enabled",
        "gauge",
        "1 if the source is tracked and switched on, so the fetcher collects it.",
        ("source",),
    )
    for source in sources:
        info.set(1, source.key, source.name, source.kind.value)
        enabled.set(1 if source.active and source_switched_on(source, settings) else 0, source.key)

    per_source = Family(
        f"{PREFIX}source_titles",
        "gauge",
        "Titles a source offers now, by whether each is a movie or a series.",
        ("source", "type"),
    )
    for key in active:
        for kind in KINDS:
            per_source.set(0, key, kind)
    rows = session.execute(
        select(Source.key, Title.type, func.count(func.distinct(Availability.title_id)))
        .join(Availability, Availability.source_id == Source.id)
        .join(Title, Title.id == Availability.title_id)
        .where(Availability.is_current.is_(True))
        .group_by(Source.key, Title.type)
    ).all()
    for key, kind, count in rows:
        per_source.set(count, key, kind.value)

    return [
        info,
        enabled,
        per_source,
        *_title_families(session),
        *_enrich_families(session, now),
        _review_family(session, active),
        _build_family(),
    ]


def _by_kind(family: Family, rows: Iterable[tuple[TitleKind, int]]) -> Family:
    for kind in KINDS:
        family.set(0, kind)
    for kind, count in rows:
        family.set(count, kind.value)
    return family


def _title_families(session: Session) -> list[Family]:
    total = _by_kind(
        Family(f"{PREFIX}titles", "gauge", "Titles in the catalog.", ("type",)),
        session.execute(select(Title.type, func.count()).group_by(Title.type)).tuples().all(),
    )
    available = _by_kind(
        Family(
            f"{PREFIX}titles_available",
            "gauge",
            "Titles at least one tracked source offers now.",
            ("type",),
        ),
        session.execute(
            select(Title.type, func.count(func.distinct(Title.id)))
            .join(Availability, Availability.title_id == Title.id)
            .where(Availability.is_current.is_(True))
            .group_by(Title.type)
        )
        .tuples()
        .all(),
    )
    scored = _by_kind(
        Family(
            f"{PREFIX}titles_scored",
            "gauge",
            "Titles with a combined score.",
            ("type",),
        ),
        session.execute(
            select(Title.type, func.count())
            .join(AggregateScore, AggregateScore.title_id == Title.id)
            .group_by(Title.type)
        )
        .tuples()
        .all(),
    )
    no_poster = _by_kind(
        Family(
            f"{PREFIX}titles_missing_poster",
            "gauge",
            "Titles with no stored poster.",
            ("type",),
        ),
        session.execute(
            select(Title.type, func.count()).where(Title.poster_path.is_(None)).group_by(Title.type)
        )
        .tuples()
        .all(),
    )

    ratings = Family(
        f"{PREFIX}title_ratings",
        "gauge",
        "Titles a rating provider has rated.",
        ("provider",),
    )
    for provider in RatingProvider:
        ratings.set(0, provider.value)
    for provider, count in session.execute(
        select(
            ExternalRating.provider, func.count(func.distinct(ExternalRating.title_id))
        ).group_by(ExternalRating.provider)
    ).all():
        ratings.set(count, provider.value)

    return [total, available, scored, no_poster, ratings]


def _enrich_families(session: Session, now: dt.datetime) -> list[Family]:
    attempts = Family(
        f"{PREFIX}enrich_attempts",
        "gauge",
        "Titles by what their latest trip through the enrichers came to.",
        ("outcome",),
    )
    for outcome in EnrichOutcome:
        attempts.set(0, outcome.value)
    for outcome, count in session.execute(
        select(EnrichAttempt.outcome, func.count()).group_by(EnrichAttempt.outcome)
    ).all():
        attempts.set(count, outcome.value)

    # The same question eifo_core.enriching.titles_due asks, counted.
    due = Family(
        f"{PREFIX}enrich_due",
        "gauge",
        "Titles due a trip through the enrichers: never tried, or due again.",
    )
    due.set(
        session.scalar(
            select(func.count())
            .select_from(Title)
            .outerjoin(EnrichAttempt, EnrichAttempt.title_id == Title.id)
            .where(or_(EnrichAttempt.title_id.is_(None), EnrichAttempt.due_at <= now))
        )
        or 0
    )
    return [attempts, due]


def _review_family(session: Session, sources: list[str]) -> Family:
    queue = Family(
        f"{PREFIX}review_queue",
        "gauge",
        "Listings waiting for someone to say which title they are.",
        ("source",),
    )
    for key in sources:
        queue.set(0, key)
    for key, count in session.execute(
        select(MatchReview.source_key, func.count())
        .where(MatchReview.resolved_at.is_(None))
        .group_by(MatchReview.source_key)
    ).all():
        queue.set(count, key)
    return queue


def _build_family() -> Family:
    build = Family(
        f"{PREFIX}build_info",
        "gauge",
        "Always 1; carries the running version.",
        ("version",),
    )
    build.set(1, __version__)
    return build
