"""Cookies, session tokens and CSRF tokens.

The primitives only; the database side of a session lives in ``sessions.py``
and the routes that use them in ``routers/auth.py``. Everything here is pure
enough to be tested without a request (docs.internal/09-auth-privacy.md).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import secrets
from dataclasses import asdict, dataclass
from typing import Any

from fastapi import Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from eifo_core.settings import Settings

SESSION_COOKIE = "eifo_session"
#: Carries the OAuth ``state`` and PKCE verifier across the provider round trip.
OAUTH_COOKIE = "eifo_oauth"
CSRF_HEADER = "X-CSRF-Token"
#: How an API token announces itself. Anything without it is not one of ours.
API_TOKEN_PREFIX = "eifo_pat_"

SESSION_TTL = dt.timedelta(days=30)
#: A session row is only rewritten once this much time has passed since its last
#: use, so a browsing session does not turn every read into a write.
SESSION_RENEW_AFTER = dt.timedelta(days=1)
#: The login round trip is a page load or two, never a browsing session.
OAUTH_STATE_TTL_SECONDS = 600

#: Requests that cannot change state, and so need no CSRF token.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

_OAUTH_SALT = "eifo.oauth.state"


class LoginNotConfiguredError(RuntimeError):
    """Login was attempted on a deployment that has no credentials for it.

    A deployment may legitimately run without accounts - the catalog is public -
    so this is a 503 on the auth routes rather than a startup failure.
    """

    def __init__(self, missing: str) -> None:
        self.missing = missing
        super().__init__(
            f"Login is not configured on this deployment: {missing} is unset. "
            f"See docs.internal/11-ops-install.md."
        )


def signing_secret(settings: Settings) -> str:
    """The application secret, or a clear failure when it is absent."""
    if settings.secret_key is None or not settings.secret_key.get_secret_value().strip():
        raise LoginNotConfiguredError("EIFO_SECRET_KEY")
    return settings.secret_key.get_secret_value()


def new_session_token() -> str:
    """A 256-bit session token. Only its hash is ever stored."""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """The stored form of a session or API token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_api_token() -> str:
    """A personal API token, prefixed so it is recognisable on sight.

    The prefix is not decoration. A token pasted into a script, a log or an
    issue is a credential somebody has to be able to identify as one - both to
    know to revoke it and, for the scanners that read public repositories, to
    know what they have found. It also lets the API tell a token that is not
    ours from one that is merely wrong.
    """
    return f"{API_TOKEN_PREFIX}{secrets.token_urlsafe(32)}"


def bearer_token(header: str | None) -> str | None:
    """The token out of an ``Authorization`` header, if there is one of ours.

    Only ``Bearer``, and only with our prefix. Anything else is treated as
    absent rather than as a failure: a request carrying somebody else's
    Authorization header is not a request that wanted this API's tokens, and
    answering it with 401 rather than the ordinary anonymous response would be
    a worse answer to a harmless mistake.
    """
    if not header:
        return None
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer":
        return None
    token = value.strip()
    return token if token.startswith(API_TOKEN_PREFIX) else None


def csrf_token_for(token_hash: str, secret: str) -> str:
    """The CSRF token belonging to one session.

    Derived from the session rather than stored alongside it: it is reproducible
    on every request, and a token lifted from one session is worthless against
    another.
    """
    return hmac.new(secret.encode("utf-8"), token_hash.encode("utf-8"), hashlib.sha256).hexdigest()


def csrf_matches(expected: str, provided: str | None) -> bool:
    """Constant-time comparison that treats a missing token as a mismatch."""
    if not provided:
        return False
    return hmac.compare_digest(expected, provided)


@dataclass(frozen=True)
class OAuthHandoff:
    """What the login route needs to remember while the provider has the user."""

    provider: str
    state: str
    code_verifier: str


def seal_handoff(handoff: OAuthHandoff, secret: str) -> str:
    """Sign the handoff for the browser to carry back."""
    return URLSafeTimedSerializer(secret, salt=_OAUTH_SALT).dumps(asdict(handoff))


def unseal_handoff(value: str | None, secret: str) -> OAuthHandoff | None:
    """Recover a handoff, or None if it is missing, expired or tampered with."""
    if not value:
        return None

    try:
        payload: Any = URLSafeTimedSerializer(secret, salt=_OAUTH_SALT).loads(
            value, max_age=OAUTH_STATE_TTL_SECONDS
        )
    except (BadSignature, SignatureExpired):
        return None

    if not isinstance(payload, dict):
        return None
    try:
        return OAuthHandoff(**payload)
    except TypeError:
        # An older release's payload shape; make the user log in again.
        return None


def cookies_are_secure(settings: Settings) -> bool:
    """Whether cookies may carry the Secure flag.

    Marking a cookie Secure over plain HTTP means the browser silently drops it,
    which would break login on a local install; every real deployment is HTTPS.
    """
    return settings.public_origin.startswith("https://")


def set_session_cookie(response: Response, token: str, settings: Settings) -> None:
    """Attach the session cookie, scoped as tightly as the app allows."""
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=int(SESSION_TTL.total_seconds()),
        httponly=True,
        secure=cookies_are_secure(settings),
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        SESSION_COOKIE,
        httponly=True,
        secure=cookies_are_secure(settings),
        samesite="lax",
        path="/",
    )


def set_oauth_cookie(response: Response, sealed: str, settings: Settings) -> None:
    """Attach the short-lived handoff cookie.

    SameSite=Lax rather than Strict: the browser arrives back on the callback
    from the provider's domain, and a Strict cookie would not be sent.
    """
    response.set_cookie(
        OAUTH_COOKIE,
        sealed,
        max_age=OAUTH_STATE_TTL_SECONDS,
        httponly=True,
        secure=cookies_are_secure(settings),
        samesite="lax",
        path="/",
    )


def clear_oauth_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        OAUTH_COOKIE,
        httponly=True,
        secure=cookies_are_secure(settings),
        samesite="lax",
        path="/",
    )
