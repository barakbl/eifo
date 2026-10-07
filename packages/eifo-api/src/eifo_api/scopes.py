"""What a narrowly scoped API token may reach.

An allow-list, not a deny-list. A route added tomorrow is closed to a scoped
token until somebody decides it belongs here - which is the right default for a
credential that lives in an AI assistant's configuration, where it reads text
from scraped catalogs and may be talked into trying things.

Checked once, where a token becomes a principal (:func:`eifo_api.deps`), so no
handler has to remember to ask. Paths are matched after the ``/api/v1`` prefix,
whole, so ``/titles`` does not also admit ``/titles-admin``.
"""

from __future__ import annotations

import re

from eifo_core.enums import TokenScope

#: Reading the catalog and the owner's own lists. Not ``/me/tokens`` - a token
#: has no business enumerating its siblings - and not the TMDB lookup, which
#: spends a shared quota.
_READ: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    ("GET", re.compile(pattern))
    for pattern in (
        r"/meta",
        r"/titles",
        r"/titles/\d+",
        r"/titles/\d+/similar",
        r"/whats-new",
        r"/people/\d+",
        r"/sources",
        r"/suggest",
        r"/genres",
        r"/stats/[a-z-]+",
        r"/me",
        r"/me/items",
        r"/me/items/services",
        r"/me/taste",
        r"/me/for-you",
    )
)

#: Keeping the owner's lists: putting a title on one, rating it, taking it off.
_LISTS = (
    *_READ,
    ("PUT", re.compile(r"/me/items/\d+")),
    ("DELETE", re.compile(r"/me/items/\d+")),
)

_ALLOWED = {TokenScope.READ: _READ, TokenScope.LISTS: _LISTS}

PREFIX = "/api/v1"


def allows(scope: TokenScope, method: str, path: str) -> bool:
    """Whether a token of this scope may make this request."""
    if scope is TokenScope.FULL:
        return True
    if not path.startswith(PREFIX):
        return False
    route = path[len(PREFIX) :].rstrip("/") or "/"
    # HEAD is a GET that only wants the headers; it reads exactly as much.
    verb = "GET" if method == "HEAD" else method
    return any(verb == allowed and pattern.fullmatch(route) for allowed, pattern in _ALLOWED[scope])


def can_write_lists(scope: TokenScope | None) -> bool:
    """Whether a caller of this scope may change its owner's lists.

    None is a browser session, which may do anything its owner may.
    """
    return scope in (None, TokenScope.FULL, TokenScope.LISTS)
