"""The remote connector end to end, the way Claude on the web uses it.

Discovery, registration, the member's approval, PKCE, the token endpoint,
the MCP endpoint itself, refreshing, theft of a refresh token, and taking it
all back from Settings - against the real app, with nothing stubbed.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import secrets
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from helpers import PUBLIC_ORIGIN, SignIn
from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from eifo_api.oauth_server import acceptable_redirect
from eifo_api.security import CSRF_HEADER, safe_next
from eifo_core.models import ApiToken, OAuthClient

CALLBACK = "https://claude.ai/api/mcp/auth_callback"
MCP_HEADERS = {"Accept": "application/json, text/event-stream"}


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    return verifier, challenge


def register(client: TestClient, **extra: Any) -> dict[str, Any]:
    response = client.post(
        "/register",
        json={
            "client_name": "Claude",
            "redirect_uris": [CALLBACK],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        }
        | extra,
    )
    assert response.status_code == 201, response.text
    registered: dict[str, Any] = response.json()
    return registered


@dataclass
class Connected:
    client_id: str
    access: str
    refresh: str


def authorize(client: TestClient, client_id: str, challenge: str) -> str:
    """Start an authorization; returns the sealed consent request."""
    response = client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": CALLBACK,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "xyz",
            "scope": "read",
        },
        follow_redirects=False,
    )
    assert response.status_code == 302, response.text
    location = response.headers["location"]
    assert location.startswith(f"{PUBLIC_ORIGIN}/#/connect?request=")
    sealed: str = parse_qs(location.split("?", 1)[1])["request"][0]
    return sealed


def connect(client: TestClient, csrf: str) -> Connected:
    """Register, approve as the signed-in member, and trade the code."""
    client_id = register(client)["client_id"]
    verifier, challenge = pkce()
    sealed = authorize(client, client_id, challenge)

    approved = client.post(
        "/api/v1/connect/approve", json={"request": sealed}, headers={CSRF_HEADER: csrf}
    )
    assert approved.status_code == 200, approved.text
    back = urlsplit(approved.json()["redirect"])
    assert f"{back.scheme}://{back.netloc}{back.path}" == CALLBACK
    query = parse_qs(back.query)
    assert query["state"] == ["xyz"]

    token = client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": query["code"][0],
            "redirect_uri": CALLBACK,
            "client_id": client_id,
            "code_verifier": verifier,
        },
    )
    assert token.status_code == 200, token.text
    body = token.json()
    assert body["scope"] == "read"
    assert body["expires_in"] == 3600
    return Connected(client_id, body["access_token"], body["refresh_token"])


def bare(client: TestClient, token: str | None = None) -> TestClient:
    """The same app with no cookie - what an app's servers look like."""
    other = TestClient(client.app, base_url="https://testserver")
    if token:
        other.headers["Authorization"] = f"Bearer {token}"
    return other


def mcp(client: TestClient, token: str, method: str, params: dict[str, Any] | None = None) -> Any:
    """One JSON-RPC call to /mcp, after initialising a session.

    Through the app's own running client: an MCP session lives in the app's
    lifespan, which a throwaway client would not keep going between calls.
    """
    headers = MCP_HEADERS | {"Authorization": f"Bearer {token}"}
    init = client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            },
        },
    )
    assert init.status_code == 200, init.text
    session_headers = headers | {"mcp-session-id": init.headers["mcp-session-id"]}
    client.post(
        "/mcp",
        headers=session_headers,
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
    )
    response = client.post(
        "/mcp",
        headers=session_headers,
        json={"jsonrpc": "2.0", "id": 2, "method": method, "params": params or {}},
    )
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture
def csrf(sign_in: SignIn) -> str:
    return sign_in()


class TestDiscovery:
    def test_the_resource_points_at_its_authorization_server(self, client: TestClient) -> None:
        found = client.get("/.well-known/oauth-protected-resource/mcp").json()

        assert found["resource"] == f"{PUBLIC_ORIGIN}/mcp"
        assert found["authorization_servers"] == [f"{PUBLIC_ORIGIN}/"] or found[
            "authorization_servers"
        ] == [PUBLIC_ORIGIN]

    def test_the_authorization_server_describes_itself(self, client: TestClient) -> None:
        found = client.get("/.well-known/oauth-authorization-server").json()

        assert found["registration_endpoint"].endswith("/register")
        assert found["token_endpoint"].endswith("/token")
        assert found["code_challenge_methods_supported"] == ["S256"]
        assert found["scopes_supported"] == ["read"]

    def test_mcp_without_a_token_says_where_to_get_one(self, client: TestClient) -> None:
        response = bare(client).post("/mcp", headers=MCP_HEADERS, json={})

        assert response.status_code == 401
        assert "resource_metadata" in response.headers["www-authenticate"]


class TestRegistering:
    def test_any_app_may_register_and_it_grants_nothing(
        self, client: TestClient, session_factory: sessionmaker[Session]
    ) -> None:
        registered = register(bare(client))

        assert registered["client_id"]
        with session_factory() as session:
            row = session.get(OAuthClient, registered["client_id"])
            assert row is not None and row.last_used_at is None
            assert session.scalar(select(ApiToken.token_hash)) is None

    @pytest.mark.parametrize(
        "uri",
        [
            "http://evil.example/callback",
            "javascript:alert(1)",
            "https://claude.ai/cb#fragment",
            "ftp://claude.ai/cb",
        ],
    )
    def test_a_redirect_must_be_https_or_this_machine(self, client: TestClient, uri: str) -> None:
        response = bare(client).post("/register", json={"client_name": "x", "redirect_uris": [uri]})

        assert response.status_code == 400

    @pytest.mark.parametrize(
        ("uri", "ok"),
        [
            ("https://claude.ai/api/mcp/auth_callback", True),
            ("http://localhost:33418/callback", True),
            ("http://127.0.0.1:5000/cb", True),
            ("http://claude.ai/cb", False),
            ("https:///cb", False),
        ],
    )
    def test_the_redirect_rule(self, uri: str, ok: bool) -> None:
        assert acceptable_redirect(uri) is ok


class TestConsent:
    def test_the_page_shows_who_asks_and_where_it_sends_you(
        self, client: TestClient, csrf: str
    ) -> None:
        client_id = register(client)["client_id"]
        sealed = authorize(client, client_id, pkce()[1])

        shown = client.get("/api/v1/connect/request", params={"sealed": sealed}).json()

        assert shown == {"client_name": "Claude", "redirect_host": "claude.ai", "scopes": ["read"]}

    def test_needs_a_signed_in_member(self, client: TestClient) -> None:
        client_id = register(client)["client_id"]
        sealed = authorize(client, client_id, pkce()[1])

        assert (
            bare(client).get("/api/v1/connect/request", params={"sealed": sealed}).status_code
            == 401
        )

    def test_a_tampered_request_is_refused(self, client: TestClient, csrf: str) -> None:
        sealed = authorize(client, register(client)["client_id"], pkce()[1])

        response = client.post(
            "/api/v1/connect/approve",
            json={"request": sealed[:-2] + "xx"},
            headers={CSRF_HEADER: csrf},
        )

        assert response.status_code == 400

    def test_saying_no_goes_back_with_a_refusal(self, client: TestClient, csrf: str) -> None:
        sealed = authorize(client, register(client)["client_id"], pkce()[1])

        back = client.post(
            "/api/v1/connect/deny", json={"request": sealed}, headers={CSRF_HEADER: csrf}
        ).json()["redirect"]

        query = parse_qs(urlsplit(back).query)
        assert query["error"] == ["access_denied"]
        assert query["state"] == ["xyz"]

    def test_a_token_cannot_approve_an_app(self, client: TestClient, csrf: str) -> None:
        """Even a full one: a credential must not mint credentials."""
        made = client.post(
            "/api/v1/me/tokens", json={"name": "script"}, headers={CSRF_HEADER: csrf}
        ).json()["token"]
        sealed = authorize(client, register(client)["client_id"], pkce()[1])

        response = bare(client, made).post("/api/v1/connect/approve", json={"request": sealed})

        assert response.status_code == 403

    def test_needs_the_csrf_token(self, client: TestClient, csrf: str) -> None:
        sealed = authorize(client, register(client)["client_id"], pkce()[1])

        assert client.post("/api/v1/connect/approve", json={"request": sealed}).status_code == 403


class TestTokens:
    def test_the_access_token_reads_and_only_reads(self, client: TestClient, csrf: str) -> None:
        app = bare(client, connect(client, csrf).access)

        me = app.get("/api/v1/me").json()
        assert me["token_scope"] == "read"
        assert me["is_admin"] is False
        assert app.patch("/api/v1/me", json={"display_name": "x"}).status_code == 403
        assert app.get("/api/v1/me/tokens").status_code == 403

    def test_a_code_is_good_once(self, client: TestClient, csrf: str) -> None:
        client_id = register(client)["client_id"]
        verifier, challenge = pkce()
        sealed = authorize(client, client_id, challenge)
        back = client.post(
            "/api/v1/connect/approve", json={"request": sealed}, headers={CSRF_HEADER: csrf}
        ).json()["redirect"]
        code = parse_qs(urlsplit(back).query)["code"][0]
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": CALLBACK,
            "client_id": client_id,
            "code_verifier": verifier,
        }

        assert client.post("/token", data=form).status_code == 200
        assert client.post("/token", data=form).status_code == 400

    def test_the_wrong_verifier_gets_nothing(self, client: TestClient, csrf: str) -> None:
        client_id = register(client)["client_id"]
        sealed = authorize(client, client_id, pkce()[1])
        back = client.post(
            "/api/v1/connect/approve", json={"request": sealed}, headers={CSRF_HEADER: csrf}
        ).json()["redirect"]

        response = client.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": parse_qs(urlsplit(back).query)["code"][0],
                "redirect_uri": CALLBACK,
                "client_id": client_id,
                "code_verifier": pkce()[0],
            },
        )

        assert response.status_code == 400

    def test_an_expired_access_token_is_refused(
        self, client: TestClient, csrf: str, session_factory: sessionmaker[Session]
    ) -> None:
        connected = connect(client, csrf)
        with session_factory() as session:
            session.execute(
                update(ApiToken)
                .where(ApiToken.client_id == connected.client_id)
                .values(expires_at=dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1))
            )
            session.commit()

        assert bare(client, connected.access).get("/api/v1/me").status_code == 401

    def test_refreshing_rotates_and_a_reused_refresh_token_ends_it_all(
        self, client: TestClient, csrf: str
    ) -> None:
        connected = connect(client, csrf)
        form = {
            "grant_type": "refresh_token",
            "refresh_token": connected.refresh,
            "client_id": connected.client_id,
        }

        renewed = client.post("/token", data=form)
        assert renewed.status_code == 200
        fresh = renewed.json()
        assert fresh["refresh_token"] != connected.refresh
        assert bare(client, fresh["access_token"]).get("/api/v1/me").status_code == 200

        # The old one again: somebody else has it. Everything goes.
        assert client.post("/token", data=form).status_code == 400
        assert bare(client, fresh["access_token"]).get("/api/v1/me").status_code == 401
        again = form | {"refresh_token": fresh["refresh_token"]}
        assert client.post("/token", data=again).status_code == 400

    def test_settings_lists_them_apart_from_tokens_made_there(
        self, client: TestClient, csrf: str
    ) -> None:
        connect(client, csrf)

        assert client.get("/api/v1/me/tokens").json() == []
        connections = client.get("/api/v1/me/connections").json()
        assert [(c["client_name"], c["redirect_host"]) for c in connections] == [
            ("Claude", "claude.ai")
        ]

    def test_disconnecting_revokes_at_once(self, client: TestClient, csrf: str) -> None:
        connected = connect(client, csrf)

        gone = client.delete(
            f"/api/v1/me/connections/{connected.client_id}", headers={CSRF_HEADER: csrf}
        )

        assert gone.status_code == 204
        assert bare(client, connected.access).get("/api/v1/me").status_code == 401
        assert client.get("/api/v1/me/connections").json() == []
        refresh = client.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": connected.refresh,
                "client_id": connected.client_id,
            },
        )
        assert refresh.status_code == 400

    def test_the_app_can_revoke_its_own_token(self, client: TestClient, csrf: str) -> None:
        connected = connect(client, csrf)

        # client_secret present but empty: the library's revocation form
        # declares it without a default, so a public client must still send it.
        revoked = client.post(
            "/revoke",
            data={"token": connected.access, "client_id": connected.client_id, "client_secret": ""},
        )

        assert revoked.status_code == 200
        assert bare(client, connected.access).get("/api/v1/me").status_code == 401


class TestTheConnector:
    def test_lists_the_read_only_tools(self, client: TestClient, csrf: str) -> None:
        listed = mcp(client, connect(client, csrf).access, "tools/list")

        names = {tool["name"] for tool in listed["result"]["tools"]}
        assert {"search_titles", "recommendations", "taste_profile", "my_lists"} <= names
        assert all(tool["annotations"]["readOnlyHint"] for tool in listed["result"]["tools"])

    def test_a_tool_answers_as_the_member(self, client: TestClient, csrf: str) -> None:
        answer = mcp(
            client,
            connect(client, csrf).access,
            "tools/call",
            {"name": "my_lists", "arguments": {}},
        )

        assert answer["result"]["isError"] is False
        assert '"total":0' in answer["result"]["content"][0]["text"]

    def test_a_personal_read_token_works_too(self, client: TestClient, csrf: str) -> None:
        made = client.post(
            "/api/v1/me/tokens",
            json={"name": "desktop", "scope": "read"},
            headers={CSRF_HEADER: csrf},
        ).json()["token"]

        answer = mcp(client, made, "tools/call", {"name": "list_genres", "arguments": {}})

        assert answer["result"]["isError"] is False


class TestComingBackAfterSignIn:
    @pytest.mark.parametrize(
        ("value", "kept"),
        [
            ("#/connect?request=abc.def-ghi_jkl", True),
            ("#/settings", True),
            ("https://evil.example", False),
            ("//evil.example", False),
            ("#/connect?request=<script>", False),
            ("#//evil.example", False),
        ],
    )
    def test_only_a_page_of_this_app(self, value: str, kept: bool) -> None:
        assert (safe_next(value) == value) is kept


class TestTidiness:
    def test_an_app_nobody_approved_is_forgotten_after_a_week(
        self, client: TestClient, session_factory: sessionmaker[Session]
    ) -> None:
        stale = register(client)["client_id"]
        with session_factory() as session:
            session.execute(
                update(OAuthClient)
                .where(OAuthClient.client_id == stale)
                .values(created_at=dt.datetime.now(dt.UTC) - dt.timedelta(days=8))
            )
            session.commit()

        register(client)

        with session_factory() as session:
            assert session.get(OAuthClient, stale) is None

    def test_an_approved_app_is_kept(
        self, client: TestClient, csrf: str, session_factory: sessionmaker[Session]
    ) -> None:
        kept = connect(client, csrf).client_id
        with session_factory() as session:
            session.execute(
                update(OAuthClient)
                .where(OAuthClient.client_id == kept)
                .values(created_at=dt.datetime.now(dt.UTC) - dt.timedelta(days=60))
            )
            session.commit()

        register(client)

        with session_factory() as session:
            assert session.get(OAuthClient, kept) is not None

    def test_registrations_have_a_ceiling(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from eifo_api import oauth_server

        monkeypatch.setattr(oauth_server, "MAX_CLIENTS", 2)
        register(client)
        register(client)

        refused = client.post(
            "/register", json={"client_name": "x", "redirect_uris": ["https://example.com/cb"]}
        )

        assert refused.status_code == 400

    def test_hourly_tokens_do_not_pile_up(
        self, client: TestClient, csrf: str, session_factory: sessionmaker[Session]
    ) -> None:
        connected = connect(client, csrf)
        refresh = connected.refresh
        for _ in range(3):
            with session_factory() as session:
                session.execute(
                    update(ApiToken)
                    .where(ApiToken.client_id == connected.client_id)
                    .values(expires_at=dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1))
                )
                session.commit()
            refresh = client.post(
                "/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh,
                    "client_id": connected.client_id,
                },
            ).json()["refresh_token"]

        with session_factory() as session:
            live = session.scalars(
                select(ApiToken).where(ApiToken.client_id == connected.client_id)
            ).all()
            assert len(live) == 1


class TestMembership:
    def test_somebody_removed_from_the_list_loses_access_at_the_next_refresh(
        self,
        client: TestClient,
        csrf: str,
        session_factory: sessionmaker[Session],
        app: Any,
    ) -> None:
        connected = connect(client, csrf)
        # An allowlist that no longer names this member.
        from eifo_core.models import Member

        with session_factory() as session:
            session.add(Member(email="somebody-else@example.com"))
            session.commit()
        app.state.settings.members_only = True

        refreshed = client.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": connected.refresh,
                "client_id": connected.client_id,
            },
        )

        assert refreshed.status_code == 400
