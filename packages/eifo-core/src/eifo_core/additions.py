"""Films a member watched somewhere no tracked service covers.

A cinema, a flight, a festival, a service nobody collects: the film was watched
and deserves a rating, but no sync will ever bring it in. A member can add it by
hand, from TMDB, and it becomes an ordinary title - one marked as added, and
kept out of the catalog's default views until some service carries it.

"User added" is not stored. It is ``added_at`` set *and* no availability row of
any kind, asked fresh every time (:func:`user_added_ids`). The night a sync
finds the film on Netflix, the matcher anchors the listing on this title by its
TMDB id like any other, it gains an availability row, and from then on it is an
ordinary title with nothing for anyone to flip. A flag would have been one more
thing a sync had to remember.

Nothing here trusts the caller about the film itself. The caller names a TMDB
id; what the title is called, when it was made and where its poster comes from
are read from TMDB by the server (:class:`MovieRecord`). A name typed in a
browser would be a name anybody could make the catalog say, and a poster URL
from one is an address the image pipeline would go and fetch.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Select, exists, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from eifo_core.enums import ItemStatus, TitleKind
from eifo_core.items import plausible_year
from eifo_core.models import Availability, Title, TmdbAlias, User, UserItem
from eifo_core.naming import is_hebrew, split_by_script
from eifo_core.types import utcnow

#: How many films one member may add in a day.
#:
#: Generous for anybody recording what they watched - nobody saw twenty films
#: yesterday - and a ceiling on what a stuck client or a careless script can do
#: to a catalog everyone shares.
DAILY_LIMIT = 20

#: The window the limit counts over. Rolling rather than by calendar day, so
#: there is no midnight at which it suddenly resets.
LIMIT_WINDOW = dt.timedelta(days=1)

#: Where TMDB serves artwork. The only host a poster URL built here can name.
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w500"

_POSTER_PATH = re.compile(r"/[A-Za-z0-9_-]{1,100}\.(?:jpg|jpeg|png|webp)")


@dataclass(frozen=True, slots=True)
class MovieRecord:
    """What TMDB says about one film, read by the server, never by a client."""

    tmdb_id: int
    imdb_id: str | None
    name_he: str | None
    name_en: str | None
    year: int | None
    overview_he: str | None
    overview_en: str | None
    poster_path: str | None
    runtime_minutes: int | None
    original_language: str | None

    @property
    def poster_source_url(self) -> str | None:
        return f"{TMDB_IMAGE_BASE}{self.poster_path}" if self.poster_path else None


def movie_from_tmdb(hebrew: dict[str, Any], english: dict[str, Any]) -> MovieRecord | None:
    """Read a film out of TMDB's Hebrew and English detail responses.

    Names go in the column of the script they are written in, never the one
    TMDB returned them under. Asked for Hebrew, TMDB answers in English when it
    has no translation, and taking that as the Hebrew name is how a catalog ends
    up with the same English string in both columns.

    Returns None for a payload that is not a film TMDB would show - one with no
    id, or no name in either script nor any other.
    """
    raw_id = english.get("id") or hebrew.get("id")
    if not isinstance(raw_id, int) or raw_id <= 0:
        return None

    name_he, name_en = split_by_script(
        _text(hebrew.get("title")),
        _text(english.get("title")),
        _text(english.get("original_title")),
    )
    if name_he is None and name_en is None:
        # A film known only by a name in a third script: keep that rather than
        # nothing, in the English column, which is where the catalog keeps
        # every name that is not Hebrew.
        name_en = _text(english.get("original_title")) or _text(english.get("title"))
    if name_he is None and name_en is None:
        return None

    overview_he = _text(hebrew.get("overview"))
    return MovieRecord(
        tmdb_id=raw_id,
        imdb_id=_imdb_id(english.get("imdb_id") or hebrew.get("imdb_id")),
        name_he=name_he[:500] if name_he else None,
        name_en=name_en[:500] if name_en else None,
        year=plausible_year(_year(english.get("release_date") or hebrew.get("release_date"))),
        overview_he=overview_he if overview_he and is_hebrew(overview_he) else None,
        overview_en=_text(english.get("overview")),
        poster_path=tmdb_poster_path(english.get("poster_path") or hebrew.get("poster_path")),
        runtime_minutes=_positive(english.get("runtime")),
        original_language=_language(english.get("original_language")),
    )


def user_added_ids() -> Select[tuple[int]]:
    """Titles added by members that no service has ever listed.

    Any availability row at all ends it, a lapsed one included. A film that
    was on Netflix last year and left is a title that left Netflix - the
    catalog's ordinary "no longer available" - not one only a member vouches for.
    """
    listed = exists().where(Availability.title_id == Title.id)
    return select(Title.id).where(Title.added_at.is_not(None), ~listed)


def is_user_added(session: Session, title_id: int) -> bool:
    """Whether one title is, today, only here because a member added it."""
    found = session.scalar(user_added_ids().where(Title.id == title_id))
    return found is not None


def find_existing(session: Session, tmdb_id: int, imdb_id: str | None) -> Title | None:
    """The title this film already is, if the catalog holds it in any form.

    In the order the matcher trusts them: the TMDB id in the film namespace,
    then a TMDB id known to be a second record of a title we hold, then the
    IMDb id. Missing any one of them is how the same film ends up twice, once
    from a sync and once from a member.
    """
    title = session.scalar(
        select(Title).where(Title.type == TitleKind.MOVIE, Title.tmdb_id == tmdb_id)
    )
    if title is not None:
        return title

    alias = session.get(TmdbAlias, (TitleKind.MOVIE, tmdb_id))
    if alias is not None:
        return session.get(Title, alias.title_id)

    if imdb_id:
        return session.scalar(select(Title).where(Title.imdb_id == imdb_id))
    return None


def added_recently(session: Session, user: User, *, now: dt.datetime | None = None) -> int:
    """How many films this member added inside the limit's window."""
    since = (now or utcnow()) - LIMIT_WINDOW
    count = session.scalar(
        select(func.count())
        .select_from(Title)
        .where(Title.added_by_user_id == user.id, Title.added_at >= since)
    )
    return count or 0


@dataclass(frozen=True, slots=True)
class Added:
    """What adding a film did."""

    title: Title
    item: UserItem
    #: False when the catalog already held the film and this only marked it
    #: watched. The caller says so, rather than claiming a title was added.
    created: bool


class DailyLimitReachedError(Exception):
    """The member has added :data:`DAILY_LIMIT` films inside the window."""


def add_to_list(
    session: Session,
    user: User,
    record: MovieRecord,
    *,
    onto: ItemStatus = ItemStatus.WATCHED,
    rating: int | None = None,
) -> Added:
    """Put a film on one of a member's lists, adding it to the catalog if new.

    Watched, or meant to be: somebody who heard about a film at a festival
    wants to remember it as much as somebody who saw it there. The other list
    is left as it was - they are two flags, not one state.

    One commit for both halves: a title nobody has on a list, left behind by a
    failure between the two, is a stray in "Other services" that no one put
    there.

    Two members adding the same film at once is settled by ``uq_title_tmdb``.
    The second insert fails, its savepoint rolls back, and it takes the title
    the first one wrote - so the call can be repeated without consequence.

    Raises:
        DailyLimitReachedError: only when this would create a title. Listing a
            film the catalog already has is not an addition and is never
            refused.
    """
    title = find_existing(session, record.tmdb_id, record.imdb_id)
    created = False
    if title is None:
        if added_recently(session, user) >= DAILY_LIMIT:
            raise DailyLimitReachedError
        title, created = _create(session, user, record)

    item = session.scalar(
        select(UserItem).where(UserItem.user_id == user.id, UserItem.title_id == title.id)
    )
    if item is None:
        item = UserItem(user_id=user.id, title_id=title.id)
        session.add(item)
    if onto is ItemStatus.WATCHED:
        item.watched = True
    else:
        item.want_to_watch = True
    if rating is not None:
        item.rating = rating

    session.commit()
    return Added(title=title, item=item, created=created)


def _create(session: Session, user: User, record: MovieRecord) -> tuple[Title, bool]:
    title = Title(
        type=TitleKind.MOVIE,
        tmdb_id=record.tmdb_id,
        imdb_id=record.imdb_id,
        name_he=record.name_he,
        name_en=record.name_en,
        year=record.year,
        overview_he=record.overview_he,
        overview_en=record.overview_en,
        poster_source_url=record.poster_source_url,
        runtime_minutes=record.runtime_minutes,
        original_language=record.original_language,
        added_at=utcnow(),
        added_by_user_id=user.id,
    )
    try:
        with session.begin_nested():
            session.add(title)
    except IntegrityError:
        # Somebody else's insert of the same film won the race - or its IMDb id
        # already belongs to a title the lookups above could not see by it.
        # Either way the catalog has it now; take that one.
        existing = find_existing(session, record.tmdb_id, record.imdb_id)
        if existing is None:
            raise
        return existing, False
    return title, True


def removable(session: Session, title: Title) -> bool:
    """Whether an administrator may delete this title outright.

    Only while it is a member's addition and nothing more. Once any service has
    listed it, it is catalog data like the rest, with history the sync keeps.
    """
    return title.added_at is not None and is_user_added(session, title.id)


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _imdb_id(value: Any) -> str | None:
    text = _text(value)
    if text and text.startswith("tt") and text[2:].isdigit() and len(text) <= 16:
        return text
    return None


def _year(value: Any) -> int | None:
    text = _text(value)
    if text and len(text) >= 4 and text[:4].isdigit():
        return int(text[:4])
    return None


def tmdb_poster_path(value: Any) -> str | None:
    """A TMDB image path, and only that: ``/abc123.jpg``, never a URL.

    The image pipeline fetches whatever ``poster_source_url`` names, so the
    one part of it that came from outside is held to the one shape TMDB uses:
    a single file name under the root.
    """
    text = _text(value)
    if text is None or _POSTER_PATH.fullmatch(text) is None:
        return None
    return text


def _positive(value: Any) -> int | None:
    return value if isinstance(value, int) and 0 < value < 2000 else None


def _language(value: Any) -> str | None:
    text = _text(value)
    return text if text and len(text) <= 8 and text.isalpha() else None
