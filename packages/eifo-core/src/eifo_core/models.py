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
    false,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from eifo_core.enums import (
    AuthProvider,
    CreditRole,
    EnrichOutcome,
    FetchPhase,
    FetchStatus,
    MatchDecision,
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
        UniqueConstraint("type", "tmdb_id", name="uq_title_tmdb"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    type: Mapped[TitleKind] = mapped_column(_enum(TitleKind, "title_kind"))

    # Canonical external anchors. Both nullable: local-only titles (Israeli
    # reality shows, say) exist on neither service.
    #: Unique per media type, not globally: TMDB numbers films and series in
    #: separate namespaces, so movie 105 (Back to the Future) and series 105
    #: (Sex and the City) are different works that share a number. Held as
    #: globally unique, the second one to arrive was silently taken for the
    #: first, and its offers were filed against a title it has nothing to do
    #: with. IMDb ids need no such qualifier - those really are global.
    tmdb_id: Mapped[int | None] = mapped_column(Integer)
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

    #: ISO 639-1, the language the title was made in ("he", "en").
    original_language: Mapped[str | None] = mapped_column(String(8))
    #: ISO 3166-1 alpha-2, comma separated, in the order the source lists them
    #: ("IL", "IL,FR"). Codes rather than names so the client can render them
    #: in whichever language the reader chose.
    origin_countries: Mapped[str | None] = mapped_column(String(100))

    credits: Mapped[list[Credit]] = relationship(
        back_populates="title",
        cascade="all, delete-orphan",
        order_by="(Credit.role, Credit.billing_order, Credit.id)",
    )
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
    enrich_attempt: Mapped[EnrichAttempt | None] = relationship(
        back_populates="title",
        cascade="all, delete-orphan",
        uselist=False,
    )
    tmdb_aliases: Mapped[list[TmdbAlias]] = relationship(
        back_populates="title",
        cascade="all, delete-orphan",
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


class Person(TimestampMixin, Base):
    """Someone who worked on a title: a director, a cinematographer, an actor.

    Addressed by ``id``, the way a title is. A person known to TMDB also
    carries ``tmdb_id``; one scraped from an Israeli catalogue has only a name,
    which is all that source knows.
    """

    __tablename__ = "people"
    __table_args__ = (
        CheckConstraint(
            "name_en IS NOT NULL OR name_he IS NOT NULL",
            name="ck_people_has_a_name",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tmdb_id: Mapped[int | None] = mapped_column(Integer, unique=True)
    name_en: Mapped[str | None] = mapped_column(String(200))
    name_he: Mapped[str | None] = mapped_column(String(200))
    profile_source_url: Mapped[str | None] = mapped_column(String(1000))

    credits: Mapped[list[Credit]] = relationship(
        back_populates="person",
        cascade="all, delete-orphan",
    )

    @property
    def display_name(self) -> str:
        """Best available name, Hebrew preferred, for logs and admin output."""
        return self.name_he or self.name_en or f"person#{self.id}"

    def __repr__(self) -> str:
        return f"<Person {self.id} {self.display_name!r}>"


class Credit(Base):
    """One person's contribution to one title.

    ``source`` records who said so - "tmdb", or the key of the plugin that
    scraped it - because the Israeli catalogues are the only ones that know
    about much of their own cinema, and a claim without a provenance is a
    rumour.
    """

    __tablename__ = "credits"
    __table_args__ = (
        UniqueConstraint("title_id", "person_id", "role", "character", name="uq_credit"),
        Index("ix_credits_person_role", "person_id", "role"),
        Index("ix_credits_title_role", "title_id", "role"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    title_id: Mapped[int] = mapped_column(ForeignKey("titles.id", ondelete="CASCADE"))
    person_id: Mapped[int] = mapped_column(ForeignKey("people.id", ondelete="CASCADE"))
    role: Mapped[CreditRole] = mapped_column(_enum(CreditRole, "credit_role"))
    #: Who they played, for a cast credit.
    character: Mapped[str | None] = mapped_column(String(300))
    #: Billing order for cast; None for crew, who are shown as listed.
    billing_order: Mapped[int | None]
    source: Mapped[str] = mapped_column(String(50))

    title: Mapped[Title] = relationship(back_populates="credits")
    person: Mapped[Person] = relationship(back_populates="credits")

    def __repr__(self) -> str:
        return f"<Credit title={self.title_id} person={self.person_id} {self.role}>"


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
    #: An operator's override of the ``[sources]`` switch in the config file.
    #: NULL - the default - means "whatever the file says", so turning a source
    #: off from the Manage tab does not silently freeze every other source at
    #: whatever the file happened to say the day somebody first used the toggle.
    enabled: Mapped[bool | None] = mapped_column(default=None)
    #: What the plugin declares this source does when nothing is configured.
    #:
    #: Written by the fetcher, which is the only thing that knows what plugins
    #: exist, and read by the API, which cannot ask it. Without it the Manage
    #: tab reported a source as on because the config file was silent, while
    #: the fetcher was skipping it because its plugin declares itself off - the
    #: screen and the run disagreeing about the same source.
    default_enabled: Mapped[bool] = mapped_column(default=True)
    #: When an operator asked for this source's catalog to be pulled in full.
    #:
    #: Switching a source on is a request for its titles, not merely permission
    #: to collect them tonight - and a source switched on at noon showing an
    #: empty catalog until 03:00 reads as broken rather than as scheduled. The
    #: API writes the time here and the fetcher clears it once it has run, the
    #: database being the only thing the two share.
    backfill_requested_at: Mapped[dt.datetime | None]

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
        Index("ix_availability_source_ref", "source_id", "source_ref"),
        Index("ix_availability_title_current", "title_id", "is_current"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    title_id: Mapped[int] = mapped_column(ForeignKey("titles.id", ondelete="CASCADE"))
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"))

    deep_link_url: Mapped[str | None] = mapped_column(String(1000))
    offer_type: Mapped[OfferType] = mapped_column(_enum(OfferType, "offer_type"))

    #: The source's own id for this listing, when it publishes one.
    #:
    #: What makes a listing the same listing tomorrow. A catalogue that names
    #: two different works identically - Disney+ lists both Beauty and the Beast
    #: films as "Beauty And The Beast", with no year - is otherwise matched by
    #: name alone, and both listings land on whichever title the matcher reaches
    #: first. The other is then never seen and retires as though it had left the
    #: service. Held here because "this source offers this title" is exactly
    #: what this row says, so the source's name for that offer belongs on it.
    source_ref: Mapped[str | None] = mapped_column(String(200))

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


class RatingProviderInfo(TimestampMixin, Base):
    """How a ratings provider presents itself, as its enricher declares it.

    Written by the fetcher, which is the only thing that knows what enrichers
    exist, and read by the API, which cannot ask one - the same arrangement as
    ``sources.default_enabled``, and for the same reason: the database is all
    the two processes share.

    Before this, the API carried a hand-kept map of provider names. It had to be
    edited to add a provider, it could disagree with the plugin that actually
    produced the scores, and it had nowhere to say the two things a chip needs
    beyond a name: that Tomatometer and Audience are one service rather than
    two, and what that service's mark looks like.
    """

    __tablename__ = "rating_providers"

    provider: Mapped[RatingProvider] = mapped_column(
        _enum(RatingProvider, "rating_provider"),
        primary_key=True,
    )
    #: What this particular score is called - "Tomatometer", "Audience".
    label: Mapped[str] = mapped_column(String(100))
    #: Providers sharing a group are one service, and read as one chip. Two
    #: figures from Rotten Tomatoes are two things it measured, not two
    #: opinions from two places, and showing them as separate chips made the
    #: page look like it had six raters when it has four.
    group_key: Mapped[str] = mapped_column(String(50))
    #: The service behind the group - "Rotten Tomatoes", "סרט".
    group_name: Mapped[str] = mapped_column(String(100))
    #: The group's mark, under the images root, when the plugin ships one.
    #: Null is an ordinary answer: the chip falls back to the name.
    logo_path: Mapped[str | None] = mapped_column(String(500))
    #: Where the service lives, for a chip whose score carries no link of its
    #: own. A rating without its source is a rumour.
    website_url: Mapped[str | None] = mapped_column(String(500))
    #: Order within the group. Critics before the crowd, because that is the
    #: order both sites print them in.
    position: Mapped[int] = mapped_column(default=0)

    def __repr__(self) -> str:
        return f"<RatingProviderInfo {self.provider} in {self.group_key!r}>"


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


class TmdbAlias(Base):
    """A TMDB id that turned out to be a second record of a title already held.

    TMDB carries the same work twice more often than one would like - a
    miniseries entered again under a different id, an anime split between its
    seasons - and the availability feed offers both ids every night. Merging the
    two titles is therefore not enough on its own: the next sync would see an id
    no title owns and faithfully recreate what was just merged.

    So the losing id is kept and pointed at the survivor. It is a fact about
    TMDB's catalog rather than about ours, which is why it lives beside the
    titles rather than inside one.
    """

    __tablename__ = "tmdb_aliases"

    #: Which namespace the id belongs to. Part of the key for the same reason it
    #: is on a title: TMDB numbers films and series separately, so an alias
    #: recorded for one would otherwise shadow the other's id.
    type: Mapped[TitleKind] = mapped_column(_enum(TitleKind, "title_kind"), primary_key=True)
    #: The id that is not the canonical one. Keyed with the type above: an id
    #: can only ever be an alias for one title of its own kind.
    tmdb_id: Mapped[int] = mapped_column(primary_key=True)
    title_id: Mapped[int] = mapped_column(
        ForeignKey("titles.id", ondelete="CASCADE"),
        index=True,
    )
    created_at: Mapped[dt.datetime] = mapped_column(default=utcnow)

    title: Mapped[Title] = relationship(back_populates="tmdb_aliases")

    def __repr__(self) -> str:
        return f"<TmdbAlias tmdb={self.tmdb_id} -> title={self.title_id}>"


class SeretTitle(Base):
    """One page of seret.co.il, as that page's own JSON-LD describes it.

    Seret publishes no working title search - its advertised ``SearchAction``,
    the real form POST and the site-wide search page all answer with a generic
    current-releases listing - so a title cannot be resolved to a page id on
    demand. What Seret does publish is a sitemap naming every page, and that is
    what this table is built from: the crawl reads each page once, and
    afterwards resolving a title is a local lookup rather than a request.

    The scores are kept here as well as in ``external_ratings``, which is not
    the duplication it looks like. The crawl has the page open anyway, so
    reading it again per title would be the same traffic twice over; and
    holding Seret's own numbers apart from the catalog's means re-indexing can
    correct them without every affected title having to fall due for
    enrichment first.
    """

    __tablename__ = "seret_index"
    __table_args__ = (Index("ix_seret_index_imdb_id", "imdb_id"),)

    #: Films and series are numbered separately on Seret - ``MID`` against
    #: ``SID``, at two different endpoints - so which numbering an id belongs
    #: to is part of the key rather than a property of the row.
    kind: Mapped[TitleKind] = mapped_column(_enum(TitleKind, "title_kind"), primary_key=True)
    seret_id: Mapped[int] = mapped_column(primary_key=True)

    name_he: Mapped[str | None] = mapped_column(String(500))
    #: Seret's ``alternateName``, which is the international title.
    name_en: Mapped[str | None] = mapped_column(String(500))
    #: From ``datePublished``, which on Seret is the *Israeli* release date and
    #: therefore trails the production year a catalog reports - "The Big Short"
    #: is 2015 upstream and 2016-01-28 here. Matching against it allows more
    #: slack than the catalog's own year comparisons do.
    year: Mapped[int | None]
    #: From ``sameAs``. Decisive about identity when present, and Seret only
    #: began publishing it on the newer pages.
    imdb_id: Mapped[str | None] = mapped_column(String(20))

    #: The audience score, on Seret's own 0-10 scale, and how many people voted.
    viewers_score: Mapped[float | None]
    viewers_votes: Mapped[int | None]
    #: "Seret Score", the site's composite editorial figure - the critic score -
    #: also 0-10.
    critics_score: Mapped[float | None]

    url: Mapped[str | None] = mapped_column(String(1000))
    #: When this page was last read, which is what the re-crawl works from: a
    #: row older than ``[seret] refresh_days`` is fetched again and every other
    #: row is skipped, so a second crawl costs almost nothing.
    indexed_at: Mapped[dt.datetime] = mapped_column(default=utcnow)
    #: A page that answered but carried no title node - a withdrawn id, or a
    #: layout change. Recorded rather than simply left out, so that the crawl
    #: stops paying for it on every subsequent run.
    unreadable: Mapped[bool] = mapped_column(default=False, server_default=false())

    def names(self) -> list[str]:
        """Every name this page gives the title, Hebrew first."""
        return [name for name in (self.name_he, self.name_en) if name]

    def __repr__(self) -> str:
        return f"<SeretTitle {self.kind} {self.seret_id} {self.name_he or self.name_en!r}>"


class EnrichAttempt(Base):
    """When a title was last put through the enrichers, and what came of it.

    Enrichment used to work out what was due from the ratings it had already
    written, which quietly meant "anything nobody has ever rated is due", and a
    title no provider carries is exactly that, permanently. The lowest-numbered
    such titles filled every batch and were asked about again the next night,
    for as long as that lasted - so the queue recorded successes and never
    learned from the failures.

    One row per title, rewritten on each attempt: the attempt is the fact worth
    keeping, not its history.
    """

    __tablename__ = "enrich_attempts"
    __table_args__ = (Index("ix_enrich_attempts_due_at", "due_at"),)

    title_id: Mapped[int] = mapped_column(
        ForeignKey("titles.id", ondelete="CASCADE"),
        primary_key=True,
    )
    attempted_at: Mapped[dt.datetime] = mapped_column(default=utcnow)
    outcome: Mapped[EnrichOutcome] = mapped_column(_enum(EnrichOutcome, "enrich_outcome"))
    #: Consecutive attempts that produced no rating; back to zero on one that
    #: did. Each one earns a longer wait, so a title nobody will ever rate
    #: stops costing a slot every month.
    fruitless: Mapped[int] = mapped_column(default=0)
    #: Not to be attempted again before this.
    due_at: Mapped[dt.datetime] = mapped_column(default=utcnow)

    title: Mapped[Title] = relationship(back_populates="enrich_attempt")

    def __repr__(self) -> str:
        return f"<EnrichAttempt title={self.title_id} {self.outcome} due={self.due_at}>"


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

    The two lists are separate flags rather than one status, because they are
    not opposites: something watched and worth watching again belongs on both,
    and a single column made that unsayable. Both false is the ordinary case -
    rating or noting a title without filing it anywhere is a real thing people
    do, and so is being in neither list.
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
        # One index per list: the two are queried separately, and a title can
        # be in both, so there is no single column to sort them under.
        Index("ix_user_items_user_watched", "user_id", "watched"),
        Index("ix_user_items_user_want", "user_id", "want_to_watch"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    title_id: Mapped[int] = mapped_column(ForeignKey("titles.id", ondelete="CASCADE"))

    want_to_watch: Mapped[bool] = mapped_column(default=False, server_default=false())
    watched: Mapped[bool] = mapped_column(default=False, server_default=false())
    rating: Mapped[int | None]
    #: Private always, including on a public profile - a memo, not a review.
    note: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[dt.datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(default=utcnow, onupdate=utcnow)

    user: Mapped[User] = relationship(back_populates="items")

    @property
    def is_empty(self) -> bool:
        """Whether nothing is left worth keeping a row for."""
        return not self.want_to_watch and not self.watched and self.rating is None and not self.note

    def __repr__(self) -> str:
        lists = " ".join(
            name
            for name, on in (("want_to_watch", self.want_to_watch), ("watched", self.watched))
            if on
        )
        return f"<UserItem user={self.user_id} title={self.title_id} {lists or '-'}>"


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
    #: What the run said while it ran. Until this existed the only record of a
    #: failed night was on the stderr of a process nobody was watching, so the
    #: answer to "why did mako return nothing" was "run it again and watch".
    #: Truncated to the tail (``eifo_fetcher.runs.RunLogCapture``) - the end of
    #: a run is the part that explains it.
    log: Mapped[str | None] = mapped_column(Text)

    def __repr__(self) -> str:
        return f"<FetchRun {self.phase} {self.source_key} {self.status}>"


class MatchReview(Base):
    """A source item the matcher could not confidently resolve to a title.

    Until somebody rules, the item is not in the catalog at all: no title, no
    availability, nothing to search for. That is the cost of parking one, and
    the reason the band that parks them is narrow.
    """

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
    #: What was decided. Null while the item is still waiting. Kept alongside
    #: ``resolved_title_id`` rather than inferred from it, because "not that
    #: title, but a real one" and "not a title at all" are different rulings and
    #: an absent id cannot say which.
    decision: Mapped[MatchDecision | None] = mapped_column(
        _enum(MatchDecision, "match_decision"),
        default=None,
    )

    def __repr__(self) -> str:
        resolved = self.resolved_at is not None
        return f"<MatchReview {self.id} {self.source_key!r} resolved={resolved}>"
