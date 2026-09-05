"""Who may sign in, and who may administer.

One module, because there is exactly one right answer to each of those and two
places to decide would eventually be two answers - and the way that failure
shows up is somebody who was removed still getting in.

The allowlist lives in the database so it can be edited from the Manage tab.
The configured ``admin_emails`` sits above it and is not editable from
anywhere: the first administrator has to come from somewhere a stranger cannot
reach, and an instance whose owner can be demoted through its own web interface
is one bad afternoon from belonging to somebody else. So a configured address
is always admitted and always an administrator, whatever the table says.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from eifo_core.enums import MemberRole
from eifo_core.models import Member, normalised_email
from eifo_core.settings import Settings


def find(session: Session, email: str | None) -> Member | None:
    """The allowlist row for an address, if it has one."""
    if not email:
        return None
    return session.get(Member, normalised_email(email))


def exists(session: Session, settings: Settings) -> bool:
    """Whether anybody has decided who may sign in.

    An instance with no configured administrator and nobody invited has no
    allowlist - not an empty one. The difference matters enormously: enforcing
    an empty list would brick a fresh install, because signing in is how
    somebody reaches the Manage tab and the Manage tab is where invitations are
    written. Nobody could ever be the first.

    So the gate switches itself on the moment there is anybody to be the first:
    a configured administrator, or a row somebody has written. Until then
    sign-in is open, which is exactly what it was before any of this existed.
    """
    if settings.admin_emails:
        return True
    return session.scalar(select(Member.email).limit(1)) is not None


def may_sign_in(session: Session, settings: Settings, email: str | None) -> bool:
    """Whether this address is allowed in at all."""
    if not exists(session, settings):
        return True
    if not email:
        # Nothing to match against a list of addresses. X does not always
        # supply one, and on an instance that has decided who may come in, an
        # account with no address cannot be one of them. A real consequence of
        # keying the allowlist on the address, stated where it is decided.
        return False
    return settings.is_admin(email) or find(session, email) is not None


def is_admin(session: Session, settings: Settings, email: str | None) -> bool:
    """Whether this address may open the Manage tab.

    Configuration first and always. Beyond that, whatever the allowlist says -
    which is what lets an administrator promote somebody without editing a file
    and restarting the service.
    """
    if not email:
        return False
    if settings.is_admin(email):
        return True
    row = find(session, email)
    return row is not None and row.role is MemberRole.ADMIN


def invite(
    session: Session,
    email: str,
    *,
    role: MemberRole = MemberRole.MEMBER,
    invited_by: str | None = None,
) -> Member:
    """Add an address to the allowlist, or change the role of one already on it.

    Idempotent on purpose: inviting somebody who is already invited is not an
    error worth a failed request, it is somebody making sure.
    """
    address = normalised_email(email)
    row = session.get(Member, address)
    if row is None:
        row = Member(email=address, role=role, invited_by=invited_by)
        session.add(row)
    else:
        row.role = role
    return row


def listed(session: Session) -> list[Member]:
    """Everybody on the allowlist, administrators first then alphabetically.

    The order a person reads this list in: who can change things, then who can
    merely get in.
    """
    rows = list(session.scalars(select(Member)).all())
    return sorted(rows, key=lambda row: (row.role is not MemberRole.ADMIN, row.email))
