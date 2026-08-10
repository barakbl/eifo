"""Initial catalog schema.

Creates the catalog side of the model: titles, genres, sources, availability,
plus the fetcher's observability and review queues. Ratings, aggregate scores
and user data arrive in later stages.

Revision ID: 0001_initial
Revises:
Create Date: 2026-08-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import eifo_core.types

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "genres",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tmdb_id", sa.Integer(), nullable=True),
        sa.Column("name_en", sa.String(length=100), nullable=False),
        sa.Column("name_he", sa.String(length=100), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tmdb_id"),
    )

    op.create_table(
        "titles",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "type",
            sa.Enum("movie", "series", name="title_kind", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column("tmdb_id", sa.Integer(), nullable=True),
        sa.Column("imdb_id", sa.String(length=16), nullable=True),
        sa.Column("name_en", sa.String(length=500), nullable=True),
        sa.Column("name_he", sa.String(length=500), nullable=True),
        sa.Column("year", sa.Integer(), nullable=True),
        sa.Column("overview_en", sa.Text(), nullable=True),
        sa.Column("overview_he", sa.Text(), nullable=True),
        sa.Column("poster_path", sa.String(length=500), nullable=True),
        sa.Column("backdrop_path", sa.String(length=500), nullable=True),
        sa.Column("runtime_minutes", sa.Integer(), nullable=True),
        sa.Column("seasons", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=50), nullable=True),
        sa.Column("created_at", eifo_core.types.UtcDateTime(), nullable=False),
        sa.Column("updated_at", eifo_core.types.UtcDateTime(), nullable=False),
        sa.CheckConstraint(
            "name_en IS NOT NULL OR name_he IS NOT NULL",
            name="ck_titles_has_a_name",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("imdb_id"),
        sa.UniqueConstraint("tmdb_id"),
    )
    with op.batch_alter_table("titles", schema=None) as batch_op:
        batch_op.create_index("ix_titles_type_year", ["type", "year"], unique=False)

    op.create_table(
        "title_genres",
        sa.Column("title_id", sa.Integer(), nullable=False),
        sa.Column("genre_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["genre_id"], ["genres.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["title_id"], ["titles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("title_id", "genre_id"),
    )

    op.create_table(
        "sources",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("key", sa.String(length=50), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "subscription",
                "free",
                "rent_buy",
                name="source_kind",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("website_url", sa.String(length=500), nullable=False),
        sa.Column("logo_path", sa.String(length=500), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("deactivated_at", eifo_core.types.UtcDateTime(), nullable=True),
        sa.Column("created_at", eifo_core.types.UtcDateTime(), nullable=False),
        sa.Column("updated_at", eifo_core.types.UtcDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key"),
    )

    op.create_table(
        "availability",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("title_id", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("deep_link_url", sa.String(length=1000), nullable=True),
        sa.Column(
            "offer_type",
            sa.Enum(
                "stream",
                "rent",
                "buy",
                "free",
                name="offer_type",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.Column("miss_count", sa.Integer(), nullable=False),
        sa.Column("first_seen", eifo_core.types.UtcDateTime(), nullable=False),
        sa.Column("last_seen", eifo_core.types.UtcDateTime(), nullable=False),
        sa.Column("gone_since", eifo_core.types.UtcDateTime(), nullable=True),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["title_id"], ["titles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("title_id", "source_id", "offer_type", name="uq_availability_offer"),
    )
    with op.batch_alter_table("availability", schema=None) as batch_op:
        batch_op.create_index(
            "ix_availability_source_current", ["source_id", "is_current"], unique=False
        )
        batch_op.create_index(
            "ix_availability_title_current", ["title_id", "is_current"], unique=False
        )

    op.create_table(
        "fetch_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source_key", sa.String(length=50), nullable=True),
        sa.Column(
            "phase",
            sa.Enum("sync", "enrich", "images", name="fetch_phase", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column("started_at", eifo_core.types.UtcDateTime(), nullable=False),
        sa.Column("finished_at", eifo_core.types.UtcDateTime(), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "ok",
                "failed",
                "aborted_suspicious",
                name="fetch_status",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("stats", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("fetch_runs", schema=None) as batch_op:
        batch_op.create_index(
            "ix_fetch_runs_source_started", ["source_key", "started_at"], unique=False
        )

    op.create_table(
        "match_reviews",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source_key", sa.String(length=50), nullable=False),
        sa.Column("raw_payload", sa.JSON(), nullable=False),
        sa.Column("candidates", sa.JSON(), nullable=False),
        sa.Column("created_at", eifo_core.types.UtcDateTime(), nullable=False),
        sa.Column("resolved_at", eifo_core.types.UtcDateTime(), nullable=True),
        sa.Column("resolved_title_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["resolved_title_id"], ["titles.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("match_reviews")
    with op.batch_alter_table("fetch_runs", schema=None) as batch_op:
        batch_op.drop_index("ix_fetch_runs_source_started")
    op.drop_table("fetch_runs")

    with op.batch_alter_table("availability", schema=None) as batch_op:
        batch_op.drop_index("ix_availability_title_current")
        batch_op.drop_index("ix_availability_source_current")
    op.drop_table("availability")

    op.drop_table("sources")
    op.drop_table("title_genres")

    with op.batch_alter_table("titles", schema=None) as batch_op:
        batch_op.drop_index("ix_titles_type_year")
    op.drop_table("titles")

    op.drop_table("genres")
