"""The full-text index over titles, and the guard that keeps it wired up.

The FTS5 index is external-content: it stores no copy of the rows, and SQLite
keeps it in step with ``titles`` through three triggers. Those triggers are
ordinary schema objects, so anything that rebuilds the table takes them with it.
Alembic's batch mode does exactly that whenever it cannot alter a column in
place - dropping a column recreates the table, and the triggers do not survive
the copy.

Nothing complains when they go. The index keeps answering, with the catalog it
had at the moment the triggers died: new titles are unsearchable, renamed ones
keep matching their old text, and deleted ones linger as results that lead
nowhere. That is why the definition lives here rather than only inside the
migration that first created it - a repair needs the same DDL the creation used,
and a second copy would be a second thing to keep in step.

SQLite only, like the migration that builds the index; on any other dialect
every function here is a no-op and the API falls back to a LIKE scan.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from types import MappingProxyType

from sqlalchemy import Connection, Engine, text

logger = logging.getLogger(__name__)

#: The FTS5 index, and the columns it covers in the order the triggers write
#: them. Must match the virtual table migration 0004 creates.
INDEX = "titles_fts"
COLUMNS = ("name_en", "name_he", "overview_en", "overview_he")

_COLUMN_LIST = ", ".join(COLUMNS)
_NEW_VALUES = ", ".join(f"new.{column}" for column in COLUMNS)
_OLD_VALUES = ", ".join(f"old.{column}" for column in COLUMNS)

#: Trigger name to the statement that creates it. An external-content table
#: needs the old values written back as a 'delete' row; that is how FTS5 is told
#: a document has left the index.
TRIGGERS: Mapping[str, str] = MappingProxyType(
    {
        f"{INDEX}_insert": f"""
            CREATE TRIGGER {INDEX}_insert AFTER INSERT ON titles BEGIN
                INSERT INTO {INDEX}(rowid, {_COLUMN_LIST})
                VALUES (new.id, {_NEW_VALUES});
            END
        """,
        f"{INDEX}_delete": f"""
            CREATE TRIGGER {INDEX}_delete AFTER DELETE ON titles BEGIN
                INSERT INTO {INDEX}({INDEX}, rowid, {_COLUMN_LIST})
                VALUES ('delete', old.id, {_OLD_VALUES});
            END
        """,
        f"{INDEX}_update": f"""
            CREATE TRIGGER {INDEX}_update AFTER UPDATE ON titles BEGIN
                INSERT INTO {INDEX}({INDEX}, rowid, {_COLUMN_LIST})
                VALUES ('delete', old.id, {_OLD_VALUES});
                INSERT INTO {INDEX}(rowid, {_COLUMN_LIST})
                VALUES (new.id, {_NEW_VALUES});
            END
        """,
    }
)

#: Reindex every stored row. Triggers only cover writes made while they existed,
#: so a repair is only half a repair without this.
REBUILD = f"INSERT INTO {INDEX}({INDEX}) VALUES ('rebuild')"


def _is_sqlite(connection: Connection) -> bool:
    return connection.dialect.name == "sqlite"


def _index_exists(connection: Connection) -> bool:
    found = connection.execute(
        text("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = :name"),
        {"name": INDEX},
    ).scalar()
    return found is not None


def _index_columns(connection: Connection) -> tuple[str, ...]:
    rows = connection.execute(text(f"PRAGMA table_info({INDEX})")).all()
    return tuple(row[1] for row in rows)


def missing_triggers(connection: Connection) -> tuple[str, ...]:
    """Which of the index's triggers are absent, in creation order.

    Empty on a healthy database, and empty on any database that has no index to
    keep in step - a non-SQLite dialect, or one migrated to before 0004.
    """
    if not _is_sqlite(connection) or not _index_exists(connection):
        return ()

    present = {
        name
        for (name,) in connection.execute(
            text("SELECT name FROM sqlite_master WHERE type = 'trigger'")
        ).all()
    }
    return tuple(name for name in TRIGGERS if name not in present)


def restore_triggers(connection: Connection) -> tuple[str, ...]:
    """Re-attach any missing trigger and reindex, on an open connection.

    Reindexing is not optional: writes made while a trigger was gone left no
    trace in the index, and there is no record of how many there were.

    Returns:
        The triggers that were restored, empty if there was nothing to do.
    """
    missing = missing_triggers(connection)
    if not missing:
        return ()

    # A trigger writes the column list it was compiled with. If the index in
    # front of us has a different shape, this build's DDL is not its DDL, and
    # guessing would wire up something that fails on the next insert.
    columns = _index_columns(connection)
    if columns != COLUMNS:
        logger.warning(
            "%s indexes %s, not %s; leaving its triggers alone",
            INDEX,
            ", ".join(columns) or "nothing",
            ", ".join(COLUMNS),
        )
        return ()

    for name in missing:
        connection.execute(text(TRIGGERS[name]))
    connection.execute(text(REBUILD))
    return missing


def ensure_search_triggers(engine: Engine) -> tuple[str, ...]:
    """Repair the index's triggers if a table rebuild has removed them.

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
