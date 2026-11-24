"""Sratim and Time Out Tel Aviv as rating providers.

Two new members of ``RatingProvider``; the column is a VARCHAR with a CHECK
listing the members, so the CHECK is widened on both tables that carry it.

Revision ID: 0034_sratim_timeout
Revises: 0033_source_partial
Create Date: 2026-11-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0034_sratim_timeout"
down_revision: str | None = "0033_source_partial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

WAS = (
    "imdb",
    "tmdb",
    "rt_critics",
    "rt_audience",
    "seret_critics",
    "seret_viewers",
    "edb",
)
NOW = (*WAS, "sratim", "timeout_tlv")
_TABLES = ("external_ratings", "rating_providers")


def _provider(members: Sequence[str]) -> sa.Enum:
    return sa.Enum(*members, name="rating_provider", native_enum=False, length=32)


def _retype(table: str, was: Sequence[str], now: Sequence[str]) -> None:
    with op.batch_alter_table(table, schema=None) as batch_op:
        batch_op.alter_column(
            "provider",
            existing_type=_provider(was),
            type_=_provider(now),
            existing_nullable=False,
        )


def upgrade() -> None:
    for table in _TABLES:
        _retype(table, WAS, NOW)


def downgrade() -> None:
    # Rows for providers the old constraint cannot describe would block it.
    for table in _TABLES:
        op.execute(f"DELETE FROM {table} WHERE provider IN ('sratim', 'timeout_tlv')")
    for table in _TABLES:
        _retype(table, NOW, WAS)
