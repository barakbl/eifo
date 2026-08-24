"""Forget the years that were never years.

Catalogs emit 0 for "we do not know" and placeholders like 2999 for "not
scheduled yet". Stored as written, both sort to the ends of a by-year list -
which is exactly where anyone sorting by year looks first, so the deployed
catalog's newest titles were a 2999 children's show and a Hebrew sketch dated
year zero.

The listing is real; only its year is not. Nulling it leaves the title in the
catalog and out of the answer to "what came out recently", which is the honest
place for a title whose date nobody knows.

The bounds here are deliberately wider than the ones ingestion now applies
(``eifo_fetcher.sources.base.plausible_year``): a migration should do exactly
what it did the day it was written, so it does not move with the calendar.

Revision ID: 0011_placeholder_years
Revises: 0010_run_statuses
Create Date: 2026-08-24
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0011_placeholder_years"
down_revision: str | None = "0010_run_statuses"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Before cinema, or long after anything anyone has announced.
CLEAR = "UPDATE titles SET year = NULL WHERE year IS NOT NULL AND (year < 1880 OR year > 2100)"


def upgrade() -> None:
    op.execute(CLEAR)


def downgrade() -> None:
    """Nothing to restore: the values this removes carried no information."""
