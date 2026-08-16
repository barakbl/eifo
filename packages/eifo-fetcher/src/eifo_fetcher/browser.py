"""Headless-browser transport for sources that will not serve plain HTTP.

Some sites (Kan is the first) sit behind bot management that blocks any
non-browser client on its TLS fingerprint - including ``robots.txt`` - so the
shared :class:`~eifo_fetcher.http.HttpClient` can never reach them. For those,
this module provides a small, polite browser transport built on Playwright.

The rules here mirror docs.internal.local/03-sources.md:

* **As few requests as possible.** A source plugin should need one or two page
  loads per sync; this class is a transport, not a crawling framework.
* **Rate limited.** Navigations and in-page fetches go through the same
  per-host :class:`~eifo_fetcher.http.RateLimiter` as plain HTTP calls.
* **No fingerprint spoofing.** The browser is a stock Chromium. The single
  adjustment is its User-Agent string: the stock headless UA carries a
  ``Headless`` token that bot management blocks on sight, so the session
  presents the same UA a headed Chrome of the identical build sends. Nothing
  else is masked or emulated.
* **Loud failure.** A WAF block page is detected and raised as
  :class:`BrowserBlockedError` rather than parsed as if it were content.
"""

from __future__ import annotations

import logging
import time
from types import TracebackType
from typing import TYPE_CHECKING, Self
from urllib.parse import urlsplit

from eifo_fetcher.http import RateLimiter

if TYPE_CHECKING:
    from playwright.sync_api import Browser, BrowserContext, Page, Playwright

logger = logging.getLogger("eifo.fetch.browser")

DEFAULT_TIMEOUT_MS = 45_000
DEFAULT_SETTLE_MS = 1_000

#: Substrings that mark a WAF interstitial rather than content (Cloudflare's
#: managed-challenge title and hard-block page, as served to this project).
_BLOCK_TITLE_MARKERS = ("attention required", "just a moment")
_BLOCK_BODY_MARKERS = ("you have been blocked",)


class BrowserUnavailableError(RuntimeError):
    """Playwright or its Chromium build is not installed on this machine."""


class BrowserBlockedError(RuntimeError):
    """The WAF served a challenge/block page instead of the requested page."""

    def __init__(self, url: str) -> None:
        super().__init__(
            f"{url!r} answered with a bot-management interstitial, not content; "
            f"the site does not want automated readers right now"
        )
        self.url = url


class BrowserSession:
    """One headless Chromium session: challenge clearance, cookies, rate limits.

    Use as a context manager. A session opens one browser with one context and
    reuses a single tab, so cookies (e.g. a cleared challenge) carry across
    navigations exactly as they would for a human visitor.
    """

    def __init__(
        self,
        *,
        rate_limiter: RateLimiter | None = None,
        locale: str = "he-IL",
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
        settle_ms: int = DEFAULT_SETTLE_MS,
        reset_delay_seconds: float = 0.0,
    ) -> None:
        self._rate_limiter = rate_limiter or RateLimiter()
        self._locale = locale
        self._timeout_ms = timeout_ms
        self._settle_ms = settle_ms
        self._reset_delay_seconds = reset_delay_seconds
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._ua = ""

    @property
    def user_agent(self) -> str:
        """The UA this session actually sends (set when the session opens)."""
        if self._context is None:
            raise RuntimeError("browser session is not open")
        return self._ua

    def __enter__(self) -> Self:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise BrowserUnavailableError(
                "playwright is required for browser-based sources: "
                "`uv sync` and then `uv run playwright install chromium`"
            ) from exc

        try:
            self._playwright = sync_playwright().start()
            # channel="chromium" selects the full browser's new headless mode
            # rather than the stripped headless shell, which some WAFs reject.
            self._browser = self._playwright.chromium.launch(channel="chromium", headless=True)
        except Exception as exc:
            self._close_quietly()
            raise BrowserUnavailableError(
                f"could not launch chromium ({exc}); run `uv run playwright install chromium` "
                f"- or, in Docker, rebuild the image without INSTALL_BROWSER=0"
            ) from exc

        self._ua = self._headed_user_agent()
        self._open_context()
        return self

    def _open_context(self) -> None:
        assert self._browser is not None
        self._context = self._browser.new_context(locale=self._locale, user_agent=self._ua)
        self._page = self._context.new_page()

    def reset(self) -> None:
        """Start over with a fresh context: new cookies, new challenge clearance.

        Some WAFs serve only one HTML document per cleared session (Kan does),
        so reading several pages means several sessions. The browser process is
        reused; only the context (cookies, storage, tabs) is replaced. Waits
        ``reset_delay_seconds`` first, so back-to-back sessions stay polite.
        """
        if self._browser is None:
            raise RuntimeError("browser session is not open")
        if self._context is not None:
            self._context.close()
        if self._reset_delay_seconds > 0:
            time.sleep(self._reset_delay_seconds)
        self._open_context()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._close_quietly()

    def _close_quietly(self) -> None:
        for closer in (
            lambda: self._context.close() if self._context else None,
            lambda: self._browser.close() if self._browser else None,
            lambda: self._playwright.stop() if self._playwright else None,
        ):
            try:
                closer()
            except Exception:
                logger.debug("error while closing browser session", exc_info=True)
        self._context = self._browser = self._playwright = self._page = None

    def _headed_user_agent(self) -> str:
        """The stock UA with the ``Headless`` token removed.

        Derived from the running browser's own ``navigator.userAgent`` so it
        always matches the real build and platform - a headed Chrome of this
        build sends exactly this string.
        """
        assert self._browser is not None
        probe = self._browser.new_context()
        try:
            ua: str = probe.new_page().evaluate("navigator.userAgent")
        finally:
            probe.close()
        return ua.replace("HeadlessChrome", "Chrome")

    def get_html(self, url: str, *, ready_selector: str | None = None) -> str:
        """Navigate to ``url`` and return the rendered page HTML.

        Args:
            url: page to load.
            ready_selector: CSS selector to wait for before reading the DOM
                (e.g. the catalog card a scraper is about to parse).

        Raises:
            BrowserBlockedError: if a WAF interstitial is served even after
                one patient wait-and-reload.
        """
        page = self._require_page()
        self._goto(page, url)
        if self._is_blocked(page):
            logger.info("bot-management interstitial on %s; waiting, then one reload", url)
            page.wait_for_timeout(self._settle_ms * 3)
            self._goto(page, url, reload=True)
        if self._is_blocked(page):
            raise BrowserBlockedError(url)

        if ready_selector is not None:
            page.wait_for_selector(ready_selector, timeout=self._timeout_ms)
        page.wait_for_timeout(self._settle_ms)
        return page.content()

    def fetch_text(self, url: str) -> tuple[int, str]:
        """GET ``url`` with the page's own ``fetch`` and return ``(status, body)``.

        Same-origin requests carry the session's cookies, so this is how a
        plugin reads humble text resources (robots.txt) without spending a
        full navigation on them.
        """
        page = self._require_page()
        host = urlsplit(url).netloc
        if host:
            self._rate_limiter.wait(host)
        status, body = page.evaluate(
            """async (url) => {
                const r = await fetch(url);
                return [r.status, await r.text()];
            }""",
            url,
        )
        return int(status), str(body)

    def _require_page(self) -> Page:
        if self._page is None:
            raise RuntimeError("browser session is not open")
        return self._page

    def _goto(self, page: Page, url: str, *, reload: bool = False) -> None:
        host = urlsplit(url).netloc
        self._rate_limiter.wait(host)
        if reload:
            page.reload(wait_until="domcontentloaded", timeout=self._timeout_ms)
        else:
            page.goto(url, wait_until="domcontentloaded", timeout=self._timeout_ms)

    def _is_blocked(self, page: Page) -> bool:
        """Whether the current page is a WAF interstitial rather than content."""
        try:
            title = page.title().lower()
            if any(marker in title for marker in _BLOCK_TITLE_MARKERS):
                return True
            body: str = page.evaluate("document.body ? document.body.innerText.slice(0, 2000) : ''")
            return any(marker in body.lower() for marker in _BLOCK_BODY_MARKERS)
        except Exception:
            # A page mid-navigation may not answer; treat as not yet blocked.
            return False
