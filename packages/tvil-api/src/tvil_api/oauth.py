"""Google and X sign-in.

Two providers, one shape: build an authorization URL, exchange the code, and
turn whatever comes back into an :class:`Identity`. What differs is only the
last step — Google states who you are in a signed ID token, X wants a second
call to its API — so that is the only method the subclasses implement.

Authlib drives the OAuth2 half (PKCE, client authentication, the token
request); the ID token is verified with joserfc, which is the library Authlib
now defers to for JOSE.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, ClassVar

import httpx
from authlib.integrations.httpx_client import OAuth2Client
from joserfc import jwt
from joserfc.jwk import KeySet

from tvil_api.security import LoginNotConfiguredError
from tvil_core.enums import AuthProvider
from tvil_core.settings import Settings

#: Every request out of this module is a login blocking a person at a redirect.
TIMEOUT_SECONDS = 10.0

GOOGLE_ISSUERS = frozenset({"accounts.google.com", "https://accounts.google.com"})
GOOGLE_ID_TOKEN_ALGORITHMS = ["RS256"]


class OAuthError(RuntimeError):
    """The provider did not complete the exchange, or answered nonsensically."""


@dataclass(frozen=True)
class Identity:
    """Who a provider says the person at the other end is.

    ``email`` is optional throughout: X frequently omits it, and TVIL neither
    needs nor displays it.
    """

    provider: AuthProvider
    subject: str
    display_name: str
    email: str | None = None
    avatar_url: str | None = None


class OAuthProvider(ABC):
    """One sign-in provider."""

    key: ClassVar[AuthProvider]
    authorize_url: ClassVar[str]
    token_url: ClassVar[str]
    scope: ClassVar[str]

    def __init__(self, client_id: str, client_secret: str, redirect_uri: str) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri

    def authorization_url(self, *, state: str, code_verifier: str) -> str:
        """Where to send the browser. PKCE is used for both providers.

        X requires it; Google supports it, and there is no reason to protect one
        authorization code less carefully than the other.
        """
        with self._client() as client:
            url, _ = client.create_authorization_url(
                self.authorize_url,
                state=state,
                code_verifier=code_verifier,
                code_challenge_method="S256",
            )
        return str(url)

    def exchange(self, *, code: str, state: str, code_verifier: str) -> dict[str, Any]:
        """Trade the authorization code for tokens."""
        try:
            with self._client() as client:
                token: dict[str, Any] = client.fetch_token(
                    self.token_url,
                    code=code,
                    state=state,
                    code_verifier=code_verifier,
                    grant_type="authorization_code",
                )
        except (httpx.HTTPError, ValueError) as cause:
            raise OAuthError(f"{self.key} rejected the authorization code") from cause

        return token

    @abstractmethod
    def identity(self, token: dict[str, Any]) -> Identity:
        """Who the token belongs to."""

    def _client(self) -> OAuth2Client:
        return OAuth2Client(
            client_id=self.client_id,
            client_secret=self.client_secret,
            scope=self.scope,
            redirect_uri=self.redirect_uri,
            code_challenge_method="S256",
            timeout=TIMEOUT_SECONDS,
        )


class GoogleProvider(OAuthProvider):
    """OpenID Connect. Identity arrives in a signed ID token."""

    key = AuthProvider.GOOGLE
    authorize_url = "https://accounts.google.com/o/oauth2/v2/auth"
    token_url = "https://oauth2.googleapis.com/token"
    #: The minimum that identifies an account; no Drive, no contacts, nothing else.
    scope = "openid email profile"
    jwks_url: ClassVar[str] = "https://www.googleapis.com/oauth2/v3/certs"

    def identity(self, token: dict[str, Any]) -> Identity:
        raw = token.get("id_token")
        if not isinstance(raw, str):
            raise OAuthError("Google returned no ID token")

        claims = self._verified_claims(raw)
        subject = claims.get("sub")
        if not subject:
            raise OAuthError("Google's ID token carries no subject")

        return Identity(
            provider=self.key,
            subject=str(subject),
            display_name=str(claims.get("name") or claims.get("email") or "").strip(),
            email=claims.get("email"),
            avatar_url=claims.get("picture"),
        )

    def _verified_claims(self, raw: str) -> dict[str, Any]:
        """Signature, issuer, audience and expiry — all of it, or nothing.

        An unverified ID token is an attacker-supplied JSON document: whoever
        holds one can claim to be anyone.
        """
        try:
            token = jwt.decode(raw, self._keys(), algorithms=GOOGLE_ID_TOKEN_ALGORITHMS)
            jwt.JWTClaimsRegistry(
                iss={"essential": True, "values": sorted(GOOGLE_ISSUERS)},
                aud={"essential": True, "value": self.client_id},
                exp={"essential": True},
            ).validate(token.claims)
        except Exception as cause:  # joserfc raises a family of decode errors
            raise OAuthError("Google's ID token failed verification") from cause

        return dict(token.claims)

    def _keys(self) -> KeySet:
        """Google's current signing keys.

        Fetched per login rather than cached: logins are rare next to catalog
        reads, and a cache that outlives a key rotation locks everyone out.
        """
        try:
            response = httpx.get(self.jwks_url, timeout=TIMEOUT_SECONDS)
            response.raise_for_status()
            return KeySet.import_key_set(response.json())
        except (httpx.HTTPError, ValueError) as cause:
            raise OAuthError("Google's signing keys could not be fetched") from cause


class XProvider(OAuthProvider):
    """OAuth 2.0 with PKCE. Identity needs a second call, and carries no email."""

    key = AuthProvider.X
    authorize_url = "https://x.com/i/oauth2/authorize"
    token_url = "https://api.x.com/2/oauth2/token"
    scope = "users.read tweet.read"
    me_url: ClassVar[str] = "https://api.x.com/2/users/me"

    def identity(self, token: dict[str, Any]) -> Identity:
        access_token = token.get("access_token")
        if not isinstance(access_token, str):
            raise OAuthError("X returned no access token")

        data = self._me(access_token)
        subject = data.get("id")
        if not subject:
            raise OAuthError("X returned no user id")

        return Identity(
            provider=self.key,
            subject=str(subject),
            display_name=str(data.get("name") or data.get("username") or "").strip(),
            # X does not release email addresses; users.email stays null.
            email=None,
            avatar_url=data.get("profile_image_url"),
        )

    def _me(self, access_token: str) -> dict[str, Any]:
        try:
            response = httpx.get(
                self.me_url,
                params={"user.fields": "profile_image_url"},
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as cause:
            raise OAuthError("X's user endpoint could not be read") from cause

        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            raise OAuthError("X's user endpoint returned no user")
        return data


PROVIDERS: dict[AuthProvider, type[OAuthProvider]] = {
    AuthProvider.GOOGLE: GoogleProvider,
    AuthProvider.X: XProvider,
}

#: Which settings each provider needs, so a missing one names itself.
_CREDENTIALS = {
    AuthProvider.GOOGLE: ("google_client_id", "google_client_secret"),
    AuthProvider.X: ("x_client_id", "x_client_secret"),
}


def redirect_uri(provider: AuthProvider, settings: Settings) -> str:
    """The callback this deployment registers with the provider."""
    return f"{settings.public_origin.rstrip('/')}/api/v1/auth/callback/{provider.value}"


def build_provider(provider: AuthProvider, settings: Settings) -> OAuthProvider:
    """A configured provider, or a clear failure about what is missing."""
    id_name, secret_name = _CREDENTIALS[provider]
    client_id = _secret(settings, id_name)
    client_secret = _secret(settings, secret_name)

    return PROVIDERS[provider](
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri(provider, settings),
    )


def configured_providers(settings: Settings) -> list[AuthProvider]:
    """Providers this deployment can actually offer, for the client to render."""
    return [
        provider
        for provider, names in _CREDENTIALS.items()
        if all(_is_set(settings, name) for name in names)
        and settings.secret_key is not None
        and settings.secret_key.get_secret_value().strip()
    ]


def _is_set(settings: Settings, name: str) -> bool:
    value = getattr(settings, name)
    return value is not None and bool(value.get_secret_value().strip())


def _secret(settings: Settings, name: str) -> str:
    if not _is_set(settings, name):
        raise LoginNotConfiguredError(f"TVIL_{name.upper()}")
    value: str = getattr(settings, name).get_secret_value()
    return value
