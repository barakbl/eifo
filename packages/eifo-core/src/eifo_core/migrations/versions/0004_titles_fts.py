"""Full-text search over title names and overviews.

An FTS5 external-content index over ``titles``, kept in sync by triggers. The
``unicode61`` tokenizer with diacritic folding handles Hebrew and English in one
index, so a single query matches either language.

SQLite only. The rest of the schema stays PostgreSQL-compatible, but full-text
search is inherently engine-specific; on any other dialect this is a no-op and
the API falls back to a LIKE scan.

Revision ID: 0004_titles_fts
Revises: 0003_ratings
Create Date: 2026-08-07
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004_titles_fts"
down_revision: str | None = "0003_ratings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Columns the index covers, in the order the triggers write them.
COLUMNS = ("name_en", "name_he", "overview_en", "overview_he")

_COLUMN_LIST = ", ".join(COLUMNS)
_NEW_VALUES = ", ".join(f"new.{column}" for column in COLUMNS)
_OLD_VALUES = ", ".join(f"old.{column}" for column in COLUMNS)

CREATE_TABLE = f"""
CREATE VIRTUAL TABLE titles_fts USING fts5(
    {_COLUMN_LIST},
    content='titles',
    content_rowid='id',
    tokenize="unicode61 remove_diacritics 2"
)
"""

# External-content tables need explicit 'delete' rows; writing the old values
# back is how FTS5 removes a document from the index.
TRIGGERS = (
    f"""
    CREATE TRIGGER titles_fts_insert AFTER INSERT ON titles BEGIN
        INSERT INTO titles_fts(rowid, {_COLUMN_LIST})
        VALUES (new.id, {_NEW_VALUES});
    END
    """,
    f"""
    CREATE TRIGGER titles_fts_delete AFTER DELETE ON titles BEGIN
        INSERT INTO titles_fts(titles_fts, rowid, {_COLUMN_LIST})
        VALUES ('delete', old.id, {_OLD_VALUES});
    END
    """,
    f"""
    CREATE TRIGGER titles_fts_update AFTER UPDATE ON titles BEGIN
        INSERT INTO titles_fts(titles_fts, rowid, {_COLUMN_LIST})
        VALUES ('delete', old.id, {_OLD_VALUES});
        INSERT INTO titles_fts(rowid, {_COLUMN_LIST})
        VALUES (new.id, {_NEW_VALUES});
    END
    """,
)

DROP = (
    "DROP TRIGGER IF EXISTS titles_fts_update",
    "DROP TRIGGER IF EXISTS titles_fts_delete",
    "DROP TRIGGER IF EXISTS titles_fts_insert",
    "DROP TABLE IF EXISTS titles_fts",
)


def _is_sqlite() -> bool:
    return op.get_bind().dialect.name == "sqlite"


def upgrade() -> None:
    if not _is_sqlite():
        return

    op.execute(CREATE_TABLE)
    for trigger in TRIGGERS:
        op.execute(trigger)
    # Index whatever is already stored; triggers only cover future writes.
    op.execute("INSERT INTO titles_fts(titles_fts) VALUES ('rebuild')")


def downgrade() -> None:
    if not _is_sqlite():
        return

    for statement in DROP:
        op.execute(statement)
