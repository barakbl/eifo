"""Migrations must produce exactly the schema the models describe."""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, create_engine, inspect, text

from eifo_core.migrate import (
    alembic_config,
    current_revision,
    downgrade,
    ensure_current,
    head_revision,
    include_object,
    is_current,
    upgrade,
)
from eifo_core.models import Base

EXPECTED_TABLES = {
    "availability",
    "enrich_attempts",
    "fetch_runs",
    "genres",
    "match_reviews",
    "sessions",
    "sources",
    "title_genres",
    "titles",
    "user_items",
    "users",
}


def _migrated_engine(tmp_path: Path) -> Engine:
    db_url = f"sqlite:///{tmp_path / 'migrated.db'}"
    upgrade(db_url)
    return create_engine(db_url)


def test_upgrade_creates_every_table(tmp_path: Path) -> None:
    engine = _migrated_engine(tmp_path)
    try:
        assert set(inspect(engine).get_table_names()) >= EXPECTED_TABLES
    finally:
        engine.dispose()


def test_upgrade_stamps_the_head_revision(tmp_path: Path) -> None:
    """Compared against the actual head so adding a migration cannot break this."""
    head = ScriptDirectory.from_config(alembic_config("sqlite://")).get_current_head()

    engine = _migrated_engine(tmp_path)
    try:
        assert current_revision(engine) == head
    finally:
        engine.dispose()


def test_unmigrated_database_has_no_revision(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'blank.db'}")
    try:
        assert current_revision(engine) is None
    finally:
        engine.dispose()


def test_migrated_schema_matches_the_models(tmp_path: Path) -> None:
    """The drift guard: a model change without a migration fails here."""
    engine = _migrated_engine(tmp_path)
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(
                connection,
                opts={
                    "compare_type": True,
                    "render_as_batch": True,
                    # Search infrastructure is built by a migration rather than
                    # declared as a model; excluded here exactly as in env.py.
                    "include_object": include_object,
                },
            )
            diff = compare_metadata(context, Base.metadata)
    finally:
        engine.dispose()

    assert diff == [], f"models and migrations have diverged: {diff}"


class TestEnrichAttemptBackfill:
    """0009 has to take existing ratings as evidence of a past attempt.

    Without that, every already-rated title would look never attempted, and
    the first run after the upgrade would re-read the whole catalog instead of
    the titles that have genuinely never been tried.
    """

    def _seed(self, db_url: str) -> Engine:
        upgrade(db_url, "0008_fts_triggers")
        engine = create_engine(db_url)
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO titles (id, type, name_en, created_at, updated_at) VALUES "
                    "(1, 'movie', 'Rated', '2026-08-01 00:00:00', '2026-08-01 00:00:00'), "
                    "(2, 'movie', 'Unrated', '2026-08-01 00:00:00', '2026-08-01 00:00:00')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO external_ratings "
                    "(title_id, provider, score_raw, score_normalized, fetched_at) VALUES "
                    "(1, 'imdb', 8.4, 84, '2026-08-10 12:00:00'), "
                    "(1, 'tmdb', 7.9, 79, '2026-08-12 12:00:00')"
                )
            )
        return engine

    def test_a_rated_title_is_dated_by_its_freshest_rating(self, tmp_path: Path) -> None:
        db_url = f"sqlite:///{tmp_path / 'backfill.db'}"
        engine = self._seed(db_url)
        try:
            upgrade(db_url)

            with engine.connect() as connection:
                row = connection.execute(
                    text(
                        "SELECT attempted_at, outcome, fruitless, due_at "
                        "FROM enrich_attempts WHERE title_id = 1"
                    )
                ).one()
            attempted_at, outcome, fruitless, due_at = row
            assert str(attempted_at).startswith("2026-08-12")
            assert outcome == "ok"
            assert fruitless == 0
            # Immediately eligible: never-attempted titles still sort ahead of it.
            assert due_at == attempted_at
        finally:
            engine.dispose()

    def test_a_title_that_was_never_rated_gets_no_row(self, tmp_path: Path) -> None:
        """It has to stay 'never attempted' - that is what puts it at the front."""
        db_url = f"sqlite:///{tmp_path / 'backfill-unrated.db'}"
        engine = self._seed(db_url)
        try:
            upgrade(db_url)

            with engine.connect() as connection:
                found = connection.execute(
                    text("SELECT count(*) FROM enrich_attempts WHERE title_id = 2")
                ).scalar()
            assert found == 0
        finally:
            engine.dispose()


class TestPlaceholderYears:
    """0 means "we do not know" and 2999 means "not scheduled"; neither is a year."""

    def _years(self, tmp_path: Path, seeded: str) -> list[int | None]:
        db_url = f"sqlite:///{tmp_path / 'years.db'}"
        upgrade(db_url, "0010_run_statuses")
        engine = create_engine(db_url)
        try:
            with engine.begin() as connection:
                connection.execute(text(seeded))
            upgrade(db_url)
            with engine.connect() as connection:
                return [
                    row[0]
                    for row in connection.execute(text("SELECT year FROM titles ORDER BY id")).all()
                ]
        finally:
            engine.dispose()

    def test_placeholders_are_cleared_and_real_years_kept(self, tmp_path: Path) -> None:
        years = self._years(
            tmp_path,
            "INSERT INTO titles (id, type, name_he, year, created_at, updated_at) VALUES "
            "(1, 'movie', 'אפס', 0, '2026-08-01 00:00:00', '2026-08-01 00:00:00'), "
            "(2, 'movie', 'עתיד', 2999, '2026-08-01 00:00:00', '2026-08-01 00:00:00'), "
            "(3, 'movie', 'אמיתי', 2015, '2026-08-01 00:00:00', '2026-08-01 00:00:00'), "
            "(4, 'movie', 'ישן', 1927, '2026-08-01 00:00:00', '2026-08-01 00:00:00'), "
            "(5, 'movie', 'ללא', NULL, '2026-08-01 00:00:00', '2026-08-01 00:00:00')",
        )

        assert years == [None, None, 2015, 1927, None]


def test_downgrade_removes_the_schema(tmp_path: Path) -> None:
    db_url = f"sqlite:///{tmp_path / 'reversible.db'}"
    upgrade(db_url)

    downgrade(db_url, "base")

    engine = create_engine(db_url)
    try:
        remaining = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
    assert remaining & EXPECTED_TABLES == set()


def test_migrating_creates_a_missing_database_directory(tmp_path: Path) -> None:
    """A fresh install has no data/ yet, and SQLite will not create one.

    Migrations build their own engine rather than going through
    create_engine_from_settings, so they must ensure the directory themselves -
    otherwise the very first documented command fails with nothing more helpful
    than "unable to open database file".
    """
    nested = tmp_path / "data" / "nested"
    assert not nested.exists()

    upgrade(f"sqlite:///{nested / 'eifo.db'}")

    assert (nested / "eifo.db").exists()


def test_ensure_current_migrates_an_empty_database(tmp_path: Path) -> None:
    """A first start is just the widest possible upgrade."""
    db_url = f"sqlite:///{tmp_path / 'fresh.db'}"
    engine = create_engine(db_url)
    try:
        assert is_current(engine) is False

        applied = ensure_current(engine, db_url)

        assert applied == head_revision()
        assert is_current(engine) is True
        assert set(inspect(engine).get_table_names()) >= EXPECTED_TABLES
    finally:
        engine.dispose()


def test_ensure_current_catches_a_database_left_behind(tmp_path: Path) -> None:
    """The case that matters: a deploy adds a migration to a live database."""
    db_url = f"sqlite:///{tmp_path / 'behind.db'}"
    upgrade(db_url)
    engine = create_engine(db_url)
    try:
        one_back = (
            ScriptDirectory.from_config(alembic_config(db_url)).get_revision("head").down_revision
        )
        assert one_back is not None
        downgrade(db_url, one_back)
        assert is_current(engine) is False

        assert ensure_current(engine, db_url) == head_revision()
        assert is_current(engine) is True
    finally:
        engine.dispose()


def test_ensure_current_does_nothing_when_already_at_head(tmp_path: Path) -> None:
    """Every restart calls this; a no-op must stay a no-op."""
    engine = _migrated_engine(tmp_path)
    try:
        assert ensure_current(engine, str(engine.url)) is None
    finally:
        engine.dispose()


def test_a_concurrent_migration_is_not_a_failure(tmp_path: Path, monkeypatch) -> None:
    """Two services starting together: the loser finds the work already done."""
    db_url = f"sqlite:///{tmp_path / 'race.db'}"
    engine = create_engine(db_url)
    try:

        def upgrade_as_if_another_process_won(url: str, revision: str = "head") -> None:
            upgrade(url, revision)  # the winner's work
            raise RuntimeError("database is locked")

        monkeypatch.setattr("eifo_core.migrate.upgrade", upgrade_as_if_another_process_won)

        assert ensure_current(engine, db_url) == head_revision()
    finally:
        engine.dispose()


def test_a_migration_that_really_fails_still_raises(tmp_path: Path, monkeypatch) -> None:
    db_url = f"sqlite:///{tmp_path / 'broken.db'}"
    engine = create_engine(db_url)
    try:

        def explode(url: str, revision: str = "head") -> None:
            raise RuntimeError("no such column")

        monkeypatch.setattr("eifo_core.migrate.upgrade", explode)

        with pytest.raises(RuntimeError, match="no such column"):
            ensure_current(engine, db_url)
    finally:
        engine.dispose()
