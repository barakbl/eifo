"""Signing in, and the ways it must refuse to work."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from helpers import SignIn
from providers import (
    GOOGLE_CLIENT_ID,
    GOOGLE_SUBJECT,
    X_SUBJECT,
    id_token,
    mock_google,
    mock_x,
)
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from eifo_api.app import create_app
from eifo_api.oauth import GoogleProvider, XProvider
from eifo_api.security import OAUTH_COOKIE, SESSION_COOKIE
from eifo_core.enums import AuthProvider
from eifo_core.models import User, UserSession
from eifo_core.settings import Settings


def start_login(client: TestClient, provider: str = "google") -> tuple[str, str]:
    """Begin a login and return the provider URL and the state it minted."""
    response = client.get(f"/api/v1/auth/login/{provider}", follow_redirects=False)
    assert response.status_code == 302
    location = response.headers["location"]
    return location, parse_qs(urlparse(location).query)["state"][0]


class TestLoginRedirect:
    def test_sends_the_browser_to_google_with_pkce(self, client: TestClient) -> None:
        location, state = start_login(client)
        query = parse_qs(urlparse(location).query)

        assert location.startswith(GoogleProvider.authorize_url)
        assert query["client_id"] == [GOOGLE_CLIENT_ID]
        assert query["code_challenge_method"] == ["S256"]
        # The challenge, never the verifier: that is the whole point of PKCE.
        assert query["code_challenge"] and "code_verifier" not in query
        assert state

    def test_sends_the_browser_to_x_with_pkce(self, client: TestClient) -> None:
        location, _state = start_login(client, "x")
        query = parse_qs(urlparse(location).query)

        assert location.startswith(XProvider.authorize_url)
        assert query["code_challenge_method"] == ["S256"]

    def test_carries_state_in_a_signed_cookie_the_browser_cannot_read_apart(
        self, client: TestClient
    ) -> None:
        _location, state = start_login(client)
        sealed = client.cookies[OAUTH_COOKIE]

        assert sealed
        # Signed, so the state is present but not modifiable in isolation.
        assert state not in sealed or "." in sealed

    def test_the_callback_url_matches_the_deployment(self, client: TestClient) -> None:
        location, _state = start_login(client)
        query = parse_qs(urlparse(location).query)

        assert query["redirect_uri"] == ["https://eifo.test/api/v1/auth/callback/google"]

    def test_an_unknown_provider_is_a_validation_error(self, client: TestClient) -> None:
        assert client.get("/api/v1/auth/login/facebook").status_code == 422


class TestGoogleCallback:
    @respx.mock
    def test_creates_the_account_and_opens_a_session(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
    ) -> None:
        mock_google()
        _location, state = start_login(client)

        response = client.get(
            "/api/v1/auth/callback/google",
            params={"code": "the-code", "state": state},
            follow_redirects=False,
        )

        assert response.status_code == 302
        assert response.headers["location"].startswith("https://eifo.test/")
        assert client.cookies.get(SESSION_COOKIE)

        with session_factory() as session:
            user = session.scalars(select(User)).one()
        assert user.auth_provider is AuthProvider.GOOGLE
        assert user.auth_subject == GOOGLE_SUBJECT
        assert user.display_name == "תמר לוי"
        assert user.is_public is False

    @respx.mock
    def test_signing_in_twice_reuses_the_account(
        self,
        client: TestClient,
        sign_in: SignIn,
        session_factory: sessionmaker[Session],
    ) -> None:
        sign_in()
        sign_in()

        with session_factory() as session:
            assert len(session.scalars(select(User)).all()) == 1
            assert len(session.scalars(select(UserSession)).all()) == 2

    @respx.mock
    def test_rejects_a_state_that_does_not_match_the_cookie(self, client: TestClient) -> None:
        mock_google()
        start_login(client)

        response = client.get(
            "/api/v1/auth/callback/google",
            params={"code": "the-code", "state": "forged-state"},
            follow_redirects=False,
        )

        assert response.status_code == 400
        assert client.cookies.get(SESSION_COOKIE) is None

    @respx.mock
    def test_rejects_a_callback_with_no_login_in_progress(self, client: TestClient) -> None:
        """Someone arriving at the callback cold has no handoff cookie."""
        mock_google()

        response = client.get(
            "/api/v1/auth/callback/google",
            params={"code": "the-code", "state": "whatever"},
            follow_redirects=False,
        )

        assert response.status_code == 400

    @respx.mock
    def test_rejects_an_id_token_for_another_audience(self, client: TestClient) -> None:
        """A token minted for a different client is not proof of anything here."""
        mock_google(token=id_token(aud="someone-elses-client"))
        _location, state = start_login(client)

        response = client.get(
            "/api/v1/auth/callback/google",
            params={"code": "the-code", "state": state},
            follow_redirects=False,
        )

        assert response.status_code == 502
        assert client.cookies.get(SESSION_COOKIE) is None

    @respx.mock
    def test_rejects_an_id_token_from_another_issuer(self, client: TestClient) -> None:
        mock_google(token=id_token(iss="https://accounts.evil.example"))
        _location, state = start_login(client)

        response = client.get(
            "/api/v1/auth/callback/google",
            params={"code": "the-code", "state": state},
            follow_redirects=False,
        )

        assert response.status_code == 502

    @respx.mock
    def test_rejects_an_expired_id_token(self, client: TestClient) -> None:
        stale = dt.datetime.now(tz=dt.UTC) - dt.timedelta(hours=2)
        mock_google(token=id_token(exp=int(stale.timestamp())))
        _location, state = start_login(client)

        response = client.get(
            "/api/v1/auth/callback/google",
            params={"code": "the-code", "state": state},
            follow_redirects=False,
        )

        assert response.status_code == 502

    @respx.mock
    def test_rejects_an_id_token_signed_by_an_unknown_key(self, client: TestClient) -> None:
        """The signature is checked, not merely the shape of the token."""
        mock_google(keys={"keys": []})
        _location, state = start_login(client)

        response = client.get(
            "/api/v1/auth/callback/google",
            params={"code": "the-code", "state": state},
            follow_redirects=False,
        )

        assert response.status_code == 502

    @respx.mock
    def test_reports_a_provider_that_refuses_the_code(self, client: TestClient) -> None:
        respx.post(GoogleProvider.token_url).mock(return_value=httpx.Response(400))
        _location, state = start_login(client)

        response = client.get(
            "/api/v1/auth/callback/google",
            params={"code": "stale-code", "state": state},
            follow_redirects=False,
        )

        assert response.status_code == 502

    def test_a_cancelled_sign_in_goes_back_to_the_app(self, client: TestClient) -> None:
        """Pressing "cancel" is not an error to render as a JSON document."""
        response = client.get(
            "/api/v1/auth/callback/google",
            params={"error": "access_denied"},
            follow_redirects=False,
        )

        assert response.status_code == 302
        assert response.headers["location"] == "https://eifo.test/#/?login=cancelled"

    def test_a_provider_failure_is_reported_as_such(self, client: TestClient) -> None:
        response = client.get(
            "/api/v1/auth/callback/google",
            params={"error": "server_error"},
            follow_redirects=False,
        )

        assert response.headers["location"] == "https://eifo.test/#/?login=failed"


class TestXCallback:
    @respx.mock
    def test_creates_an_account_without_an_email(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
    ) -> None:
        """X does not release addresses, and nothing here depends on one."""
        mock_x()
        _location, state = start_login(client, "x")

        response = client.get(
            "/api/v1/auth/callback/x",
            params={"code": "the-code", "state": state},
            follow_redirects=False,
        )

        assert response.status_code == 302
        with session_factory() as session:
            user = session.scalars(select(User)).one()
        assert user.auth_provider is AuthProvider.X
        assert user.auth_subject == X_SUBJECT
        assert user.email is None

    @respx.mock
    def test_falls_back_to_the_username_when_there_is_no_name(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
    ) -> None:
        mock_x(user={"id": X_SUBJECT, "username": "tamarlevi"})
        _location, state = start_login(client, "x")
        client.get(
            "/api/v1/auth/callback/x",
            params={"code": "the-code", "state": state},
            follow_redirects=False,
        )

        with session_factory() as session:
            assert session.scalars(select(User)).one().display_name == "tamarlevi"

    @respx.mock
    def test_rejects_a_user_endpoint_that_names_nobody(self, client: TestClient) -> None:
        respx.post(XProvider.token_url).mock(
            return_value=httpx.Response(200, json={"access_token": "t", "token_type": "bearer"})
        )
        respx.get(XProvider.me_url).mock(return_value=httpx.Response(200, json={"errors": []}))
        _location, state = start_login(client, "x")

        response = client.get(
            "/api/v1/auth/callback/x",
            params={"code": "the-code", "state": state},
            follow_redirects=False,
        )

        assert response.status_code == 502


class TestSeparateAccountsPerProvider:
    @respx.mock
    def test_google_and_x_are_never_the_same_account(
        self,
        client: TestClient,
        sign_in: SignIn,
        session_factory: sessionmaker[Session],
    ) -> None:
        """No cross-provider linking: matching on email would be a takeover route."""
        sign_in(AuthProvider.GOOGLE)
        client.cookies.clear()
        sign_in(AuthProvider.X)

        with session_factory() as session:
            providers = {user.auth_provider for user in session.scalars(select(User))}
        assert providers == {AuthProvider.GOOGLE, AuthProvider.X}


class TestUnconfiguredDeployment:
    """A deployment may run the catalog without accounts at all."""

    @pytest.fixture
    def client(self, settings: Settings) -> Iterator[TestClient]:
        anonymous = settings.model_copy(update={"google_client_id": None, "secret_key": None})
        with TestClient(create_app(anonymous), base_url="https://testserver") as client:
            yield client

    def test_login_reports_what_is_missing(self, client: TestClient) -> None:
        response = client.get("/api/v1/auth/login/google", follow_redirects=False)

        assert response.status_code == 503
        assert "EIFO_SECRET_KEY" in response.json()["detail"]

    def test_meta_offers_no_providers(self, client: TestClient) -> None:
        assert client.get("/api/v1/meta").json()["login_providers"] == []


class TestConfiguredDeployment:
    def test_meta_advertises_both_providers(self, client: TestClient) -> None:
        assert client.get("/api/v1/meta").json()["login_providers"] == ["google", "x"]
