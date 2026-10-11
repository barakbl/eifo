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
from eifo_core.people import (
    _initials_agree,
    apply_credits,
    get_or_create_person,
    merge_duplicates,
)


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


class TestMergeDuplicates:
    """The same director, once from TMDB in English and once from a catalogue
    in Hebrew, met on the same film."""

    def _film(self, session: Session, name: str) -> Title:
        row = Title(type=TitleKind.MOVIE, name_en=name, year=1990)
        session.add(row)
        session.flush()
        return row

    def _tmdb(self, tmdb_id: int, name: str, role: str = "director") -> dict[str, object]:
        return {"role": role, "tmdb_id": tmdb_id, "name_en": name}

    def _catalogue(self, name: str, role: str = "director") -> dict[str, object]:
        return {"role": role, "name_he": name}

    def _directors(self, session: Session, title: Title) -> list[Person]:
        return list(
            session.scalars(
                select(Person)
                .join(Credit, Credit.person_id == Person.id)
                .where(Credit.title_id == title.id, Credit.role == CreditRole.DIRECTOR)
            )
        )

    def test_a_catalogue_name_joins_the_tmdb_person(self, session: Session) -> None:
        film = self._film(session, "The Band's Visit")
        apply_credits(session, film, [self._tmdb(42412, "Eran Kolirin")], source="tmdb")
        apply_credits(session, film, [self._catalogue("ערן קולירין")], source="lev")

        [director] = self._directors(session, film)
        assert (director.tmdb_id, director.name_en, director.name_he) == (
            42412,
            "Eran Kolirin",
            "ערן קולירין",
        )
        assert session.scalar(select(Person).where(Person.tmdb_id.is_(None))) is None

    def test_whichever_arrives_first(self, session: Session) -> None:
        film = self._film(session, "The Circus")
        apply_credits(session, film, [self._catalogue("צ'ארלי צ'פלין")], source="lev")
        apply_credits(session, film, [self._tmdb(13848, "Charlie Chaplin")], source="tmdb")

        [director] = self._directors(session, film)
        assert (director.name_en, director.name_he) == ("Charlie Chaplin", "צ'ארלי צ'פלין")

    def test_names_that_cannot_be_one_person_stay_apart(self, session: Session) -> None:
        """A catalogue matched to the wrong TMDB film: same role, other man."""
        film = self._film(session, "Mismatched")
        apply_credits(session, film, [self._tmdb(1, "Colin Tilley")], source="tmdb")
        apply_credits(session, film, [self._catalogue("אמל כראם")], source="lev")

        assert len(self._directors(session, film)) == 2

    def test_two_names_in_one_stay_apart(self, session: Session) -> None:
        film = self._film(session, "Co-directed")
        apply_credits(session, film, [self._tmdb(1, "Reshef Levi")], source="tmdb")
        apply_credits(session, film, [self._catalogue("רשף לוי, שי כנות")], source="lev")

        assert len(self._directors(session, film)) == 2

    def test_co_directors_are_left_alone_until_another_film_tells(self, session: Session) -> None:
        both = self._film(session, "Together")
        apply_credits(
            session,
            both,
            [self._tmdb(1, "Shmuel Hasfari"), self._tmdb(2, "Shlomo Hasson")],
            source="tmdb",
        )
        apply_credits(session, both, [self._catalogue("שמואל הספרי")], source="lev")
        assert len(self._directors(session, both)) == 3, "S. H. and S. H.: either could be him"

        alone = self._film(session, "Alone")
        apply_credits(session, alone, [self._tmdb(1, "Shmuel Hasfari")], source="tmdb")
        apply_credits(session, alone, [self._catalogue("שמואל הספרי")], source="lev")

        assert {person.tmdb_id for person in self._directors(session, both)} == {1, 2}
        assert [person.name_he for person in self._directors(session, alone)] == ["שמואל הספרי"]

    def test_films_that_disagree_merge_nothing(self, session: Session) -> None:
        first, second = self._film(session, "First"), self._film(session, "Second")
        named = Person(name_he="אבי כהן")
        avi, avraham = (
            Person(tmdb_id=1, name_en="Avi Cohen"),
            Person(tmdb_id=2, name_en="Avraham Cohen"),
        )
        session.add_all([named, avi, avraham])
        session.flush()
        for film, person in ((first, named), (second, named), (first, avi), (second, avraham)):
            session.add(
                Credit(title_id=film.id, person_id=person.id, role=CreditRole.DIRECTOR, source="x")
            )
        session.flush()

        assert merge_duplicates(session) == 0

    def test_a_film_tmdb_credits_nobody_on_says_nothing(self, session: Session) -> None:
        known, unknown = self._film(session, "Known"), self._film(session, "Unknown")
        apply_credits(session, unknown, [self._catalogue("ארי פולמן")], source="lev")
        apply_credits(session, known, [self._catalogue("ארי פולמן")], source="lev")
        apply_credits(session, known, [self._tmdb(1, "Ari Folman")], source="tmdb")

        [director] = self._directors(session, unknown)
        assert director.tmdb_id == 1

    def test_one_film_of_many_leads_is_no_evidence(self, session: Session) -> None:
        film = self._film(session, "Ensemble")
        apply_credits(
            session,
            film,
            [self._tmdb(1, "Assi Dayan", "cast"), self._tmdb(2, "Avi Dagan", "cast")],
            source="tmdb",
        )
        apply_credits(session, film, [self._catalogue("אסי דיין", "cast")], source="lev")

        assert session.scalar(select(Person).where(Person.name_he == "אסי דיין")).tmdb_id is None

    def test_everybody_at_once_and_only_once(self, session: Session) -> None:
        """What a catalog that already holds the duplicates is cleaned with."""
        film = self._film(session, "Before the fix")
        kept = Person(tmdb_id=1, name_en="Keren Yedaya")
        gone = Person(name_he="קרן ידעיה")
        session.add_all([kept, gone])
        session.flush()
        for person in (kept, gone):
            session.add(
                Credit(title_id=film.id, person_id=person.id, role=CreditRole.DIRECTOR, source="x")
            )
        session.flush()

        assert merge_duplicates(session) == 1
        assert merge_duplicates(session) == 0
        assert [person.name_he for person in self._directors(session, film)] == ["קרן ידעיה"]


@pytest.mark.parametrize(
    ("hebrew", "english"),
    [
        ("צ'ארלי צ'פלין", "Charlie Chaplin"),
        ("שמואל פירסטנברג", "Sam Firstenberg"),
        ("יוחנן (חורחה) ולר", "Yohanan Weller"),
        ("שרון בר־זיו", "Sharon Bar-Ziv"),
        ("חנה אזולאי הספרי", "Hana Azoulay-Hasfari"),
        ("ז'אק מורי קתמור", "Jacques Katmor"),
        ("מיכאל רייחל", "Michael Reichel Dudov"),
        ("ראדו מיכאיליאנו", "Radu Mihăileanu"),
        ("יוסף לייטס", "Józef Lejtes"),
    ],
)
def test_initials_that_agree(hebrew: str, english: str) -> None:
    assert _initials_agree(hebrew, english)


@pytest.mark.parametrize(
    ("hebrew", "english"),
    [
        ("אמל כראם", "Colin Tilley"),
        ("יונתן שצ'וצ'ינסקי", "Anthony T. Migliori"),
        ("שמואל הספרי", "Shimon Dotan"),
    ],
)
def test_initials_that_do_not(hebrew: str, english: str) -> None:
    assert not _initials_agree(hebrew, english)
