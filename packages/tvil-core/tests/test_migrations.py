"""Migrations must produce exactly the schema the models describe."""

from __future__ import annotations

from pathlib import Path

from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, create_engine, inspect

from tvil_core.migrate import (
    alembic_config,
    current_revision,
    downgrade,
    include_object,
    upgrade,
)
from tvil_core.models import Base

EXPECTED_TABLES = {
    "availability",
    "fetch_runs",
    "genres",
    "match_reviews",
    "sources",
    "title_genres",
    "titles",
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
    create_engine_from_settings, so they must ensure the directory themselves —
    otherwise the very first documented command fails with nothing more helpful
    than "unable to open database file".
    """
    nested = tmp_path / "data" / "nested"
    assert not nested.exists()

    upgrade(f"sqlite:///{nested / 'tvil.db'}")

    assert (nested / "tvil.db").exists()
