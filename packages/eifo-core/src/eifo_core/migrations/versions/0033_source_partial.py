"""Mark sources whose plugin reads only part of the catalog.

A plugin may only be able to see a chart or a showcase rather than the whole
library. The fetcher declares that with ``SourceInfo.partial`` and writes it
here so the API and web UI can say so. Existing rows default to false: every
source declared before this read what it could call its catalog.

Revision ID: 0033_source_partial
Revises: 0032_formatted_notes
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0033_source_partial"
down_revision = "0032_formatted_notes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "sources",
        sa.Column("partial", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("sources", "partial")
