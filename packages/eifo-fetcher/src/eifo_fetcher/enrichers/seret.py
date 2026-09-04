"""Seret (seret.co.il) - the primary Israeli ratings provider.

Seret publishes schema.org JSON-LD on every title page, so the scores come from
structured data rather than CSS selectors that rot on the next redesign:

* ``aggregateRating.ratingValue`` and ``ratingCount`` - the audience score,
  0-10, and how many people voted for it.
* an ``additionalProperty`` entry named "Seret Score (Composite Editorial
  Score)", which is the critic figure.
* ``sameAs``, carrying an IMDb link, which settles identity far more reliably
  than comparing names. Only the newer pages have one.

**Resolving a title to a page is the hard part, and it is not done here.**
Seret publishes no working title search: its advertised ``SearchAction``
endpoint, the real form POST and the site-wide search page all answer with a
generic current-releases listing whatever they are asked. What Seret does
publish is a sitemap naming every page, so the id is looked up in an index
built from that sitemap by :mod:`eifo_fetcher.enrichers.seret_index`, which
also stores the three scores it read on the way past. This enricher is then a
local lookup that makes no request at all for a title the index knows.

For a title the index has never heard of - anything released since the last
crawl - there is one live fallback, ``searchAUAjax.asp``. It is the site's own
autocomplete, it answers over recent releases only, and it is indexed in Hebrew,
so it is asked only when a title has a Hebrew name and the index missed.

Site-specific details this must get right: pages are **windows-1255**, and so
are query strings, which is why the autocomplete URL is assembled by hand
rather than handed to httpx as params; films and series live at different
endpoints (``s_movies.asp?MID=`` against ``s_series.asp?SID=``) under different
id parameters and declare different JSON-LD types; and the site writes those
parameters in either case, so every pattern here is case-insensitive.

``robots.txt`` disallows ``/ajax/getExtraMovieRatingsAjax.asp``, which nothing
here touches. The sitemap, the title pages and ``searchAUAjax.asp`` are all
permitted, and :class:`~eifo_fetcher.robots.RobotsPolicy` is asked rather than
assumed.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from eifo_core.enums import RatingProvider, TitleKind
from eifo_fetcher.enrichers.base import (
    ICONS_DIR,
    Enricher,
    EnrichResult,
    ProviderInfo,
    Rating,
    TitleView,
)
from eifo_fetcher.http import USER_AGENT
from eifo_fetcher.match import similarity, years_match
from eifo_fetcher.robots import RobotsPolicy
from eifo_fetcher.sources.base import FetchContext

if TYPE_CHECKING:  # pragma: no cover - import cycle broken for typing only
    from eifo_fetcher.enrichers.seret_index import SeretLookup

logger = logging.getLogger("eifo.fetch.enrich.seret")

HOST = "www.seret.co.il"
BASE_URL = f"https://{HOST}"
MOVIE_URL = f"{BASE_URL}/movies/s_movies.asp"
SERIES_URL = f"{BASE_URL}/series/s_series.asp"
#: The site's own autocomplete, and the only search endpoint that answers.
AUTOCOMPLETE_URL = f"{BASE_URL}/searchAUAjax.asp"

#: Seret serves legacy Hebrew encoding, declared in the Content-Type header.
ENCODING = "windows-1255"

#: Requests per second, for both the index crawl and the live fallback.
#:
#: Half the project-wide default. This is one small site being asked for
#: thousands of pages it gains nothing from serving, and the crawl is resumable
#: and incremental, so there is nothing to gain by hurrying it. Overridable
#: through ``[enrich.rate_limits] seret``, beside every other provider's.
DEFAULT_RATE_LIMIT_RPS = 0.5

#: Name similarity required before accepting a page as this title.
MATCH_THRESHOLD = 85.0
#: Pages fetched per live lookup; the autocomplete returns loose matches.
MAX_CANDIDATES = 3

#: How far Seret's year may sit from the catalog's and still be one title.
#:
#: Wider than :data:`eifo_fetcher.match.YEAR_TOLERANCE` on purpose, and in one
#: direction for a reason: ``datePublished`` here is the *Israeli release date*,
#: not the production year, so it trails what every other source reports. "The
#: Big Short" is 2015 upstream and 2016-01-28 on Seret; a festival film can
#: reach Israeli screens two years after it was made.
SERET_YEAR_TOLERANCE = 2

#: JSON-LD types Seret uses for a title page.
_TITLE_TYPES = frozenset({"Movie", "TVSeries"})

#: The additionalProperty entry carrying the editorial score. Matched on this
#: fragment rather than in full: the name reads "Seret Score (Composite
#: Editorial Score)", and the parenthetical is the part likely to be reworded.
_CRITIC_PROPERTY = "seret score"

_JSON_LD = re.compile(
    r'<script[^>]+type="application/ld\+json"[^>]*>(?P<payload>.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)

#: A link to a title page, in either numbering. Case-insensitive because the
#: site writes ``MID=`` in its markup and ``mid=`` in its autocomplete, and a
#: case-sensitive pattern here silently matched neither kind of page for years.
_TITLE_LINK = re.compile(
    r"s_(?P<endpoint>movies|series)\.asp\?(?:mid|sid)=(?P<id>\d+)",
    re.IGNORECASE,
)

#: Which endpoint and id parameter each kind is served from.
_ENDPOINTS: dict[TitleKind, tuple[str, str]] = {
    TitleKind.MOVIE: (MOVIE_URL, "MID"),
    TitleKind.SERIES: (SERIES_URL, "SID"),
}


@dataclass(frozen=True, slots=True)
class SeretEntry:
    """One Seret page, reduced to what identity and scoring need.

    The same shape whether it came from the stored index or from a page just
    fetched, so the enricher does not care which it is holding.
    """

    kind: TitleKind
    seret_id: int
    name_he: str | None = None
    name_en: str | None = None
    year: int | None = None
    imdb_id: str | None = None
    #: The audience score on Seret's own 0-10 scale, and its vote count.
    viewers_score: float | None = None
    viewers_votes: int | None = None
    #: "Seret Score", the site's composite editorial figure, also 0-10.
    critics_score: float | None = None
    url: str | None = None

    @property
    def page_url(self) -> str:
        """Where to send a reader. A score is never shown without one."""
        return self.url or page_url(self.kind, self.seret_id)

    def names(self) -> list[str]:
        """Every name this page gives the title, Hebrew first."""
        return [name for name in (self.name_he, self.name_en) if name]


class SeretEnricher(Enricher):
    """Israeli critic and viewer scores, resolved through the page index.

    Args:
        lookup: the loaded index. Without one this enricher has no way to turn
            a title into a page and says so once, rather than falling back to
            a search that does not work.
    """

    providers = (RatingProvider.SERET_CRITICS, RatingProvider.SERET_VIEWERS)
    #: One chip, two figures, and both named in Hebrew: this is an Israeli site
    #: reporting on Israeli films, and "Critics" beside a Hebrew logo would be
    #: a translation nobody asked for.
    provider_info = (
        ProviderInfo(
            provider=RatingProvider.SERET_CRITICS,
            label="מבקרים",
            group_key="seret",
            group_name="סרט",
            icon=ICONS_DIR / "seret.png",
            website_url=BASE_URL,
            position=0,
        ),
        ProviderInfo(
            provider=RatingProvider.SERET_VIEWERS,
            label="צופים",
            group_key="seret",
            group_name="סרט",
            icon=ICONS_DIR / "seret.png",
            website_url=BASE_URL,
            position=1,
        ),
    )
    host = HOST
    default_rate_limit_rps = DEFAULT_RATE_LIMIT_RPS

    def __init__(self, lookup: SeretLookup | None = None) -> None:
        self._lookup = lookup
        self._robots = RobotsPolicy(user_agent=USER_AGENT)
        self._warned_empty = False

    @property
    def key(self) -> str:
        return "seret"

    def enrich(self, title: TitleView, ctx: FetchContext) -> EnrichResult | None:
        entry = self._from_index(title)
        if entry is None:
            entry = self._from_autocomplete(title, ctx)
        if entry is None:
            # Foreign titles frequently are not on Seret; that is not a failure.
            return None

        ratings = ratings_from(entry)
        return EnrichResult(ratings=ratings) if ratings else None

    def _from_index(self, title: TitleView) -> SeretEntry | None:
        """The stored index's answer, which costs no request."""
        if self._lookup is None or not self._lookup:
            self._warn_once()
            return None
        return self._lookup.find(title)

    def _warn_once(self) -> None:
        """Say why no title is getting an Israeli score, once per run."""
        if self._warned_empty:
            return
        self._warned_empty = True
        logger.info(
            "the Seret page index is empty, so no title can be resolved to a page; "
            "build it with `eifo-fetch seret index`"
        )

    def _from_autocomplete(self, title: TitleView, ctx: FetchContext) -> SeretEntry | None:
        """Ask the site about a title the index has not got.

        Only for titles with a Hebrew name: the autocomplete is indexed in
        Hebrew and answers nothing at all for a Latin query, so asking would
        spend a request to learn what is already known.
        """
        if not ctx.settings.seret.live_fallback or not title.name_he:
            return None
        for kind, seret_id in self._candidates(title.name_he, ctx):
            entry = self._page(kind, seret_id, ctx)
            if entry is not None and matches(title, entry):
                return entry
        return None

    def _candidates(self, query: str, ctx: FetchContext) -> list[tuple[TitleKind, int]]:
        """Page ids the autocomplete offers for a Hebrew name, best-effort."""
        url = autocomplete_url(query)
        if url is None or not self._robots.allows(url):
            return []

        try:
            html = decode(ctx.http.get(url).content)
        except Exception as exc:
            ctx.record_error(f"Seret autocomplete failed for {query!r}", exc=exc)
            return []

        return parse_links(html)[:MAX_CANDIDATES]

    def _page(self, kind: TitleKind, seret_id: int, ctx: FetchContext) -> SeretEntry | None:
        """One title page, read into an entry."""
        url = page_url(kind, seret_id)
        if not self._robots.allows(url):
            return None
        try:
            html = decode(ctx.http.get(url).content)
        except Exception as exc:
            ctx.record_error(f"Seret page {kind}/{seret_id} failed", exc=exc)
            return None

        node = parse_title_node(html)
        return None if node is None else entry_from(kind, seret_id, node)


def ratings_from(entry: SeretEntry) -> list[Rating]:
    """Both Seret scores, for whichever of them the page carries."""
    ratings: list[Rating] = []
    url = entry.page_url

    if entry.viewers_score is not None:
        ratings.append(
            Rating(
                provider=RatingProvider.SERET_VIEWERS,
                score_raw=entry.viewers_score,
                vote_count=entry.viewers_votes,
                url=url,
            )
        )
    if entry.critics_score is not None:
        ratings.append(
            Rating(
                provider=RatingProvider.SERET_CRITICS,
                score_raw=entry.critics_score,
                url=url,
            )
        )
    return ratings


def matches(title: TitleView, entry: SeretEntry) -> bool:
    """Whether this Seret page is the title we were asked about.

    Newer pages publish an IMDb link in ``sameAs``; when both sides have one
    that settles it outright, with no name comparison to get wrong. Older pages
    do not, so those fall back to name and year - and the year is compared with
    Seret's own :data:`SERET_YEAR_TOLERANCE`, because its dates are Israeli
    release dates rather than production years.
    """
    if title.imdb_id and entry.imdb_id:
        return title.imdb_id == entry.imdb_id

    if not years_match(title.year, entry.year, tolerance=SERET_YEAR_TOLERANCE):
        return False

    return any(
        similarity(name, candidate) >= MATCH_THRESHOLD
        for name in title.names()
        for candidate in entry.names()
    )


def entry_from(kind: TitleKind, seret_id: int, node: dict[str, Any]) -> SeretEntry:
    """Read one JSON-LD title node into an entry."""
    score, votes = viewer_score(node)
    return SeretEntry(
        kind=kind,
        seret_id=seret_id,
        name_he=_text(node.get("name")),
        name_en=_text(node.get("alternateName")),
        year=title_year(node),
        imdb_id=imdb_id_of(node),
        viewers_score=score,
        viewers_votes=votes,
        critics_score=critic_score(node),
        url=_declared_url(node) or page_url(kind, seret_id),
    )


def page_url(kind: TitleKind, seret_id: int) -> str:
    """The canonical address of a title page in the right numbering."""
    url, param = _ENDPOINTS[kind]
    return f"{url}?{param}={seret_id}"


def autocomplete_url(query: str) -> str | None:
    """The autocomplete URL for a Hebrew query, or None if it cannot be asked.

    Assembled by hand because the query string has to be **windows-1255**
    percent-encoded. Handing httpx a ``params`` mapping encodes it as UTF-8,
    which this endpoint reads as mojibake and answers nothing for - which is
    indistinguishable from "no such film", and so is exactly the kind of
    breakage that looks like the site being unsearchable.
    """
    try:
        encoded = quote(query.encode(ENCODING))
    except UnicodeEncodeError:
        # A name with characters the legacy codepage has no room for cannot be
        # asked about at all; the index is the only route for those.
        return None
    return f"{AUTOCOMPLETE_URL}?s={encoded}&t=movie"


def parse_links(html: str) -> list[tuple[TitleKind, int]]:
    """Title pages linked from a listing, in order, without repeats.

    Both numberings: one autocomplete response mixes films and series, and
    taking the number from a film link to ask the series endpoint - which is
    what reading only ``MID`` used to do - asks about a different title.
    """
    found: list[tuple[TitleKind, int]] = []
    for match in _TITLE_LINK.finditer(html):
        kind = TitleKind.MOVIE if match.group("endpoint").lower() == "movies" else TitleKind.SERIES
        candidate = (kind, int(match.group("id")))
        if candidate not in found:
            found.append(candidate)
    return found


def parse_title_node(html: str) -> dict[str, Any] | None:
    """Pull the ``Movie`` or ``TVSeries`` node out of a page's JSON-LD graph."""
    for match in _JSON_LD.finditer(html):
        try:
            payload = json.loads(match.group("payload"))
        except json.JSONDecodeError:
            continue

        for node in _nodes(payload):
            if node.get("@type") in _TITLE_TYPES:
                return node
    return None


def _nodes(payload: Any) -> list[dict[str, Any]]:
    """Flatten the shapes JSON-LD is legitimately served in."""
    if isinstance(payload, dict):
        graph = payload.get("@graph")
        if isinstance(graph, list):
            return [node for node in graph if isinstance(node, dict)]
        return [payload]
    if isinstance(payload, list):
        return [node for node in payload if isinstance(node, dict)]
    return []


def imdb_id_of(node: dict[str, Any]) -> str | None:
    """The IMDb id Seret links to, when the page publishes one."""
    same_as = node.get("sameAs")
    values = same_as if isinstance(same_as, list) else [same_as]
    for value in values:
        if isinstance(value, str):
            found = re.search(r"(tt\d{6,})", value)
            if found:
                return found.group(1)
    return None


def viewer_score(node: dict[str, Any]) -> tuple[float | None, int | None]:
    """The audience score and its vote count, or ``(None, None)``.

    A ``ratingValue`` of 0 is not a score. Seret publishes one on every page
    that has no rating yet - an unreleased film carries ``ratingValue: 0``
    beside a ``ratingCount`` in the dozens, because the count is of people
    following it rather than of votes cast. Stored as a rating it would be a
    zero out of ten for a film nobody has seen, dragging down an aggregate that
    is meant to say the opposite.
    """
    aggregate = node.get("aggregateRating")
    if not isinstance(aggregate, dict):
        return None, None

    value = _number(aggregate.get("ratingValue"))
    if value is None or value <= 0:
        return None, None

    # Seret publishes 0-10, but the field is generic schema.org: trust the
    # declared bestRating rather than assuming.
    best = _number(aggregate.get("bestRating")) or 10.0
    if best > 0 and best != 10.0:
        value = value * 10.0 / best

    return value, _int(aggregate.get("ratingCount"))


def critic_score(node: dict[str, Any]) -> float | None:
    """Seret's own composite editorial score, when the page carries one."""
    properties = node.get("additionalProperty")
    if not isinstance(properties, list):
        return None

    for entry in properties:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").casefold()
        if _CRITIC_PROPERTY not in name:
            continue
        value = _number(entry.get("value"))
        # Same reasoning as the audience score: an unscored film reads 0.
        if value is not None and value > 0:
            return value
    return None


def title_year(node: dict[str, Any]) -> int | None:
    """The year off ``datePublished`` - the Israeli release, not production."""
    published = node.get("datePublished")
    if isinstance(published, str) and len(published) >= 4 and published[:4].isdigit():
        return int(published[:4])
    return None


def _declared_url(node: dict[str, Any]) -> str | None:
    url = node.get("url")
    return url if isinstance(url, str) and url.startswith("http") else None


def _text(value: Any) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _int(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None else None


def decode(body: bytes) -> str:
    """Decode a Seret page, tolerating the odd stray byte."""
    return body.decode(ENCODING, errors="replace")
