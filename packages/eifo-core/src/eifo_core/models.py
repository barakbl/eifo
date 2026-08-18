"""Database schema.

Catalog, ratings and user data; see docs.internal/04-data-model.md for the
reference table-by-table description.

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

from eifo_core.enums import (
    AuthProvider,
    FetchPhase,
    FetchStatus,
    ItemStatus,
    OfferType,
    RatingProvider,
    SourceKind,
    TitleKind,
)
from eifo_core.types import UtcDateTime, utcnow


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
    ratings: Mapped[list[ExternalRating]] = relationship(
        back_populates="title",
        cascade="all, delete-orphan",
    )
    aggregate: Mapped[AggregateScore | None] = relationship(
        back_populates="title",
        cascade="all, delete-orphan",
        uselist=False,
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

    #: What the offer costs, in the currency's minor unit (1990 = 19.90 ILS) -
    #: integer, because money in a float rounds where nobody is looking. Only a
    #: rent/buy source sets it; subscription and free offers leave both columns
    #: NULL, and so does a source that publishes no price.
    price_minor: Mapped[int | None] = mapped_column(Integer)
    price_currency: Mapped[str | None] = mapped_column(String(3))

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


class ExternalRating(Base):
    """One provider's score for one title.

    ``score_raw`` keeps the provider's own scale for display ("8.4/10", "92%");
    ``score_normalized`` is the 0-100 value aggregation works from.
    """

    __tablename__ = "external_ratings"
    __table_args__ = (
        UniqueConstraint("title_id", "provider", name="uq_rating_provider"),
        Index("ix_external_ratings_title", "title_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    title_id: Mapped[int] = mapped_column(ForeignKey("titles.id", ondelete="CASCADE"))
    provider: Mapped[RatingProvider] = mapped_column(_enum(RatingProvider, "rating_provider"))

    score_raw: Mapped[float]
    score_normalized: Mapped[int]
    vote_count: Mapped[int | None]
    #: Always shown next to the score: a rating without its source is a rumour.
    url: Mapped[str | None] = mapped_column(String(1000))
    fetched_at: Mapped[dt.datetime] = mapped_column(default=utcnow)

    title: Mapped[Title] = relationship(back_populates="ratings")

    def __repr__(self) -> str:
        return f"<ExternalRating {self.provider} {self.score_raw} title={self.title_id}>"


class AggregateScore(Base):
    """The combined score for a title, plus how it was arrived at."""

    __tablename__ = "aggregate_scores"

    title_id: Mapped[int] = mapped_column(
        ForeignKey("titles.id", ondelete="CASCADE"),
        primary_key=True,
    )
    #: Null until enough providers agree to make an average meaningful.
    score: Mapped[int | None]
    #: Israeli providers only; shown alongside the global score for local content.
    score_israeli: Mapped[int | None]
    #: Every input and the weight it was given, so the UI can show its working.
    components: Mapped[dict[str, Any]] = mapped_column(default=dict)
    computed_at: Mapped[dt.datetime] = mapped_column(default=utcnow)

    title: Mapped[Title] = relationship(back_populates="aggregate")

    def __repr__(self) -> str:
        return f"<AggregateScore title={self.title_id} score={self.score}>"


#: Length limits shared by the schema and the API's request validation, so the
#: two can never disagree about what a valid value is.
DISPLAY_NAME_MAX_LENGTH = 100
HANDLE_MAX_LENGTH = 30
NOTE_MAX_LENGTH = 2000
RATING_MIN = 1
RATING_MAX = 10


class User(Base):
    """An account.

    Identity is whatever the provider gave us and nothing more: no password to
    leak, and ``email`` is nullable because X does not always supply one and
    nothing here depends on having it.
    """

    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("auth_provider", "auth_subject", name="uq_users_identity"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    auth_provider: Mapped[AuthProvider] = mapped_column(_enum(AuthProvider, "auth_provider"))
    auth_subject: Mapped[str] = mapped_column(String(255))
    email: Mapped[str | None] = mapped_column(String(320))

    display_name: Mapped[str] = mapped_column(String(DISPLAY_NAME_MAX_LENGTH))
    #: Required before a profile can go public, and unique across accounts.
    handle: Mapped[str | None] = mapped_column(String(HANDLE_MAX_LENGTH), unique=True)
    avatar_url: Mapped[str | None] = mapped_column(String(1000))
    #: Private by default. Going public is an explicit, informed choice (S7).
    is_public: Mapped[bool] = mapped_column(default=False)
    #: Source ids behind the "my services" filter preset.
    my_source_ids: Mapped[list[Any]] = mapped_column(default=list)

    created_at: Mapped[dt.datetime] = mapped_column(default=utcnow)
    last_login_at: Mapped[dt.datetime] = mapped_column(default=utcnow)

    sessions: Mapped[list[UserSession]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
    )
    items: Mapped[list[UserItem]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<User {self.id} {self.auth_provider}:{self.auth_subject}>"


class UserSession(Base):
    """A logged-in browser.

    Sessions are server-side rather than self-contained tokens precisely so that
    logging out and deleting an account revoke access immediately instead of
    when some signed token happens to expire. Only the SHA-256 of the cookie
    value is stored: a database copy cannot be replayed as a login.
    """

    __tablename__ = "sessions"
    __table_args__ = (Index("ix_sessions_expires_at", "expires_at"),)

    #: Hex SHA-256 of the token held by the cookie; the token itself is never stored.
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))

    created_at: Mapped[dt.datetime] = mapped_column(default=utcnow)
    expires_at: Mapped[dt.datetime]
    last_used_at: Mapped[dt.datetime] = mapped_column(default=utcnow)

    user: Mapped[User] = relationship(back_populates="sessions")

    def __repr__(self) -> str:
        return f"<UserSession user={self.user_id} expires={self.expires_at}>"


class UserItem(Base):
    """What one user has to say about one title.

    ``status`` is nullable because rating or noting a title without filing it
    under a list is a real thing people do.
    """

    __tablename__ = "user_items"
    __table_args__ = (
        UniqueConstraint("user_id", "title_id", name="uq_user_item"),
        CheckConstraint(
            f"rating IS NULL OR (rating >= {RATING_MIN} AND rating <= {RATING_MAX})",
            name="ck_user_items_rating_range",
        ),
        CheckConstraint(
            f"note IS NULL OR length(note) <= {NOTE_MAX_LENGTH}",
            name="ck_user_items_note_length",
        ),
        Index("ix_user_items_user_status", "user_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    title_id: Mapped[int] = mapped_column(ForeignKey("titles.id", ondelete="CASCADE"))

    status: Mapped[ItemStatus | None] = mapped_column(_enum(ItemStatus, "item_status"))
    rating: Mapped[int | None]
    #: Private always, including on a public profile - a memo, not a review.
    note: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[dt.datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(default=utcnow, onupdate=utcnow)

    user: Mapped[User] = relationship(back_populates="items")

    @property
    def is_empty(self) -> bool:
        """Whether nothing is left worth keeping a row for."""
        return self.status is None and self.rating is None and not self.note

    def __repr__(self) -> str:
        return f"<UserItem user={self.user_id} title={self.title_id} {self.status}>"


class FetchRun(Base):
    """One fetcher run - the whole observability story, no external stack."""

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
