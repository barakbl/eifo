"""Full-text search over people's names.

Thirty thousand people are in the catalog and none of them can be searched for.
The only way to reach a person's page is to already be on a title they worked
on and click their name - so "what else has she been in" is answerable and "find
her" is not.

Prefix indexing is configured here and not on titles, because a name is what
somebody types a few letters of and waits: "gal g" has to find Gal Gadot before
the rest of it is typed.

SQLite only, like 0004; on any other dialect this is a no-op.

Revision ID: 0014_people_fts
Revises: 0013_review_decisions
Create Date: 2026-08-25
"""

from collections.abc import Sequence

from alembic import op

from eifo_core.fts import PEOPLE

revision: str = "0014_people_fts"
down_revision: str | None = "0013_review_decisions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CREATE_TABLE = f"""
CREATE VIRTUAL TABLE {PEOPLE.name} USING fts5(
    {", ".join(PEOPLE.columns)},
    content='{PEOPLE.source}',
    content_rowid='id',
    tokenize="unicode61 remove_diacritics 2",
    prefix='2 3'
)
"""


def _is_sqlite() -> bool:
    return op.get_bind().dialect.name == "sqlite"


def upgrade() -> None:
    if not _is_sqlite():
        return

    op.execute(CREATE_TABLE)
    for statement in PEOPLE.triggers.values():
        op.execute(statement)
    # Index whatever is already stored; triggers only cover future writes.
    op.execute(PEOPLE.rebuild)


def downgrade() -> None:
    if not _is_sqlite():
        return

    for name in reversed(list(PEOPLE.triggers)):
        op.execute(f"DROP TRIGGER IF EXISTS {name}")
    op.execute(f"DROP TABLE IF EXISTS {PEOPLE.name}")
