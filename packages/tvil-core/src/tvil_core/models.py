"""Database schema.

Only the catalog side of the model lives here for now (stage S0/S1). Ratings,
aggregate scores and user data arrive in later stages with their own migrations;
see docs.internal/04-data-model.md for the full target schema.

Enum columns are stored as their string *values* in a VARCHAR with a CHECK
constraint (``native_enum=False``) so the schema behaves identically on SQLite
and PostgreSQL.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, ClassVar

from sqlalchemy import (
    CheckConstraint,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from tvil_core.enums import FetchPhase, FetchStatus, OfferType, SourceKind, TitleKind
from tvil_core.types import UtcDateTime, utcnow


def _enum(enum_cls: type, name: str) -> Enum:
    """A portable VARCHAR + CHECK enum column storing the member values."""
    return Enum(
        enum_cls,
        name=name,
        native_enum=False,
        length=32,
        validate_strings=True,
        values_callable=lambda cls: [member.value for member in cls],
    )


class Base(DeclarativeBase):
    """Declarative base carrying the shared type map."""

    type_annotation_map: ClassVar[dict[Any, Any]] = {
        dt.datetime: UtcDateTime,
        dict[str, Any]: JSON,
        list[Any]: JSON,
    }


class TimestampMixin:
    """``created_at`` / ``updated_at`` maintained on the Python side."""

    created_at: Mapped[dt.datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class Title(TimestampMixin, Base):
    """A canonical movie or series, independent of where it can be watched."""

    __tablename__ = "titles"
    __table_args__ = (
        CheckConstraint(
            "name_en IS NOT NULL OR name_he IS NOT NULL",
            name="ck_titles_has_a_name",
        ),
        Index("ix_titles_type_year", "type", "year"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    type: Mapped[TitleKind] = mapped_column(_enum(TitleKind, "title_kind"))

    # Canonical external anchors. Both nullable: local-only titles (Israeli
    # reality shows, say) exist on neither service.
    tmdb_id: Mapped[int | None] = mapped_column(Integer, unique=True)
    imdb_id: Mapped[str | None] = mapped_column(String(16), unique=True)

    name_en: Mapped[str | None] = mapped_column(String(500))
    name_he: Mapped[str | None] = mapped_column(String(500))
    year: Mapped[int | None]
    overview_en: Mapped[str | None] = mapped_column(Text)
    overview_he: Mapped[str | None] = mapped_column(Text)

    # Paths relative to settings.images_dir, not URLs.
    poster_path: Mapped[str | None] = mapped_column(String(500))
    backdrop_path: Mapped[str | None] = mapped_column(String(500))
    # Where the artwork can be downloaded from; the image pipeline reads this
    # and writes the stored location to poster_path.
    poster_source_url: Mapped[str | None] = mapped_column(String(1000))

    runtime_minutes: Mapped[int | None]
    seasons: Mapped[int | None]
    status: Mapped[str | None] = mapped_column(String(50))

    availability: Mapped[list[Availability]] = relationship(
        back_populates="title",
        cascade="all, delete-orphan",
    )
    genres: Mapped[list[Genre]] = relationship(
        secondary="title_genres",
        back_populates="titles",
    )

    @property
    def display_name(self) -> str:
        """Best available name, Hebrew preferred, for logs and admin output."""
        return self.name_he or self.name_en or f"title#{self.id}"

    def __repr__(self) -> str:
        return f"<Title {self.id} {self.display_name!r} ({self.year})>"


class Genre(Base):
    """TMDB's genre taxonomy, localised."""

    __tablename__ = "genres"

    id: Mapped[int] = mapped_column(primary_key=True)
    tmdb_id: Mapped[int | None] = mapped_column(Integer, unique=True)
    name_en: Mapped[str] = mapped_column(String(100))
    name_he: Mapped[str | None] = mapped_column(String(100))

    titles: Mapped[list[Title]] = relationship(
        secondary="title_genres",
        back_populates="genres",
    )

    def __repr__(self) -> str:
        return f"<Genre {self.id} {self.name_en!r}>"


class TitleGenre(Base):
    """Join table between titles and genres."""

    __tablename__ = "title_genres"

    title_id: Mapped[int] = mapped_column(
        ForeignKey("titles.id", ondelete="CASCADE"),
        primary_key=True,
    )
    genre_id: Mapped[int] = mapped_column(
        ForeignKey("genres.id", ondelete="CASCADE"),
        primary_key=True,
    )


class Source(TimestampMixin, Base):
    """A tracked service (Netflix IL, Cellcom TV, Mako VOD, ...).

    Retiring a source sets ``active = False``; its rows are never deleted so the
    UI can badge them as "no longer tracked" instead of losing history.
    """

    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(50), unique=True)
    name: Mapped[str] = mapped_column(String(100))
    kind: Mapped[SourceKind] = mapped_column(_enum(SourceKind, "source_kind"))
    website_url: Mapped[str] = mapped_column(String(500))
    logo_path: Mapped[str | None] = mapped_column(String(500))
    active: Mapped[bool] = mapped_column(default=True)
    deactivated_at: Mapped[dt.datetime | None]

    availability: Mapped[list[Availability]] = relationship(
        back_populates="source",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<Source {self.key!r} active={self.active}>"


class Availability(Base):
    """A title offered by a source, with its own lifecycle.

    ``is_current`` only flips to False after the title has been missing from two
    consecutive *successful* syncs of that source (``miss_count``), so a single
    flaky scrape never expires a catalog.
    """

    __tablename__ = "availability"
    __table_args__ = (
        UniqueConstraint("title_id", "source_id", "offer_type", name="uq_availability_offer"),
        Index("ix_availability_source_current", "source_id", "is_current"),
        Index("ix_availability_title_current", "title_id", "is_current"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    title_id: Mapped[int] = mapped_column(ForeignKey("titles.id", ondelete="CASCADE"))
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"))

    deep_link_url: Mapped[str | None] = mapped_column(String(1000))
    offer_type: Mapped[OfferType] = mapped_column(_enum(OfferType, "offer_type"))

    is_current: Mapped[bool] = mapped_column(default=True)
    miss_count: Mapped[int] = mapped_column(default=0)
    first_seen: Mapped[dt.datetime] = mapped_column(default=utcnow)
    last_seen: Mapped[dt.datetime] = mapped_column(default=utcnow)
    gone_since: Mapped[dt.datetime | None]

    title: Mapped[Title] = relationship(back_populates="availability")
    source: Mapped[Source] = relationship(back_populates="availability")

    def __repr__(self) -> str:
        return (
            f"<Availability title={self.title_id} source={self.source_id} "
            f"current={self.is_current}>"
        )


class FetchRun(Base):
    """One fetcher run — the whole observability story, no external stack."""

    __tablename__ = "fetch_runs"
    __table_args__ = (Index("ix_fetch_runs_source_started", "source_key", "started_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    source_key: Mapped[str | None] = mapped_column(String(50))
    phase: Mapped[FetchPhase] = mapped_column(_enum(FetchPhase, "fetch_phase"))
    started_at: Mapped[dt.datetime] = mapped_column(default=utcnow)
    finished_at: Mapped[dt.datetime | None]
    status: Mapped[FetchStatus] = mapped_column(_enum(FetchStatus, "fetch_status"))
    stats: Mapped[dict[str, Any]] = mapped_column(default=dict)

    def __repr__(self) -> str:
        return f"<FetchRun {self.phase} {self.source_key} {self.status}>"


class MatchReview(Base):
    """A source item the matcher could not confidently resolve to a title."""

    __tablename__ = "match_reviews"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_key: Mapped[str] = mapped_column(String(50))
    raw_payload: Mapped[dict[str, Any]]
    candidates: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(default=utcnow)
    resolved_at: Mapped[dt.datetime | None]
    resolved_title_id: Mapped[int | None] = mapped_column(
        ForeignKey("titles.id", ondelete="SET NULL")
    )

    def __repr__(self) -> str:
        resolved = self.resolved_at is not None
        return f"<MatchReview {self.id} {self.source_key!r} resolved={resolved}>"
