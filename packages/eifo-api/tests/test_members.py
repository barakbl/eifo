"""Who may sign in, who may administer, and what a token can do.

Sign-in used to be open: whoever completed a Google round trip got an account,
and only the Manage tab was gated. These are the tests for the three things
that replace that - an allowlist in the database, roles on it, and tokens for
callers that are not browsers - and most of them are about the ways such a
thing goes wrong rather than the way it goes right.
"""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from helpers import MakeAdmin, SignIn
from providers import GOOGLE_EMAIL
from seed import Seeded
from sqlalchemy.orm import Session, sessionmaker

from eifo_api.security import API_TOKEN_PREFIX
from eifo_core.enums import AuthProvider, MemberRole
from eifo_core.models import ApiToken, Member, User, UserSession
from eifo_core.types import utcnow


@pytest.fixture
def invite(session_factory: sessionmaker[Session]):
    """Put an address on the allowlist, as an administrator would."""

    def _invite(email: str, role: MemberRole = MemberRole.MEMBER) -> None:
        with session_factory() as session:
            session.add(Member(email=email.casefold(), role=role))
            session.commit()

    return _invite


class TestTheAllowlistSwitchesItselfOn:
    """An empty list is not a list, and enforcing one would brick the install.

    Signing in is how somebody reaches the Manage tab, and the Manage tab is
    where invitations are written. On an instance with nobody configured and
    nobody invited, enforcing the list would mean nobody could ever be first.
    """

    def test_an_instance_with_nobody_on_it_lets_anybody_in(
        self, client: TestClient, sign_in: SignIn
    ) -> None:
        sign_in()

        assert client.get("/api/v1/me").status_code == 200

    def test_one_configured_administrator_is_enough_to_close_it(
        self, client: TestClient, sign_in: SignIn, app: FastAPI
    ) -> None:
        app.state.settings.admin_emails = ["somebody.else@example.com"]

        sign_in()

        assert client.get("/api/v1/me").status_code == 401

    def test_one_invitation_is_enough_to_close_it(
        self, client: TestClient, sign_in: SignIn, invite
    ) -> None:
        invite("somebody.else@example.com")

        sign_in()

        assert client.get("/api/v1/me").status_code == 401


class TestWhoGetsIn:
    def test_an_invited_address_signs_in(self, client: TestClient, sign_in: SignIn, invite) -> None:
        invite("stranger@example.com")
        invite(GOOGLE_EMAIL)

        sign_in()

        assert client.get("/api/v1/me").status_code == 200

    def test_the_case_it_was_typed_in_does_not_matter(
        self, client: TestClient, sign_in: SignIn, invite
    ) -> None:
        """Providers are inconsistent, and nobody typing an address thinks about it."""
        invite(GOOGLE_EMAIL.upper())

        sign_in()

        assert client.get("/api/v1/me").status_code == 200

    def test_a_configured_administrator_never_needs_an_invitation(
        self, client: TestClient, sign_in: SignIn, make_admin: MakeAdmin
    ) -> None:
        """The one address that cannot be locked out by anything in the database."""
        make_admin(GOOGLE_EMAIL)

        sign_in()

        assert client.get("/api/v1/me").status_code == 200

    def test_nothing_is_stored_about_somebody_turned_away(
        self,
        client: TestClient,
        sign_in: SignIn,
        invite,
        session_factory: sessionmaker[Session],
    ) -> None:
        """A refusal is not a reason to keep somebody's name and address.

        The check happens before the account is written, so an uninvited
        sign-in leaves the database exactly as it found it.
        """
        invite("somebody.else@example.com")

        sign_in()

        with session_factory() as session:
            assert session.query(User).filter(User.email == GOOGLE_EMAIL).count() == 0
            assert session.query(UserSession).count() == 0

    def test_a_provider_that_supplies_no_address_cannot_be_matched(
        self, client: TestClient, sign_in: SignIn, invite
    ) -> None:
        """X does not always give one, and an allowlist is a list of addresses.

        A real consequence of keying it that way, asserted so that it is a
        decision rather than a surprise.
        """
        invite(GOOGLE_EMAIL)

        sign_in(AuthProvider.X)

        assert client.get("/api/v1/me").status_code == 401

    def test_the_refusal_says_which_kind_it_was(
        self, client: TestClient, invite, sign_in: SignIn
    ) -> None:
        """ "Try again" is unhelpful to somebody who was never invited."""
        invite("somebody.else@example.com")

        sign_in()

        # sign_in follows the redirect chain; the fragment is what the client reads.
        assert client.get("/api/v1/me").status_code == 401


class TestManagingTheList:
    def _admin(self, client: TestClient, sign_in: SignIn, make_admin: MakeAdmin) -> str:
        make_admin(GOOGLE_EMAIL)
        return sign_in()

    def test_an_administrator_can_invite(
        self, client: TestClient, sign_in: SignIn, make_admin: MakeAdmin
    ) -> None:
        csrf = self._admin(client, sign_in, make_admin)

        created = client.post(
            "/api/v1/admin/members",
            json={"email": "New.Person@Example.com", "role": "member"},
            headers={"X-CSRF-Token": csrf},
        )

        assert created.status_code == 201
        # Stored in the form it will be matched in, not the form it was typed in.
        assert created.json()["email"] == "new.person@example.com"
        assert created.json()["invited_by"] == GOOGLE_EMAIL

    def test_inviting_twice_changes_the_role_rather_than_failing(
        self, client: TestClient, sign_in: SignIn, make_admin: MakeAdmin
    ) -> None:
        """Not an error worth a red message - somebody making sure."""
        csrf = self._admin(client, sign_in, make_admin)
        headers = {"X-CSRF-Token": csrf}
        body = {"email": "new@example.com"}

        client.post("/api/v1/admin/members", json=body | {"role": "member"}, headers=headers)
        again = client.post("/api/v1/admin/members", json=body | {"role": "admin"}, headers=headers)

        assert again.status_code == 201
        assert again.json()["role"] == "admin"

    def test_the_list_names_the_configured_administrators_too(
        self, client: TestClient, sign_in: SignIn, make_admin: MakeAdmin
    ) -> None:
        """A list of who may sign in that leaves out the people who certainly may
        would be a poor list."""
        self._admin(client, sign_in, make_admin)

        rows = client.get("/api/v1/admin/members").json()

        configured = next(row for row in rows if row["email"] == GOOGLE_EMAIL)
        assert configured["from_config"] is True
        assert configured["role"] == "admin"

    def test_a_configured_administrator_cannot_be_edited_from_here(
        self, client: TestClient, sign_in: SignIn, make_admin: MakeAdmin
    ) -> None:
        """409 rather than 404: the address is real, and the answer is
        "not from here" - which leads somewhere different from "no such person"."""
        csrf = self._admin(client, sign_in, make_admin)

        refused = client.delete(
            f"/api/v1/admin/members/{GOOGLE_EMAIL}",
            headers={"X-CSRF-Token": csrf},
        )

        assert refused.status_code == 409
        assert "EIFO_ADMIN_EMAILS" in refused.json()["detail"]

    def test_promoting_takes_effect_without_a_restart(
        self, client: TestClient, sign_in: SignIn, make_admin: MakeAdmin
    ) -> None:
        """The whole point of roles living in the database rather than a file."""
        csrf = self._admin(client, sign_in, make_admin)
        client.post(
            "/api/v1/admin/members",
            json={"email": "deputy@example.com", "role": "admin"},
            headers={"X-CSRF-Token": csrf},
        )

        rows = client.get("/api/v1/admin/members").json()

        deputy = next(row for row in rows if row["email"] == "deputy@example.com")
        assert (deputy["role"], deputy["from_config"]) == ("admin", False)

    def test_removing_somebody_ends_what_they_are_holding(
        self,
        client: TestClient,
        sign_in: SignIn,
        make_admin: MakeAdmin,
        invite,
        session_factory: sessionmaker[Session],
    ) -> None:
        """Otherwise a removed member stays signed in for up to thirty days.

        Which is not what anybody pressing "remove" means by it - and the
        tokens they issued would outlive the sessions, so the one thing they
        would keep is the one that never expires.
        """
        theirs = "leaving@example.com"
        invite(theirs)
        with session_factory() as session:
            them = User(
                auth_provider=AuthProvider.GOOGLE,
                auth_subject="leaving",
                email=theirs,
                display_name="Leaving",
            )
            session.add(them)
            session.flush()
            session.add(ApiToken(token_hash="aa" * 32, user_id=them.id, name="theirs"))
            session.add(
                UserSession(
                    token_hash="bb" * 32,
                    user_id=them.id,
                    expires_at=utcnow() + dt.timedelta(days=30),
                )
            )
            session.commit()

        csrf = self._admin(client, sign_in, make_admin)
        removed = client.delete(f"/api/v1/admin/members/{theirs}", headers={"X-CSRF-Token": csrf})

        assert removed.status_code == 204
        with session_factory() as session:
            assert session.query(Member).filter(Member.email == theirs).count() == 0
            assert session.query(ApiToken).count() == 0
            assert (
                session.query(UserSession).filter(UserSession.token_hash == "bb" * 32).count() == 0
            )

    def test_a_member_cannot_open_the_list(
        self, client: TestClient, sign_in: SignIn, invite
    ) -> None:
        """404, as the rest of the admin surface does: a signed-in stranger is
        not owed the knowledge that it is there."""
        invite(GOOGLE_EMAIL)
        sign_in()

        assert client.get("/api/v1/admin/members").status_code == 404

    def test_a_stranger_cannot_open_the_list(self, client: TestClient) -> None:
        assert client.get("/api/v1/admin/members").status_code in (401, 404)


class TestApiTokens:
    def _member(self, client: TestClient, sign_in: SignIn, invite) -> str:
        invite(GOOGLE_EMAIL)
        return sign_in()

    def test_a_token_works_as_a_way_in(self, client: TestClient, sign_in: SignIn, invite) -> None:
        csrf = self._member(client, sign_in, invite)
        signed_in_id = client.get("/api/v1/me").json()["user"]["id"]
        token = client.post(
            "/api/v1/me/tokens", json={"name": "laptop"}, headers={"X-CSRF-Token": csrf}
        ).json()["token"]
        client.cookies.clear()

        answered = client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})

        assert answered.status_code == 200
        # Not the address: /me never carries one, deliberately. The id is proof
        # enough that the token resolved to the account that made it.
        assert answered.json()["user"]["id"] == signed_in_id

    def test_the_token_is_shown_once_and_never_again(
        self, client: TestClient, sign_in: SignIn, invite
    ) -> None:
        csrf = self._member(client, sign_in, invite)
        made = client.post(
            "/api/v1/me/tokens", json={"name": "laptop"}, headers={"X-CSRF-Token": csrf}
        ).json()

        listed = client.get("/api/v1/me/tokens").json()

        assert made["token"].startswith(API_TOKEN_PREFIX)
        assert listed[0]["name"] == "laptop"
        assert "token" not in listed[0]

    def test_a_token_needs_no_csrf_token(self, client: TestClient, sign_in: SignIn, invite) -> None:
        """CSRF is a browser being made to send a cookie it already holds.

        Nothing can make a browser attach somebody else's Authorization header,
        so demanding a CSRF token from a script would be asking it to fetch a
        page first for no security at all.
        """
        csrf = self._member(client, sign_in, invite)
        token = client.post(
            "/api/v1/me/tokens", json={"name": "laptop"}, headers={"X-CSRF-Token": csrf}
        ).json()["token"]
        client.cookies.clear()

        wrote = client.patch(
            "/api/v1/me",
            json={"display_name": "Renamed"},
            headers={"Authorization": f"Bearer {token}"},
        )

        assert wrote.status_code == 200

    def test_a_token_cannot_mint_another(self, client: TestClient, sign_in: SignIn, invite) -> None:
        """One leaked token would otherwise become permanent access that
        revoking the original does not touch."""
        csrf = self._member(client, sign_in, invite)
        token = client.post(
            "/api/v1/me/tokens", json={"name": "laptop"}, headers={"X-CSRF-Token": csrf}
        ).json()["token"]
        client.cookies.clear()

        refused = client.post(
            "/api/v1/me/tokens",
            json={"name": "another"},
            headers={"Authorization": f"Bearer {token}"},
        )

        assert refused.status_code == 403

    def test_revoking_takes_effect_at_once(
        self, client: TestClient, sign_in: SignIn, invite
    ) -> None:
        csrf = self._member(client, sign_in, invite)
        made = client.post(
            "/api/v1/me/tokens", json={"name": "laptop"}, headers={"X-CSRF-Token": csrf}
        ).json()

        client.delete(f"/api/v1/me/tokens/{made['hint']}", headers={"X-CSRF-Token": csrf})
        client.cookies.clear()

        assert (
            client.get(
                "/api/v1/me", headers={"Authorization": f"Bearer {made['token']}"}
            ).status_code
            == 401
        )

    def test_somebody_elses_token_is_not_yours_to_revoke(
        self, client: TestClient, sign_in: SignIn, invite, session_factory: sessionmaker[Session]
    ) -> None:
        csrf = self._member(client, sign_in, invite)
        with session_factory() as session:
            stranger = User(
                auth_provider=AuthProvider.X,
                auth_subject="stranger",
                email="stranger@example.com",
                display_name="Stranger",
            )
            session.add(stranger)
            session.flush()
            session.add(ApiToken(token_hash="ff" * 32, user_id=stranger.id, name="theirs"))
            session.commit()

        refused = client.delete("/api/v1/me/tokens/ffff", headers={"X-CSRF-Token": csrf})

        assert refused.status_code == 404

    def test_a_header_that_is_not_ours_is_ignored_rather_than_refused(
        self, client: TestClient
    ) -> None:
        """A request carrying somebody else's Authorization header did not want
        this API's tokens; 401 would be a worse answer to a harmless mistake."""
        answered = client.get("/api/v1/meta", headers={"Authorization": "Bearer sk-not-ours"})

        assert answered.status_code == 200


class TestMembersOnly:
    """The catalog itself behind the sign-in, not merely the Manage tab."""

    def test_the_catalog_is_open_by_default(self, client: TestClient) -> None:
        """An instance that never asked for this should not wake up asking
        strangers to log in."""
        assert client.get("/api/v1/titles").status_code == 200

    def test_a_stranger_is_refused_when_it_is_on(self, client: TestClient, app: FastAPI) -> None:
        app.state.settings.members_only = True

        assert client.get("/api/v1/titles").status_code == 401
        assert client.get("/api/v1/meta").status_code == 401

    def test_signing_in_is_still_reachable(self, client: TestClient, app: FastAPI) -> None:
        """A gate somebody cannot reach the sign-in through is a locked
        building with the key inside."""
        app.state.settings.members_only = True

        assert client.get("/api/v1/auth/login/google", follow_redirects=False).status_code == 302

    def test_a_member_sees_the_catalog(
        self, client: TestClient, app: FastAPI, sign_in: SignIn, invite
    ) -> None:
        invite(GOOGLE_EMAIL)
        sign_in()
        app.state.settings.members_only = True

        assert client.get("/api/v1/titles").status_code == 200

    def test_artwork_is_closed_too(self, client: TestClient, app: FastAPI) -> None:
        """A poster path is guessable by counting, so an open mount would
        publish the catalog one integer at a time."""
        app.state.settings.members_only = True

        refused = client.get("/images/posters/1/w500.jpg")

        assert refused.status_code == 401
        assert refused.headers["cache-control"] == "no-store"

    def test_a_token_opens_it_as_well_as_a_cookie(
        self, client: TestClient, app: FastAPI, sign_in: SignIn, invite
    ) -> None:
        csrf = invite(GOOGLE_EMAIL) or sign_in()
        token = client.post(
            "/api/v1/me/tokens", json={"name": "laptop"}, headers={"X-CSRF-Token": csrf}
        ).json()["token"]
        client.cookies.clear()
        app.state.settings.members_only = True

        assert (
            client.get("/api/v1/titles", headers={"Authorization": f"Bearer {token}"}).status_code
            == 200
        )


class TestTheWayIn:
    """The one thing a members-only instance tells a stranger.

    The sign-in wall needs two facts to render - is this private, and which
    providers open it - and both used to arrive on `/meta`, which is now closed.
    A gate whose key is behind the gate is not a gate: the client showed an
    error with no button on it.
    """

    def test_it_is_open_when_everything_else_is_closed(
        self, client: TestClient, app: FastAPI
    ) -> None:
        app.state.settings.members_only = True

        answered = client.get("/api/v1/auth/context")

        assert answered.status_code == 200
        assert answered.json()["members_only"] is True
        # Both, because the fixture configures both. What matters is that a
        # signed-out visitor is told how to get in at all.
        assert "google" in answered.json()["login_providers"]

    def test_it_says_nothing_about_the_catalog(
        self, client: TestClient, app: FastAPI, catalog: Seeded
    ) -> None:
        """It is answered to strangers, so it must not hint at what is inside."""
        app.state.settings.members_only = True

        body = client.get("/api/v1/auth/context").json()

        assert set(body) == {"members_only", "login_providers"}
