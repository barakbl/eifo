"""Sign in, sign out.

The provider round trip carries two secrets that must survive it: the ``state``
that proves the callback belongs to this login, and the PKCE verifier that
proves it belongs to this client. Both ride in one short-lived signed cookie
rather than in server memory, so a restart mid-login is merely a retry.
"""

from __future__ import annotations

import logging
import secrets

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from eifo_api import members
from eifo_api.deps import CsrfDep, PrincipalDep, SessionDep, SettingsDep
from eifo_api.oauth import Identity, OAuthError, build_provider, configured_providers
from eifo_api.schemas import AuthContext
from eifo_api.security import (
    OAUTH_COOKIE,
    OAuthHandoff,
    clear_oauth_cookie,
    clear_session_cookie,
    seal_handoff,
    set_oauth_cookie,
    set_session_cookie,
    signing_secret,
    unseal_handoff,
)
from eifo_api.sessions import end_session, start_session
from eifo_core.enums import AuthProvider
from eifo_core.models import User
from eifo_core.settings import Settings
from eifo_core.types import utcnow

router = APIRouter(prefix="/auth", tags=["auth"])

#: The outcome for an address that completed a provider sign-in but is not on
#: the allowlist. Not one of the provider's errors - nothing went wrong at the
#: provider - so it is named here.
NOT_INVITED = "not_invited"

logger = logging.getLogger("eifo.api.auth")

#: Long enough that guessing is hopeless; it only has to survive one redirect.
STATE_BYTES = 32
VERIFIER_BYTES = 48

#: Provider-side outcomes that mean "the person changed their mind", which is
#: not an error to shout about - they go back to the app.
CANCELLED_ERRORS = frozenset({"access_denied", "user_cancelled_login", "user_cancelled_authorize"})


@router.get("/context", response_model=AuthContext, summary="How to get in")
def context(settings: SettingsDep) -> AuthContext:
    """Whether this instance is private, and which providers can open it.

    Deliberately outside the gate, and deliberately carrying nothing about the
    catalog - no counts, no source names, nothing that says what is inside. It
    exists because the sign-in wall needs two facts to render, and both of them
    used to arrive on `/meta`: a members-only instance answered 401 there and
    the client was left showing an error with no button on it. A gate whose key
    is behind the gate is not a gate.
    """
    return AuthContext(
        members_only=settings.members_only,
        login_providers=configured_providers(settings),
    )


@router.get("/login/{provider}", summary="Start sign-in with a provider")
def login(provider: AuthProvider, settings: SettingsDep) -> RedirectResponse:
    """Redirect to the provider, remembering state and PKCE on the way out."""
    secret = signing_secret(settings)
    handoff = OAuthHandoff(
        provider=provider.value,
        state=secrets.token_urlsafe(STATE_BYTES),
        code_verifier=secrets.token_urlsafe(VERIFIER_BYTES),
    )

    url = build_provider(provider, settings).authorization_url(
        state=handoff.state,
        code_verifier=handoff.code_verifier,
    )

    response = RedirectResponse(url, status_code=302)
    set_oauth_cookie(response, seal_handoff(handoff, secret), settings)
    return response


@router.get("/callback/{provider}", summary="Complete sign-in")
def callback(
    provider: AuthProvider,
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> RedirectResponse:
    """Verify the callback, upsert the account and open a session."""
    if error is not None:
        return _abandon(error, settings)

    handoff = unseal_handoff(request.cookies.get(OAUTH_COOKIE), signing_secret(settings))
    if handoff is None or handoff.provider != provider.value:
        raise HTTPException(status_code=400, detail="This sign-in has expired. Please try again.")
    if not code or not state or not secrets.compare_digest(state, handoff.state):
        raise HTTPException(status_code=400, detail="This sign-in could not be verified.")

    client = build_provider(provider, settings)
    try:
        identity = client.identity(
            client.exchange(code=code, state=state, code_verifier=handoff.code_verifier)
        )
    except OAuthError:
        logger.warning("sign-in failed", extra={"provider": provider.value}, exc_info=True)
        raise HTTPException(
            status_code=502, detail="The sign-in provider could not confirm who you are."
        ) from None

    # Checked before anything is written. An instance that has decided who may
    # come in should not be storing rows about people it is turning away - and
    # a `users` row created here would be exactly that, complete with the name
    # and address of somebody who was refused.
    if not members.may_sign_in(session, settings, identity.email):
        logger.info("sign-in refused: not invited", extra={"provider": provider.value})
        return _abandon(NOT_INVITED, settings)

    user = _upsert_user(session, identity)
    token = start_session(session, user)

    response = RedirectResponse(_app_url(settings), status_code=302)
    set_session_cookie(response, token, settings)
    clear_oauth_cookie(response, settings)
    return response


@router.post("/logout", status_code=204, summary="End this session")
def logout(
    principal: PrincipalDep,
    _csrf: CsrfDep,
    session: SessionDep,
    settings: SettingsDep,
) -> Response:
    """Revoke the session server-side, not merely on the client."""
    end_session(session, principal.token_hash)

    response = Response(status_code=204)
    clear_session_cookie(response, settings)
    return response


def _upsert_user(session: Session, identity: Identity) -> User:
    """Find or create the account for an identity.

    Keyed on ``(provider, subject)`` only. Matching on email instead would let
    whoever controls an address take over an account on another provider, so a
    Google login and an X login stay separate accounts even for the same person.
    """
    user = session.scalars(
        select(User).where(
            User.auth_provider == identity.provider,
            User.auth_subject == identity.subject,
        )
    ).one_or_none()

    if user is None:
        user = User(
            auth_provider=identity.provider,
            auth_subject=identity.subject,
            display_name=identity.display_name or _fallback_name(identity),
            email=identity.email,
            avatar_url=identity.avatar_url,
        )
        session.add(user)
        session.commit()
        return user

    # Refreshed on every login: the provider is the authority for these, except
    # a display name the user has since chosen for themselves.
    user.email = identity.email
    user.avatar_url = identity.avatar_url
    user.last_login_at = utcnow()
    session.commit()
    return user


def _fallback_name(identity: Identity) -> str:
    """A name for a provider that sent none, rather than an empty header."""
    return f"{identity.provider.value}-{identity.subject[-6:]}"


def _app_url(settings: Settings, fragment: str = "") -> str:
    return f"{settings.public_origin.rstrip('/')}/{fragment}"


def _abandon(error: str, settings: Settings) -> RedirectResponse:
    """Send an aborted sign-in back to the app instead of to a JSON document.

    Someone who pressed "cancel" is not looking at a problem document; they are
    looking for the page they came from.
    """
    # Three outcomes, because they need three different sentences. "Sign-in
    # failed, please try again" is actively unhelpful to somebody who was not
    # invited: trying again is the one thing that will never work.
    if error == NOT_INVITED:
        return RedirectResponse(_app_url(settings, f"#/?login={NOT_INVITED}"), status_code=302)

    cancelled = error in CANCELLED_ERRORS
    if not cancelled:
        logger.info("provider refused sign-in", extra={"oauth_error": error})

    outcome = "cancelled" if cancelled else "failed"
    return RedirectResponse(_app_url(settings, f"#/?login={outcome}"), status_code=302)
