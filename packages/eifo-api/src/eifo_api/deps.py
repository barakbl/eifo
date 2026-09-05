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

from eifo_api import members
from eifo_api.security import (
    CSRF_HEADER,
    SAFE_METHODS,
    SESSION_COOKIE,
    bearer_token,
    csrf_matches,
    csrf_token_for,
    signing_secret,
)
from eifo_api.sessions import resolve_api_token, resolve_session
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
    """The signed-in user, and how they arrived."""

    user: User
    token_hash: str
    csrf_token: str
    #: Settled once, when the request is authenticated, rather than asked again
    #: by each handler - so there is one answer per request and one place that
    #: decides it.
    is_admin: bool = False
    #: True when the caller is a script holding an API token rather than a
    #: browser holding a cookie. What turns on it is CSRF: the attack is a
    #: browser being made to send a cookie it holds anyway, and nothing makes a
    #: browser attach somebody else's Authorization header.
    via_token: bool = False


def current_principal(
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
) -> Principal | None:
    """Who is calling, or None.

    Two ways in, and the cookie is tried first because it is how every browser
    request arrives. Deliberately silent about *why* nobody is: an expired
    cookie, a revoked token and a forged one are the same non-event to a caller.
    """
    row = resolve_session(session, request.cookies.get(SESSION_COOKIE))
    if row is not None:
        return _principal(session, settings, row.user, row.token_hash, via_token=False)

    token = resolve_api_token(session, bearer_token(request.headers.get("Authorization")))
    if token is not None:
        return _principal(session, settings, token.user, token.token_hash, via_token=True)

    return None


def _principal(
    session: Session,
    settings: Settings,
    user: User,
    token_hash: str,
    *,
    via_token: bool,
) -> Principal:
    return Principal(
        user=user,
        token_hash=token_hash,
        csrf_token=csrf_token_for(token_hash, signing_secret(settings)),
        is_admin=members.is_admin(session, settings, user.email),
        via_token=via_token,
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

    # A token holder is not a browser. CSRF is the attack where somebody else's
    # page makes a browser send the cookie it is already holding; nothing can
    # make a browser attach an Authorization header it was never given. Asking a
    # curl script for a CSRF token would be asking it to fetch a page first, for
    # no security at all.
    if principal.via_token:
        return

    if not csrf_matches(principal.csrf_token, request.headers.get(CSRF_HEADER)):
        raise HTTPException(status_code=403, detail=f"Missing or invalid {CSRF_HEADER}.")


def require_membership(
    principal: Annotated[Principal | None, Depends(current_principal)],
    settings: SettingsDep,
) -> None:
    """Refuse an anonymous caller when the catalog itself is private.

    A no-op on a public instance, which is every instance that has not set
    ``members_only``. On a private one it is the gate: the page still loads,
    because it has to in order to offer a sign-in button, and everything with
    catalog in it answers 401 until somebody is behind it.
    """
    if not settings.members_only or principal is not None:
        return
    raise HTTPException(status_code=401, detail="This catalog is for members. Please sign in.")


PrincipalDep = Annotated[Principal, Depends(require_principal)]
AdminDep = Annotated[Principal, Depends(require_admin)]
OptionalPrincipalDep = Annotated[Principal | None, Depends(current_principal)]
CsrfDep = Annotated[None, Depends(verify_csrf)]
