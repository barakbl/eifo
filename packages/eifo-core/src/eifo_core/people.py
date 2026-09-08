"""Turning names into people, and people into credits.

Every source that knows who made a title hands the pipeline plain names, so
this module owns the question that follows: which person is this?

A person TMDB knows is identified by ``tmdb_id`` and is exact. A person scraped
from an Israeli catalogue has only a name, so an existing person with that same
name is assumed to be them. That is a real limitation - two directors called
דוד כהן would merge - and it is the honest trade for having Israeli cinema
credited at all, since TMDB does not carry most of it.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from eifo_core.enums import CreditRole
from eifo_core.models import Credit, Person, Title

logger = logging.getLogger("eifo.fetch.people")


def get_or_create_person(
    session: Session,
    *,
    tmdb_id: int | None = None,
    name_en: str | None = None,
    name_he: str | None = None,
    profile_source_url: str | None = None,
) -> Person | None:
    """Find this person, or add them. None if there is no usable name."""
    name_en = (name_en or "").strip() or None
    name_he = (name_he or "").strip() or None
    if not name_en and not name_he:
        return None

    person = _existing(session, tmdb_id=tmdb_id, name_en=name_en, name_he=name_he)
    if person is not None:
        # A later sighting may carry what the first one lacked.
        person.name_en = person.name_en or name_en
        person.name_he = person.name_he or name_he
        person.tmdb_id = person.tmdb_id or tmdb_id
        person.profile_source_url = person.profile_source_url or profile_source_url
        return person

    person = Person(
        tmdb_id=tmdb_id,
        name_en=name_en,
        name_he=name_he,
        profile_source_url=profile_source_url,
    )
    session.add(person)
    session.flush()
    return person


def _existing(
    session: Session,
    *,
    tmdb_id: int | None,
    name_en: str | None,
    name_he: str | None,
) -> Person | None:
    if tmdb_id is not None:
        person = session.scalar(select(Person).where(Person.tmdb_id == tmdb_id))
        if person is not None:
            return person

    matches = [
        column == value
        for column, value in ((Person.name_en, name_en), (Person.name_he, name_he))
        if value
    ]
    if not matches:
        return None
    # A person TMDB knows is never merged into a scraped namesake: only rows
    # without an id of their own are open to being matched by name.
    return session.scalar(
        select(Person).where(or_(*matches), Person.tmdb_id.is_(None)).order_by(Person.id)
    )


def apply_credits(
    session: Session,
    title: Title,
    entries: Sequence[Mapping[str, Any]],
    *,
    source: str,
) -> int:
    """Attach credits to a title, skipping any it already carries.

    Additive on purpose: TMDB and an Israeli catalogue can both credit the same
    film, and the one that got there first is not necessarily the fuller. The
    unique key (title, person, role, character) keeps repeats out.

    Returns:
        How many credits were added.
    """
    if not entries:
        return 0

    existing = {
        (credit.person_id, credit.role, credit.character)
        for credit in session.scalars(select(Credit).where(Credit.title_id == title.id)).all()
    }

    added = 0
    for entry in entries:
        role = _role(entry.get("role"))
        if role is None:
            continue

        person = get_or_create_person(
            session,
            tmdb_id=_int_or_none(entry.get("tmdb_id")),
            name_en=entry.get("name_en"),
            name_he=entry.get("name_he"),
            profile_source_url=entry.get("profile_source_url"),
        )
        if person is None:
            continue

        character = (entry.get("character") or "").strip() or None
        key = (person.id, role, character)
        if key in existing:
            continue

        session.add(
            Credit(
                title_id=title.id,
                person_id=person.id,
                role=role,
                character=character,
                billing_order=_int_or_none(entry.get("billing_order")),
                source=source,
            )
        )
        existing.add(key)
        added += 1

    return added


def _role(value: Any) -> CreditRole | None:
    if isinstance(value, CreditRole):
        return value
    try:
        return CreditRole(str(value))
    except ValueError:
        logger.debug("unknown credit role %r; skipped", value)
        return None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
