"""Sratim (sratim.co.il) - the site-user rating of a film.

**Off by default.** Sratim's terms of use (section 17) forbid pulling data by
scrapers or crawlers other than through software its management has approved,
and it publishes no API. robots.txt allows everything but ``/l.php``, so the
code is polite, but politeness is not permission. Enable it with
``[enrich] enabled = ["sratim"]`` only once the operators have agreed (see
``/contact.php`` on the site).

Resolution: Sratim's page ids (``/tt151248/``) are its own, not IMDb's. A film
is found through ``/search.php?q=`` and a candidate is accepted only when the
IMDb link on its page equals the title's IMDb id, so a title without one is
skipped. The rating is the first ``10/ X`` block of the page followed by its
``N הצבעות`` vote count; ``10/ ?`` means nobody has rated it.

The markup was only seen as rendered text, so the parser works on the text left
after stripping tags. An HTTP failure propagates and ends the sync as failed
rather than looking like "no rating" (see CLAUDE.md, catalog safety).
"""

from __future__ import annotations

import html as html_lib
import logging
import re
from urllib.parse import quote

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

logger = logging.getLogger("eifo.fetch.enrich.sratim")

HOST = "www.sratim.co.il"
BASE_URL = f"https://{HOST}"

#: Very slow on purpose: the terms do not welcome automated readers.
DEFAULT_RATE_LIMIT_RPS = 0.2
#: Pages opened per title.
MAX_CANDIDATES = 2
#: Search years are release years that can differ by one from ours.
YEAR_TOLERANCE = 1

_LINK = re.compile(
    r'<a\b[^>]*href="(?P<href>(?:https?://[^"/]+)?/tt\d+/)"[^>]*>(?P<body>.*?)</a>', re.S
)
_YEAR = re.compile(r"\b(?:19|20)\d{2}\b")
_IMDB_LINK = re.compile(r"imdb\.com/title/(tt\d+)")
_TAG = re.compile(r"<(?:script|style)\b.*?</(?:script|style)>|<[^>]+>", re.S | re.I)
_RATING = re.compile(r"10/\s*(?P<score>\d+(?:\.\d+)?|\?)\s+(?P<votes>[\d.,]+\s?[KkMm]?)\s*הצבעות")


class SratimEnricher(Enricher):
    """The user rating, on a 0-10 scale."""

    providers = (RatingProvider.SRATIM,)
    provider_info = (
        ProviderInfo(
            provider=RatingProvider.SRATIM,
            label="משתמשים",
            group_key="sratim",
            group_name="סרטים",
            icon=ICONS_DIR / "sratim.svg",
            website_url=BASE_URL,
            position=0,
        ),
    )
    name = "Sratim"
    host = HOST
    default_rate_limit_rps = DEFAULT_RATE_LIMIT_RPS

    def __init__(self) -> None:
        self._robots = RobotsPolicy(user_agent=USER_AGENT)

    @property
    def key(self) -> str:
        return "sratim"

    def enrich(self, title: TitleView, ctx: FetchContext) -> EnrichResult | None:
        # The IMDb id is the only safe proof that a page is this film.
        if not title.imdb_id:
            return None

        seen: list[str] = []
        for query in title.names():
            search = f"{BASE_URL}/search.php?q={quote(query)}"
            if not self._robots.allows(search):
                return None
            for path in parse_candidates(ctx.http.get(search).text, title.year):
                if path in seen:
                    continue
                seen.append(path)
                if len(seen) > MAX_CANDIDATES:
                    return None
                url = f"{BASE_URL}{path}"
                if not self._robots.allows(url):
                    continue
                rating = parse_page(ctx.http.get(url).text, title.imdb_id, url)
                if rating is not None:
                    return EnrichResult(ratings=[rating])
        return None


def parse_candidates(page: str, year: int | None) -> list[str]:
    """Film paths from a search page, dropping those whose year is clearly off."""
    paths: list[str] = []
    for match in _LINK.finditer(page):
        path = (
            "/" + match.group("href").split("/", 3)[-1]
            if "://" in match.group("href")
            else (match.group("href"))
        )
        context = _text(match.group("body")) + " " + _text(page[match.end() : match.end() + 120])
        found = _YEAR.search(context)
        if year is not None and found and abs(int(found.group()) - year) > YEAR_TOLERANCE:
            continue
        if path not in paths:
            paths.append(path)
    return paths


def parse_page(page: str, imdb_id: str, url: str) -> Rating | None:
    """The rating, if this is the film with ``imdb_id`` and somebody rated it."""
    linked = _IMDB_LINK.search(page)
    if linked is None or linked.group(1) != imdb_id:
        return None

    match = _RATING.search(_text(page))
    if match is None or match.group("score") == "?":
        return None
    votes = _votes(match.group("votes"))
    score = float(match.group("score"))
    if not votes or not 0.0 <= score <= 10.0:
        return None
    return Rating(provider=RatingProvider.SRATIM, score_raw=score, vote_count=votes, url=url)


def _votes(raw: str) -> int:
    """``21`` or ``647.3K`` as a count."""
    raw = raw.strip().replace(",", "")
    factor = {"k": 1_000, "m": 1_000_000}.get(raw[-1:].lower(), 1)
    number = raw[:-1].strip() if factor != 1 else raw
    try:
        return int(float(number) * factor)
    except ValueError:
        return 0


def _text(markup: str) -> str:
    return re.sub(r"\s+", " ", html_lib.unescape(_TAG.sub(" ", markup))).strip()
