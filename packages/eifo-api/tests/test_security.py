"""The security primitives, tested away from any request."""

from __future__ import annotations

import time

import pytest
from helpers import SECRET_KEY
from itsdangerous import TimestampSigner, URLSafeTimedSerializer

from eifo_api.security import (
    OAuthHandoff,
    csrf_matches,
    csrf_token_for,
    hash_token,
    new_session_token,
    seal_handoff,
    signing_secret,
    unseal_handoff,
)
from eifo_core.settings import Settings

HANDOFF = OAuthHandoff(provider="google", state="the-state", code_verifier="the-verifier")


class TestTokens:
    def test_every_session_token_is_different(self) -> None:
        assert len({new_session_token() for _ in range(100)}) == 100

    def test_hashing_is_stable_and_one_way(self) -> None:
        token = new_session_token()

        assert hash_token(token) == hash_token(token)
        assert token not in hash_token(token)


class TestCsrfTokens:
    def test_a_token_belongs_to_exactly_one_session(self) -> None:
        mine = csrf_token_for(hash_token("a"), SECRET_KEY)
        yours = csrf_token_for(hash_token("b"), SECRET_KEY)

        assert mine != yours
        assert csrf_matches(mine, mine)
        assert not csrf_matches(mine, yours)

    def test_a_different_secret_produces_a_different_token(self) -> None:
        assert csrf_token_for(hash_token("a"), SECRET_KEY) != csrf_token_for(
            hash_token("a"), "another-secret"
        )

    @pytest.mark.parametrize("provided", [None, ""])
    def test_a_missing_token_is_a_mismatch(self, provided: str | None) -> None:
        assert not csrf_matches(csrf_token_for(hash_token("a"), SECRET_KEY), provided)


class TestOAuthHandoff:
    def test_survives_the_round_trip(self) -> None:
        assert unseal_handoff(seal_handoff(HANDOFF, SECRET_KEY), SECRET_KEY) == HANDOFF

    def test_a_tampered_cookie_is_nobody(self) -> None:
        sealed = seal_handoff(HANDOFF, SECRET_KEY)

        assert unseal_handoff(sealed + "x", SECRET_KEY) is None

    def test_another_deployments_cookie_is_nobody(self) -> None:
        assert unseal_handoff(seal_handoff(HANDOFF, "another-secret"), SECRET_KEY) is None

    def test_an_absent_cookie_is_nobody(self) -> None:
        assert unseal_handoff(None, SECRET_KEY) is None

    def test_a_stale_cookie_is_nobody(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Ten minutes is a login, not a browsing session."""
        monkeypatch.setattr(TimestampSigner, "get_timestamp", lambda _self: int(time.time()) - 3600)
        sealed = seal_handoff(HANDOFF, SECRET_KEY)
        monkeypatch.undo()

        assert unseal_handoff(sealed, SECRET_KEY) is None

    def test_a_payload_of_the_wrong_shape_is_nobody(self) -> None:
        """A cookie from an older release must expire a login, not crash one."""
        stale_shape = URLSafeTimedSerializer(SECRET_KEY, salt="eifo.oauth.state").dumps(
            {"provider": "google"}
        )

        assert unseal_handoff(stale_shape, SECRET_KEY) is None

    def test_a_payload_that_is_not_an_object_is_nobody(self) -> None:
        not_an_object = URLSafeTimedSerializer(SECRET_KEY, salt="eifo.oauth.state").dumps("nope")

        assert unseal_handoff(not_an_object, SECRET_KEY) is None


class TestSigningSecret:
    @pytest.mark.parametrize("value", [None, "   "])
    def test_a_deployment_without_one_cannot_sign_anything(self, value: str | None) -> None:
        settings = Settings(_env_file=None, secret_key=value)

        with pytest.raises(Exception, match="EIFO_SECRET_KEY"):
            signing_secret(settings)

    def test_a_configured_deployment_returns_it(self) -> None:
        settings = Settings(_env_file=None, secret_key=SECRET_KEY)

        assert signing_secret(settings) == SECRET_KEY
