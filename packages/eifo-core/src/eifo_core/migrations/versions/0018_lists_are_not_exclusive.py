"""Watched and want-to-watch stop being the same column.

One ``status`` column could hold only one of them, so filing something under
"watched" silently took it off "want to watch". They are not opposites: a film
somebody has seen and means to see again belongs on both lists, and there was
no way to say so.

Two flags instead. Every existing row carried at most one, so the backfill is
exact and nothing has to be guessed - which is also why this is reversible: an
entry on both lists comes back as watched, the stronger claim of the two.

Revision ID: 0018_lists_are_not_exclusive
Revises: 0017_clear_unfetchable_posters
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0018_lists_are_not_exclusive"
down_revision = "0017_clear_unfetchable_posters"
branch_labels = None
depends_on = None

#: The enum as 0005 declared it, so the downgrade puts back what it made.
_STATUS = sa.Enum("watched", "want_to_watch", name="item_status", native_enum=False, length=32)

#: Just enough of the table to write the backfill in SQL any dialect accepts.
_items = sa.table(
    "user_items",
    sa.column("status", sa.String()),
    sa.column("want_to_watch", sa.Boolean()),
    sa.column("watched", sa.Boolean()),
)


def upgrade() -> None:
    op.add_column(
        "user_items",
        sa.Column("want_to_watch", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "user_items",
        sa.Column("watched", sa.Boolean(), nullable=False, server_default=sa.false()),
    )

    op.execute(_items.update().where(_items.c.status == "want_to_watch").values(want_to_watch=True))
    op.execute(_items.update().where(_items.c.status == "watched").values(watched=True))

    with op.batch_alter_table("user_items", schema=None) as batch_op:
        batch_op.drop_index("ix_user_items_user_status")
        batch_op.drop_column("status")
        batch_op.create_index("ix_user_items_user_watched", ["user_id", "watched"], unique=False)
        batch_op.create_index("ix_user_items_user_want", ["user_id", "want_to_watch"], unique=False)


def downgrade() -> None:
    op.add_column("user_items", sa.Column("status", _STATUS, nullable=True))

    # Watched is applied second so it wins where both are set: it is the one
    # that actually happened, and the column can only hold one of them.
    op.execute(_items.update().where(_items.c.want_to_watch).values(status="want_to_watch"))
    op.execute(_items.update().where(_items.c.watched).values(status="watched"))

    with op.batch_alter_table("user_items", schema=None) as batch_op:
        batch_op.drop_index("ix_user_items_user_want")
        batch_op.drop_index("ix_user_items_user_watched")
        batch_op.drop_column("watched")
        batch_op.drop_column("want_to_watch")
        batch_op.create_index("ix_user_items_user_status", ["user_id", "status"], unique=False)
