"""The remote MCP connector: Eifo's tools at ``/mcp``, behind "Sign in with Eifo".

The same twelve read-only tools the local ``eifo-mcp`` serves, for assistants
that cannot run a program on anybody's machine - Claude on the web and on a
phone. An app finds the OAuth endpoints from ``/.well-known/...``, sends the
member to approve it, and calls ``/mcp`` with the hour-long token it is given.

The tools do not reach into the database. They call this API, in process, with
the token the request arrived with - so a tool sees exactly what that member's
read-only token may see, by the same allow-list as every other client, and the
rate limiter counts it like any other call. Nothing here is a second way in.

The OAuth endpoints sit at the root, where the specification's discovery puts
them (``/authorize``, ``/token``, ``/register``, ``/revoke``); none of those
paths means anything to the web app, whose pages live after the ``#``.
"""

from __future__ import annotations

import logging
from typing import Any

import anyio.from_thread
import httpx
from fastapi import FastAPI
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl
from starlette.routing import Route

from eifo_api.oauth_server import SCOPE, EifoOAuthProvider

# Who a tool's call to the API comes from, as the rate limiter sees it - not
# an address anybody outside can send from: the server writes the client of a
# real connection itself.
from eifo_api.ratelimit import IN_PROCESS
from eifo_core.settings import Settings
from eifo_mcp.client import EifoClient
from eifo_mcp.server import build_server

logger = logging.getLogger("eifo.api.remote_mcp")

MCP_PATH = "/mcp"


class InProcessTransport(httpx.BaseTransport):
    """A synchronous httpx transport that answers from the app itself.

    The tools are synchronous and the library runs each in a worker thread, so
    a request from one is handed back to the event loop that serves the app -
    no socket, no second process, and no way to deadlock the single worker.
    """

    def __init__(self, app: Any) -> None:
        self._asgi = httpx.ASGITransport(app=app, client=(IN_PROCESS, 0))

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        body = request.read()
        async_request = httpx.Request(
            request.method, request.url, headers=request.headers, content=body
        )

        async def send() -> tuple[int, list[tuple[bytes, bytes]], bytes]:
            response = await self._asgi.handle_async_request(async_request)
            content = b"".join([chunk async for chunk in response.stream])  # type: ignore[union-attr]
            return response.status_code, response.headers.raw, content

        status, headers, content = anyio.from_thread.run(send)
        return httpx.Response(status, headers=headers, content=content, request=request)


def _caller_token() -> str:
    """The token the current MCP request was authenticated with."""
    access = get_access_token()
    if access is None:  # the library refuses unauthenticated calls before this
        raise PermissionError("No token on this request.")
    return access.token


def mount_remote_mcp(app: FastAPI, settings: Settings) -> StreamableHTTPSessionManager | None:
    """Add the connector's routes to ``app``; returns what the lifespan must run.

    Off - and saying why in the log - without a public origin the OAuth
    discovery documents can name, or when switched off.
    """
    origin = (settings.public_origin or "").rstrip("/")
    if not settings.remote_mcp:
        return None
    if not origin.startswith("https://") and not origin.startswith(
        ("http://localhost", "http://127.0.0.1")
    ):
        logger.warning(
            "remote MCP connector off: it needs EIFO_PUBLIC_ORIGIN set to this "
            "instance's https address (it is %r)",
            settings.public_origin,
        )
        return None

    provider = EifoOAuthProvider(app.state.session_factory, settings)
    app.state.oauth_provider = provider

    client = EifoClient(origin, _caller_token, http=httpx.Client(transport=InProcessTransport(app)))
    server = build_server(
        client,
        auth_server_provider=provider,
        auth=AuthSettings(
            issuer_url=AnyHttpUrl(origin),
            resource_server_url=AnyHttpUrl(f"{origin}{MCP_PATH}"),
            # This server issues tokens for one resource - its own /mcp - so a
            # token it recognises at all was issued for it. Checking the
            # audience on top would refuse the Settings-made tokens it also
            # accepts, which carry no resource.
            validate_token_resource=False,
            required_scopes=[SCOPE],
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE]
            ),
            revocation_options=RevocationOptions(enabled=True),
        ),
    )
    # Bearer tokens on every call are the protection here; DNS-rebinding
    # checks are for unauthenticated servers listening on localhost.
    connector = server.streamable_http_app(
        streamable_http_path=MCP_PATH,
        # Plain JSON answers rather than an event stream: every tool here
        # answers once, and JSON is what every client reads.
        json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        host=origin,
    )
    for route in connector.routes:
        path = getattr(route, "path", None)
        if path:
            # The whole connector app as the endpoint, so its own middleware
            # (bearer authentication) runs and its router matches the path.
            app.router.routes.append(Route(path, endpoint=connector))
    logger.info("remote MCP connector at %s%s", origin, MCP_PATH)
    return server.session_manager
