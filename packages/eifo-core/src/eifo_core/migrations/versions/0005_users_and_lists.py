"""Add users, sessions and user_items.

The account side of the schema: an identity from an OAuth provider, the
server-side sessions that make logout and deletion take effect immediately, and
one row per (user, title) carrying list membership, rating and private note.

Revision ID: 0005_users
Revises: 0004_titles_fts
Create Date: 2026-08-08
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# Always available: autogenerate emits eifo_core.types.UtcDateTime() for every
# timestamp column but does not add the import itself.
import eifo_core.types

revision: str = "0005_users"
down_revision: str | None = "0004_titles_fts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "auth_provider",
            sa.Enum("google", "x", name="auth_provider", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column("auth_subject", sa.String(length=255), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("display_name", sa.String(length=100), nullable=False),
        sa.Column("handle", sa.String(length=30), nullable=True),
        sa.Column("avatar_url", sa.String(length=1000), nullable=True),
        sa.Column("is_public", sa.Boolean(), nullable=False),
        sa.Column("my_source_ids", sa.JSON(), nullable=False),
        sa.Column("created_at", eifo_core.types.UtcDateTime(), nullable=False),
        sa.Column("last_login_at", eifo_core.types.UtcDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("auth_provider", "auth_subject", name="uq_users_identity"),
        sa.UniqueConstraint("handle"),
    )
    op.create_table(
        "sessions",
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("created_at", eifo_core.types.UtcDateTime(), nullable=False),
        sa.Column("expires_at", eifo_core.types.UtcDateTime(), nullable=False),
        sa.Column("last_used_at", eifo_core.types.UtcDateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("token_hash"),
    )
    with op.batch_alter_table("sessions", schema=None) as batch_op:
        batch_op.create_index("ix_sessions_expires_at", ["expires_at"], unique=False)

    op.create_table(
        "user_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("title_id", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "watched",
                "want_to_watch",
                name="item_status",
                native_enum=False,
                length=32,
            ),
            nullable=True,
        ),
        sa.Column("rating", sa.Integer(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", eifo_core.types.UtcDateTime(), nullable=False),
        sa.Column("updated_at", eifo_core.types.UtcDateTime(), nullable=False),
        sa.CheckConstraint(
            "note IS NULL OR length(note) <= 2000", name="ck_user_items_note_length"
        ),
        sa.CheckConstraint(
            "rating IS NULL OR (rating >= 1 AND rating <= 10)",
            name="ck_user_items_rating_range",
        ),
        sa.ForeignKeyConstraint(["title_id"], ["titles.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "title_id", name="uq_user_item"),
    )
    with op.batch_alter_table("user_items", schema=None) as batch_op:
        batch_op.create_index("ix_user_items_user_status", ["user_id", "status"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("user_items", schema=None) as batch_op:
        batch_op.drop_index("ix_user_items_user_status")

    op.drop_table("user_items")

    with op.batch_alter_table("sessions", schema=None) as batch_op:
        batch_op.drop_index("ix_sessions_expires_at")

    op.drop_table("sessions")
    op.drop_table("users")
