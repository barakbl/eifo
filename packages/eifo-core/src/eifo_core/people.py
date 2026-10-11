"""Turning names into people, and people into credits.

Every source that knows who made a title hands the pipeline plain names, so
this module owns the question that follows: which person is this?

A person TMDB knows is identified by ``tmdb_id`` and is exact. A person scraped
from an Israeli catalogue has only a name, so an existing person with that same
name is assumed to be them. That is a real limitation - two directors called
דוד כהן would merge - and it is the honest trade for having Israeli cinema
credited at all, since TMDB does not carry most of it.

The opposite fault is the commoner one: the same director twice, once from TMDB
under an English name and once from a catalogue under a Hebrew one, because
neither name is the other's. :func:`merge_duplicates` joins them on the one
evidence that is not a name - both credited, in the same role, on the same
titles - and only where the names could be one person's.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from sqlalchemy import delete, or_, select
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

    if added:
        session.flush()
        merge_duplicates(
            session,
            session.scalars(select(Credit.person_id).where(Credit.title_id == title.id)).all(),
        )
    return added


def merge_duplicates(session: Session, person_ids: Iterable[int] | None = None) -> int:
    """Fold people held twice into one, and say how many were folded.

    A person without a ``tmdb_id`` - a catalogue's name, nothing more - is the
    TMDB person it duplicates when, on every title it is credited on where
    TMDB credits anyone in that role, exactly one TMDB person holds it, the
    same one each time, and its names
    could be theirs (:func:`_could_be`). Then its credits move over, its Hebrew
    name fills theirs, and it goes.

    ``person_ids`` limits the question to these people and whoever shares a
    title with them - what a new title's credits can have changed. None asks
    it of everybody.
    """
    folded = 0
    for gone, kept in _plan(session, person_ids):
        _fold(session, gone, kept)
        folded += 1
    if folded:
        session.flush()
        logger.info("merged %d people held twice", folded)
    return folded


def _plan(session: Session, person_ids: Iterable[int] | None) -> list[tuple[Person, Person]]:
    asked = None if person_ids is None else set(person_ids)
    named = select(Person).where(Person.tmdb_id.is_(None))
    if asked is not None:
        if not asked:
            return []
        named = named.where(Person.id.in_(asked))
    candidates = [person for person in session.scalars(named) if not _several(person)]
    if not candidates:
        return []

    roles: dict[int, set[tuple[int, CreditRole]]] = defaultdict(set)
    for title_id, person_id, role in session.execute(
        select(Credit.title_id, Credit.person_id, Credit.role).where(
            Credit.person_id.in_([person.id for person in candidates])
        )
    ):
        roles[person_id].add((title_id, role))
    titles = {title_id for held in roles.values() for title_id, _ in held}

    known: dict[tuple[int, CreditRole], set[int]] = defaultdict(set)
    tmdb_people: dict[int, Person] = {}
    for credit, person in session.execute(
        select(Credit, Person)
        .join(Person, Person.id == Credit.person_id)
        .where(Credit.title_id.in_(titles), Person.tmdb_id.is_not(None))
    ).tuples():
        known[(credit.title_id, credit.role)].add(person.id)
        tmdb_people[person.id] = person

    plan: list[tuple[Person, Person]] = []
    taken: set[int] = set()
    for person in candidates:
        common: set[int] | None = None
        for key in roles[person.id]:
            # A title TMDB credits nobody in this role on says nothing.
            if not known[key]:
                continue
            here = {other for other in known[key] if _could_be(person, tmdb_people[other])}
            common = here if common is None else common & here
        if common is not None and len(common) == 1:
            kept = tmdb_people[next(iter(common))]
            # Two catalogue names for one TMDB person is one too many to trust.
            if kept.id not in taken:
                taken.add(kept.id)
                plan.append((person, kept))
    return plan


def _fold(session: Session, gone: Person, kept: Person) -> None:
    held = {
        (credit.title_id, credit.role, credit.character)
        for credit in session.scalars(select(Credit).where(Credit.person_id == kept.id))
    }
    for credit in session.scalars(select(Credit).where(Credit.person_id == gone.id)).all():
        if (credit.title_id, credit.role, credit.character) in held:
            session.delete(credit)
        else:
            credit.person_id = kept.id
    kept.name_he = kept.name_he or gone.name_he
    kept.name_en = kept.name_en or gone.name_en
    kept.profile_source_url = kept.profile_source_url or gone.profile_source_url
    session.flush()
    session.execute(delete(Person).where(Person.id == gone.id))
    session.expire(kept, ["credits"])


def _several(person: Person) -> bool:
    """A catalogue name that is really two: "רשף לוי, שי כנות"."""
    return any("," in (name or "") for name in (person.name_en, person.name_he))


def _could_be(named: Person, known: Person) -> bool:
    """Whether a catalogue's person could be this TMDB one by their names.

    Names in the same script must agree outright. A Hebrew name against an
    English one is held to their initials - first name and surname each
    starting with a letter the other could be written with - which is enough
    to tell Colin Tilley from אמל כראם and lets צ'ארלי צ'פלין be Charlie
    Chaplin.
    """
    if named.name_he and known.name_he and named.name_he != known.name_he:
        return False
    if named.name_en and known.name_en:
        return named.name_en.casefold() == known.name_en.casefold()
    if named.name_he and known.name_en:
        return _initials_agree(named.name_he, known.name_en)
    return bool(named.name_he and named.name_he == known.name_he)


#: What a Hebrew letter at the start of a name may be written as in English.
_LATIN = {
    "א": "AEIOUY",
    "ב": "BV",
    "ג": "GJ",
    "ד": "D",
    "ה": "HAE",
    "ו": "VWOU",  # noqa: RUF001 - Hebrew, not Latin
    "ז": "ZJ",
    "ח": "HCK",
    "ט": "T",  # noqa: RUF001 - Hebrew, not Latin
    "י": "YIJE",  # noqa: RUF001 - Hebrew, not Latin
    "כ": "KCH",
    "ל": "L",
    "מ": "M",
    "נ": "N",
    "ס": "SC",  # noqa: RUF001 - Hebrew, not Latin
    "ע": "AEIOUY",
    "פ": "PF",
    "צ": "TZCS",
    "ק": "KQC",
    "ר": "R",
    "ש": "SC",
    "ת": "T",
}


def _words(name: str) -> list[str]:
    name = re.sub(r"\([^)]*\)", " ", name)
    return [word for word in re.split(r"[\s\-\u05be.]+", name) if word]


def _initials_agree(hebrew: str, english: str) -> bool:
    he = [word[0] for word in _words(hebrew) if word[0] in _LATIN]
    en = [
        unicodedata.normalize("NFKD", word)[0].upper()
        for word in _words(english)
        if word[0].isalpha()
    ]
    if not he or not en:
        return False

    def agree(letter: str, latin: str) -> bool:
        return latin in _LATIN[letter]

    if not agree(he[0], en[0]):
        return False
    if len(he) == 1 or len(en) == 1:
        return True
    # The surname: last against last, or - for a middle name one side lacks -
    # any other word against any other.
    return agree(he[-1], en[-1]) or any(agree(h, e) for h in he[1:] for e in en[1:])


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
