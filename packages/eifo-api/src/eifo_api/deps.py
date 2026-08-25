"""Request-scoped dependencies.

Handlers stay short by pulling settings, a database session and the current
user from here rather than constructing anything themselves.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session, sessionmaker

from eifo_api.security import (
    CSRF_HEADER,
    SAFE_METHODS,
    SESSION_COOKIE,
    csrf_matches,
    csrf_token_for,
    signing_secret,
)
from eifo_api.sessions import resolve_session
from eifo_core.models import User
from eifo_core.settings import Settings


def get_settings(request: Request) -> Settings:
    """Settings resolved when the application was created."""
    settings: Settings = request.app.state.settings
    return settings


def get_session(request: Request) -> Iterator[Session]:
    """A read-only-by-convention session for one request.

    The API never writes catalog data; user writes commit explicitly.
    """
    factory: sessionmaker[Session] = request.app.state.session_factory
    session = factory()
    try:
        yield session
    finally:
        session.close()


SettingsDep = Annotated[Settings, Depends(get_settings)]
SessionDep = Annotated[Session, Depends(get_session)]


@dataclass(frozen=True)
class Principal:
    """The signed-in user and the session they arrived on."""

    user: User
    token_hash: str
    csrf_token: str
    #: Settled once, when the request is authenticated, rather than asked again
    #: by each handler - so there is one answer per request and one place that
    #: decides it.
    is_admin: bool = False


def current_principal(
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
) -> Principal | None:
    """Who is signed in, or None.

    Deliberately silent about *why* nobody is: an expired cookie and a forged
    one are the same non-event to a caller.
    """
    row = resolve_session(session, request.cookies.get(SESSION_COOKIE))
    if row is None:
        return None

    return Principal(
        user=row.user,
        token_hash=row.token_hash,
        csrf_token=csrf_token_for(row.token_hash, signing_secret(settings)),
        is_admin=settings.is_admin(row.user.email),
    )


def require_principal(
    principal: Annotated[Principal | None, Depends(current_principal)],
) -> Principal:
    """The signed-in user, or 401."""
    if principal is None:
        raise HTTPException(status_code=401, detail="Sign in to use this endpoint.")
    return principal


def require_admin(
    principal: Annotated[Principal, Depends(require_principal)],
) -> Principal:
    """An administrator, or 404.

    404 rather than 403: a signed-in stranger poking at ``/api/v1/admin`` learns
    that the path does not exist for them, which is all they are owed. 403 would
    confirm the surface is there and that the only thing missing is being the
    right person.
    """
    if not principal.is_admin:
        raise HTTPException(status_code=404, detail="Not found.")
    return principal


def verify_csrf(
    request: Request,
    principal: Annotated[Principal, Depends(require_principal)],
) -> None:
    """Reject a state-changing request that did not present its CSRF token.

    SameSite=Lax already blocks the cross-site form post; this is the second
    lock, and the one that does not depend on browser behaviour.
    """
    if request.method in SAFE_METHODS:
        return

    if not csrf_matches(principal.csrf_token, request.headers.get(CSRF_HEADER)):
        raise HTTPException(status_code=403, detail=f"Missing or invalid {CSRF_HEADER}.")


PrincipalDep = Annotated[Principal, Depends(require_principal)]
AdminDep = Annotated[Principal, Depends(require_admin)]
OptionalPrincipalDep = Annotated[Principal | None, Depends(current_principal)]
CsrfDep = Annotated[None, Depends(verify_csrf)]
