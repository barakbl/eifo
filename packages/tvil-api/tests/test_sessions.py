"""Sessions, CSRF and logout."""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient
from helpers import SECRET_KEY, SignIn
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from tvil_api.security import (
    CSRF_HEADER,
    SESSION_COOKIE,
    SESSION_RENEW_AFTER,
    SESSION_TTL,
    csrf_token_for,
    hash_token,
    new_session_token,
)
from tvil_api.sessions import resolve_session, start_session
from tvil_core.enums import AuthProvider
from tvil_core.models import User, UserSession
from tvil_core.types import utcnow


class TestSessionCookie:
    def test_login_sets_a_cookie_the_page_javascript_cannot_read(
        self, client: TestClient, sign_in: SignIn
    ) -> None:
        sign_in()

        cookie = next(entry for entry in client.cookies.jar if entry.name == SESSION_COOKIE)
        assert cookie.has_nonstandard_attr("HttpOnly")
        assert cookie.secure
        assert cookie.get_nonstandard_attr("SameSite", "").lower() == "lax"

    def test_only_the_hash_of_the_token_is_stored(
        self,
        client: TestClient,
        sign_in: SignIn,
        session_factory: sessionmaker[Session],
    ) -> None:
        """A stolen copy of the database must not be replayable as a login."""
        sign_in()
        token = client.cookies[SESSION_COOKIE]

        with session_factory() as session:
            row = session.scalars(select(UserSession)).one()

        assert row.token_hash != token
        assert row.token_hash == hash_token(token)

    def test_a_request_without_a_cookie_is_anonymous(self, client: TestClient) -> None:
        assert client.get("/api/v1/me").status_code == 401

    def test_a_forged_cookie_is_anonymous(self, client: TestClient) -> None:
        client.cookies.set(SESSION_COOKIE, new_session_token())

        assert client.get("/api/v1/me").status_code == 401


class TestSessionLifecycle:
    def test_an_expired_session_stops_working_and_is_cleaned_up(
        self,
        client: TestClient,
        sign_in: SignIn,
        session_factory: sessionmaker[Session],
    ) -> None:
        sign_in()
        _expire_sessions(session_factory)

        assert client.get("/api/v1/me").status_code == 401
        with session_factory() as session:
            assert session.scalars(select(UserSession)).all() == []

    def test_use_slides_the_expiry_forward(self, session_factory: sessionmaker[Session]) -> None:
        with session_factory() as session:
            user = _user(session)
            long_ago = utcnow() - SESSION_RENEW_AFTER - dt.timedelta(hours=1)
            token = start_session(session, user, now=long_ago)

            row = resolve_session(session, token)

            assert row is not None
            assert row.expires_at > long_ago + SESSION_TTL

    def test_a_session_used_moments_ago_is_not_rewritten(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        """Sliding on every request would make each read a write."""
        with session_factory() as session:
            user = _user(session)
            token = start_session(session, user)
            first = session.scalars(select(UserSession)).one().expires_at

            resolve_session(session, token)

            assert session.scalars(select(UserSession)).one().expires_at == first

    def test_resolving_nothing_is_nobody(self, session_factory: sessionmaker[Session]) -> None:
        with session_factory() as session:
            assert resolve_session(session, None) is None


class TestLogout:
    def test_revokes_the_session_server_side(
        self,
        client: TestClient,
        sign_in: SignIn,
        session_factory: sessionmaker[Session],
    ) -> None:
        """Not merely dropping the cookie: a copy of it must stop working too."""
        csrf = sign_in()
        token = client.cookies[SESSION_COOKIE]

        assert client.post("/api/v1/auth/logout", headers={CSRF_HEADER: csrf}).status_code == 204

        with session_factory() as session:
            assert session.scalars(select(UserSession)).all() == []

        client.cookies.set(SESSION_COOKIE, token)
        assert client.get("/api/v1/me").status_code == 401

    def test_requires_a_session(self, client: TestClient) -> None:
        assert client.post("/api/v1/auth/logout").status_code == 401

    def test_requires_the_csrf_token(self, client: TestClient, sign_in: SignIn) -> None:
        sign_in()

        assert client.post("/api/v1/auth/logout").status_code == 403


class TestCsrf:
    @pytest.fixture
    def csrf(self, sign_in: SignIn) -> str:
        return sign_in()

    def test_me_hands_out_the_token(self, client: TestClient, csrf: str) -> None:
        assert csrf and len(csrf) == 64

    def test_a_write_without_the_header_is_refused(self, client: TestClient, csrf: str) -> None:
        response = client.patch("/api/v1/me", json={"display_name": "חדש"})

        assert response.status_code == 403
        assert CSRF_HEADER in response.json()["detail"]

    def test_a_write_with_the_header_is_accepted(self, client: TestClient, csrf: str) -> None:
        response = client.patch(
            "/api/v1/me",
            json={"display_name": "חדש"},
            headers={CSRF_HEADER: csrf},
        )

        assert response.status_code == 200
        assert response.json()["display_name"] == "חדש"

    def test_another_session_token_does_not_work(self, client: TestClient, csrf: str) -> None:
        """The token is derived from the session, so it is useless elsewhere."""
        other = csrf_token_for(hash_token(new_session_token()), SECRET_KEY)

        response = client.patch(
            "/api/v1/me",
            json={"display_name": "חדש"},
            headers={CSRF_HEADER: other},
        )

        assert response.status_code == 403

    def test_reads_need_no_token(self, client: TestClient, csrf: str) -> None:
        assert client.get("/api/v1/me/items").status_code == 200


def _user(session: Session) -> User:
    """An account created directly, for tests about sessions rather than login."""
    user = User(
        auth_provider=AuthProvider.GOOGLE,
        auth_subject="direct-subject",
        display_name="צופה",
    )
    session.add(user)
    session.commit()
    return user


def _expire_sessions(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as session:
        for row in session.scalars(select(UserSession)):
            row.expires_at = utcnow() - dt.timedelta(seconds=1)
        session.commit()
