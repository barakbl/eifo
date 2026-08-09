"""Rotten Tomatoes - Tomatometer and audience scores.

Scores come from the ``media-scorecard-json`` payload the page embeds, which is
far steadier than the rendered markup.

**Resolution is deliberately limited.** RT's robots.txt disallows ``/search``,
so a title cannot be looked up the obvious way. Instead the English title is
turned into RT's own slug form and that page is requested directly: RT redirects
near-miss slugs to the right film (``foxtrot_2017`` lands on ``foxtrot_2018``),
and anything genuinely absent answers 404, which is a clean "not found". A title
with no English name is skipped outright, and so is every Israeli title RT has
never heard of - both ordinary outcomes, not failures.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from typing import Any

import httpx

from tvil_core.enums import RatingProvider, TitleKind
from tvil_fetcher.enrichers.base import Enricher, EnrichResult, Rating, TitleView
from tvil_fetcher.match import is_hebrew
from tvil_fetcher.sources.base import FetchContext

logger = logging.getLogger("tvil.fetch.enrich.rt")

HOST = "www.rottentomatoes.com"
BASE_URL = f"https://{HOST}"

_SCORECARD = re.compile(
    r'<script[^>]+id="media-scorecard-json"[^>]*>(?P<payload>.*?)</script>',
    re.DOTALL,
)
_NON_SLUG = re.compile(r"[^a-z0-9]+")


class RottenTomatoesEnricher(Enricher):
    """Critic and audience percentages."""

    providers = (RatingProvider.RT_CRITICS, RatingProvider.RT_AUDIENCE)

    @property
    def key(self) -> str:
        return "rt"

    def enrich(self, title: TitleView, ctx: FetchContext) -> EnrichResult | None:
        ctx.apply_rate_limit(HOST)

        for url in candidate_urls(title):
            html = self._page(url, ctx)
            if html is None:
                continue
            scorecard = parse_scorecard(html)
            if scorecard is None:
                continue
            ratings = _ratings_from(scorecard, url)
            if ratings:
                return EnrichResult(ratings=ratings)

        return None

    def _page(self, url: str, ctx: FetchContext) -> str | None:
        """Fetch a candidate page; a 404 simply means RT has no such film."""
        try:
            return ctx.http.get(url).text
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            ctx.record_error(f"RT returned {exc.response.status_code} for {url}", exc=exc)
            return None
        except Exception as exc:
            ctx.record_error(f"RT request failed for {url}", exc=exc)
            return None


def slugify(name: str) -> str:
    """Turn a title into RT's slug form.

    RT slugs are lowercase ASCII words joined by underscores; accents are folded
    and punctuation dropped.
    """
    folded = unicodedata.normalize("NFKD", name)
    ascii_only = "".join(char for char in folded if not unicodedata.combining(char))
    return _NON_SLUG.sub("_", ascii_only.casefold()).strip("_")


def candidate_urls(title: TitleView) -> list[str]:
    """Pages worth trying for this title, most likely first.

    Only the English name is usable: RT slugs are ASCII, so a Hebrew-only title
    has nothing to build a slug from.
    """
    name = title.name_en
    if not name or is_hebrew(name):
        return []

    slug = slugify(name)
    if not slug:
        return []

    section = "m" if title.kind is TitleKind.MOVIE else "tv"
    urls = [f"{BASE_URL}/{section}/{slug}"]
    # RT disambiguates same-named films by year; try that form too.
    if title.year is not None:
        urls.append(f"{BASE_URL}/{section}/{slug}_{title.year}")
    return urls


def parse_scorecard(html: str) -> dict[str, Any] | None:
    """The embedded score payload, or None if the page has none."""
    match = _SCORECARD.search(html)
    if match is None:
        return None
    try:
        payload = json.loads(match.group("payload"))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _ratings_from(scorecard: dict[str, Any], url: str) -> list[Rating]:
    ratings: list[Rating] = []

    critics = _score(
        scorecard.get("criticsScore"),
        provider=RatingProvider.RT_CRITICS,
        url=url,
    )
    if critics is not None:
        ratings.append(critics)

    # RT hides the audience score on some titles; respect that rather than
    # reporting a figure the site itself declines to show.
    if not scorecard.get("hideAudienceScore"):
        audience = _score(
            scorecard.get("audienceScore"),
            provider=RatingProvider.RT_AUDIENCE,
            url=url,
        )
        if audience is not None:
            ratings.append(audience)

    return ratings


def _score(payload: Any, *, provider: RatingProvider, url: str) -> Rating | None:
    """One score block. RT sends percentages as strings."""
    if not isinstance(payload, dict):
        return None

    value = _percent(payload.get("score"))
    if value is None:
        return None

    # ratingCount is null on audience blocks; reviewCount is the populated one.
    votes = _int(payload.get("ratingCount"))
    if votes is None:
        votes = _int(payload.get("reviewCount"))

    return Rating(provider=provider, score_raw=value, vote_count=votes, url=url)


def _percent(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip().rstrip("%"))
        except ValueError:
            return None
    return None


def _int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None
