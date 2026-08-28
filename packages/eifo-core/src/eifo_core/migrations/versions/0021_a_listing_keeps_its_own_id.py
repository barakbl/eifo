"""Somewhere to keep a source's own id for a listing.

Disney+ lists both Beauty and the Beast films as "Beauty And The Beast", with no
year and nothing else to tell them apart. Matched on the name alone, both
listings landed on whichever title the matcher reached first; the other was
never seen and retired two syncs later as though it had left the service. The
1991 film went missing on 25 August while its sing-along cut, its sequel and its
30th-anniversary special all stayed.

The id is kept on the offer, because "this source offers this title" is exactly
what an availability row says, so the source's name for that offer belongs on
it. Once bound, a listing stays bound - no name can overrule the catalogue's own
answer about what a thing is.

Existing Disney rows are backfilled from the deep link, which already ends in
the content id. Nothing else stored one, so nothing else is backfilled: those
sources bind on their next sync.

Revision ID: 0021_a_listing_keeps_its_own_id
Revises: 0020_tmdb_ids_are_per_media_type
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0021_a_listing_keeps_its_own_id"
down_revision = "0020_tmdb_ids_are_per_media_type"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("availability", sa.Column("source_ref", sa.String(length=200), nullable=True))
    op.create_index("ix_availability_source_ref", "availability", ["source_id", "source_ref"])

    # ".../movies/beauty-and-the-beast/1260017283" - the id is the last segment,
    # and it is the only source that has been writing one down.
    op.execute(
        """
        UPDATE availability
           SET source_ref = replace(deep_link_url, rtrim(deep_link_url, '0123456789'), '')
         WHERE deep_link_url LIKE 'https://www.apps.disneyplus.com/%'
           AND rtrim(deep_link_url, '0123456789') <> deep_link_url
        """
    )

    # A backfilled id that two titles claim is a record of the fault, not a
    # binding: one of the two was never really that listing. Which one the data
    # cannot say, so the id is forgotten and both listings bind again on the
    # next sync - where the matcher now parks the ambiguity for review instead
    # of quietly handing it to whichever title it reached first.
    op.execute(
        """
        UPDATE availability SET source_ref = NULL
         WHERE source_ref IS NOT NULL
           AND (source_id, source_ref) IN (
               SELECT source_id, source_ref FROM availability
                WHERE source_ref IS NOT NULL
                GROUP BY source_id, source_ref
               HAVING COUNT(DISTINCT title_id) > 1
           )
        """
    )


def downgrade() -> None:
    op.drop_index("ix_availability_source_ref", table_name="availability")
    op.drop_column("availability", "source_ref")
