"""Somewhere for an operator to say "and fetch it now".

Switching a source on is a request for its catalog, not just permission to
collect it tonight. The API cannot call the fetcher - the database is all they
share - so the ask is written here and the fetcher clears it once it has run.

Existing rows start NULL: nothing was asked for before there was a way to ask.

Revision ID: 0019_backfill_on_enable
Revises: 0018_lists_are_not_exclusive
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

import eifo_core.types

revision = "0019_backfill_on_enable"
down_revision = "0018_lists_are_not_exclusive"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "sources",
        sa.Column("backfill_requested_at", eifo_core.types.UtcDateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("sources", "backfill_requested_at")
