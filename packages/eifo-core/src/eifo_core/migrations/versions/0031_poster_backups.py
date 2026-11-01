"""Keep a listing's other artwork, for when its poster is a dead link.

FreeTV lists a film's artwork in two or three sizes. Only one of them was kept,
and when that one went 404 the nightly artwork run failed on it every night
while the other sizes loaded fine. ``titles.poster_backup_urls`` holds the rest,
best first; the next sync of each source fills it in.

A nullable column needs no table rebuild, but ``restore_triggers`` runs anyway:
dropping it on the way down does rebuild ``titles``, and that is what silently
removed the search triggers once before (0008).

Revision ID: 0031_poster_backups
Revises: 0030_one_person_once
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from eifo_core.fts import restore_triggers

revision = "0031_poster_backups"
down_revision = "0030_one_person_once"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("titles", schema=None) as batch_op:
        batch_op.add_column(sa.Column("poster_backup_urls", sa.JSON(), nullable=True))
    restore_triggers(op.get_bind())


def downgrade() -> None:
    with op.batch_alter_table("titles", schema=None) as batch_op:
        batch_op.drop_column("poster_backup_urls")
    restore_triggers(op.get_bind())
