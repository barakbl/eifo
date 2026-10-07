"""Calling the server the way an assistant does, and issuing it a token."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from mcp import Client
from mcp.server.mcpserver import MCPServer
from sqlalchemy.orm import Session, sessionmaker

from eifo_core.enums import TokenScope
from eifo_core.models import ApiToken
from eifo_core.tokens import hash_token, new_api_token


def token_for(
    session_factory: sessionmaker[Session], user_id: int, scope: TokenScope = TokenScope.READ
) -> str:
    token = new_api_token()
    with session_factory() as session:
        session.add(
            ApiToken(token_hash=hash_token(token), user_id=user_id, name="assistant", scope=scope)
        )
        session.commit()
    return token


def call(server: MCPServer, tool: str, arguments: dict[str, Any] | None = None) -> Any:
    """Call a tool over the protocol and return what the model would read.

    Raises AssertionError with the message when the tool reported an error, so
    a test of an error asserts on :func:`call_error` instead.
    """
    result = asyncio.run(_call(server, tool, arguments or {}))
    text = "".join(part.text for part in result.content)
    assert not result.is_error, text
    return json.loads(text)


def call_error(server: MCPServer, tool: str, arguments: dict[str, Any] | None = None) -> str:
    """Call a tool expected to fail, and return the message the model reads."""
    result = asyncio.run(_call(server, tool, arguments or {}))
    assert result.is_error, "the tool was expected to fail"
    return "".join(part.text for part in result.content)


async def _call(server: MCPServer, tool: str, arguments: dict[str, Any]) -> Any:
    async with Client(server) as client:
        return await client.call_tool(tool, arguments)
