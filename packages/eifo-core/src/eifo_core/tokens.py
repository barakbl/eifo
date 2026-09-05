"""How a credential is made and how it is stored.

In core because two packages now mint tokens: the API, when somebody presses
the button in Settings, and the fetcher, for the times the button cannot be
reached. Two implementations of "what a token looks like and how it is hashed"
would be two things that must never disagree - and the way they would announce
their disagreement is a token that works in one place and is silently rejected
in the other.

Only the hash is ever stored. That is the whole reason the raw value is
returned once and never again: a copy of the database is not a set of working
credentials.
"""

from __future__ import annotations

import hashlib
import secrets

#: How an API token announces itself. Anything without it is not one of ours.
#:
#: The prefix is not decoration. A token pasted into a script, a log or an issue
#: is a credential somebody has to be able to identify as one - both to know to
#: revoke it and, for the scanners that read public repositories, to know what
#: they have found. It also lets the API tell a token that is not ours from one
#: that is merely wrong, and answer the two differently.
API_TOKEN_PREFIX = "eifo_pat_"

#: Bytes of randomness behind the prefix. 256 bits: this is a bearer credential
#: with no second factor and no expiry, so guessing must not be a strategy.
TOKEN_BYTES = 32


def new_api_token() -> str:
    """A personal API token, prefixed so it is recognisable on sight."""
    return f"{API_TOKEN_PREFIX}{secrets.token_urlsafe(TOKEN_BYTES)}"


def hash_token(token: str) -> str:
    """The stored form of a session or API token.

    Plain SHA-256 rather than a password hash, and deliberately: this is a
    256-bit random value, not something a person chose. There is no dictionary
    to attack and no work factor worth paying on every request that presents
    one - the property being relied on is that the input has enough entropy
    that reversing the digest is not a route.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def looks_like_api_token(value: str) -> bool:
    """Whether this is shaped like one of ours, before anything is done with it.

    Checked where a token is pasted rather than where it is used, so a
    mis-paste is refused at the moment it can still be explained instead of
    becoming a 401 nobody can account for later.
    """
    return value.strip().startswith(API_TOKEN_PREFIX)
