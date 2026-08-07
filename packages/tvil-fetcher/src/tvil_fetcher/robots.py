"""robots.txt enforcement.

docs.internal/03-sources.md commits every scraping plugin to respecting
robots.txt. Making that a property of the HTTP layer rather than of each plugin
author's diligence is the only way the promise actually holds — and it is how we
learned that several Israeli broadcasters disallow their own VOD indexes.

Fetching robots.txt itself is always permitted; a site that cannot serve one is
treated as fully allowed, matching RFC 9309.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from urllib.parse import urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import httpx

logger = logging.getLogger("tvil.fetch.robots")


class RobotsDisallowedError(RuntimeError):
    """A URL this project has promised not to fetch."""

    def __init__(self, url: str, user_agent: str) -> None:
        super().__init__(
            f"robots.txt disallows {url!r} for {user_agent!r}; "
            f"TVIL does not fetch disallowed paths (docs.internal/03-sources.md)"
        )
        self.url = url


class RobotsPolicy:
    """Per-host robots.txt rules, fetched once and cached."""

    def __init__(self, user_agent: str, *, fetch: Callable[[str], str] | None = None) -> None:
        self._user_agent = user_agent
        self._parsers: dict[str, RobotFileParser | None] = {}
        self._fetch = fetch

    def _robots_url(self, url: str) -> tuple[str, str]:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        return origin, urlunsplit((parts.scheme, parts.netloc, "/robots.txt", "", ""))

    def _parser_for(self, url: str) -> RobotFileParser | None:
        origin, robots_url = self._robots_url(url)
        if origin in self._parsers:
            return self._parsers[origin]

        parser: RobotFileParser | None = None
        try:
            text = self._read(robots_url)
        except Exception as exc:
            # No robots.txt, or it could not be read: nothing is disallowed.
            logger.debug("no usable robots.txt at %s (%r)", robots_url, exc)
        else:
            parser = RobotFileParser()
            parser.parse(text.splitlines())

        self._parsers[origin] = parser
        return parser

    def _read(self, robots_url: str) -> str:
        if self._fetch is not None:
            return self._fetch(robots_url)
        response = httpx.get(
            robots_url,
            timeout=10.0,
            follow_redirects=True,
            headers={"User-Agent": self._user_agent},
        )
        response.raise_for_status()
        return response.text

    def allows(self, url: str) -> bool:
        """Whether robots.txt permits fetching ``url``."""
        parser = self._parser_for(url)
        if parser is None:
            return True
        return parser.can_fetch(self._user_agent, url)

    def require_allowed(self, url: str) -> None:
        """Raise unless ``url`` may be fetched.

        Raises:
            RobotsDisallowedError: when robots.txt forbids it.
        """
        if not self.allows(url):
            raise RobotsDisallowedError(url, self._user_agent)
