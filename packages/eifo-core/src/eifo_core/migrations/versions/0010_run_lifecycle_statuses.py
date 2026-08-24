"""Let a run say it is still going, or that it never got to say anything.

A ``fetch_runs`` row was only written once a phase had finished, so the runs
worth knowing about - the ones killed by an OOM, a power cut or a closed lid -
left no trace at all. Recording the row up front needs a status for "still
going", and the sweep that finds one left behind needs a status for "this ended
without being able to tell us how".

Revision ID: 0010_run_statuses
Revises: 0009_enrich_attempts
Create Date: 2026-08-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_run_statuses"
down_revision: str | None = "0009_enrich_attempts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

WAS = ("ok", "failed", "aborted_suspicious")
NOW = ("running", "ok", "failed", "aborted_suspicious", "crashed")


def _status(members: Sequence[str]) -> sa.Enum:
    return sa.Enum(*members, name="fetch_status", native_enum=False, length=32)


def upgrade() -> None:
    with op.batch_alter_table("fetch_runs", schema=None) as batch_op:
        batch_op.alter_column(
            "status",
            existing_type=_status(WAS),
            type_=_status(NOW),
            existing_nullable=False,
        )


def downgrade() -> None:
    # A row in one of the new states cannot be described by the old constraint;
    # the honest reading of a run that never finished is that it failed.
    op.execute("UPDATE fetch_runs SET status = 'failed' WHERE status IN ('running', 'crashed')")
    with op.batch_alter_table("fetch_runs", schema=None) as batch_op:
        batch_op.alter_column(
            "status",
            existing_type=_status(NOW),
            type_=_status(WAS),
            existing_nullable=False,
        )
