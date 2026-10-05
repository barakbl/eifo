"""Let a member add a film they watched somewhere no service is tracked.

Two columns on ``titles``: when it was added by hand, and by whom. Both null for
every title that exists today, which is what they are - brought in by a sync.

The foreign key means SQLite rebuilds the table, and rebuilding ``titles`` is
what silently removed the search triggers once before (0008). So the triggers
are put back in the same step rather than trusted to survive.

Not added in place, though SQLite could: an ``ALTER TABLE ... ADD COLUMN ...
REFERENCES`` keeps its ``ON DELETE`` rule in the database but not in what
SQLAlchemy reads back, and the schema drift guard would then fail on a
difference that is not there.

Revision ID: 0027_members_add_what_they_watched
Revises: 0026_watch_links_go_to_the_service
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

import eifo_core.types
from eifo_core.fts import restore_triggers

revision = "0027_members_add_what_they_watched"
down_revision = "0026_watch_links_go_to_the_service"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("titles", schema=None) as batch_op:
        batch_op.add_column(sa.Column("added_at", eifo_core.types.UtcDateTime(), nullable=True))
        batch_op.add_column(sa.Column("added_by_user_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_titles_added_by_user_id_users",
            "users",
            ["added_by_user_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_index("ix_titles_added_by_user_id", ["added_by_user_id"])
    restore_triggers(op.get_bind())


def downgrade() -> None:
    with op.batch_alter_table("titles", schema=None) as batch_op:
        batch_op.drop_index("ix_titles_added_by_user_id")
        batch_op.drop_constraint("fk_titles_added_by_user_id_users", type_="foreignkey")
        batch_op.drop_column("added_by_user_id")
        batch_op.drop_column("added_at")
    restore_triggers(op.get_bind())
