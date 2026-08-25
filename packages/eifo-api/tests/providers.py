"""Stand-ins for Google and X.

Real provider responses, minus the network: a genuinely signed ID token backed
by a key set we serve ourselves, so the callback exercises the same signature,
issuer, audience and expiry checks it will in production.
"""

from __future__ import annotations

import datetime as dt
from functools import lru_cache
from typing import Any

import httpx
import respx
from joserfc import jwt
from joserfc.jwk import KeySet, RSAKey

from eifo_api.oauth import GoogleProvider, XProvider

GOOGLE_CLIENT_ID = "google-client.apps.googleusercontent.com"
GOOGLE_CLIENT_SECRET = "google-secret"
X_CLIENT_ID = "x-client"
X_CLIENT_SECRET = "x-secret"

GOOGLE_SUBJECT = "108100000000000000042"
#: The address the mocked Google login vouches for, which is what decides
#: whether the account is an administrator.
GOOGLE_EMAIL = "viewer@example.com"
X_SUBJECT = "1723456789"

KEY_ID = "test-signing-key"


@lru_cache(maxsize=1)
def signing_key() -> RSAKey:
    """One key for the whole suite; generating RSA keys is not free."""
    return RSAKey.generate_key(2048, parameters={"kid": KEY_ID})


def jwks() -> dict[str, Any]:
    """The public half, in the shape Google publishes it."""
    return KeySet([signing_key()]).as_dict(private=False)


def id_token(**overrides: Any) -> str:
    """A signed Google ID token, valid unless a test bends one of the claims."""
    now = dt.datetime.now(tz=dt.UTC)
    claims: dict[str, Any] = {
        "iss": "https://accounts.google.com",
        "aud": GOOGLE_CLIENT_ID,
        "sub": GOOGLE_SUBJECT,
        "email": GOOGLE_EMAIL,
        "email_verified": True,
        "name": "תמר לוי",
        "picture": "https://lh3.googleusercontent.com/a/avatar",
        "iat": int(now.timestamp()),
        "exp": int((now + dt.timedelta(hours=1)).timestamp()),
    }
    claims.update(overrides)
    return jwt.encode({"alg": "RS256", "kid": KEY_ID}, claims, signing_key())


def mock_google(*, token: str | None = None, keys: dict[str, Any] | None = None) -> None:
    """Route Google's token and key endpoints at this process."""
    respx.get(GoogleProvider.jwks_url).mock(
        return_value=httpx.Response(200, json=keys if keys is not None else jwks())
    )
    respx.post(GoogleProvider.token_url).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "google-access-token",
                "token_type": "Bearer",
                "expires_in": 3599,
                "id_token": token if token is not None else id_token(),
            },
        )
    )


def mock_x(*, user: dict[str, Any] | None = None) -> None:
    """Route X's token and user endpoints at this process."""
    respx.post(XProvider.token_url).mock(
        return_value=httpx.Response(
            200,
            json={"access_token": "x-access-token", "token_type": "bearer", "expires_in": 7200},
        )
    )
    respx.get(XProvider.me_url).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": user
                if user is not None
                else {
                    "id": X_SUBJECT,
                    "name": "Tamar Levi",
                    "username": "tamarlevi",
                    "profile_image_url": "https://pbs.twimg.com/profile_images/avatar.jpg",
                }
            },
        )
    )
