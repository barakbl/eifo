"""Remember that a title was put through the enrichers, not just that it scored.

Enrichment worked out what was due from the ratings it had already written, so
a title no provider carries was due every night for ever, and the lowest-numbered
of those filled every batch. Ten consecutive runs on the deployed catalog read
500 titles and wrote nothing, while thirteen thousand others were never reached.

Existing ratings are backfilled as successful attempts, dated when they were
fetched and immediately eligible: the ordering is least-recently-attempted
first, so the titles that have never been tried at all go first, and the rated
ones return to the refresh schedule as the backlog drains.

Revision ID: 0009_enrich_attempts
Revises: 0008_fts_triggers
Create Date: 2026-08-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# Always available: autogenerate emits eifo_core.types.UtcDateTime() for every
# timestamp column but does not add the import itself.
import eifo_core.types

revision: str = "0009_enrich_attempts"
down_revision: str | None = "0008_fts_triggers"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BACKFILL = """
INSERT INTO enrich_attempts (title_id, attempted_at, outcome, fruitless, due_at)
SELECT title_id, MAX(fetched_at), 'ok', 0, MAX(fetched_at)
FROM external_ratings
GROUP BY title_id
"""


def upgrade() -> None:
    op.create_table(
        "enrich_attempts",
        sa.Column("title_id", sa.Integer(), nullable=False),
        sa.Column("attempted_at", eifo_core.types.UtcDateTime(), nullable=False),
        sa.Column(
            "outcome",
            sa.Enum(
                "ok",
                "no_data",
                "no_match",
                "error",
                name="enrich_outcome",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("fruitless", sa.Integer(), nullable=False),
        sa.Column("due_at", eifo_core.types.UtcDateTime(), nullable=False),
        sa.ForeignKeyConstraint(["title_id"], ["titles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("title_id"),
    )
    with op.batch_alter_table("enrich_attempts", schema=None) as batch_op:
        batch_op.create_index("ix_enrich_attempts_due_at", ["due_at"], unique=False)

    op.execute(BACKFILL)


def downgrade() -> None:
    with op.batch_alter_table("enrich_attempts", schema=None) as batch_op:
        batch_op.drop_index("ix_enrich_attempts_due_at")

    op.drop_table("enrich_attempts")
