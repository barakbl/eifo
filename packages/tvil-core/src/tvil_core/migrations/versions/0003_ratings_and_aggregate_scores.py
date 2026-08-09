"""Add external_ratings and aggregate_scores.

One row per (title, provider) for scores, plus the combined score with the
components it was computed from so the UI can show its working.

Revision ID: 0003_ratings
Revises: 0002_poster_source_url
Create Date: 2026-08-07 15:32:53.315021
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# Always available: autogenerate emits tvil_core.types.UtcDateTime() for every
# timestamp column but does not add the import itself.
import tvil_core.types

revision: str = "0003_ratings"
down_revision: str | None = "0002_poster_source_url"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "aggregate_scores",
        sa.Column("title_id", sa.Integer(), nullable=False),
        sa.Column("score", sa.Integer(), nullable=True),
        sa.Column("score_israeli", sa.Integer(), nullable=True),
        sa.Column("components", sa.JSON(), nullable=False),
        sa.Column("computed_at", tvil_core.types.UtcDateTime(), nullable=False),
        sa.ForeignKeyConstraint(["title_id"], ["titles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("title_id"),
    )
    op.create_table(
        "external_ratings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("title_id", sa.Integer(), nullable=False),
        sa.Column(
            "provider",
            sa.Enum(
                "imdb",
                "tmdb",
                "rt_critics",
                "rt_audience",
                "seret_critics",
                "seret_viewers",
                "edb",
                name="rating_provider",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("score_raw", sa.Float(), nullable=False),
        sa.Column("score_normalized", sa.Integer(), nullable=False),
        sa.Column("vote_count", sa.Integer(), nullable=True),
        sa.Column("url", sa.String(length=1000), nullable=True),
        sa.Column("fetched_at", tvil_core.types.UtcDateTime(), nullable=False),
        sa.ForeignKeyConstraint(["title_id"], ["titles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("title_id", "provider", name="uq_rating_provider"),
    )
    with op.batch_alter_table("external_ratings", schema=None) as batch_op:
        batch_op.create_index("ix_external_ratings_title", ["title_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("external_ratings", schema=None) as batch_op:
        batch_op.drop_index("ix_external_ratings_title")

    op.drop_table("external_ratings")
    op.drop_table("aggregate_scores")
