"""The database side of a login session.

Sessions are rows rather than self-contained tokens so that logging out and
deleting an account take effect on the next request instead of whenever a
signed token happens to expire (docs.internal/09-auth-privacy.md).
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import delete
from sqlalchemy.orm import Session

from tvil_api.security import (
    SESSION_RENEW_AFTER,
    SESSION_TTL,
    hash_token,
    new_session_token,
)
from tvil_core.models import User, UserSession
from tvil_core.types import utcnow


def start_session(session: Session, user: User, *, now: dt.datetime | None = None) -> str:
    """Open a session for a user and return the raw token for their cookie.

    The token is returned once and never stored; the row keeps only its hash.
    """
    moment = now or utcnow()
    token = new_session_token()

    session.add(
        UserSession(
            token_hash=hash_token(token),
            user_id=user.id,
            created_at=moment,
            last_used_at=moment,
            expires_at=moment + SESSION_TTL,
        )
    )
    user.last_login_at = moment
    session.commit()
    return token


def resolve_session(
    session: Session,
    token: str | None,
    *,
    now: dt.datetime | None = None,
) -> UserSession | None:
    """The live session a cookie refers to, renewed if it is due.

    An expired row is deleted on sight rather than merely ignored: the cheapest
    place to clean up is where we already know it is dead.
    """
    if not token:
        return None

    moment = now or utcnow()
    row = session.get(UserSession, hash_token(token))
    if row is None:
        return None

    if row.expires_at <= moment:
        session.delete(row)
        session.commit()
        return None

    _renew(session, row, moment)
    return row


def _renew(session: Session, row: UserSession, now: dt.datetime) -> None:
    """Slide the expiry forward, but only occasionally.

    Sliding on every request would make each read a write; a day's granularity
    keeps an active session alive just as effectively.
    """
    if now - row.last_used_at < SESSION_RENEW_AFTER:
        return

    row.last_used_at = now
    row.expires_at = now + SESSION_TTL
    session.commit()


def end_session(session: Session, token_hash: str) -> None:
    """Revoke one session immediately."""
    session.execute(delete(UserSession).where(UserSession.token_hash == token_hash))
    session.commit()
