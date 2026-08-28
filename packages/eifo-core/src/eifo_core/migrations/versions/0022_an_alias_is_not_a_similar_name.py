"""Forget the aliases that merged two different films.

An alias says "TMDB carries this work twice, and this is the id we do not use".
It was being written whenever an incoming id had no owner and some stored title
had the same name after normalisation - which strips the leading article, so
"The Strays" and "Strays" are one string, and so are "An Intrusion" and
"Intrusion". Four distinct films were merged in a single night's run.

Only an alias onto a title with no id of its own was ever the intended case. Of
221 rows, 4 were that; 217 pointed at a title already holding a different id -
which is TMDB saying, in the only way it can, that these are two works.

Those 217 go. The ids they held resolve honestly again on the next sync, which
creates the titles that should have existed all along. The offers that went to
the wrong title with them are left to the sweep: unseen for two runs, they
retire the way anything else does that stopped being true.

Revision ID: 0022_an_alias_is_not_a_similar_name
Revises: 0021_a_listing_keeps_its_own_id
"""

from __future__ import annotations

from alembic import op

revision = "0022_an_alias_is_not_a_similar_name"
down_revision = "0021_a_listing_keeps_its_own_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DELETE FROM tmdb_aliases
         WHERE title_id IN (
             SELECT t.id FROM titles t
              WHERE t.id = tmdb_aliases.title_id
                AND t.tmdb_id IS NOT NULL
                AND t.tmdb_id <> tmdb_aliases.tmdb_id
         )
        """
    )


def downgrade() -> None:
    """Nothing to put back: these rows said two films were one."""
