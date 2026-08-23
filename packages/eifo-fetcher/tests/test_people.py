"""Turning names into people, and people into credits.

The interesting cases are all about identity: TMDB knows exactly who someone
is, a scraped Israeli catalogue knows only a name, and the two have to meet in
one table without either inventing a person or merging two real ones.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from eifo_core.enums import CreditRole, TitleKind
from eifo_core.models import Credit, Person, Title
from eifo_fetcher.people import apply_credits, get_or_create_person


@pytest.fixture
def title(session: Session) -> Title:
    row = Title(type=TitleKind.MOVIE, name_he="עורבים", year=1988)
    session.add(row)
    session.flush()
    return row


class TestGetOrCreatePerson:
    def test_creates_someone_new(self, session: Session) -> None:
        person = get_or_create_person(session, tmdb_id=7, name_en="Jon Watts")

        assert person is not None
        assert (person.name_en, person.tmdb_id) == ("Jon Watts", 7)

    def test_returns_the_same_person_for_the_same_tmdb_id(self, session: Session) -> None:
        first = get_or_create_person(session, tmdb_id=7, name_en="Jon Watts")
        again = get_or_create_person(session, tmdb_id=7, name_en="Jon Watts")

        assert first is again
        assert session.scalar(select(Person).where(Person.tmdb_id == 7)) is first

    def test_matches_a_scraped_person_by_name(self, session: Session) -> None:
        """The archive only ever gives a name, so a name is what identifies them."""
        first = get_or_create_person(session, name_he="איילת מנחמי")
        again = get_or_create_person(session, name_he="איילת מנחמי")

        assert first is again

    def test_a_later_sighting_fills_what_the_first_lacked(self, session: Session) -> None:
        get_or_create_person(session, name_he="איילת מנחמי")
        person = get_or_create_person(session, name_he="איילת מנחמי", name_en="Ayelet Menachemi")

        assert person is not None
        assert (person.name_he, person.name_en) == ("איילת מנחמי", "Ayelet Menachemi")

    def test_a_namesake_never_swallows_someone_tmdb_knows(self, session: Session) -> None:
        """Only rows without an id of their own are open to a name match."""
        known = get_or_create_person(session, tmdb_id=7, name_en="David Cohen")
        scraped = get_or_create_person(session, name_en="David Cohen")

        assert scraped is not known
        assert scraped is not None
        assert scraped.id != known.id

    def test_two_people_may_share_a_name(self, session: Session) -> None:
        """Names are not unique; ids are, which is why a person is addressed by one."""
        first = get_or_create_person(session, tmdb_id=1, name_en="David Cohen")
        second = get_or_create_person(session, tmdb_id=2, name_en="David Cohen")

        assert first is not None and second is not None
        assert first.id != second.id

    def test_a_person_with_no_usable_name_is_not_invented(self, session: Session) -> None:
        assert get_or_create_person(session, tmdb_id=7, name_en="   ") is None


class TestApplyCredits:
    def _entries(self) -> list[dict[str, object]]:
        return [
            {"role": CreditRole.DIRECTOR, "name_en": "Jon Watts", "tmdb_id": 7},
            {
                "role": CreditRole.CAST,
                "name_en": "Tom Holland",
                "tmdb_id": 8,
                "character": "Peter Parker",
                "billing_order": 0,
            },
        ]

    def test_attaches_credits_and_counts_them(self, session: Session, title: Title) -> None:
        added = apply_credits(session, title, self._entries(), source="tmdb")
        session.flush()

        assert added == 2
        roles = {credit.role for credit in session.scalars(select(Credit)).all()}
        assert roles == {CreditRole.DIRECTOR, CreditRole.CAST}

    def test_keeps_the_billing_of_the_lead(self, session: Session, title: Title) -> None:
        """Billing zero is the top of the bill, not a missing value."""
        apply_credits(session, title, self._entries(), source="tmdb")
        session.flush()

        lead = session.scalar(select(Credit).where(Credit.role == CreditRole.CAST))
        assert lead is not None
        assert lead.billing_order == 0

    def test_records_who_said_so(self, session: Session, title: Title) -> None:
        apply_credits(session, title, self._entries()[:1], source="israel_film_archive")
        session.flush()

        credit = session.scalars(select(Credit)).one()
        assert credit.source == "israel_film_archive"

    def test_running_twice_adds_nothing_the_second_time(
        self, session: Session, title: Title
    ) -> None:
        apply_credits(session, title, self._entries(), source="tmdb")
        session.flush()

        assert apply_credits(session, title, self._entries(), source="tmdb") == 0
        assert len(session.scalars(select(Credit)).all()) == 2

    def test_two_sources_can_credit_the_same_film(self, session: Session, title: Title) -> None:
        """TMDB and an Israeli catalogue both know things; neither is complete."""
        apply_credits(session, title, self._entries(), source="tmdb")
        session.flush()
        added = apply_credits(
            session,
            title,
            [{"role": CreditRole.DIRECTOR, "name_he": "איילת מנחמי"}],
            source="israel_film_archive",
        )
        session.flush()

        assert added == 1
        directors = session.scalars(select(Credit).where(Credit.role == CreditRole.DIRECTOR)).all()
        assert len(directors) == 2

    def test_an_unknown_role_is_skipped_rather_than_stored(
        self, session: Session, title: Title
    ) -> None:
        added = apply_credits(
            session, title, [{"role": "best boy", "name_en": "Someone"}], source="tmdb"
        )

        assert added == 0
        assert session.scalars(select(Credit)).all() == []

    def test_a_nameless_credit_is_skipped(self, session: Session, title: Title) -> None:
        added = apply_credits(session, title, [{"role": CreditRole.DIRECTOR}], source="tmdb")

        assert added == 0

    def test_the_same_person_can_hold_two_roles_on_one_title(
        self, session: Session, title: Title
    ) -> None:
        added = apply_credits(
            session,
            title,
            [
                {"role": CreditRole.DIRECTOR, "name_en": "Amos Gitai", "tmdb_id": 3},
                {"role": CreditRole.CAST, "name_en": "Amos Gitai", "tmdb_id": 3},
            ],
            source="tmdb",
        )
        session.flush()

        assert added == 2
        assert len(session.scalars(select(Person)).all()) == 1
