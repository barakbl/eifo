"""The search index must stay wired to the table it indexes.

The failure these guard against is silent by construction: the index answers
queries whether or not anything is still updating it, so a broken one looks
exactly like a correct one until somebody notices a title they know is in the
catalog cannot be found.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, text

from eifo_core.fts import (
    INDEX,
    TRIGGERS,
    ensure_search_triggers,
    missing_triggers,
    restore_triggers,
)
from eifo_core.migrate import downgrade, upgrade

TITLE = "Waltz with Bashir"
RENAMED = "The Band's Visit"


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    """A migrated database - create_all does not build the index."""
    url = f"sqlite:///{tmp_path / 'search.db'}"
    upgrade(url)
    return url


@pytest.fixture
def engine(db_url: str) -> Iterator[Engine]:
    engine = create_engine(db_url)
    yield engine
    engine.dispose()


def _unwire(engine: Engine) -> None:
    """Leave the database exactly as a rebuild of ``titles`` leaves it.

    Alembic's batch mode copies the table and renames it over the original; the
    triggers are attached to the table that gets dropped, and nothing recreates
    them. This is that end state, reached the short way.
    """
    with engine.begin() as connection:
        for name in TRIGGERS:
            connection.execute(text(f"DROP TRIGGER {name}"))


def _add_title(engine: Engine, name: str) -> int:
    with engine.begin() as connection:
        cursor = connection.execute(
            text(
                "INSERT INTO titles (type, name_en, created_at, updated_at) "
                "VALUES ('movie', :name, '2026-08-24 00:00:00', '2026-08-24 00:00:00')"
            ),
            {"name": name},
        )
        return int(cursor.lastrowid)


def _execute(engine: Engine, statement: str, **parameters: Any) -> None:
    with engine.begin() as connection:
        connection.execute(text(statement), parameters)


def _search(engine: Engine, term: str) -> set[int]:
    with engine.connect() as connection:
        rows = connection.execute(
            text(f"SELECT rowid FROM {INDEX} WHERE {INDEX} MATCH :query"),
            {"query": f'"{term}"'},
        ).all()
    return {int(row[0]) for row in rows}


def _trigger_names(engine: Engine) -> set[str]:
    with engine.connect() as connection:
        return {
            name
            for (name,) in connection.execute(
                text("SELECT name FROM sqlite_master WHERE type = 'trigger'")
            ).all()
        }


class TestMigratedSchema:
    def test_migrations_leave_every_trigger_attached(self, engine: Engine) -> None:
        """The CI guard: a migration that recreates titles fails here."""
        assert _trigger_names(engine) == set(TRIGGERS)

        with engine.connect() as connection:
            assert missing_triggers(connection) == ()

    def test_the_index_covers_titles_that_already_existed(self, engine: Engine) -> None:
        title_id = _add_title(engine, TITLE)

        assert _search(engine, "bashir") == {title_id}


class TestUnwiredIndex:
    def test_a_title_written_without_triggers_never_reaches_the_index(self, engine: Engine) -> None:
        """The fault itself: the write succeeds, and search never hears about it."""
        _unwire(engine)

        _add_title(engine, TITLE)

        assert _search(engine, "bashir") == set()

    def test_every_trigger_is_reported_missing(self, engine: Engine) -> None:
        _unwire(engine)

        with engine.connect() as connection:
            assert set(missing_triggers(connection)) == set(TRIGGERS)

    def test_restoring_reindexes_what_was_written_meanwhile(self, engine: Engine) -> None:
        """Reattaching alone is not enough - the missed writes left no trace."""
        _unwire(engine)
        title_id = _add_title(engine, TITLE)

        restored = ensure_search_triggers(engine)

        assert set(restored) == set(TRIGGERS)
        assert _search(engine, "bashir") == {title_id}

    def test_only_the_missing_trigger_is_restored(self, engine: Engine) -> None:
        _execute(engine, f"DROP TRIGGER {INDEX}_update")

        assert ensure_search_triggers(engine) == (f"{INDEX}_update",)
        assert _trigger_names(engine) == set(TRIGGERS)

    def test_a_restored_index_follows_later_writes(self, engine: Engine) -> None:
        _unwire(engine)
        ensure_search_triggers(engine)

        title_id = _add_title(engine, TITLE)
        assert _search(engine, "bashir") == {title_id}

        _execute(
            engine, "UPDATE titles SET name_en = :name WHERE id = :id", name=RENAMED, id=title_id
        )
        assert _search(engine, "bashir") == set()
        assert _search(engine, "band") == {title_id}

        _execute(engine, "DELETE FROM titles WHERE id = :id", id=title_id)
        assert _search(engine, "band") == set()

    def test_restoring_reports_what_it_did(self, engine: Engine, caplog: Any) -> None:
        _unwire(engine)

        with caplog.at_level(logging.WARNING, logger="eifo_core.fts"):
            ensure_search_triggers(engine)

        assert "search index was not being updated" in caplog.text


class TestNothingToRepair:
    def test_a_healthy_index_is_left_alone(self, engine: Engine) -> None:
        """Every start calls this; a no-op must stay a no-op."""
        assert ensure_search_triggers(engine) == ()

    def test_a_database_without_an_index_is_left_alone(self, engine: Engine) -> None:
        """Building the index is 0004's job, not a repair's."""
        _unwire(engine)
        _execute(engine, f"DROP TABLE {INDEX}")

        assert ensure_search_triggers(engine) == ()

    def test_an_index_of_another_shape_is_left_alone(self, engine: Engine, caplog: Any) -> None:
        """A trigger writes the columns it was compiled with; guessing would break inserts."""
        _unwire(engine)
        _execute(engine, f"DROP TABLE {INDEX}")
        _execute(
            engine,
            f"CREATE VIRTUAL TABLE {INDEX} USING fts5"
            "(name_en, content='titles', content_rowid='id')",
        )

        with caplog.at_level(logging.WARNING, logger="eifo_core.fts"):
            assert ensure_search_triggers(engine) == ()

        assert "leaving its triggers alone" in caplog.text
        assert _trigger_names(engine) == set()

    def test_a_non_sqlite_database_has_no_index_to_wire_up(self) -> None:
        """PostgreSQL has no FTS5 table; the services still call this on the way up."""
        elsewhere = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

        assert missing_triggers(elsewhere) == ()  # type: ignore[arg-type]
        assert restore_triggers(elsewhere) == ()  # type: ignore[arg-type]


class TestRepairMigration:
    def test_upgrading_restores_triggers_a_rebuild_removed(
        self, db_url: str, engine: Engine
    ) -> None:
        """The deployed catalog's exact state: at head, index present, nothing updating it."""
        downgrade(db_url, "0007_credits")
        _unwire(engine)
        title_id = _add_title(engine, TITLE)
        assert _search(engine, "bashir") == set()

        upgrade(db_url)

        assert _trigger_names(engine) == set(TRIGGERS)
        assert _search(engine, "bashir") == {title_id}

    def test_a_database_that_never_lost_them_is_untouched(
        self, db_url: str, engine: Engine
    ) -> None:
        downgrade(db_url, "0007_credits")
        title_id = _add_title(engine, TITLE)

        upgrade(db_url)

        assert _trigger_names(engine) == set(TRIGGERS)
        assert _search(engine, "bashir") == {title_id}
