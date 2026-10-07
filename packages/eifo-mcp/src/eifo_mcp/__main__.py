"""``eifo-mcp``: serve one Eifo instance to an AI assistant over stdio.

Configured by two environment variables, set in the assistant's MCP settings:

* ``EIFO_URL`` - the instance, e.g. ``eifo.example.com``
* ``EIFO_TOKEN`` - an API token from the instance's Settings. Make it a
  read-only one: everything here only reads, and a token in an assistant's
  configuration should not be able to do more than the assistant needs.

Anything printed goes to stderr, never stdout: stdout is the protocol.
"""

from __future__ import annotations

import logging
import os
import sys

from eifo_mcp.client import ConfigError, EifoClient, EifoError
from eifo_mcp.server import build_server


def main() -> None:
    # One line per request is noise in an assistant's server log. URLs only -
    # the token travels in a header and is never logged - but noise all the same.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        client = EifoClient(os.environ.get("EIFO_URL", ""), os.environ.get("EIFO_TOKEN", ""))
    except ConfigError as error:
        print(f"eifo-mcp: {error}", file=sys.stderr)
        sys.exit(2)

    _say_what_the_token_can_do(client)
    try:
        build_server(client).run("stdio")
    finally:
        client.close()


def _say_what_the_token_can_do(client: EifoClient) -> None:
    """A word on stderr, where the assistant's logs keep it, before serving.

    A token that is refused is said once here rather than discovered by every
    tool call; one with more access than this needs is worth a warning too.
    Neither stops the server - a server that is briefly unreachable at start
    should not mean an assistant with no Eifo until it is restarted.
    """
    try:
        me = client.me()
    except EifoError as error:
        print(f"eifo-mcp: {error}", file=sys.stderr)
        return
    scope = me.get("token_scope")
    print(
        f"eifo-mcp: serving {client.base_url} as {me['user'].get('display_name')} "
        f"(token scope: {scope})",
        file=sys.stderr,
    )
    if scope == "full":
        print(
            "eifo-mcp: this token has full access. It only needs to read - create a "
            "read-only token in Eifo's Settings and use that instead.",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
