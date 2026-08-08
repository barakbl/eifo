"""Add titles.poster_source_url.

Separates "where the artwork can be downloaded from" (set by the sync
pipeline) from "where it is stored" (poster_path, set by the image
pipeline); overloading one column for both made its meaning ambiguous.

Revision ID: 0002_poster_source_url
Revises: 0001_initial
Create Date: 2026-08-07 05:20:33.962321
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# Always available: autogenerate emits tvil_core.types.UtcDateTime() for every
# timestamp column but does not add the import itself.
import tvil_core.types

revision: str = "0002_poster_source_url"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("titles", schema=None) as batch_op:
        batch_op.add_column(sa.Column("poster_source_url", sa.String(length=1000), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("titles", schema=None) as batch_op:
        batch_op.drop_column("poster_source_url")

