"""Sign-in becomes an invitation, and the API gets tokens of its own.

Signing in used to be open to anybody with a Google account: whoever completed
the flow got a ``users`` row, and only the Manage tab was gated - by a list of
addresses in the configuration file. That is the right shape for a public
instance and the wrong one for a private catalog, which is what most of these
are.

``members`` is the allowlist. A row is a decision about who may come in, so it
exists before its person ever arrives and outlives them deleting their account.
It carries a role too, which is what lets an administrator promote somebody
from the Manage tab instead of editing a config file and restarting. The
configured ``admin_emails`` still stands above all of it, and deliberately: the
first administrator has to come from somewhere that a stranger cannot reach,
and it is the one thing that cannot be demoted away.

``api_tokens`` is the same idea as ``sessions`` for things that are not
browsers. A row rather than a self-contained token, so revoking one takes
effect on the next request; only the hash of the value is stored, so a copy of
this table cannot be replayed as a login.

**Everybody already here is grandfathered in.** An upgrade that turned the
existing users of an instance into strangers - including, on a single-user
instance, the person running the upgrade - would be a poor way to deliver a
security improvement. They come in as members; the configured administrators
remain administrators through configuration, as they were yesterday.

Revision ID: 0025_invited_members_and_api_tokens
Revises: 0024_providers_describe_themselves
Create Date: 2026-09-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# Always available: autogenerate emits eifo_core.types.UtcDateTime() for every
# timestamp column but does not add the import itself.
import eifo_core.types

revision: str = "0025_invited_members_and_api_tokens"
down_revision: str | None = "0024_providers_describe_themselves"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    members = op.create_table(
        "members",
        sa.Column("email", sa.String(length=320), nullable=False),
        # A plain VARCHAR, as every other enum column in this schema is: the
        # models declare the CHECK, and emitting a second one here is what the
        # models-against-migrations drift guard reports.
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("invited_by", sa.String(length=320), nullable=True),
        sa.Column("created_at", eifo_core.types.UtcDateTime(), nullable=False),
        sa.Column("updated_at", eifo_core.types.UtcDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("email"),
    )

    op.create_table(
        "api_tokens",
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("created_at", eifo_core.types.UtcDateTime(), nullable=False),
        sa.Column("last_used_at", eifo_core.types.UtcDateTime(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("token_hash"),
    )
    with op.batch_alter_table("api_tokens", schema=None) as batch_op:
        batch_op.create_index("ix_api_tokens_user", ["user_id"], unique=False)

    _grandfather_existing_users(members)


def _grandfather_existing_users(members: sa.Table) -> None:
    """Invite everybody who has already signed in.

    Casefolded here rather than copied across, because that is the form the
    allowlist is read in and a row nobody can match is the same as no row -
    which, for the person running the upgrade, means being locked out of their
    own instance by a migration.

    An account with no email - X does not always supply one - cannot be put on
    an allowlist keyed by address, and is left out. They keep the session they
    are holding and cannot start a new one, which is the honest outcome for a
    row that has nothing an invitation could be addressed to.
    """
    connection = op.get_bind()
    existing = connection.execute(
        sa.text(
            "SELECT DISTINCT lower(trim(email)) AS email FROM users "
            "WHERE email IS NOT NULL AND trim(email) <> ''"
        )
    ).all()
    if not existing:
        return

    now = eifo_core.types.utcnow()
    op.bulk_insert(
        members,
        [
            {
                "email": row.email,
                "role": "member",
                "invited_by": None,
                "created_at": now,
                "updated_at": now,
            }
            for row in existing
        ],
    )


def downgrade() -> None:
    """Drops both tables, and with them the allowlist.

    Sign-in goes back to being open to anybody the provider vouches for, which
    is what it was before this revision. Any token issued is revoked by the
    table going away, which is the correct outcome for a credential whose
    server-side record no longer exists.
    """
    with op.batch_alter_table("api_tokens", schema=None) as batch_op:
        batch_op.drop_index("ix_api_tokens_user")

    op.drop_table("api_tokens")
    op.drop_table("members")
