"""Index the pages of seret.co.il, so a title can be resolved to one.

The Israeli scores were implemented but switched off, because there was no way
to get from a title to its page: Seret's search endpoints all answer with the
same generic listing whatever they are asked. Its sitemap, however, names every
page, so the id can be looked up locally once each page has been read once.

This table is that index. It holds the identity fields resolution needs -
Hebrew and international name, year, and the IMDb id newer pages publish in
``sameAs`` - together with the three figures Seret reports: the audience score
and its vote count, and "Seret Score", the site's composite editorial figure.

Revision ID: 0023_seret_index
Revises: 0022_an_alias_is_not_a_similar_name
Create Date: 2026-08-31
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# Always available: autogenerate emits eifo_core.types.UtcDateTime() for every
# timestamp column but does not add the import itself.
import eifo_core.types

revision: str = "0023_seret_index"
down_revision: str | None = "0022_an_alias_is_not_a_similar_name"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

def upgrade() -> None:
    op.create_table(
        "seret_index",
        # A plain VARCHAR, as every other enum column in this schema is: the
        # models declare the CHECK, and emitting a second one here is what the
        # models-against-migrations drift guard reports.
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("seret_id", sa.Integer(), nullable=False),
        sa.Column("name_he", sa.String(length=500), nullable=True),
        sa.Column("name_en", sa.String(length=500), nullable=True),
        sa.Column("year", sa.Integer(), nullable=True),
        sa.Column("imdb_id", sa.String(length=20), nullable=True),
        sa.Column("viewers_score", sa.Float(), nullable=True),
        sa.Column("viewers_votes", sa.Integer(), nullable=True),
        sa.Column("critics_score", sa.Float(), nullable=True),
        sa.Column("url", sa.String(length=1000), nullable=True),
        sa.Column("indexed_at", eifo_core.types.UtcDateTime(), nullable=False),
        sa.Column(
            "unreadable",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.PrimaryKeyConstraint("kind", "seret_id"),
    )
    with op.batch_alter_table("seret_index", schema=None) as batch_op:
        batch_op.create_index("ix_seret_index_imdb_id", ["imdb_id"], unique=False)


def downgrade() -> None:
    """Drops the index outright.

    Nothing is lost that cannot be rebuilt: every row here is a copy of a page
    that is still on Seret, and ``eifo-fetch seret index`` reads them again.
    """
    with op.batch_alter_table("seret_index", schema=None) as batch_op:
        batch_op.drop_index("ix_seret_index_imdb_id")

    op.drop_table("seret_index")
