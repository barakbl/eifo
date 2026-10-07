"""Configuration: where the token may be sent, and what happens without one."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from eifo_mcp import __main__ as entry
from eifo_mcp.client import ConfigError, EifoClient, EifoError, normalise_base_url


class TestTheAddress:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("eifo.example.com", "https://eifo.example.com"),
            ("https://eifo.example.com/", "https://eifo.example.com"),
            ("https://eifo.example.com/api/v1", "https://eifo.example.com"),
            ("  https://eifo.example.com:8443  ", "https://eifo.example.com:8443"),
            ("https://example.com/eifo", "https://example.com/eifo"),
            ("http://localhost:3436", "http://localhost:3436"),
            ("http://127.0.0.1:3436", "http://127.0.0.1:3436"),
        ],
    )
    def test_is_taken_as_people_write_it(self, raw: str, expected: str) -> None:
        assert normalise_base_url(raw) == expected

    @pytest.mark.parametrize("raw", ["http://eifo.example.com", "http://203.0.113.7"])
    def test_plain_http_to_anywhere_else_is_refused(self, raw: str) -> None:
        """The token rides on every request; over http it rides readable."""
        with pytest.raises(ConfigError, match="unencrypted"):
            normalise_base_url(raw)

    @pytest.mark.parametrize("raw", ["", "   ", "ftp://eifo.example.com", "https://"])
    def test_nonsense_is_refused(self, raw: str) -> None:
        with pytest.raises(ConfigError):
            normalise_base_url(raw)


class TestTheToken:
    def test_is_required(self) -> None:
        with pytest.raises(ConfigError, match="read-only token"):
            EifoClient("eifo.example.com", "  ")

    def test_is_sent_and_only_as_a_bearer(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json=[])

        client = EifoClient(
            "eifo.example.com",
            "eifo_pat_secret",
            http=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        client.get("/genres", {"unused": None})

        assert seen[0].headers["Authorization"] == "Bearer eifo_pat_secret"
        assert str(seen[0].url) == "https://eifo.example.com/api/v1/genres"
        assert "eifo_pat_secret" not in str(seen[0].url)


class TestWhenTheServerIsAway:
    def test_says_where_it_tried(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no route", request=request)

        client = EifoClient(
            "eifo.example.com", "t", http=httpx.Client(transport=httpx.MockTransport(handler))
        )

        with pytest.raises(EifoError, match=r"Could not reach Eifo at https://eifo\.example\.com"):
            client.get("/meta")

    def test_a_server_error_without_words_still_says_something(self) -> None:
        client = EifoClient(
            "eifo.example.com",
            "t",
            http=httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(502))),
        )

        with pytest.raises(EifoError, match="502"):
            client.get("/meta")


class TestReferenceData:
    def test_services_are_asked_for_once_in_a_while(self) -> None:
        calls: list[str] = []
        now = [0.0]

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            return httpx.Response(200, json=[{"id": 1, "key": "netflix_il"}])

        client = EifoClient(
            "eifo.example.com",
            "t",
            http=httpx.Client(transport=httpx.MockTransport(handler)),
            clock=lambda: now[0],
        )
        client.sources()
        client.sources()
        now[0] += 601
        client.sources()

        assert calls == ["/api/v1/sources", "/api/v1/sources"]


class TestStarting:
    def test_without_configuration_it_says_what_is_missing(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.delenv("EIFO_URL", raising=False)
        monkeypatch.delenv("EIFO_TOKEN", raising=False)

        with pytest.raises(SystemExit) as exited:
            entry.main()

        assert exited.value.code == 2
        assert "EIFO_TOKEN is not set" in capsys.readouterr().err

    def test_warns_about_a_full_token_on_stderr(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Never stdout: that is the protocol, and a stray line there breaks it."""
        client = _answering({"user": {"display_name": "Viewer"}, "token_scope": "full"})

        entry._say_what_the_token_can_do(client)

        out, err = capsys.readouterr()
        assert out == ""
        assert "full access" in err

    def test_a_read_token_gets_no_lecture(self, capsys: pytest.CaptureFixture[str]) -> None:
        entry._say_what_the_token_can_do(
            _answering({"user": {"display_name": "Viewer"}, "token_scope": "read"})
        )

        assert "full access" not in capsys.readouterr().err

    def test_a_refused_token_is_reported_not_fatal(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        client = EifoClient(
            "eifo.example.com",
            "t",
            http=httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(401))),
        )

        entry._say_what_the_token_can_do(client)

        assert "did not accept the token" in capsys.readouterr().err


def _answering(payload: Any) -> EifoClient:
    return EifoClient(
        "eifo.example.com",
        "t",
        http=httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
        ),
    )
