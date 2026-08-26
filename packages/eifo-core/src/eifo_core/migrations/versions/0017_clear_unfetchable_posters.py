"""Forget artwork URLs nothing can fetch.

A source that gives a page-relative ``og:image`` had it stored as written, and
the image pipeline handed it to an HTTP client that refuses a URL with no
scheme. Two of them were enough to make the nightly artwork phase report itself
failed every night, for posters it was never going to get.

The parser now resolves them against the page they came from
(``eifo_fetcher.sources.israel_film_archive``), but the rows already written
keep their broken value: the sync only fills this in when it is empty, so a
corrected plugin has nothing to correct. Emptying them is what lets the next
sync write the URL that works.

The artwork itself is not lost - there was never any. Only the address of it.

Revision ID: 0017_clear_unfetchable_posters
Revises: 0016_source_default_enabled
"""

from __future__ import annotations

from alembic import op

revision = "0017_clear_unfetchable_posters"
down_revision = "0016_source_default_enabled"
branch_labels = None
depends_on = None

#: Anything an HTTP client cannot be handed.
CLEAR = """
UPDATE titles SET poster_source_url = NULL
WHERE poster_source_url IS NOT NULL
  AND poster_source_url NOT LIKE 'http://%'
  AND poster_source_url NOT LIKE 'https://%'
"""


def upgrade() -> None:
    op.execute(CLEAR)


def downgrade() -> None:
    """Nothing to restore: the values this removes addressed nothing."""
