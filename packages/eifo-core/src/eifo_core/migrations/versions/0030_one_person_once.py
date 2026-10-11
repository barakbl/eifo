"""Fold the people the catalog holds twice into one.

A director TMDB knows came in under an English name; the same director, from
an Israeli catalogue, came in under a Hebrew one, and neither name matched the
other - so the catalog held two people, each with half the films, and a
recommendation sharing "both" counted the director twice. Ingestion now joins
them as credits arrive (``eifo_core.people.merge_duplicates``); this joins the
ones already there.

The rule is the same, written out here rather than imported, because a
migration should do exactly what it did the day it was written: a person
without a TMDB id is the TMDB person who, on every title they are both
credited on in one role - and that TMDB credits anyone in that role on - is
the only one there, whose name could be theirs (first name and surname
starting with letters the other could be written with), and who is claimed by
no other catalogue name.

Revision ID: 0030_one_person_once
Revises: 0029_sign_in_with_eifo
Create Date: 2026-10-11
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict

import sqlalchemy as sa
from alembic import op

revision = "0030_one_person_once"
down_revision = "0029_sign_in_with_eifo"
branch_labels = None
depends_on = None

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
    return [word for word in re.split(r"[\s\-־.]+", name) if word]


def _initials_agree(hebrew: str, english: str) -> bool:
    he = [word[0] for word in _words(hebrew) if word[0] in _LATIN]
    en = [
        unicodedata.normalize("NFKD", word)[0].upper()
        for word in _words(english)
        if word[0].isalpha()
    ]
    if not he or not en or en[0] not in _LATIN[he[0]]:
        return False
    if len(he) == 1 or len(en) == 1:
        return True
    return en[-1] in _LATIN[he[-1]] or any(e in _LATIN[h] for h in he[1:] for e in en[1:])


def _could_be(named: sa.Row, known: sa.Row) -> bool:
    if named.name_he and known.name_he and named.name_he != known.name_he:
        return False
    if named.name_en and known.name_en:
        return bool(named.name_en.casefold() == known.name_en.casefold())
    if named.name_he and known.name_en:
        return _initials_agree(named.name_he, known.name_en)
    return bool(named.name_he and named.name_he == known.name_he)


def upgrade() -> None:
    bind = op.get_bind()
    people = {
        row.id: row
        for row in bind.execute(sa.text("SELECT id, tmdb_id, name_en, name_he FROM people"))
    }
    named = [
        row
        for row in people.values()
        if row.tmdb_id is None and "," not in (row.name_en or "") + (row.name_he or "")
    ]
    if not named:
        return

    roles: dict[int, set[tuple[int, str]]] = defaultdict(set)
    known: dict[tuple[int, str], set[int]] = defaultdict(set)
    for title_id, person_id, role in bind.execute(
        sa.text("SELECT title_id, person_id, role FROM credits")
    ):
        if people[person_id].tmdb_id is None:
            roles[person_id].add((title_id, role))
        else:
            known[(title_id, role)].add(person_id)

    taken: set[int] = set()
    for person in named:
        common: set[int] | None = None
        for key in roles[person.id]:
            if not known[key]:
                continue
            here = {other for other in known[key] if _could_be(person, people[other])}
            common = here if common is None else common & here
        if common is None or len(common) != 1:
            continue
        kept = next(iter(common))
        if kept in taken:
            continue
        taken.add(kept)
        _fold(bind, person.id, kept)


def _fold(bind: sa.Connection, gone: int, kept: int) -> None:
    held = {"gone": gone, "kept": kept}
    # Both credited for the same thing on the same title: one credit is enough.
    bind.execute(
        sa.text(
            """
            DELETE FROM credits
            WHERE person_id = :gone AND EXISTS (
                SELECT 1 FROM credits AS other
                WHERE other.person_id = :kept
                  AND other.title_id = credits.title_id
                  AND other.role = credits.role
                  AND other.character IS credits.character
            )
            """
        ),
        held,
    )
    bind.execute(sa.text("UPDATE credits SET person_id = :kept WHERE person_id = :gone"), held)
    bind.execute(
        sa.text(
            """
            UPDATE people SET
                name_he = COALESCE(name_he, (SELECT name_he FROM people WHERE id = :gone)),
                name_en = COALESCE(name_en, (SELECT name_en FROM people WHERE id = :gone)),
                profile_source_url = COALESCE(
                    profile_source_url,
                    (SELECT profile_source_url FROM people WHERE id = :gone)
                )
            WHERE id = :kept
            """
        ),
        held,
    )
    bind.execute(sa.text("DELETE FROM people WHERE id = :gone"), held)


def downgrade() -> None:
    """Nothing to restore: the second copy of a person carried nothing the
    first does not now hold."""
