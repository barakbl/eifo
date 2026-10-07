"""A small catalog with a viewer who has taste: enough to recommend from."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sqlalchemy.orm import Session

from eifo_core.enums import AuthProvider, CreditRole, OfferType, SourceKind, TitleKind
from eifo_core.models import (
    AggregateScore,
    Availability,
    Credit,
    Genre,
    Person,
    Source,
    Title,
    User,
    UserItem,
)

NOW = dt.datetime(2026, 10, 1, 12, 0, tzinfo=dt.UTC)


@dataclass(frozen=True)
class Catalog:
    viewer: int
    other_viewer: int
    netflix: int
    yes: int
    shoplifters: int
    broker: int
    nobody_knows: int
    fauda: int
    old_film: int
    kore_eda: int


def seed(session: Session) -> Catalog:
    drama = Genre(tmdb_id=18, name_en="Drama", name_he="דרמה")
    thriller = Genre(tmdb_id=53, name_en="Thriller", name_he="מותחן")
    netflix = Source(
        key="netflix_il",
        name="Netflix",
        kind=SourceKind.SUBSCRIPTION,
        website_url="https://n.example",
    )
    yes = Source(
        key="yes", name="yes VOD", kind=SourceKind.SUBSCRIPTION, website_url="https://y.example"
    )
    store = Source(
        key="apple_tv_store",
        name="Apple TV Store",
        kind=SourceKind.RENT_BUY,
        website_url="https://a.example",
    )
    session.add_all([drama, thriller, netflix, yes, store])
    session.flush()

    kore_eda = Person(name_en="Hirokazu Kore-eda")
    session.add(kore_eda)

    def film(name: str, year: int, score: int, genres: list[Genre], **extra: object) -> Title:
        title = Title(type=TitleKind.MOVIE, name_en=name, year=year, genres=genres, **extra)
        session.add(title)
        session.flush()
        session.add(AggregateScore(title_id=title.id, score=score, components={}))
        return title

    shoplifters = film(
        "Shoplifters",
        2018,
        86,
        [drama],
        name_he="גנבים",
        runtime_minutes=121,
        overview_en="A family of small-time crooks. " * 40,
    )
    broker = film("Broker", 2022, 74, [drama], runtime_minutes=129)
    nobody_knows = film("Nobody Knows", 2004, 81, [drama], runtime_minutes=141)
    old_film = film("An Old Film", 1950, 60, [thriller], runtime_minutes=90)
    fauda = Title(
        type=TitleKind.SERIES, name_en="Fauda", name_he="פאודה", year=2015, genres=[thriller]
    )
    session.add(fauda)
    session.flush()

    for title in (shoplifters, broker, nobody_knows):
        session.add(
            Credit(
                title_id=title.id, person_id=kore_eda.id, role=CreditRole.DIRECTOR, source="tmdb"
            )
        )

    def offer(title: Title, source: Source, kind: OfferType, **extra: object) -> None:
        session.add(
            Availability(
                title_id=title.id, source_id=source.id, offer_type=kind, first_seen=NOW, **extra
            )
        )

    offer(shoplifters, netflix, OfferType.STREAM)
    offer(broker, netflix, OfferType.STREAM)
    offer(nobody_knows, yes, OfferType.STREAM)
    offer(nobody_knows, store, OfferType.RENT, price_minor=1990, price_currency="ILS")
    offer(fauda, netflix, OfferType.STREAM)
    offer(old_film, yes, OfferType.STREAM, is_current=False, gone_since=NOW)

    viewer = User(
        auth_provider=AuthProvider.GOOGLE,
        auth_subject="1",
        email="viewer@example.com",
        display_name="Viewer",
        my_source_ids=[netflix.id],
    )
    other = User(
        auth_provider=AuthProvider.GOOGLE,
        auth_subject="2",
        email="other@example.com",
        display_name="Other",
    )
    session.add_all([viewer, other])
    session.flush()

    session.add_all(
        [
            UserItem(
                user_id=viewer.id, title_id=shoplifters.id, watched=True, rating=9, note="loved it"
            ),
            UserItem(user_id=viewer.id, title_id=fauda.id, want_to_watch=True),
            # Somebody else's taste, which must never reach this viewer's assistant.
            UserItem(user_id=other.id, title_id=broker.id, watched=True, rating=2, note="private"),
        ]
    )
    session.commit()

    return Catalog(
        viewer=viewer.id,
        other_viewer=other.id,
        netflix=netflix.id,
        yes=yes.id,
        shoplifters=shoplifters.id,
        broker=broker.id,
        nobody_knows=nobody_knows.id,
        fauda=fauda.id,
        old_film=old_film.id,
        kore_eda=kore_eda.id,
    )
