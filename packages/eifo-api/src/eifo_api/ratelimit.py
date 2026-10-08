"""How fast anybody may call the API.

Token buckets, kept in this process's memory: each caller has a bucket that
holds up to ``burst`` requests and refills at ``per_second``. A page of the web
app is a burst of a few calls and then nothing; a script stuck in a loop, a
guessed-password run against the sign-in or an assistant gone wild is a steady
stream, and the bucket is what tells the two apart.

Who "a caller" is decides most of it:

* a signed-in member, or a token, is one caller wherever they are - keyed on a
  hash of the credential, never the credential;
* somebody with no credential is their address;
* every address also has a ceiling over everything sent from it, so inventing a
  fresh fake token per request does not buy a fresh bucket per request.

Only the API is limited - ``/api/...``, and the OAuth and MCP endpoints the
remote connector adds. Static files and posters are not: one page of the
catalog loads dozens of them, and they cost nothing to serve.

The fetcher's ingest calls carry an administrator's token and are exempt: a
sync sends chunks as fast as it can work them out, and an instance's own
fetcher is not who this is for.

Memory, not the database, so a limit costs nothing to check and nothing to
forget. With several worker processes each keeps its own buckets, which
loosens every limit by the number of workers rather than breaking anything.
"""

from __future__ import annotations

import hashlib
import logging
import math
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from http.cookies import SimpleCookie
from typing import Any

from eifo_api.errors import problem_response
from eifo_api.security import SESSION_COOKIE

logger = logging.getLogger("eifo.api.ratelimit")

API_PREFIX = "/api/"

#: Methods that only read. Everything else is a write and also draws from
#: the much smaller write bucket.
READS = frozenset({"GET", "HEAD", "OPTIONS"})

#: Where a sign-in starts and comes back. A guessing run, and the one place a
#: real person never needs to be more than a few times a minute.
SIGN_IN_PREFIXES = ("/api/v1/auth/login/", "/api/v1/auth/callback/")

INGEST_PREFIX = "/api/v1/ingest/"


@dataclass(frozen=True, slots=True)
class Rate:
    per_second: float
    burst: int


#: A signed-in member or a token: far past a person clicking, and past a page
#: that fires a handful of requests at once.
MEMBER = Rate(per_second=10, burst=30)
#: Somebody not signed in, per address.
ANONYMOUS = Rate(per_second=5, burst=20)
#: Everything from one address, signed in or not. Above any member's own
#: limit, so it only ever stops somebody rotating credentials to dodge theirs.
ADDRESS = Rate(per_second=30, burst=90)
#: Changing something - a rating, a list, a setting.
WRITES = Rate(per_second=2, burst=10)
#: Starting or finishing a sign-in, per address: ten a minute.
SIGN_IN = Rate(per_second=10 / 60, burst=10)

#: Buckets kept before the least recently used is forgotten. A forgotten
#: bucket is a full one, which only ever errs toward letting somebody in.
MAX_BUCKETS = 20_000


@dataclass(frozen=True, slots=True)
class Draw:
    """One bucket a request has to take a token from."""

    key: str
    rate: Rate


class Buckets:
    """Token buckets by key."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._held: OrderedDict[str, tuple[float, float]] = OrderedDict()
        self._lock = threading.Lock()

    def take(self, draws: list[Draw]) -> float:
        """Take one token from every bucket, or none of them.

        Returns 0 when the request may go ahead, else how many seconds until
        it could. All or nothing, so a request refused by one bucket does not
        quietly spend the others.
        """
        now = self._clock()
        with self._lock:
            levels = [self._level(draw, now) for draw in draws]
            waits = [
                (1 - level) / draw.rate.per_second
                for draw, level in zip(draws, levels, strict=True)
                if level < 1
            ]
            if waits:
                for draw, level in zip(draws, levels, strict=True):
                    self._store(draw.key, level, now)
                return max(waits)
            for draw, level in zip(draws, levels, strict=True):
                self._store(draw.key, level - 1, now)
            return 0.0

    def forget(self) -> None:
        with self._lock:
            self._held.clear()

    def _level(self, draw: Draw, now: float) -> float:
        held = self._held.get(draw.key)
        if held is None:
            return float(draw.rate.burst)
        tokens, stamp = held
        return min(float(draw.rate.burst), tokens + (now - stamp) * draw.rate.per_second)

    def _store(self, key: str, tokens: float, now: float) -> None:
        self._held[key] = (tokens, now)
        self._held.move_to_end(key)
        while len(self._held) > MAX_BUCKETS:
            self._held.popitem(last=False)


def draws_for(method: str, path: str, *, credential: str | None, address: str) -> list[Draw]:
    """The buckets one request takes from; empty for what is not limited.

    ``credential`` is an opaque stand-in for the caller's session or token -
    a hash, never the thing itself.
    """
    if not path.startswith(API_PREFIX):
        return []
    if path.startswith(SIGN_IN_PREFIXES):
        return [Draw(f"sign-in:{address}", SIGN_IN)]
    if path.startswith(INGEST_PREFIX) and credential is not None:
        # The fetcher. A stranger without a token still meets the address
        # limits below, and then the 404 that every ingest route gives them.
        return []

    caller = f"cred:{credential}" if credential is not None else f"addr:{address}"
    draws = [
        Draw(f"address:{address}", ADDRESS),
        Draw(caller, MEMBER if credential is not None else ANONYMOUS),
    ]
    if method.upper() not in READS:
        draws.append(Draw(f"write:{caller}", WRITES))
    return draws


def credential_of(headers: list[tuple[bytes, bytes]]) -> str | None:
    """A short hash of the bearer token or session cookie, if there is one.

    Hashed so the buckets - and any log line about them - never hold a
    credential. Not verified: a made-up token gets its own bucket, and the
    address ceiling is what stops making up a new one per request paying off.
    """
    authorization = cookie = None
    for name, value in headers:
        if name == b"authorization":
            authorization = value.decode("latin-1")
        elif name == b"cookie":
            cookie = value.decode("latin-1")

    secret = None
    if authorization and authorization.lower().startswith("bearer "):
        secret = authorization[7:].strip()
    elif cookie:
        jar: SimpleCookie = SimpleCookie()
        try:
            jar.load(cookie)
        except Exception:  # a malformed Cookie header is no credential
            jar = SimpleCookie()
        morsel = jar.get(SESSION_COOKIE)
        secret = morsel.value if morsel is not None else None
    if not secret:
        return None
    return hashlib.sha256(secret.encode()).hexdigest()[:24]


class RateLimitMiddleware:
    """Refuse a request with 429 when its caller has spent their bucket."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        state = scope["app"].state
        if not state.settings.rate_limit:
            await self.app(scope, receive, send)
            return

        client = scope.get("client")
        address = client[0] if client else "unknown"
        draws = draws_for(
            scope["method"],
            scope["path"],
            credential=credential_of(scope["headers"]),
            address=address,
        )
        if not draws:
            await self.app(scope, receive, send)
            return

        wait = state.rate_buckets.take(draws)
        if wait <= 0:
            await self.app(scope, receive, send)
            return

        retry_after = max(1, math.ceil(wait))
        logger.info(
            "rate limited %s %s (%s)", scope["method"], scope["path"], draws[-1].key.split(":")[0]
        )
        response = problem_response(
            status=429,
            title="Too many requests",
            detail=f"That is more than this service takes at once. Try again in {retry_after}s.",
        )
        response.headers["Retry-After"] = str(retry_after)
        await response(scope, receive, send)
