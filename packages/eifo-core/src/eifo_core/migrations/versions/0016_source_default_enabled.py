"""What a source does when nothing is configured.

Three things can decide whether a source is collected: an operator's switch in
the Manage tab, the ``[sources]`` block in the config file, and - when both are
silent - what the plugin itself declares. The first two were already legible to
the API; the third was not, because only the fetcher knows what plugins exist
and the two services never talk.

So the fetcher writes it here. Existing rows default to true, which is what
every source declared before any of them declared otherwise.

Revision ID: 0016_source_default_enabled
Revises: 0015_manage_tab
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0016_source_default_enabled"
down_revision = "0015_manage_tab"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "sources",
        sa.Column("default_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
    )


def downgrade() -> None:
    op.drop_column("sources", "default_enabled")
