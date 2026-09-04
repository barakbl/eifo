"""Let a ratings provider say what it is called and what it looks like.

How a score was credited used to be a dictionary in the API: a provider's name
lived in ``eifo_api.converters.PROVIDER_NAMES``, nowhere near the enricher that
produced the score. Adding a provider meant editing a package that has never
heard of it, and the two could disagree - the API naming a provider the fetcher
no longer collects, or not naming one it does.

It also had nowhere to put the two things a rating chip needs beyond a name:
which scores belong to the same service, so that Tomatometer and Audience read
as one Rotten Tomatoes rather than as two separate raters, and where that
service's logo is.

So the enricher declares it and the fetcher writes it here, exactly as it does
for ``sources`` - the database being the only thing the fetcher and the API
share.

The rows are seeded with what the API was hard-coding, so an upgrade is not a
page full of ``rt_critics`` until the next enrich runs. Everything after that is
the plugins' answer, including the logos, which a migration cannot copy because
they are files rather than rows.

Revision ID: 0024_providers_describe_themselves
Revises: 0023_seret_index
Create Date: 2026-09-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# Always available: autogenerate emits eifo_core.types.UtcDateTime() for every
# timestamp column but does not add the import itself.
import eifo_core.types

revision: str = "0024_providers_describe_themselves"
down_revision: str | None = "0023_seret_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: What the API used to hard-code, as rows. Kept here as a historical record of
#: the moment the knowledge moved, not as a list anybody should edit: from the
#: next enrich onwards these rows are whatever the enrichers declare.
_SEED = [
    ("imdb", "IMDb", "imdb", "IMDb", "https://www.imdb.com", 0),
    ("tmdb", "TMDB", "tmdb", "TMDB", "https://www.themoviedb.org", 0),
    (
        "rt_critics",
        "Tomatometer",
        "rt",
        "Rotten Tomatoes",
        "https://www.rottentomatoes.com",
        0,
    ),
    (
        "rt_audience",
        "Audience",
        "rt",
        "Rotten Tomatoes",
        "https://www.rottentomatoes.com",
        1,
    ),
    ("seret_critics", "מבקרים", "seret", "סרט", "https://www.seret.co.il", 0),
    ("seret_viewers", "צופים", "seret", "סרט", "https://www.seret.co.il", 1),
    ("edb", "EDB", "edb", "EDB", None, 0),
]


def upgrade() -> None:
    table = op.create_table(
        "rating_providers",
        # A plain VARCHAR, as every other enum column in this schema is: the
        # models declare the CHECK, and emitting a second one here is what the
        # models-against-migrations drift guard reports.
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("label", sa.String(length=100), nullable=False),
        sa.Column("group_key", sa.String(length=50), nullable=False),
        sa.Column("group_name", sa.String(length=100), nullable=False),
        sa.Column("logo_path", sa.String(length=500), nullable=True),
        sa.Column("website_url", sa.String(length=500), nullable=True),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("created_at", eifo_core.types.UtcDateTime(), nullable=False),
        sa.Column("updated_at", eifo_core.types.UtcDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("provider"),
    )

    now = eifo_core.types.utcnow()
    op.bulk_insert(
        table,
        [
            {
                "provider": provider,
                "label": label,
                "group_key": group_key,
                "group_name": group_name,
                "website_url": website_url,
                "logo_path": None,
                "position": position,
                "created_at": now,
                "updated_at": now,
            }
            for provider, label, group_key, group_name, website_url, position in _SEED
        ],
    )


def downgrade() -> None:
    """Drops the table outright.

    Nothing is lost that the next enrich would not write again: every row is a
    copy of what an installed plugin declares about itself.
    """
    op.drop_table("rating_providers")
