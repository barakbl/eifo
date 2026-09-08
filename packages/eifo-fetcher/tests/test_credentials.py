"""How the fetcher proves it may write, including when nobody has told it.

The behaviour under test is a trade, and both halves of it matter. On a
single-box install the fetcher mints a credential for itself, uses it, and
revokes it - so no secret has to exist, and in particular none has to be kept
in a file beside the database, which would be a strictly worse arrangement than
the direct access it replaced.

On a remote fetcher it cannot do that, and must not pretend otherwise: there is
no catalog on that machine to mint against. What it owes there is a refusal that
says what to do, because a 401 from a URL nobody typed is not an answer.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from live import FETCHER_EMAIL, serving
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from eifo_core.db import create_engine_from_settings, make_session_factory
from eifo_core.enums import AuthProvider, MemberRole
from eifo_core.migrate import upgrade
from eifo_core.models import ApiToken, Member, User
from eifo_core.settings import Settings
from eifo_core.tokens import API_TOKEN_PREFIX, hash_token
from eifo_fetcher.credentials import (
    EPHEMERAL_TOKEN_NAME,
    api_client,
    ephemeral_token,
)
from eifo_fetcher.ingest import IngestError


@pytest.fixture
def local(tmp_path: Path) -> Settings:
    """A catalog on this machine, migrated, with nobody signed in yet."""
    settings = Settings(
        _env_file=None,
        db_url=f"sqlite:///{tmp_path / 'catalog.db'}",
        images_dir=tmp_path / "images",
        admin_emails=[FETCHER_EMAIL],
    )
    upgrade(settings.db_url)
    return settings


@pytest.fixture
def factory(local: Settings) -> Iterator[sessionmaker[Session]]:
    engine = create_engine_from_settings(local)
    yield make_session_factory(engine)
    engine.dispose()


def sign_in(factory: sessionmaker[Session], *, email: str | None = FETCHER_EMAIL) -> None:
    """An account, as completing the web app's login once would leave one."""
    with factory() as session:
        session.add(
            User(
                auth_provider=AuthProvider.GOOGLE,
                auth_subject="somebody",
                email=email,
                display_name="Somebody",
            )
        )
        session.commit()


def tokens(factory: sessionmaker[Session]) -> list[ApiToken]:
    with factory() as session:
        return list(session.scalars(select(ApiToken)).all())


class TestMintingOneForThisRun:
    def test_a_configured_token_is_used_as_it_is(
        self, local: Settings, factory: sessionmaker[Session]
    ) -> None:
        """Nothing is minted when somebody has already issued one on purpose.

        Which is the remote arrangement as seen from a machine that happens to
        have a catalog too - and a fetcher that quietly issued itself a second
        credential would leave one behind on every run that was killed.
        """
        sign_in(factory)
        configured = local.model_copy(update={"api_token": SecretStr("eifo_pat_configured")})

        with api_client(configured):
            assert tokens(factory) == []

    def test_it_mints_when_there_is_no_token_and_a_catalog_here(
        self, local: Settings, factory: sessionmaker[Session]
    ) -> None:
        sign_in(factory)

        with ephemeral_token(local) as token:
            assert token.startswith(API_TOKEN_PREFIX)
            stored = tokens(factory)
            assert [row.token_hash for row in stored] == [hash_token(token)]
            assert stored[0].name == EPHEMERAL_TOKEN_NAME

    def test_it_is_revoked_on_the_way_out(
        self, local: Settings, factory: sessionmaker[Session]
    ) -> None:
        """The credential exists for the length of a command and is never written down."""
        sign_in(factory)

        with ephemeral_token(local):
            pass

        assert tokens(factory) == []

    def test_it_is_revoked_even_when_the_run_explodes(
        self, local: Settings, factory: sessionmaker[Session]
    ) -> None:
        """A crash must not leave an administrator's credential in the table."""
        sign_in(factory)

        with pytest.raises(ZeroDivisionError), ephemeral_token(local):
            raise ZeroDivisionError

        assert tokens(factory) == []

    def test_the_token_is_never_the_same_twice(
        self, local: Settings, factory: sessionmaker[Session]
    ) -> None:
        sign_in(factory)

        with ephemeral_token(local) as first:
            pass
        with ephemeral_token(local) as second:
            pass

        assert first != second


class TestWhoItMintsFor:
    def test_an_administrator_named_in_configuration(
        self, local: Settings, factory: sessionmaker[Session]
    ) -> None:
        sign_in(factory)

        with ephemeral_token(local), factory() as session:
            owner = session.scalars(select(User)).one()
            assert session.scalars(select(ApiToken)).one().user_id == owner.id

    def test_one_the_allowlist_has_promoted(
        self, local: Settings, factory: sessionmaker[Session]
    ) -> None:
        """The same order the API decides admin in: configuration, then the list."""
        settings = local.model_copy(update={"admin_emails": []})
        sign_in(factory, email="promoted@eifo.test")
        with factory() as session:
            session.add(Member(email="promoted@eifo.test", role=MemberRole.ADMIN))
            session.commit()

        with ephemeral_token(settings) as token:
            assert token.startswith(API_TOKEN_PREFIX)

    def test_it_will_not_mint_for_somebody_who_is_not_one(
        self, local: Settings, factory: sessionmaker[Session]
    ) -> None:
        """It would be minted, used once and rejected, which is a worse failure."""
        settings = local.model_copy(update={"admin_emails": []})
        sign_in(factory, email="viewer@eifo.test")

        with pytest.raises(IngestError, match="administrator"), ephemeral_token(settings):
            pass

        assert tokens(factory) == []

    def test_with_no_accounts_at_all_it_says_so(self, local: Settings) -> None:
        """A token carries an account's permissions, so it needs one to carry them from."""
        with (
            pytest.raises(IngestError, match="sign in through the web app"),
            ephemeral_token(local),
        ):
            pass


class TestWhenItCannotMint:
    def test_a_machine_with_no_catalog_is_told_what_to_configure(self, tmp_path: Path) -> None:
        """The remote case, which is the whole point of the architecture - and
        the one case where somebody does have to issue a credential on purpose."""
        remote = Settings(
            _env_file=None,
            db_url=f"sqlite:///{tmp_path / 'not-here.db'}",
            images_dir=tmp_path / "images",
        )

        with pytest.raises(IngestError, match="EIFO_API_TOKEN"), ephemeral_token(remote):
            pass

    def test_it_does_not_create_a_database_looking(self, tmp_path: Path) -> None:
        """Opening an engine creates the SQLite parent directory, so a remote
        fetcher on the default URL would quietly acquire an empty ``data/`` and
        then fail for a second, less true reason."""
        missing = tmp_path / "nothing" / "eifo.db"
        remote = Settings(
            _env_file=None,
            db_url=f"sqlite:///{missing}",
            images_dir=tmp_path / "images",
        )

        with pytest.raises(IngestError), ephemeral_token(remote):
            pass

        assert not missing.parent.exists()


class TestUsingIt:
    def test_a_minted_token_actually_opens_the_ingest_surface(
        self, local: Settings, factory: sessionmaker[Session]
    ) -> None:
        """The whole trade, end to end: no configured secret, and it still writes.

        Against the real application, because the thing worth proving is that
        what this mints is a credential the ingest surface accepts - which
        depends on the account being an administrator, and on nothing else
        having gone wrong between minting and presenting it.
        """
        sign_in(factory)

        with serving(local) as running, ephemeral_token(local) as token:
            answer = running.api._http.get(
                "/api/v1/ingest/enrich/seret/status",
                headers={"Authorization": f"Bearer {token}"},
            )

        assert answer.status_code == 200
        assert answer.json()["pages"] == 0

    def test_and_stops_working_once_the_run_is_over(
        self, local: Settings, factory: sessionmaker[Session]
    ) -> None:
        """Revoked means revoked on the next request, which is what a row buys."""
        sign_in(factory)
        with ephemeral_token(local) as token:
            pass

        with serving(local) as running:
            answer = running.api._http.get(
                "/api/v1/ingest/enrich/seret/status",
                headers={"Authorization": f"Bearer {token}"},
            )

        assert answer.status_code in {401, 404}
