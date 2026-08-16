"""The browser transport's block detection and retry flow, with a fake page.

No Chromium is launched: ``BrowserSession`` only needs Playwright inside
``__enter__``, so tests inject a fake page and exercise the navigation,
interstitial-detection and rate-limiting logic directly.
"""

from __future__ import annotations

from typing import Any

import pytest

from eifo_fetcher.browser import (
    BrowserBlockedError,
    BrowserSession,
)
from eifo_fetcher.http import RateLimiter

URL = "https://www.kan.org.il/lobby/kan-box/"


class FakePage:
    """A minimal stand-in for a Playwright ``Page``."""

    def __init__(
        self, *, title: str = "כאן BOX", body: str = "content", html: str = "<html/>"
    ) -> None:
        self._title = title
        self._body = body
        self._html = html
        self.navigations: list[str] = []
        self.reloads = 0
        self.selector_waits: list[str] = []
        self.fetches: list[str] = []

    # -- queries ------------------------------------------------------
    def title(self) -> str:
        return self._title

    def content(self) -> str:
        return self._html

    def evaluate(self, script: str, arg: Any = None) -> Any:
        if "innerText" in script:
            return self._body
        if "fetch" in script:
            self.fetches.append(arg)
            return [200, "robots-body"]
        raise AssertionError(f"unexpected evaluate: {script[:60]}")

    # -- actions ------------------------------------------------------
    def goto(self, url: str, **kwargs: Any) -> None:
        self.navigations.append(url)

    def reload(self, **kwargs: Any) -> None:
        self.reloads += 1

    def wait_for_selector(self, selector: str, **kwargs: Any) -> None:
        self.selector_waits.append(selector)

    def wait_for_timeout(self, ms: int) -> None:
        pass


def _session(page: FakePage) -> BrowserSession:
    session = BrowserSession(rate_limiter=RateLimiter(default_rps=0), settle_ms=0)
    session._page = page  # tests drive the session without opening a browser
    return session


class TestBlockDetection:
    @pytest.mark.parametrize(
        "title",
        ["Attention Required! | Cloudflare", "Just a moment..."],
    )
    def test_a_challenge_title_is_detected(self, title: str) -> None:
        session = _session(FakePage(title=title))

        with pytest.raises(BrowserBlockedError):
            session.get_html(URL)

    def test_a_hard_block_body_is_detected(self) -> None:
        page = FakePage(title="כאן", body="Sorry, you have been blocked")
        session = _session(page)

        with pytest.raises(BrowserBlockedError):
            session.get_html(URL)

    def test_one_reload_is_attempted_before_giving_up(self) -> None:
        page = FakePage(title="Attention Required! | Cloudflare")
        session = _session(page)

        with pytest.raises(BrowserBlockedError):
            session.get_html(URL)

        assert page.reloads == 1
        assert page.navigations == [URL]  # exactly one real navigation


class UnblockingPage(FakePage):
    """Blocked on first load, clear after the reload - a passed challenge."""

    def __init__(self) -> None:
        super().__init__(title="Attention Required! | Cloudflare")

    def reload(self, **kwargs: Any) -> None:
        super().reload(**kwargs)
        self._title = "כאן BOX"


class TestGetHtml:
    def test_returns_the_rendered_html(self) -> None:
        page = FakePage(html="<html><body>cards</body></html>")
        session = _session(page)

        html = session.get_html(URL, ready_selector="div.card")

        assert html == "<html><body>cards</body></html>"
        assert page.selector_waits == ["div.card"]
        assert page.navigations == [URL]

    def test_recovers_when_the_reload_clears_the_challenge(self) -> None:
        page = UnblockingPage()
        session = _session(page)

        html = session.get_html(URL)

        assert html == "<html/>"
        assert page.reloads == 1

    def test_a_closed_session_refuses_to_navigate(self) -> None:
        with pytest.raises(RuntimeError, match="not open"):
            BrowserSession().get_html(URL)


class TestFetchText:
    def test_fetches_through_the_page_and_returns_status_and_body(self) -> None:
        page = FakePage()
        session = _session(page)

        status, body = session.fetch_text("https://www.kan.org.il/robots.txt")

        assert (status, body) == (200, "robots-body")
        assert page.fetches == ["https://www.kan.org.il/robots.txt"]
