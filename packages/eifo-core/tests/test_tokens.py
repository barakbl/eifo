"""What a credential looks like, and what is kept of it.

These live here rather than beside either caller because the module exists to
stop the API and the fetcher from each having their own answer. A test that ran
only in one package would leave the other free to drift, which is the exact
failure the module was written to prevent: a token that works in one place and
is silently rejected in the other.
"""

from __future__ import annotations

import hashlib

from eifo_core.tokens import (
    API_TOKEN_PREFIX,
    TOKEN_BYTES,
    hash_token,
    looks_like_api_token,
    new_api_token,
)


class TestMintingOne:
    def test_it_announces_itself(self) -> None:
        # The prefix is what lets a token pasted into a log or an issue be
        # recognised as a credential by whoever has to go and revoke it.
        assert new_api_token().startswith(API_TOKEN_PREFIX)

    def test_no_two_are_alike(self) -> None:
        assert len({new_api_token() for _ in range(100)}) == 100

    def test_it_carries_the_randomness_it_claims_to(self) -> None:
        # A bearer credential with no second factor and no expiry. If this ever
        # shrinks, guessing becomes a strategy - so assert the width rather
        # than trust the constant to be the one that was used.
        body = new_api_token().removeprefix(API_TOKEN_PREFIX)

        assert TOKEN_BYTES == 32
        assert len(body) >= 40, f"{len(body)} characters is not 256 bits of base64url"


class TestStoringOne:
    def test_only_the_digest_is_ever_kept(self) -> None:
        token = new_api_token()

        stored = hash_token(token)

        assert token not in stored
        assert stored == hashlib.sha256(token.encode("utf-8")).hexdigest()

    def test_the_same_token_always_hashes_the_same(self) -> None:
        # Unsalted, deliberately: a lookup by hash is how a presented token is
        # matched to its row, and a per-row salt would make that a table scan.
        token = new_api_token()

        assert hash_token(token) == hash_token(token)

    def test_the_digest_is_the_width_the_column_declares(self) -> None:
        # api_tokens.token_hash is VARCHAR(64). A wider digest would be
        # truncated by SQLite without complaint and match the wrong row.
        assert len(hash_token(new_api_token())) == 64

    def test_it_hashes_something_that_is_not_a_token(self) -> None:
        # Sessions are hashed with the same function, and they carry no prefix.
        assert len(hash_token("session-value")) == 64


class TestRecognisingOne:
    def test_it_accepts_one_of_ours(self) -> None:
        assert looks_like_api_token(new_api_token())

    def test_it_forgives_the_whitespace_a_paste_brings(self) -> None:
        assert looks_like_api_token(f"  {new_api_token()}\n")

    def test_it_refuses_what_is_not_one(self) -> None:
        # Checked where a token is pasted, so a mis-paste is refused while it
        # can still be explained rather than becoming an unaccountable 401.
        assert not looks_like_api_token("")
        assert not looks_like_api_token("https://example.com")
        assert not looks_like_api_token("Bearer eifo_pat_something")
