"""Engine configuration, UTC handling and readiness checks."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from factories import make_title
from sqlalchemy import Engine, select, text
from sqlalchemy.exc import StatementError
from sqlalchemy.orm import Session

from tvil_core.db import (
    DatabaseNotReadyError,
    create_engine_from_settings,
    make_session_factory,
    require_schema,
    schema_exists,
    session_scope,
)
from tvil_core.models import Base, Title
from tvil_core.settings import Settings


def test_sqlite_uses_wal_and_enforces_foreign_keys(engine: Engine) -> None:
    with engine.connect() as connection:
        journal_mode = connection.execute(text("PRAGMA journal_mode")).scalar_one()
        foreign_keys = connection.execute(text("PRAGMA foreign_keys")).scalar_one()

    assert journal_mode == "wal"
    assert foreign_keys == 1


def test_creates_the_database_directory(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, db_url=f"sqlite:///{tmp_path / 'nested' / 'dir' / 'a.db'}")

    engine = create_engine_from_settings(settings)
    try:
        Base.metadata.create_all(engine)
    finally:
        engine.dispose()

    assert (tmp_path / "nested" / "dir" / "a.db").exists()


def test_in_memory_database_needs_no_directory() -> None:
    engine = create_engine_from_settings(Settings(_env_file=None, db_url="sqlite:///:memory:"))
    try:
        Base.metadata.create_all(engine)
        assert schema_exists(engine)
    finally:
        engine.dispose()


class TestUtcDateTime:
    def test_round_trips_as_utc_aware(self, session: Session) -> None:
        moment = dt.datetime(2026, 8, 7, 12, 30, tzinfo=dt.UTC)
        session.add(make_title(created_at=moment))
        session.commit()
        session.expunge_all()

        stored = session.scalars(select(Title)).one().created_at
        assert stored == moment
        assert stored.tzinfo is not None

    def test_converts_other_zones_to_utc(self, session: Session) -> None:
        jerusalem = dt.timezone(dt.timedelta(hours=3))
        session.add(make_title(created_at=dt.datetime(2026, 8, 7, 15, 0, tzinfo=jerusalem)))
        session.commit()
        session.expunge_all()

        assert session.scalars(select(Title)).one().created_at == dt.datetime(
            2026, 8, 7, 12, 0, tzinfo=dt.UTC
        )

    def test_rejects_naive_datetimes(self, session: Session) -> None:
        session.add(make_title(created_at=dt.datetime(2026, 8, 7, 12, 0)))

        with pytest.raises(StatementError):
            session.commit()


class TestSessionScope:
    def test_commits_on_success(self, engine: Engine) -> None:
        factory = make_session_factory(engine)

        with session_scope(factory) as session:
            session.add(make_title())

        with factory() as session:
            assert len(session.scalars(select(Title)).all()) == 1

    def test_rolls_back_on_error(self, engine: Engine) -> None:
        factory = make_session_factory(engine)

        with pytest.raises(RuntimeError), session_scope(factory) as session:
            session.add(make_title())
            session.flush()
            raise RuntimeError("boom")

        with factory() as session:
            assert session.scalars(select(Title)).all() == []


class TestSchemaReadiness:
    def test_reports_a_migrated_database_as_ready(self, engine: Engine) -> None:
        assert schema_exists(engine) is True
        require_schema(engine, "sqlite://")

    def test_unmigrated_database_fails_with_an_actionable_message(self, tmp_path: Path) -> None:
        db_url = f"sqlite:///{tmp_path / 'empty.db'}"
        engine = create_engine_from_settings(Settings(_env_file=None, db_url=db_url))
        try:
            assert schema_exists(engine) is False
            with pytest.raises(DatabaseNotReadyError, match="tvil-fetch db upgrade"):
                require_schema(engine, db_url)
        finally:
            engine.dispose()
