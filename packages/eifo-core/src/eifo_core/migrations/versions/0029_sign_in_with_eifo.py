"""Sign in with Eifo: apps a member can connect, and what they are given.

Three tables for the OAuth exchange - the apps that registered, the one-time
codes a member's approval produces, and the refresh tokens that keep a
connection alive - and two columns on ``api_tokens``, because an access token
issued to an app *is* an API token: read-only, an hour long, and belonging to
the connection that asked for it.

``api_tokens`` has no search triggers, so its batch rebuild is cheap.

Revision ID: 0029_sign_in_with_eifo
Revises: 0028_tokens_can_be_narrower
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

import eifo_core.types

revision = "0029_sign_in_with_eifo"
down_revision = "0028_tokens_can_be_narrower"
branch_labels = None
depends_on = None

_WHEN = eifo_core.types.UtcDateTime


def upgrade() -> None:
    op.create_table(
        "oauth_clients",
        sa.Column("client_id", sa.String(length=64), nullable=False),
        sa.Column("info", sa.JSON(), nullable=False),
        sa.Column("client_name", sa.String(length=200), nullable=True),
        sa.Column("created_at", _WHEN(), nullable=False),
        sa.Column("last_used_at", _WHEN(), nullable=True),
        sa.PrimaryKeyConstraint("client_id"),
    )
    op.create_table(
        "oauth_codes",
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("client_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("redirect_uri", sa.String(length=2000), nullable=False),
        sa.Column("redirect_uri_provided_explicitly", sa.Boolean(), nullable=False),
        sa.Column("code_challenge", sa.String(length=200), nullable=False),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("resource", sa.String(length=2000), nullable=True),
        sa.Column("expires_at", _WHEN(), nullable=False),
        sa.ForeignKeyConstraint(["client_id"], ["oauth_clients.client_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("code_hash"),
    )
    op.create_table(
        "oauth_refresh_tokens",
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("client_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("resource", sa.String(length=2000), nullable=True),
        sa.Column("created_at", _WHEN(), nullable=False),
        sa.Column("expires_at", _WHEN(), nullable=False),
        sa.Column("used_at", _WHEN(), nullable=True),
        sa.ForeignKeyConstraint(["client_id"], ["oauth_clients.client_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("token_hash"),
    )
    with op.batch_alter_table("oauth_refresh_tokens", schema=None) as batch_op:
        batch_op.create_index("ix_oauth_refresh_user_client", ["user_id", "client_id"])

    with op.batch_alter_table("api_tokens", schema=None) as batch_op:
        batch_op.add_column(sa.Column("client_id", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("expires_at", _WHEN(), nullable=True))
        batch_op.create_foreign_key(
            "fk_api_tokens_client_id_oauth_clients",
            "oauth_clients",
            ["client_id"],
            ["client_id"],
            ondelete="CASCADE",
        )
        batch_op.create_index("ix_api_tokens_client_id", ["client_id"])


def downgrade() -> None:
    with op.batch_alter_table("api_tokens", schema=None) as batch_op:
        batch_op.drop_index("ix_api_tokens_client_id")
        batch_op.drop_constraint("fk_api_tokens_client_id_oauth_clients", type_="foreignkey")
        batch_op.drop_column("expires_at")
        batch_op.drop_column("client_id")
    with op.batch_alter_table("oauth_refresh_tokens", schema=None) as batch_op:
        batch_op.drop_index("ix_oauth_refresh_user_client")
    op.drop_table("oauth_refresh_tokens")
    op.drop_table("oauth_codes")
    op.drop_table("oauth_clients")
