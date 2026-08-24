"""Remember which TMDB ids are second records of a title already held.

TMDB carries the same work twice more often than one would like, and the
availability feed offers both ids every night. Merging the two titles is
therefore not enough on its own: the next sync sees an id no title owns and
faithfully recreates what was just merged.

Revision ID: 0012_tmdb_aliases
Revises: 0011_placeholder_years
Create Date: 2026-08-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# Always available: autogenerate emits eifo_core.types.UtcDateTime() for every
# timestamp column but does not add the import itself.
import eifo_core.types

revision: str = "0012_tmdb_aliases"
down_revision: str | None = "0011_placeholder_years"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tmdb_aliases",
        sa.Column("tmdb_id", sa.Integer(), nullable=False),
        sa.Column("title_id", sa.Integer(), nullable=False),
        sa.Column("created_at", eifo_core.types.UtcDateTime(), nullable=False),
        sa.ForeignKeyConstraint(["title_id"], ["titles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("tmdb_id"),
    )
    with op.batch_alter_table("tmdb_aliases", schema=None) as batch_op:
        batch_op.create_index("ix_tmdb_aliases_title_id", ["title_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("tmdb_aliases", schema=None) as batch_op:
        batch_op.drop_index("ix_tmdb_aliases_title_id")

    op.drop_table("tmdb_aliases")
