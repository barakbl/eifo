"""Add availability.price_minor and availability.price_currency.

Rent/buy sources have a price, and an offer that costs money is not the same
fact as one included in a subscription: the Cinematheque's VOD is the first
source that charges per title, so the offer row has to be able to say how much.
Stored in the currency's minor unit (1990 = 19.90 ILS) rather than as a float.

Revision ID: 0006_availability_price
Revises: 0005_users
Create Date: 2026-08-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# Always available: autogenerate emits eifo_core.types.UtcDateTime() for every
# timestamp column but does not add the import itself.
import eifo_core.types

revision: str = "0006_availability_price"
down_revision: str | None = "0005_users"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("availability", schema=None) as batch_op:
        batch_op.add_column(sa.Column("price_minor", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("price_currency", sa.String(length=3), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("availability", schema=None) as batch_op:
        batch_op.drop_column("price_currency")
        batch_op.drop_column("price_minor")
