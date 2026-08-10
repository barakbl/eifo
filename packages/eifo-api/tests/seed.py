"""A small but realistic catalog for the API tests.

Deliberately covers the awkward cases rather than only happy ones: a title that
has gone away, a source that is no longer tracked, a title on nothing at all,
and a mix of Hebrew-only, English-only and bilingual names.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sqlalchemy.orm import Session

from eifo_core.enums import OfferType, RatingProvider, SourceKind, TitleKind
from eifo_core.models import (
    AggregateScore,
    Availability,
    ExternalRating,
    Genre,
    Source,
    Title,
)

NOW = dt.datetime(2026, 8, 7, 12, 0, tzinfo=dt.UTC)


@dataclass(frozen=True)
class Seeded:
    """Ids of the fixture rows, so tests can refer to them by name."""

    fauda: int
    foxtrot: int
    shtisel: int
    orphan: int
    netflix: int
    mako: int
    retired: int
    drama: int


def seed_catalog(session: Session) -> Seeded:
    """Populate a catalog covering the states the API must distinguish."""
    drama = Genre(tmdb_id=18, name_en="Drama", name_he="דרמה")
    comedy = Genre(tmdb_id=35, name_en="Comedy", name_he="קומדיה")
    session.add_all([drama, comedy])

    netflix = Source(
        key="netflix_il",
        name="Netflix",
        kind=SourceKind.SUBSCRIPTION,
        website_url="https://www.netflix.com/il/",
    )
    mako = Source(
        key="mako",
        name="Mako VOD (Keshet 12)",
        kind=SourceKind.FREE,
        website_url="https://www.mako.co.il/mako-vod-index",
    )
    retired = Source(
        key="free_tv",
        name="Free TV",
        kind=SourceKind.SUBSCRIPTION,
        website_url="https://freetv.example",
        active=False,
        deactivated_at=NOW - dt.timedelta(days=30),
    )
    session.add_all([netflix, mako, retired])
    session.flush()

    fauda = Title(
        type=TitleKind.SERIES,
        name_he="פאודה",
        name_en="Fauda",
        year=2015,
        overview_he="יחידה מסתערבת.",
        overview_en="An undercover unit.",
        poster_path="posters/1/w500.jpg",
        seasons=4,
        imdb_id="tt4565380",
    )
    fauda.genres.append(drama)

    foxtrot = Title(
        type=TitleKind.MOVIE,
        name_he="פוקסטרוט",
        name_en="Foxtrot",
        year=2017,
        runtime_minutes=113,
    )
    foxtrot.genres.append(drama)

    # Hebrew-only, to prove search and sorting do not assume a Latin name.
    shtisel = Title(type=TitleKind.SERIES, name_he="שטיסל", year=2013)
    shtisel.genres.append(comedy)

    # On nothing at all: must be invisible by default, findable with available=any.
    orphan = Title(type=TitleKind.MOVIE, name_en="Forgotten Film", year=1999)

    session.add_all([fauda, foxtrot, shtisel, orphan])
    session.flush()

    session.add_all(
        [
            Availability(
                title_id=fauda.id,
                source_id=netflix.id,
                offer_type=OfferType.STREAM,
                deep_link_url="https://www.netflix.com/title/80174708",
                first_seen=NOW - dt.timedelta(days=100),
                last_seen=NOW,
            ),
            Availability(
                title_id=fauda.id,
                source_id=mako.id,
                offer_type=OfferType.FREE,
                first_seen=NOW - dt.timedelta(days=2),
                last_seen=NOW,
            ),
            # Gone from Netflix: kept, badged, never deleted.
            Availability(
                title_id=foxtrot.id,
                source_id=netflix.id,
                offer_type=OfferType.STREAM,
                is_current=False,
                gone_since=NOW - dt.timedelta(days=5),
                first_seen=NOW - dt.timedelta(days=200),
                last_seen=NOW - dt.timedelta(days=7),
            ),
            # Only on a source we no longer track.
            Availability(
                title_id=shtisel.id,
                source_id=retired.id,
                offer_type=OfferType.STREAM,
                first_seen=NOW - dt.timedelta(days=300),
                last_seen=NOW - dt.timedelta(days=30),
            ),
        ]
    )

    session.add_all(
        [
            ExternalRating(
                title_id=fauda.id,
                provider=RatingProvider.IMDB,
                score_raw=8.3,
                score_normalized=83,
                vote_count=43610,
                url="https://www.imdb.com/title/tt4565380/",
            ),
            ExternalRating(
                title_id=fauda.id,
                provider=RatingProvider.SERET_VIEWERS,
                score_raw=8.9,
                score_normalized=89,
                vote_count=120,
                url="https://www.seret.co.il/series/s_series.asp?SID=268",
            ),
            ExternalRating(
                title_id=foxtrot.id,
                provider=RatingProvider.RT_CRITICS,
                score_raw=94.0,
                score_normalized=94,
                vote_count=141,
                url="https://www.rottentomatoes.com/m/foxtrot_2018",
            ),
        ]
    )

    session.add_all(
        [
            AggregateScore(
                title_id=fauda.id,
                score=85,
                score_israeli=89,
                components={"imdb": {"normalized": 83, "weight": 3.0}},
            ),
            AggregateScore(title_id=foxtrot.id, score=94, score_israeli=None, components={}),
        ]
    )

    session.commit()
    return Seeded(
        fauda=fauda.id,
        foxtrot=foxtrot.id,
        shtisel=shtisel.id,
        orphan=orphan.id,
        netflix=netflix.id,
        mako=mako.id,
        retired=retired.id,
        drama=drama.id,
    )
