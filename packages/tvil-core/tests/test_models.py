"""Schema constraints and relationships."""

from __future__ import annotations

import datetime as dt

import pytest
from factories import make_source, make_title, make_user
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.orm import Session

from tvil_core.enums import (
    AuthProvider,
    FetchPhase,
    FetchStatus,
    ItemStatus,
    OfferType,
    TitleKind,
)
from tvil_core.models import (
    Availability,
    FetchRun,
    Genre,
    MatchReview,
    Title,
    User,
    UserItem,
    UserSession,
)


class TestTitle:
    def test_round_trips_bilingual_names(self, session: Session) -> None:
        session.add(make_title())
        session.commit()

        title = session.scalars(select(Title)).one()
        assert title.name_he == "פאודה"
        assert title.name_en == "Fauda"
        assert title.type is TitleKind.SERIES

    def test_requires_at_least_one_name(self, session: Session) -> None:
        session.add(make_title(name_he=None, name_en=None))

        with pytest.raises(IntegrityError):
            session.commit()

    def test_one_name_is_enough(self, session: Session) -> None:
        session.add(make_title(name_en=None))
        session.commit()

        assert session.scalars(select(Title)).one().name_he == "פאודה"

    def test_tmdb_id_is_unique(self, session: Session) -> None:
        session.add_all([make_title(tmdb_id=42), make_title(tmdb_id=42, name_he="אחר")])

        with pytest.raises(IntegrityError):
            session.commit()

    def test_imdb_id_is_unique(self, session: Session) -> None:
        session.add_all(
            [make_title(imdb_id="tt0000001"), make_title(imdb_id="tt0000001", name_he="אחר")]
        )

        with pytest.raises(IntegrityError):
            session.commit()

    def test_rejects_an_unknown_kind(self, session: Session) -> None:
        session.add(make_title(type="documentary"))

        with pytest.raises(StatementError):
            session.commit()

    def test_display_name_prefers_hebrew(self, session: Session) -> None:
        assert make_title().display_name == "פאודה"
        assert make_title(name_he=None).display_name == "Fauda"

    def test_timestamps_are_set_automatically(self, session: Session) -> None:
        session.add(make_title())
        session.commit()

        title = session.scalars(select(Title)).one()
        assert title.created_at.tzinfo is not None
        assert title.updated_at.tzinfo is not None


class TestAvailability:
    def test_links_a_title_to_a_source(self, session: Session) -> None:
        title, source = make_title(), make_source()
        session.add_all([title, source])
        session.flush()
        session.add(
            Availability(
                title_id=title.id,
                source_id=source.id,
                offer_type=OfferType.STREAM,
                deep_link_url="https://cellcom.co.il/fauda",
            )
        )
        session.commit()

        availability = session.scalars(select(Availability)).one()
        assert availability.is_current is True
        assert availability.miss_count == 0
        assert availability.gone_since is None
        assert availability.title.name_en == "Fauda"
        assert availability.source.key == "cellcom_tv"

    def test_one_row_per_title_source_and_offer_type(self, session: Session) -> None:
        title, source = make_title(), make_source()
        session.add_all([title, source])
        session.flush()
        session.add_all(
            [
                Availability(title_id=title.id, source_id=source.id, offer_type=OfferType.STREAM),
                Availability(title_id=title.id, source_id=source.id, offer_type=OfferType.STREAM),
            ]
        )

        with pytest.raises(IntegrityError):
            session.commit()

    def test_the_same_title_may_have_several_offer_types(self, session: Session) -> None:
        title, source = make_title(), make_source()
        session.add_all([title, source])
        session.flush()
        session.add_all(
            [
                Availability(title_id=title.id, source_id=source.id, offer_type=OfferType.RENT),
                Availability(title_id=title.id, source_id=source.id, offer_type=OfferType.BUY),
            ]
        )
        session.commit()

        assert len(session.scalars(select(Availability)).all()) == 2

    def test_deleting_a_title_removes_its_availability(self, session: Session) -> None:
        title, source = make_title(), make_source()
        session.add_all([title, source])
        session.flush()
        session.add(
            Availability(title_id=title.id, source_id=source.id, offer_type=OfferType.STREAM)
        )
        session.commit()

        session.delete(title)
        session.commit()

        assert session.scalars(select(Availability)).all() == []


class TestSource:
    def test_key_is_unique(self, session: Session) -> None:
        session.add_all([make_source(), make_source(name="Duplicate")])

        with pytest.raises(IntegrityError):
            session.commit()

    def test_retiring_a_source_keeps_its_availability(self, session: Session) -> None:
        """A removed source is deactivated, never deleted — history survives."""
        title, source = make_title(), make_source()
        session.add_all([title, source])
        session.flush()
        session.add(
            Availability(title_id=title.id, source_id=source.id, offer_type=OfferType.STREAM)
        )
        session.commit()

        source.active = False
        source.deactivated_at = dt.datetime(2026, 8, 1, tzinfo=dt.UTC)
        session.commit()

        assert session.scalars(select(Availability)).one().source.active is False


class TestGenres:
    def test_titles_and_genres_associate_both_ways(self, session: Session) -> None:
        title = make_title()
        drama = Genre(tmdb_id=18, name_en="Drama", name_he="דרמה")
        title.genres.append(drama)
        session.add(title)
        session.commit()

        assert session.scalars(select(Genre)).one().titles[0].name_en == "Fauda"


class TestFetchRun:
    def test_records_a_run_with_its_stats(self, session: Session) -> None:
        session.add(
            FetchRun(
                source_key="mako",
                phase=FetchPhase.SYNC,
                status=FetchStatus.ABORTED_SUSPICIOUS,
                stats={"items_seen": 3, "previous": 900},
            )
        )
        session.commit()

        run = session.scalars(select(FetchRun)).one()
        assert run.status is FetchStatus.ABORTED_SUSPICIOUS
        assert run.stats["items_seen"] == 3
        assert run.finished_at is None

    def test_phase_runs_may_have_no_source(self, session: Session) -> None:
        session.add(FetchRun(phase=FetchPhase.ENRICH, status=FetchStatus.OK))
        session.commit()

        assert session.scalars(select(FetchRun)).one().source_key is None


class TestMatchReview:
    def test_parks_an_unresolved_item(self, session: Session) -> None:
        session.add(
            MatchReview(
                source_key="mako",
                raw_payload={"name": "תוכנית כלשהי", "year": 2024},
                candidates={"fuzzy": []},
            )
        )
        session.commit()

        review = session.scalars(select(MatchReview)).one()
        assert review.resolved_at is None
        assert review.raw_payload["year"] == 2024


class TestUser:
    def test_is_private_with_no_services_by_default(self, session: Session) -> None:
        """The privacy default is schema-level, not something a caller opts into."""
        session.add(make_user())
        session.commit()

        user = session.scalars(select(User)).one()
        assert user.is_public is False
        assert user.my_source_ids == []
        assert user.handle is None

    def test_the_same_subject_twice_on_one_provider_is_rejected(self, session: Session) -> None:
        session.add_all([make_user(), make_user(display_name="שוב")])

        with pytest.raises(IntegrityError):
            session.commit()

    def test_the_same_subject_on_another_provider_is_a_separate_account(
        self, session: Session
    ) -> None:
        """No cross-provider linking: two providers means two accounts."""
        session.add_all([make_user(), make_user(auth_provider=AuthProvider.X, email=None)])
        session.commit()

        assert len(session.scalars(select(User)).all()) == 2

    def test_handles_are_unique(self, session: Session) -> None:
        session.add_all(
            [
                make_user(handle="tal"),
                make_user(auth_subject="other", handle="tal"),
            ]
        )

        with pytest.raises(IntegrityError):
            session.commit()

    def test_email_is_optional(self, session: Session) -> None:
        """X does not always return one, and nothing here depends on it."""
        session.add(make_user(auth_provider=AuthProvider.X, email=None))
        session.commit()

        assert session.scalars(select(User)).one().email is None


class TestUserItem:
    def test_records_a_list_entry_with_a_rating_and_note(self, session: Session) -> None:
        user, title = _user_and_title(session)
        session.add(
            UserItem(
                user_id=user.id,
                title_id=title.id,
                status=ItemStatus.WATCHED,
                rating=9,
                note="לצפות שוב",
            )
        )
        session.commit()

        item = session.scalars(select(UserItem)).one()
        assert item.status is ItemStatus.WATCHED
        assert item.rating == 9
        assert item.is_empty is False

    def test_a_rating_needs_no_list(self, session: Session) -> None:
        user, title = _user_and_title(session)
        session.add(UserItem(user_id=user.id, title_id=title.id, rating=7))
        session.commit()

        assert session.scalars(select(UserItem)).one().status is None

    def test_rejects_a_rating_outside_one_to_ten(self, session: Session) -> None:
        user, title = _user_and_title(session)
        session.add(UserItem(user_id=user.id, title_id=title.id, rating=11))

        with pytest.raises(IntegrityError):
            session.commit()

    def test_rejects_an_over_long_note(self, session: Session) -> None:
        """Validation also lives in the schema, so no writer can bypass it."""
        user, title = _user_and_title(session)
        session.add(UserItem(user_id=user.id, title_id=title.id, note="א" * 2001))

        with pytest.raises(IntegrityError):
            session.commit()

    def test_one_row_per_user_and_title(self, session: Session) -> None:
        user, title = _user_and_title(session)
        session.add_all(
            [
                UserItem(user_id=user.id, title_id=title.id, rating=5),
                UserItem(user_id=user.id, title_id=title.id, rating=6),
            ]
        )

        with pytest.raises(IntegrityError):
            session.commit()

    def test_an_untouched_row_reports_itself_empty(self, session: Session) -> None:
        assert UserItem(status=None, rating=None, note="").is_empty is True


class TestUserSession:
    def test_deleting_a_user_takes_sessions_and_items_with_it(self, session: Session) -> None:
        """Account deletion has to be complete, so the cascade is tested, not assumed."""
        user, title = _user_and_title(session)
        session.add_all(
            [
                UserSession(
                    token_hash="a" * 64,
                    user_id=user.id,
                    expires_at=dt.datetime(2030, 1, 1, tzinfo=dt.UTC),
                ),
                UserItem(user_id=user.id, title_id=title.id, status=ItemStatus.WANT_TO_WATCH),
            ]
        )
        session.commit()

        session.delete(user)
        session.commit()

        assert session.scalars(select(UserSession)).all() == []
        assert session.scalars(select(UserItem)).all() == []

    def test_deleting_a_title_removes_it_from_lists(self, session: Session) -> None:
        user, title = _user_and_title(session)
        session.add(UserItem(user_id=user.id, title_id=title.id, rating=8))
        session.commit()

        session.delete(title)
        session.commit()

        assert session.scalars(select(UserItem)).all() == []


def _user_and_title(session: Session) -> tuple[User, Title]:
    user = make_user()
    title = make_title()
    session.add_all([user, title])
    session.flush()
    return user, title
