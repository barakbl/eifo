"""The real application, in this process, for the tests that talk to it.

Its own module rather than a name in ``conftest``: the three suites each have a
``conftest`` and only one of them can be imported under that name at a time, so
a shared type living there is importable when the fetcher's tests run alone and
not when the whole suite does.

The fixture that builds one of these is in ``conftest``; this is only its shape.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.orm import Session, sessionmaker

from eifo_api.app import create_app
from eifo_core.enums import AuthProvider
from eifo_core.migrate import upgrade
from eifo_core.models import ApiToken, User
from eifo_core.settings import Settings
from eifo_core.tokens import hash_token
from eifo_fetcher.ingest import IngestClient

#: Who the fetcher signs in as against the live application, and with what.
#:
#: An administrator, because the ingest surface answers 404 to anybody else -
#: which is the right answer to a stranger and a mystery to a test.
FETCHER_EMAIL = "fetcher@eifo.test"
FETCHER_TOKEN = "eifo_pat_test"


@dataclass(frozen=True, slots=True)
class LiveApi:
    """The real application, in this process, with a fetcher wired to it.

    Not a stand-in. Everything the fetcher does now is a conversation with this
    surface - what is due, what a listing matched, whether a score is believable
    - and a hand-written double would only ever prove that both halves of one
    author's idea agree. The far end here is the application, over a migrated
    catalog, and a change to either side that the other cannot live with fails
    the moment it is made.

    In-process rather than over a socket: ``TestClient`` is an ``httpx.Client``,
    which is exactly what :class:`IngestClient` takes, so nothing is stubbed on
    the way through - the request is serialised, routed, validated and answered.
    """

    app: FastAPI
    api: IngestClient
    session_factory: sessionmaker[Session]
    settings: Settings

    @contextmanager
    def session(self) -> Iterator[Session]:
        """A session on the catalog the API just wrote to."""
        with self.session_factory() as session:
            yield session


@contextmanager
def serving(settings: Settings) -> Iterator[LiveApi]:
    """Bring up the application over this catalog and hand back a client for it.

    Its own function rather than only a fixture because two suites need it
    against two different databases: the fetcher's own tests against a throwaway
    one, and the CLI's tests against whatever the command was pointed at.

    The token is real and so is the account behind it: the ingest surface answers
    only to administrators, and a fixture that bypassed that would let a routing
    change which locks the fetcher out pass every test in this package.
    """
    upgrade(settings.db_url)
    served = settings.model_copy(
        update={
            "admin_emails": [FETCHER_EMAIL],
            "secret_key": SecretStr("test-secret-key-not-used-anywhere-real"),
            # The application would otherwise try to bring the schema up itself
            # on a database that has just been migrated.
            "auto_migrate": False,
        }
    )
    app = create_app(served)
    session_factory: sessionmaker[Session] = app.state.session_factory

    try:
        with session_factory() as session:
            if session.get(User, 1) is None:
                user = User(
                    auth_provider=AuthProvider.GOOGLE,
                    auth_subject="fetcher",
                    email=FETCHER_EMAIL,
                    display_name="The Fetcher",
                )
                session.add(user)
                session.flush()
                session.add(
                    ApiToken(token_hash=hash_token(FETCHER_TOKEN), user_id=user.id, name="tests")
                )
                session.commit()

        with TestClient(app, base_url="http://api.test") as client:
            yield LiveApi(
                app=app,
                api=IngestClient("", FETCHER_TOKEN, http=client),
                session_factory=session_factory,
                settings=settings,
            )
    finally:
        # In a finally, so a fixture that fails while seeding still lets go of
        # the file. Otherwise the connection is collected later and Python 3.14
        # raises an unraisable ResourceWarning that this suite treats as an
        # error - reporting the teardown instead of the test that actually broke.
        app.state.engine.dispose()
