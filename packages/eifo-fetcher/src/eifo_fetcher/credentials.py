"""How the fetcher proves it may write, including when nobody has told it.

Everything the fetcher does now goes through the API, and the API wants a
bearer token. On a remote fetcher that is exactly right: somebody issues a
credential once, puts it in the environment, and a laptop can fill a catalog it
has no other way to reach.

On the ordinary single-box install it is ceremony. The fetcher, the API and the
database are all on one machine, run by one person, and requiring them to issue
a credential to themselves - and then keep it somewhere - is asking for a secret
to exist where none needs to. Worse, the obvious place to keep it is a config
file, and a long-lived administrator token sitting in a file beside the database
is a strictly worse arrangement than the direct access it replaced. The sidecar
keeps its secrets in the Keychain precisely to avoid that; this must not undo it.

So: **when no token is configured and the database is reachable from here, one
is minted for this run and revoked at the end of it.** The credential exists for
the length of a command, is never written down, and is gone whether the run
succeeded or not. A fetcher that cannot open the database cannot do this - which
is exactly the remote case, and exactly the case where somebody has to configure
a token on purpose.

It mints for an account that already exists, and only for an administrator's. A
token carries an account's permissions, so it needs an account to carry them
from; the ingest surface answers only to administrators, so a token from anybody
else would be minted, used once and rejected.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from eifo_core.db import create_engine_from_settings, make_session_factory, require_schema
from eifo_core.enums import MemberRole
from eifo_core.models import ApiToken, Member, User
from eifo_core.settings import Settings
from eifo_core.tokens import hash_token, new_api_token
from eifo_fetcher.ingest import IngestClient, IngestError

logger = logging.getLogger("eifo.fetch.credentials")

#: What an ephemeral token calls itself while it exists.
#:
#: Named for the person who might see it in ``eifo-fetch token list`` during the
#: seconds it is alive - or afterwards, if the machine was killed between
#: minting and revoking. It should read as something to delete.
EPHEMERAL_TOKEN_NAME = "eifo-fetch (one run; revoked automatically)"

#: What to say when there is no token and none can be made here.
NO_TOKEN_MESSAGE = (
    "No API token configured, and none could be minted here: this machine cannot "
    "open the catalog. The fetcher writes through the API now, so it needs one - run "
    "`eifo-fetch token create fetcher` on the machine with the database and set "
    "EIFO_API_TOKEN to what it prints."
)


@contextmanager
def api_client(settings: Settings, *, http: httpx.Client | None = None) -> Iterator[IngestClient]:
    """A client for one command, with a credential it may have made itself.

    Raises:
        IngestError: when there is no configured token and none can be minted -
            no catalog on this machine, or no administrator to mint for. The
            message says which, because the alternative is a 401 from a URL the
            operator never typed.
    """
    configured = settings.api_token.get_secret_value().strip() if settings.api_token else ""
    if configured:
        with IngestClient.from_settings(settings, http=http) as api:
            yield api
        return

    with (
        ephemeral_token(settings) as token,
        IngestClient(settings.api_url(), token, http=http) as api,
    ):
        yield api


@contextmanager
def ephemeral_token(settings: Settings) -> Iterator[str]:
    """Mint a token for the duration, and revoke it however the block ends.

    Revoked in a ``finally``, so a run that crashes does not leave an
    administrator's credential in the table. The revocation is best effort: a
    database that has gone away between minting and finishing is a real problem
    but not one worth replacing the run's own exception with.
    """
    if not _catalog_is_here(settings):
        raise IngestError(NO_TOKEN_MESSAGE)

    engine = create_engine_from_settings(settings)
    try:
        require_schema(engine, settings.db_url)
        session_factory = make_session_factory(engine)
        with session_factory() as session:
            user = _an_administrator(session, settings)
            raw = new_api_token()
            session.add(
                ApiToken(token_hash=hash_token(raw), user_id=user.id, name=EPHEMERAL_TOKEN_NAME)
            )
            session.commit()
            logger.debug("minted a token for this run as %s", user.email or user.display_name)
        try:
            yield raw
        finally:
            _revoke(session_factory, raw)
    finally:
        engine.dispose()


def _catalog_is_here(settings: Settings) -> bool:
    """Whether there is a database on this machine to mint against.

    Asked before an engine is built rather than by trying one, because building
    one has a side effect: it creates the SQLite parent directory. A remote
    fetcher whose ``db_url`` is still the default would quietly acquire an empty
    ``data/`` and then fail for a second, less true reason.

    Anything that is not SQLite is taken to be reachable and left to the connect
    attempt to disprove: a Postgres URL is a deliberate act of configuration,
    and there is no file to look for.
    """
    url = make_url(settings.db_url)
    if not url.drivername.startswith("sqlite"):
        return True
    # No database in a SQLite URL means ``:memory:``, which nothing outside a
    # test has, and which no API on the other end of a socket could be sharing.
    return url.database is not None and Path(url.database).is_file()


def _an_administrator(session: Session, settings: Settings) -> User:
    """An account whose token the ingest surface will accept.

    Configuration first and always, then the allowlist - the same order the API
    decides admin in, because a token minted on any other rule would be minted
    and then rejected.
    """
    users = list(session.scalars(select(User).order_by(User.id)).all())
    if not users:
        raise IngestError(
            "There are no accounts here, so there is no identity to act as. Somebody has "
            "to sign in through the web app once - or set EIFO_API_TOKEN to a token "
            "issued elsewhere with `eifo-fetch token create fetcher`."
        )

    allowed = {
        (row.email or "").casefold()
        for row in session.scalars(select(Member).where(Member.role == MemberRole.ADMIN)).all()
    }
    for user in users:
        email = (user.email or "").strip()
        if settings.is_admin(email) or (email and email.casefold() in allowed):
            return user

    raise IngestError(
        "No account here is an administrator, and the ingest API answers to nobody else. "
        "Add an address to admin_emails in the configuration, or set EIFO_API_TOKEN to a "
        "token belonging to an administrator."
    )


def _revoke(session_factory: sessionmaker[Session], raw: str) -> None:
    """Delete the row, and do not let failing to become the failure."""
    try:
        with session_factory() as session:
            token = session.get(ApiToken, hash_token(raw))
            if token is not None:
                session.delete(token)
                session.commit()
    except Exception as exc:  # pragma: no cover - a database that went away mid-run
        logger.warning(
            "could not revoke this run's token; remove it with `eifo-fetch token list` "
            "and `eifo-fetch token revoke`: %s",
            exc,
        )
