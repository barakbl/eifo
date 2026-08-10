"""The privacy suite.

Mandatory per docs.internal/10-quality.md, and deliberately paranoid: these
assert on the *whole* response body rather than on the fields we remembered to
check, because the failure mode being guarded against is somebody adding a
field without thinking about who gets to see it.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest
from fastapi.testclient import TestClient
from helpers import SignIn
from seed import Seeded
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from eifo_api.caching import NO_STORE
from eifo_api.logging_privacy import (
    ACCESS_LOGGER,
    REDACTION,
    PrivateQueryFilter,
    redact_private_query,
)
from eifo_api.security import CSRF_HEADER
from eifo_core.enums import AuthProvider
from eifo_core.models import User

#: Anything that identifies the person behind the account rather than the account.
FORBIDDEN_KEYS = frozenset({"email", "auth_provider", "auth_subject", "token_hash", "password"})

USER_ROUTES = ("/api/v1/me", "/api/v1/me/items")


@pytest.fixture
def csrf(sign_in: SignIn) -> str:
    return sign_in()


def keys_anywhere(payload: Any) -> set[str]:
    """Every key in a response, however deeply nested."""
    if isinstance(payload, dict):
        found = set(payload)
        for value in payload.values():
            found |= keys_anywhere(value)
        return found
    if isinstance(payload, list):
        return set().union(*(keys_anywhere(item) for item in payload)) if payload else set()
    return set()


class TestUserResponsesCarryNoIdentity:
    def test_me_never_names_the_provider_or_the_address(
        self, client: TestClient, csrf: str
    ) -> None:
        body = client.get("/api/v1/me").json()

        assert keys_anywhere(body) & FORBIDDEN_KEYS == set()

    def test_the_address_does_not_appear_anywhere_in_the_body(
        self,
        client: TestClient,
        csrf: str,
        session_factory: sessionmaker[Session],
    ) -> None:
        """Not as a key, and not as a value under some other name either."""
        with session_factory() as session:
            email = session.scalars(select(User.email)).one()
        assert email

        raw = client.get("/api/v1/me").text

        assert email not in raw

    def test_a_profile_update_answers_with_the_same_shape(
        self, client: TestClient, csrf: str
    ) -> None:
        body = client.patch(
            "/api/v1/me", json={"display_name": "תמר"}, headers={CSRF_HEADER: csrf}
        ).json()

        assert keys_anywhere(body) & FORBIDDEN_KEYS == set()

    def test_a_list_response_carries_nothing_but_catalog_and_the_users_own_words(
        self,
        client: TestClient,
        csrf: str,
        catalog: Seeded,
    ) -> None:
        client.put(
            f"/api/v1/me/items/{catalog.fauda}",
            json={"status": "watched", "rating": 9, "note": "פרטי"},
            headers={CSRF_HEADER: csrf},
        )

        body = client.get("/api/v1/me/items").json()

        assert keys_anywhere(body) & FORBIDDEN_KEYS == set()
        assert "user_id" not in json.dumps(body)

    def test_an_account_with_no_address_is_unremarkable(
        self, client: TestClient, sign_in: SignIn
    ) -> None:
        """X sends none; the response shape must not depend on having one."""
        sign_in(AuthProvider.X)

        assert keys_anywhere(client.get("/api/v1/me").json()) & FORBIDDEN_KEYS == set()


class TestPrivateByDefault:
    def test_a_new_account_is_not_public(self, client: TestClient, csrf: str) -> None:
        assert client.get("/api/v1/me").json()["user"]["is_public"] is False

    def test_the_database_default_is_private_too(
        self,
        client: TestClient,
        csrf: str,
        session_factory: sessionmaker[Session],
    ) -> None:
        """Not merely the API's doing: a row inserted anywhere starts private."""
        with session_factory() as session:
            assert session.scalars(select(User.is_public)).one() is False


class TestNoStore:
    @pytest.mark.parametrize("path", USER_ROUTES)
    def test_user_reads_are_never_written_down(
        self, client: TestClient, csrf: str, path: str
    ) -> None:
        assert client.get(path).headers["cache-control"] == NO_STORE

    @pytest.mark.parametrize("path", USER_ROUTES)
    def test_the_rule_holds_when_nobody_is_signed_in(self, client: TestClient, path: str) -> None:
        """A 401 body is as unfit for a shared cache as the data would be."""
        response = client.get(path)

        assert response.status_code == 401
        assert response.headers["cache-control"] == NO_STORE

    def test_auth_routes_are_never_cached(self, client: TestClient) -> None:
        response = client.get("/api/v1/auth/login/google", follow_redirects=False)

        assert response.headers["cache-control"] == NO_STORE

    def test_the_catalog_is_still_cacheable(self, client: TestClient, catalog: Seeded) -> None:
        """The point is user data, not a blanket ban on caching."""
        assert "max-age" in client.get("/api/v1/titles").headers["cache-control"]

    def test_a_user_response_carries_no_etag_to_revalidate_against(
        self, client: TestClient, csrf: str
    ) -> None:
        assert "etag" not in client.get("/api/v1/me").headers


class TestLogsExcludeUserQueryStrings:
    def test_a_user_route_keeps_its_path_and_loses_its_query(self) -> None:
        redacted = redact_private_query("/api/v1/me/items?status=watched&page=2")

        assert redacted == f"/api/v1/me/items{REDACTION}"
        assert "status=watched" not in redacted

    def test_catalog_searches_are_left_alone(self) -> None:
        """What somebody searched for in public is not private data."""
        path = "/api/v1/titles?q=fauda"

        assert redact_private_query(path) == path

    def test_a_path_without_a_query_is_untouched(self) -> None:
        assert redact_private_query("/api/v1/me") == "/api/v1/me"

    def test_the_filter_rewrites_an_access_log_record(self) -> None:
        record = logging.LogRecord(
            name=ACCESS_LOGGER,
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg='%s - "%s %s HTTP/%s" %d',
            args=("127.0.0.1:1234", "GET", "/api/v1/me/items?status=watched", "1.1", 200),
            exc_info=None,
        )

        PrivateQueryFilter().filter(record)

        assert "status=watched" not in record.getMessage()
        assert "/api/v1/me/items" in record.getMessage()

    def test_a_record_with_no_arguments_is_left_alone(self) -> None:
        record = logging.LogRecord(
            name=ACCESS_LOGGER,
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="starting up",
            args=None,
            exc_info=None,
        )

        assert PrivateQueryFilter().filter(record) is True
        assert record.getMessage() == "starting up"


class TestNotesStayPrivate:
    def test_a_note_is_returned_to_its_author(
        self,
        client: TestClient,
        csrf: str,
        catalog: Seeded,
    ) -> None:
        """The other half - that nobody else sees it - arrives with profiles in S7."""
        client.put(
            f"/api/v1/me/items/{catalog.fauda}",
            json={"note": "לצפות עם אמא"},
            headers={CSRF_HEADER: csrf},
        )

        page = client.get("/api/v1/me/items").json()

        assert page["items"][0]["note"] == "לצפות עם אמא"
