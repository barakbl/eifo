"""Time Out Tel Aviv - the critic's star rating of a film.

timeout.com/telaviv is a city guide with no reviews of its own; the Tel Aviv
edition's Hebrew film reviews live on ``timeout.co.il`` at
``/<hebrew-slug>-ביקורת/`` and carry a star rating out of five.

Resolution is by the Hebrew name: the site's WordPress search (``/?s=``) is
asked, links ending in ``ביקורת`` whose slug contains the name are candidates,
and at most two are opened. The stars come from the page's JSON-LD
``reviewRating`` when present, else from a count of filled-star elements. The
score is stored on the 0-10 scale (stars x 2), which is what the aggregate
expects of a non-percentage provider.

The markup was not verified against live pages; a page that cannot be read
yields no rating. HTTP failures propagate (see CLAUDE.md, catalog safety).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any
from urllib.parse import quote, unquote, urljoin

from eifo_core.enums import RatingProvider
from eifo_fetcher.enrichers.base import (
    ICONS_DIR,
    Enricher,
    EnrichResult,
    ProviderInfo,
    Rating,
    TitleView,
)
from eifo_fetcher.http import USER_AGENT
from eifo_fetcher.robots import RobotsPolicy
from eifo_fetcher.sources.base import FetchContext

logger = logging.getLogger("eifo.fetch.enrich.timeout_tlv")

HOST = "timeout.co.il"
BASE_URL = f"https://{HOST}"

DEFAULT_RATE_LIMIT_RPS = 0.5
MAX_CANDIDATES = 2
_REVIEW_SUFFIX = "ביקורת"

_HREF = re.compile(r'href="(?P<href>[^"]+)"')
_JSON_LD = re.compile(
    r'<script[^>]+type="application/ld\+json"[^>]*>(?P<payload>.*?)</script>',
    re.S | re.I,
)
_FILLED_STAR = re.compile(r'class="[^"]*\bstar-(?:full|filled)\b[^"]*"')


class TimeOutTelAvivEnricher(Enricher):
    """Stars, doubled onto a 0-10 scale."""

    providers = (RatingProvider.TIMEOUT_TLV,)
    provider_info = (
        ProviderInfo(
            provider=RatingProvider.TIMEOUT_TLV,
            label="מבקר",
            group_key="timeout_tlv",
            group_name="Time Out תל אביב",
            icon=ICONS_DIR / "timeout.svg",
            website_url=BASE_URL,
            position=0,
        ),
    )
    name = "Time Out Tel Aviv"
    host = HOST
    default_rate_limit_rps = DEFAULT_RATE_LIMIT_RPS

    def __init__(self) -> None:
        self._robots = RobotsPolicy(user_agent=USER_AGENT)

    @property
    def key(self) -> str:
        return "timeout_tlv"

    def enrich(self, title: TitleView, ctx: FetchContext) -> EnrichResult | None:
        if not title.name_he:
            return None
        search = f"{BASE_URL}/?s={quote(title.name_he)}"
        if not self._robots.allows(search):
            return None

        for url in parse_candidates(ctx.http.get(search).text, title.name_he)[:MAX_CANDIDATES]:
            if not self._robots.allows(url):
                continue
            rating = parse_page(ctx.http.get(url).text, url)
            if rating is not None:
                return EnrichResult(ratings=[rating])
        return None


def parse_candidates(page: str, name: str) -> list[str]:
    """Review links in a search page whose slug contains the film's name."""
    wanted = _squash(name)
    urls: list[str] = []
    for match in _HREF.finditer(page):
        url = urljoin(BASE_URL + "/", match.group("href"))
        slug = unquote(url).rstrip("/").rsplit("/", 1)[-1]
        if not slug.endswith(_REVIEW_SUFFIX) or wanted not in _squash(slug):
            continue
        if not url.startswith(BASE_URL) or url in urls:
            continue
        urls.append(url)
    return urls


def parse_page(page: str, url: str) -> Rating | None:
    """The star rating of a review page, on a 0-10 scale, or None."""
    stars = _stars_from_json_ld(page)
    if stars is None:
        filled = len(_FILLED_STAR.findall(page))
        stars = (float(filled), 5.0) if filled else None
    if stars is None:
        return None
    value, best = stars
    if best <= 0 or not 0 < value <= best:
        return None
    # Stars (usually of 5) onto the 0-10 scale the aggregate expects.
    return Rating(provider=RatingProvider.TIMEOUT_TLV, score_raw=value / best * 10, url=url)


def _stars_from_json_ld(page: str) -> tuple[float, float] | None:
    for block in _JSON_LD.finditer(page):
        try:
            payload = json.loads(block.group("payload"))
        except json.JSONDecodeError:
            continue
        rating = _find_review_rating(payload)
        if rating is None:
            continue
        try:
            value = float(rating["ratingValue"])
            best = float(rating.get("bestRating", 5))
        except (KeyError, TypeError, ValueError):
            continue
        return value, best
    return None


def _find_review_rating(node: Any) -> dict[str, Any] | None:
    if isinstance(node, dict):
        found = node.get("reviewRating")
        if isinstance(found, dict):
            return found
        children: list[Any] = list(node.values())
    elif isinstance(node, list):
        children = node
    else:
        return None
    for child in children:
        found = _find_review_rating(child)
        if found is not None:
            return found
    return None


def _squash(text: str) -> str:
    return re.sub(r"[\s\-_]+", "", text).casefold()
