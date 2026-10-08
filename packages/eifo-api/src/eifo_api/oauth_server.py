"""Sign in with Eifo: the OAuth side of the remote MCP connector.

An app - Claude on the web or a phone, or any MCP client - registers, sends a
member here to approve it, and is given a read-only token for that member. The
OAuth protocol itself (discovery, PKCE, the token endpoint, revocation) is the
MCP library's; this is the storage and the decisions behind it:

* **Registering grants nothing.** Any app may register; only a member signing
  in and approving the consent page produces a token. A redirect address has
  to be https, or plain http to this machine. An app nobody approves within a
  week is forgotten.
* **Tokens are read-only and short.** An access token is an hour long and is
  an ordinary :class:`~eifo_core.models.ApiToken` with the ``read`` scope, so
  everything it can reach is decided by the same allow-list as any other
  narrow token. A refresh token lasts thirty days and is replaced on every use.
* **A refresh token used twice ends the connection.** Rotation means the
  legitimate app only ever holds the newest one; an old one coming back means
  somebody else has it.
* **Membership is checked whenever a token is issued**, so removing somebody
  from the allowlist stops their connections at the next refresh at the latest.
* Every secret is stored as its SHA-256 - except an app's client secret, which
  the library compares in the clear, and which is worth nothing without a
  member's approval and the PKCE verifier only the app holds.
"""

from __future__ import annotations

import datetime as dt
import logging
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar, cast
from urllib.parse import urlencode, urlsplit

import anyio.to_thread
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl
from sqlalchemy import CursorResult, delete, select
from sqlalchemy.orm import Session, sessionmaker

from eifo_api import members
from eifo_api.security import signing_secret
from eifo_core.enums import TokenScope
from eifo_core.models import ApiToken, OAuthClient, OAuthCode, OAuthRefreshToken, User
from eifo_core.settings import Settings
from eifo_core.tokens import hash_token, new_api_token
from eifo_core.types import utcnow

logger = logging.getLogger("eifo.api.oauth_server")

#: The one scope there is. Asking for anything else is asking for nothing.
SCOPE = "read"

ACCESS_LIFETIME = dt.timedelta(hours=1)
REFRESH_LIFETIME = dt.timedelta(days=30)
CODE_LIFETIME = dt.timedelta(minutes=5)
#: How long a member has to approve, from the app sending them here.
CONSENT_LIFETIME = dt.timedelta(minutes=15)
#: An app nobody approved within this long is forgotten.
UNUSED_CLIENT_LIFETIME = dt.timedelta(days=7)
#: Registrations kept at once. Far past any real instance's apps; a ceiling
#: for somebody registering in a loop.
MAX_CLIENTS = 500

#: Plain http is a redirect address only for this machine - Claude Code and
#: Claude Desktop listen on a local port for the answer.
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

_CONSENT_SALT = "eifo-connect-request"

_T = TypeVar("_T")


@dataclass(frozen=True, slots=True)
class ConsentRequest:
    """What an app asked for, carried to the consent page and back, sealed."""

    client_id: str
    redirect_uri: str
    redirect_uri_provided_explicitly: bool
    code_challenge: str
    state: str | None
    resource: str | None


@dataclass(frozen=True, slots=True)
class Connection:
    """One app a member approved, as Settings lists it."""

    client_id: str
    client_name: str | None
    redirect_host: str
    connected_at: dt.datetime
    last_used_at: dt.datetime | None


class EifoOAuthProvider:
    """The library's ``OAuthAuthorizationServerProvider``, backed by the catalog."""

    def __init__(self, session_factory: sessionmaker[Session], settings: Settings) -> None:
        self._sessions = session_factory
        self._settings = settings
        self._origin = (settings.public_origin or "").rstrip("/")

    # -- registration ---------------------------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        def load(session: Session) -> OAuthClientInformationFull | None:
            row = session.get(OAuthClient, client_id)
            return None if row is None else OAuthClientInformationFull.model_validate(row.info)

        return await self._run(load)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        for uri in client_info.redirect_uris or []:
            if not acceptable_redirect(str(uri)):
                raise RegistrationError(
                    error="invalid_redirect_uri",
                    error_description=(
                        "Redirect addresses must be https, or http to this machine "
                        f"(localhost): {uri}"
                    ),
                )

        def store(session: Session) -> None:
            now = utcnow()
            session.execute(
                delete(OAuthClient).where(
                    OAuthClient.last_used_at.is_(None),
                    OAuthClient.created_at < now - UNUSED_CLIENT_LIFETIME,
                )
            )
            if (session.query(OAuthClient).count()) >= MAX_CLIENTS:
                session.commit()
                raise RegistrationError(
                    error="invalid_client_metadata",
                    error_description="This instance is not accepting new apps right now.",
                )
            session.add(
                OAuthClient(
                    client_id=client_info.client_id,
                    info=client_info.model_dump(mode="json"),
                    client_name=(client_info.client_name or None),
                    created_at=now,
                )
            )
            session.commit()

        await self._run(store)
        logger.info("app registered: %r", client_info.client_name)

    # -- consent --------------------------------------------------------------

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        """Send the member to the consent page with the request sealed."""
        sealed = self.seal(
            ConsentRequest(
                client_id=client.client_id,
                redirect_uri=str(params.redirect_uri),
                redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
                code_challenge=params.code_challenge,
                state=params.state,
                resource=params.resource,
            )
        )
        return f"{self._origin}/#/connect?{urlencode({'request': sealed})}"

    def seal(self, request: ConsentRequest) -> str:
        return self._serializer().dumps(
            {
                "c": request.client_id,
                "r": request.redirect_uri,
                "e": request.redirect_uri_provided_explicitly,
                "p": request.code_challenge,
                "s": request.state,
                "x": request.resource,
            }
        )

    def unseal(self, sealed: str) -> ConsentRequest | None:
        try:
            raw = self._serializer().loads(sealed, max_age=int(CONSENT_LIFETIME.total_seconds()))
        except (BadSignature, SignatureExpired):
            return None
        if not isinstance(raw, dict):
            return None
        return ConsentRequest(
            client_id=str(raw["c"]),
            redirect_uri=str(raw["r"]),
            redirect_uri_provided_explicitly=bool(raw["e"]),
            code_challenge=str(raw["p"]),
            state=raw.get("s"),
            resource=raw.get("x"),
        )

    def approve(self, session: Session, request: ConsentRequest, user: User) -> str:
        """The member said yes: a one-time code, on the way back to the app."""
        code = secrets.token_urlsafe(32)
        session.add(
            OAuthCode(
                code_hash=hash_token(code),
                client_id=request.client_id,
                user_id=user.id,
                redirect_uri=request.redirect_uri,
                redirect_uri_provided_explicitly=request.redirect_uri_provided_explicitly,
                code_challenge=request.code_challenge,
                scopes=[SCOPE],
                resource=request.resource,
                expires_at=utcnow() + CODE_LIFETIME,
            )
        )
        client = session.get(OAuthClient, request.client_id)
        if client is not None:
            client.last_used_at = utcnow()
        session.commit()
        logger.info("member %s connected app %s", user.id, request.client_id)
        return construct_redirect_uri(
            request.redirect_uri, code=code, state=request.state, iss=self._origin or None
        )

    def deny(self, request: ConsentRequest) -> str:
        return construct_redirect_uri(
            request.redirect_uri,
            error="access_denied",
            error_description="The member did not allow it.",
            state=request.state,
        )

    # -- codes and tokens -----------------------------------------------------

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        def load(session: Session) -> AuthorizationCode | None:
            row = session.get(OAuthCode, hash_token(authorization_code))
            if row is None or row.client_id != client.client_id or row.expires_at <= utcnow():
                return None
            return AuthorizationCode(
                code=authorization_code,
                scopes=list(row.scopes),
                expires_at=row.expires_at.timestamp(),
                client_id=row.client_id,
                code_challenge=row.code_challenge,
                redirect_uri=AnyUrl(row.redirect_uri),
                redirect_uri_provided_explicitly=row.redirect_uri_provided_explicitly,
                resource=row.resource,
                subject=str(row.user_id),
            )

        return await self._run(load)

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        def exchange(session: Session) -> OAuthToken:
            row = session.get(OAuthCode, hash_token(authorization_code.code))
            if row is None:
                raise TokenError(error="invalid_grant", error_description="Code already used.")
            user_id = row.user_id
            resource = row.resource
            # Spent the moment it is used, whatever happens next.
            session.delete(row)
            session.flush()
            token = self._issue(session, client, user_id, resource)
            session.commit()
            return token

        return await self._run(exchange)

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        def load(session: Session) -> RefreshToken | None:
            row = session.get(OAuthRefreshToken, hash_token(refresh_token))
            if row is None or row.client_id != client.client_id:
                return None
            if row.used_at is not None:
                # Already traded in, and here again: two parties hold it.
                logger.warning(
                    "refresh token reused for app %s, member %s: revoking the connection",
                    row.client_id,
                    row.user_id,
                )
                self._disconnect(session, row.user_id, row.client_id)
                session.commit()
                return None
            if row.expires_at <= utcnow():
                return None
            return RefreshToken(
                token=refresh_token,
                client_id=row.client_id,
                scopes=list(row.scopes),
                expires_at=int(row.expires_at.timestamp()),
                resource=row.resource,
                subject=str(row.user_id),
            )

        return await self._run(load)

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        if any(scope != SCOPE for scope in scopes):
            raise TokenError(error="invalid_scope", error_description="Only 'read' is offered.")

        def exchange(session: Session) -> OAuthToken:
            row = session.get(OAuthRefreshToken, hash_token(refresh_token.token))
            if row is None or row.used_at is not None:
                raise TokenError(error="invalid_grant", error_description="Refresh token used.")
            row.used_at = utcnow()
            session.flush()
            token = self._issue(session, client, row.user_id, row.resource)
            session.commit()
            return token

        return await self._run(exchange)

    async def load_access_token(self, token: str) -> AccessToken | None:
        """Any live token this instance issued - an app's, or one made in Settings.

        What it may then reach is the token's own scope, enforced where the
        tools call the API; this only says whether it is a token at all.
        """

        def load(session: Session) -> AccessToken | None:
            row = session.get(ApiToken, hash_token(token))
            now = utcnow()
            if row is None or (row.expires_at is not None and row.expires_at <= now):
                return None
            return AccessToken(
                token=token,
                client_id=row.client_id or "personal",
                scopes=[SCOPE],
                expires_at=int(row.expires_at.timestamp()) if row.expires_at else None,
                subject=str(row.user_id),
            )

        return await self._run(load)

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        def revoke(session: Session) -> None:
            if token.subject is None or token.client_id == "personal":
                return
            self._disconnect(session, int(token.subject), token.client_id)
            session.commit()

        await self._run(revoke)

    # -- connections, for Settings --------------------------------------------

    def connections(self, session: Session, user: User) -> list[Connection]:
        """The apps this member has approved and not since disconnected."""
        rows = session.execute(
            select(OAuthRefreshToken.client_id, OAuthRefreshToken.created_at, OAuthClient)
            .join(OAuthClient, OAuthClient.client_id == OAuthRefreshToken.client_id)
            .where(
                OAuthRefreshToken.user_id == user.id,
                OAuthRefreshToken.used_at.is_(None),
                OAuthRefreshToken.expires_at > utcnow(),
            )
        ).all()
        first = {
            client_id: created
            for client_id, created in session.execute(
                select(OAuthRefreshToken.client_id, OAuthRefreshToken.created_at)
                .where(OAuthRefreshToken.user_id == user.id)
                .order_by(OAuthRefreshToken.created_at.desc())
            ).all()
        }
        found: dict[str, Connection] = {}
        for client_id, created_at, client in rows:
            found[client_id] = Connection(
                client_id=client_id,
                client_name=client.client_name,
                redirect_host=redirect_host(client.info),
                connected_at=first.get(client_id, created_at),
                last_used_at=created_at,
            )
        return sorted(found.values(), key=lambda c: c.last_used_at or c.connected_at, reverse=True)

    def disconnect(self, session: Session, user: User, client_id: str) -> bool:
        removed = self._disconnect(session, user.id, client_id)
        session.commit()
        return removed

    # -- internals ------------------------------------------------------------

    def _issue(
        self,
        session: Session,
        client: OAuthClientInformationFull,
        user_id: int,
        resource: str | None,
    ) -> OAuthToken:
        user = session.get(User, user_id)
        if user is None or not members.may_sign_in(session, self._settings, user.email):
            raise TokenError(
                error="invalid_grant", error_description="This account may no longer sign in."
            )

        now = utcnow()
        # Tidy as it goes: an app refreshes hourly, and every refresh would
        # otherwise leave the last hour's token behind.
        session.execute(
            delete(ApiToken).where(
                ApiToken.user_id == user_id,
                ApiToken.client_id == client.client_id,
                ApiToken.expires_at <= now,
            )
        )
        session.execute(
            delete(OAuthRefreshToken).where(
                OAuthRefreshToken.user_id == user_id,
                OAuthRefreshToken.client_id == client.client_id,
                OAuthRefreshToken.expires_at <= now,
            )
        )

        access = new_api_token()
        session.add(
            ApiToken(
                token_hash=hash_token(access),
                user_id=user_id,
                name=(client.client_name or "Connected app")[:100],
                scope=TokenScope.READ,
                client_id=client.client_id,
                expires_at=now + ACCESS_LIFETIME,
            )
        )
        refresh = f"eifo_rt_{secrets.token_urlsafe(32)}"
        session.add(
            OAuthRefreshToken(
                token_hash=hash_token(refresh),
                client_id=client.client_id,
                user_id=user_id,
                scopes=[SCOPE],
                resource=resource,
                created_at=now,
                expires_at=now + REFRESH_LIFETIME,
            )
        )
        row = session.get(OAuthClient, client.client_id)
        if row is not None:
            row.last_used_at = now
        return OAuthToken(
            access_token=access,
            expires_in=int(ACCESS_LIFETIME.total_seconds()),
            scope=SCOPE,
            refresh_token=refresh,
        )

    def _disconnect(self, session: Session, user_id: int, client_id: str) -> bool:
        tokens = _deleted(
            session.execute(
                delete(ApiToken).where(ApiToken.user_id == user_id, ApiToken.client_id == client_id)
            )
        )
        refreshes = _deleted(
            session.execute(
                delete(OAuthRefreshToken).where(
                    OAuthRefreshToken.user_id == user_id, OAuthRefreshToken.client_id == client_id
                )
            )
        )
        session.execute(
            delete(OAuthCode).where(OAuthCode.user_id == user_id, OAuthCode.client_id == client_id)
        )
        return bool(tokens or refreshes)

    def _serializer(self) -> URLSafeTimedSerializer:
        return URLSafeTimedSerializer(signing_secret(self._settings), salt=_CONSENT_SALT)

    async def _run(self, work: Callable[[Session], _T]) -> _T:
        """Database work off the event loop, in a session of its own."""

        def run() -> _T:
            with self._sessions() as session:
                return work(session)

        return await anyio.to_thread.run_sync(run)


def _deleted(result: Any) -> int:
    """Rows a DELETE removed."""
    return int(cast(CursorResult[Any], result).rowcount or 0)


def acceptable_redirect(uri: str) -> bool:
    """https anywhere, or plain http to this machine only."""
    parts = urlsplit(uri)
    if parts.fragment:
        return False
    if parts.scheme == "https":
        return bool(parts.hostname)
    return parts.scheme == "http" and parts.hostname in LOOPBACK_HOSTS


def redirect_host(info: dict[str, Any]) -> str:
    """Where an app sends a member back - the part of it that cannot lie."""
    uris = info.get("redirect_uris") or []
    hosts = sorted({urlsplit(str(uri)).hostname or "" for uri in uris} - {""})
    return ", ".join(hosts) or "unknown"
