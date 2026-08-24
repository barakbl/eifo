"""Re-attach the search triggers a table rebuild removed.

The FTS5 index over ``titles`` is kept in step by three triggers (0004). They
are ordinary schema objects, so recreating the table drops them - which is what
SQLite does under the covers whenever Alembic's batch mode cannot alter a column
in place, and what a downgrade past 0004 followed by a re-stamp leaves behind.
A database can therefore sit at head with the index in place and nothing
updating it, which is how the deployed catalog was found: index present, all
three triggers gone, search frozen at whatever the catalog looked like when they
died.

This restores whatever is missing and reindexes, because writes made in the
meantime left no trace in the index. On a database that never lost them - every
fresh install - it finds nothing to do.

There is no downgrade: 0004 owns these triggers, and removing them again would
only recreate the fault this repairs.

Revision ID: 0008_fts_triggers
Revises: 0007_credits
Create Date: 2026-08-24
"""

from collections.abc import Sequence

from alembic import op

from eifo_core.fts import restore_triggers

revision: str = "0008_fts_triggers"
down_revision: str | None = "0007_credits"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    restore_triggers(op.get_bind())


def downgrade() -> None:
    """Nothing to undo: this migration only ever puts back what 0004 created."""
