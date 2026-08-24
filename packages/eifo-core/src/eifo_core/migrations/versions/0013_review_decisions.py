"""Let a review say what was decided, not only that something was.

A resolved review recorded a title id or nothing, and nothing had to mean "not
the candidate we suggested, so make this a title of its own". There was no way
to say "this is a trailer" - so the only available answer to a sing-along was to
put it in the catalog.

Rulings already made are read the way they were meant at the time: with a title,
attached; without one, created.

Revision ID: 0013_review_decisions
Revises: 0012_tmdb_aliases
Create Date: 2026-08-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013_review_decisions"
down_revision: str | None = "0012_tmdb_aliases"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BACKFILL = """
UPDATE match_reviews
SET decision = CASE WHEN resolved_title_id IS NULL THEN 'created' ELSE 'attached' END
WHERE resolved_at IS NOT NULL
"""


def upgrade() -> None:
    with op.batch_alter_table("match_reviews", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "decision",
                sa.Enum(
                    "attached",
                    "created",
                    "dismissed",
                    name="match_decision",
                    native_enum=False,
                    length=32,
                ),
                nullable=True,
            )
        )
    op.execute(BACKFILL)


def downgrade() -> None:
    with op.batch_alter_table("match_reviews", schema=None) as batch_op:
        batch_op.drop_column("decision")
