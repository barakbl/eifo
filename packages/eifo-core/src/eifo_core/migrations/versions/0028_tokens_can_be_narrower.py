"""Give API tokens a scope.

A token carried everything its owner could do. That is right for the fetcher
and wrong for a token pasted into an AI assistant, so a token can now be
issued to read only, or to read and keep the owner's lists. Every token that
exists today is ``full``: that is what it could already do, and narrowing one
under its holder would break whatever is using it.

``api_tokens`` has no search triggers, so the batch rebuild costs nothing here.

Revision ID: 0028_tokens_can_be_narrower
Revises: 0027_members_add_what_they_watched
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0028_tokens_can_be_narrower"
down_revision = "0027_members_add_what_they_watched"
branch_labels = None
depends_on = None

_SCOPES = ("full", "read", "lists")


def upgrade() -> None:
    with op.batch_alter_table("api_tokens", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "scope",
                sa.Enum(*_SCOPES, name="token_scope", native_enum=False, length=32),
                nullable=False,
                server_default="full",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("api_tokens", schema=None) as batch_op:
        batch_op.drop_column("scope")
