"""The full-text indexes, and the guard that keeps them wired up.

Each index is external-content: it stores no copy of the rows, and SQLite keeps
it in step with its table through three triggers. Those triggers are ordinary
schema objects, so anything that rebuilds the table takes them with it.
Alembic's batch mode does exactly that whenever it cannot alter a column in
place - dropping a column recreates the table, and the triggers do not survive
the copy.

Nothing complains when they go. The index keeps answering, with the catalog it
had at the moment the triggers died: new rows unsearchable, renamed ones still
matching their old text, deleted ones lingering as results that lead nowhere.
That is why the definitions live here rather than only inside the migrations
that built them - a repair needs the same DDL the creation used, and a second
copy would be a second thing to keep in step.

There are two indexes now, and everything here works over both, so the second
cannot quietly acquire the fault the first spent a while having.

SQLite only, like the migrations that build them; on any other dialect every
function here is a no-op and the API falls back to a LIKE scan.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from sqlalchemy import Connection, Engine, text

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SearchIndex:
    """One FTS5 index over one table, and the triggers that keep it honest."""

    #: The virtual table's name.
    name: str
    #: The table it indexes.
    source: str
    #: The columns it covers, in the order the triggers write them.
    columns: tuple[str, ...]

    @property
    def triggers(self) -> Mapping[str, str]:
        """Trigger name to the statement that creates it.

        An external-content table needs the old values written back as a
        'delete' row; that is how FTS5 is told a document has left the index.
        """
        listed = ", ".join(self.columns)
        new = ", ".join(f"new.{column}" for column in self.columns)
        old = ", ".join(f"old.{column}" for column in self.columns)
        return MappingProxyType(
            {
                f"{self.name}_insert": f"""
                    CREATE TRIGGER {self.name}_insert AFTER INSERT ON {self.source} BEGIN
                        INSERT INTO {self.name}(rowid, {listed})
                        VALUES (new.id, {new});
                    END
                """,
                f"{self.name}_delete": f"""
                    CREATE TRIGGER {self.name}_delete AFTER DELETE ON {self.source} BEGIN
                        INSERT INTO {self.name}({self.name}, rowid, {listed})
                        VALUES ('delete', old.id, {old});
                    END
                """,
                f"{self.name}_update": f"""
                    CREATE TRIGGER {self.name}_update AFTER UPDATE ON {self.source} BEGIN
                        INSERT INTO {self.name}({self.name}, rowid, {listed})
                        VALUES ('delete', old.id, {old});
                        INSERT INTO {self.name}(rowid, {listed})
                        VALUES (new.id, {new});
                    END
                """,
            }
        )

    @property
    def rebuild(self) -> str:
        """Reindex every stored row.

        Triggers only cover writes made while they existed, so a repair is only
        half a repair without this.
        """
        return f"INSERT INTO {self.name}({self.name}) VALUES ('rebuild')"

    @property
    def shadow_tables(self) -> frozenset[str]:
        """The tables SQLite builds around the virtual one."""
        return frozenset(
            {self.name, *(f"{self.name}_{part}" for part in ("data", "idx", "docsize", "config"))}
        )


#: Titles, searched by name and overview in both languages (migration 0004).
TITLES = SearchIndex(
    name="titles_fts",
    source="titles",
    columns=("name_en", "name_he", "overview_en", "overview_he"),
)

#: People, searched by name (migration 0014). A person has one name and it is
#: in one language or the other, so both columns are indexed and nearly every
#: row fills exactly one of them.
PEOPLE = SearchIndex(name="people_fts", source="people", columns=("name_en", "name_he"))

INDEXES: tuple[SearchIndex, ...] = (TITLES, PEOPLE)


def _is_sqlite(connection: Connection) -> bool:
    return connection.dialect.name == "sqlite"


def _exists(connection: Connection, table: str) -> bool:
    found = connection.execute(
        text("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = :name"),
        {"name": table},
    ).scalar()
    return found is not None


def _index_columns(connection: Connection, index: SearchIndex) -> tuple[str, ...]:
    rows = connection.execute(text(f"PRAGMA table_info({index.name})")).all()
    return tuple(row[1] for row in rows)


def missing_triggers(connection: Connection) -> tuple[str, ...]:
    """Which triggers are absent, across every index, in creation order.

    Empty on a healthy database, and empty on any database that has no index to
    keep in step - a non-SQLite dialect, or one migrated to before 0004.
    """
    if not _is_sqlite(connection):
        return ()

    present = {
        name
        for (name,) in connection.execute(
            text("SELECT name FROM sqlite_master WHERE type = 'trigger'")
        ).all()
    }
    return tuple(
        name
        for index in INDEXES
        if _exists(connection, index.name)
        for name in index.triggers
        if name not in present
    )


def restore_triggers(connection: Connection) -> tuple[str, ...]:
    """Re-attach any missing trigger and reindex, on an open connection.

    Reindexing is not optional: writes made while a trigger was gone left no
    trace in the index, and there is no record of how many there were.

    Returns:
        The triggers that were restored, empty if there was nothing to do.
    """
    if not _is_sqlite(connection):
        return ()

    restored: list[str] = []
    for index in INDEXES:
        restored.extend(_restore_one(connection, index))
    return tuple(restored)


def _restore_one(connection: Connection, index: SearchIndex) -> tuple[str, ...]:
    if not _exists(connection, index.name):
        return ()

    triggers = index.triggers
    present = {
        name
        for (name,) in connection.execute(
            text("SELECT name FROM sqlite_master WHERE type = 'trigger'")
        ).all()
    }
    missing = tuple(name for name in triggers if name not in present)
    if not missing:
        return ()

    # A trigger writes the column list it was compiled with. If the index in
    # front of us has a different shape, this build's DDL is not its DDL, and
    # guessing would wire up something that fails on the next insert.
    columns = _index_columns(connection, index)
    if columns != index.columns:
        logger.warning(
            "%s indexes %s, not %s; leaving its triggers alone",
            index.name,
            ", ".join(columns) or "nothing",
            ", ".join(index.columns),
        )
        return ()

    for name in missing:
        connection.execute(text(triggers[name]))
    connection.execute(text(index.rebuild))
    return missing


def ensure_search_triggers(engine: Engine) -> tuple[str, ...]:
    """Repair the indexes' triggers if a table rebuild has removed them.

    Cheap enough to call on every start: one lookup against ``sqlite_master``
    when there is nothing wrong, which is the usual case.

    Returns:
        The triggers that were restored, empty if there was nothing to do.
    """
    with engine.begin() as connection:
        restored = restore_triggers(connection)

    if restored:
        logger.warning(
            "search index was not being updated: restored %s and reindexed",
            ", ".join(restored),
        )
    return restored
