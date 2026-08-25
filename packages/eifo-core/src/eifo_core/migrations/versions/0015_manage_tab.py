"""Two columns the Manage tab needs: a source switch, and what a run said.

``sources.enabled`` is an operator's override of the ``[sources]`` block in the
config file, and is nullable on purpose: NULL means "whatever the file says".
A boolean defaulting to true would have frozen every source at whatever the
file happened to say the moment somebody first used a toggle, which is a
surprising thing for one click on one row to do.

``fetch_runs.log`` is the tail of what a run wrote while it ran. Runs were
already recorded - when they started, how they ended, what they counted - but
the reason a night went wrong only ever existed on the stderr of a process
nobody was watching.

Revision ID: 0015_manage_tab
Revises: 0014_people_fts
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0015_manage_tab"
down_revision = "0014_people_fts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sources", sa.Column("enabled", sa.Boolean(), nullable=True))
    op.add_column("fetch_runs", sa.Column("log", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("fetch_runs", "log")
    op.drop_column("sources", "enabled")
