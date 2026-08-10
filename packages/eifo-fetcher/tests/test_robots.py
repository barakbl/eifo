"""robots.txt enforcement.

The rules exercised here are taken from a real Israeli broadcaster's
robots.txt, including the near-miss that matters: ``Disallow: /vod-index/``
does not cover ``/mako-vod-index``, which is a different path.
"""

from __future__ import annotations

import pytest

from eifo_fetcher.robots import RobotsDisallowedError, RobotsPolicy

REAL_RULES = """
User-Agent: *
Disallow: /vod-index/
Disallow: */VOD-*vcmid=*
Disallow: /AjaxPage
Disallow: /mako-vod-*?sCh=
"""


def policy(text: str = REAL_RULES, *, user_agent: str = "eifo-fetcher/0.1") -> RobotsPolicy:
    return RobotsPolicy(user_agent, fetch=lambda _url: text)


class TestAllows:
    def test_the_vod_catalog_is_permitted(self) -> None:
        """/vod-index/ and /mako-vod-index are different paths."""
        assert policy().allows("https://www.mako.co.il/mako-vod-index")

    def test_a_show_page_is_permitted(self) -> None:
        assert policy().allows("https://www.mako.co.il/mako-vod-keshet/some-show")

    def test_the_next_data_endpoint_is_permitted(self) -> None:
        assert policy().allows("https://www.mako.co.il/_next/data/7.20.0/mako-vod-index.json")

    def test_the_disallowed_index_is_refused(self) -> None:
        assert not policy().allows("https://www.mako.co.il/vod-index/anything")

    def test_the_disallowed_ajax_endpoint_is_refused(self) -> None:
        assert not policy().allows("https://www.mako.co.il/AjaxPage?jspName=x")


class TestMissingRobots:
    def test_an_unreachable_robots_file_allows_everything(self) -> None:
        def explode(_url: str) -> str:
            raise OSError("connection refused")

        assert RobotsPolicy("eifo", fetch=explode).allows("https://example.com/anything")

    def test_an_empty_robots_file_allows_everything(self) -> None:
        assert policy("").allows("https://example.com/anything")


class TestRequireAllowed:
    def test_permitted_urls_pass_silently(self) -> None:
        policy().require_allowed("https://www.mako.co.il/mako-vod-index")

    def test_a_disallowed_url_raises(self) -> None:
        with pytest.raises(RobotsDisallowedError, match="does not fetch disallowed"):
            policy().require_allowed("https://www.mako.co.il/AjaxPage")


class TestCaching:
    def test_robots_is_read_once_per_host(self) -> None:
        reads: list[str] = []

        def counting_fetch(url: str) -> str:
            reads.append(url)
            return REAL_RULES

        checker = RobotsPolicy("eifo", fetch=counting_fetch)
        checker.allows("https://example.com/a")
        checker.allows("https://example.com/b")

        assert reads == ["https://example.com/robots.txt"]

    def test_each_host_is_read_separately(self) -> None:
        reads: list[str] = []

        def counting_fetch(url: str) -> str:
            reads.append(url)
            return REAL_RULES

        checker = RobotsPolicy("eifo", fetch=counting_fetch)
        checker.allows("https://a.example/x")
        checker.allows("https://b.example/x")

        assert len(reads) == 2
