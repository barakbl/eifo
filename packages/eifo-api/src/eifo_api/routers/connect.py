"""The member's side of "Sign in with Eifo": approving apps, and undoing it.

An app sends the member to ``/#/connect?request=...``; the web app shows them
who is asking and where they will be sent back, using ``GET /connect/request``,
and their answer goes to ``approve`` or ``deny``. Both are a signed-in
browser's to give, never a token's: a token approving apps would be a
credential minting credentials, the one thing tokens may not do.
"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from eifo_api.deps import CsrfDep, PrincipalDep, SessionDep
from eifo_api.oauth_server import SCOPE, ConsentRequest, EifoOAuthProvider, redirect_host
from eifo_core.models import OAuthClient

router = APIRouter(tags=["user"])


class ConnectRequestOut(BaseModel):
    """Who is asking, as the consent page shows it."""

    #: What the app calls itself. Its own claim.
    client_name: str | None = None
    #: Where approving sends the member - the part an app cannot fake.
    redirect_host: str
    #: What it would be allowed: always read only here.
    scopes: list[str]


class ConnectAnswer(BaseModel):
    request: str = Field(min_length=1, max_length=4000)


class Redirect(BaseModel):
    """Where the browser goes next: back to the app."""

    redirect: str


class ConnectionOut(BaseModel):
    client_id: str
    client_name: str | None = None
    redirect_host: str
    connected_at: dt.datetime
    last_used_at: dt.datetime | None = None


def _provider(request: Request) -> EifoOAuthProvider:
    provider: EifoOAuthProvider | None = getattr(request.app.state, "oauth_provider", None)
    if provider is None:
        raise HTTPException(status_code=404, detail="Connecting apps is not enabled here.")
    return provider


def _open(request: Request, session: SessionDep, sealed: str) -> tuple[ConsentRequest, OAuthClient]:
    consent = _provider(request).unseal(sealed)
    if consent is None:
        raise HTTPException(
            status_code=400,
            detail="This request has expired or is not valid. Start again from the app.",
        )
    client = session.get(OAuthClient, consent.client_id)
    if client is None:
        raise HTTPException(status_code=400, detail="The app asking is no longer registered.")
    return consent, client


def _a_person(principal: PrincipalDep) -> None:
    if principal.via_token:
        raise HTTPException(
            status_code=403, detail="Only a signed-in person can connect an app, not a token."
        )


@router.get("/connect/request", response_model=ConnectRequestOut, summary="Who is asking")
def read_request(
    request: Request, principal: PrincipalDep, session: SessionDep, sealed: str
) -> ConnectRequestOut:
    """What the consent page needs to show, for a request an app sent here."""
    _a_person(principal)
    _, client = _open(request, session, sealed)
    return ConnectRequestOut(
        client_name=client.client_name,
        redirect_host=redirect_host(client.info),
        scopes=[SCOPE],
    )


@router.post("/connect/approve", response_model=Redirect, summary="Allow an app")
def approve(
    body: ConnectAnswer,
    request: Request,
    principal: PrincipalDep,
    _csrf: CsrfDep,
    session: SessionDep,
) -> Redirect:
    """Give the app read access, and send the browser back to it with a code."""
    _a_person(principal)
    consent, _ = _open(request, session, body.request)
    return Redirect(redirect=_provider(request).approve(session, consent, principal.user))


@router.post("/connect/deny", response_model=Redirect, summary="Refuse an app")
def deny(
    body: ConnectAnswer,
    request: Request,
    principal: PrincipalDep,
    _csrf: CsrfDep,
    session: SessionDep,
) -> Redirect:
    """Send the browser back to the app with a refusal."""
    _a_person(principal)
    consent, _ = _open(request, session, body.request)
    return Redirect(redirect=_provider(request).deny(consent))


@router.get(
    "/me/connections", response_model=list[ConnectionOut], summary="Apps you have connected"
)
def my_connections(
    request: Request, principal: PrincipalDep, session: SessionDep
) -> list[ConnectionOut]:
    provider: EifoOAuthProvider | None = getattr(request.app.state, "oauth_provider", None)
    if provider is None:
        return []
    return [
        ConnectionOut(
            client_id=connection.client_id,
            client_name=connection.client_name,
            redirect_host=connection.redirect_host,
            connected_at=connection.connected_at,
            last_used_at=connection.last_used_at,
        )
        for connection in provider.connections(session, principal.user)
    ]


@router.delete("/me/connections/{client_id}", status_code=204, summary="Disconnect an app")
def disconnect(
    client_id: str,
    request: Request,
    principal: PrincipalDep,
    _csrf: CsrfDep,
    session: SessionDep,
) -> Response:
    """Revoke every token the app holds for you, at once."""
    if not _provider(request).disconnect(session, principal.user, client_id):
        raise HTTPException(status_code=404, detail="No app by that id is connected.")
    return Response(status_code=204)
