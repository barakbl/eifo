"""Add people and credits, and a title's language and origin countries.

Who made a title is the metadata a viewer scans before deciding what to watch,
and it is also what makes a person's body of work browsable: one page per
person, listing what they directed, shot or appeared in.

Credits carry a ``source`` because TMDB does not know much of Israeli cinema -
the Film Archive reads its own directors off its pages - and a claim without a
provenance is a rumour.

Revision ID: 0007_credits
Revises: 0006_availability_price
Create Date: 2026-08-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# Always available: autogenerate emits eifo_core.types.UtcDateTime() for every
# timestamp column but does not add the import itself.
import eifo_core.types

revision: str = "0007_credits"
down_revision: str | None = "0006_availability_price"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "people",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tmdb_id", sa.Integer(), nullable=True),
        sa.Column("name_en", sa.String(length=200), nullable=True),
        sa.Column("name_he", sa.String(length=200), nullable=True),
        sa.Column("profile_source_url", sa.String(length=1000), nullable=True),
        sa.Column("created_at", eifo_core.types.UtcDateTime(), nullable=False),
        sa.Column("updated_at", eifo_core.types.UtcDateTime(), nullable=False),
        sa.CheckConstraint(
            "name_en IS NOT NULL OR name_he IS NOT NULL", name="ck_people_has_a_name"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tmdb_id"),
    )
    op.create_table(
        "credits",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("title_id", sa.Integer(), nullable=False),
        sa.Column("person_id", sa.Integer(), nullable=False),
        sa.Column(
            "role",
            sa.Enum(
                "director",
                "cinematographer",
                "cast",
                name="credit_role",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("character", sa.String(length=300), nullable=True),
        sa.Column("billing_order", sa.Integer(), nullable=True),
        sa.Column("source", sa.String(length=50), nullable=False),
        sa.ForeignKeyConstraint(["person_id"], ["people.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["title_id"], ["titles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("title_id", "person_id", "role", "character", name="uq_credit"),
    )
    with op.batch_alter_table("credits", schema=None) as batch_op:
        batch_op.create_index("ix_credits_person_role", ["person_id", "role"], unique=False)
        batch_op.create_index("ix_credits_title_role", ["title_id", "role"], unique=False)

    with op.batch_alter_table("titles", schema=None) as batch_op:
        batch_op.add_column(sa.Column("original_language", sa.String(length=8), nullable=True))
        batch_op.add_column(sa.Column("origin_countries", sa.String(length=100), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("titles", schema=None) as batch_op:
        batch_op.drop_column("origin_countries")
        batch_op.drop_column("original_language")

    with op.batch_alter_table("credits", schema=None) as batch_op:
        batch_op.drop_index("ix_credits_title_role")
        batch_op.drop_index("ix_credits_person_role")

    op.drop_table("credits")
    op.drop_table("people")
