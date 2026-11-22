"""HOT - registered, but honestly empty.

Research on 2026-11-22 found no public, unauthenticated catalog for HOT:

* Its only web player (NEXT TV, ``watch.nexttv.co.il``) sits behind a phone
  verification code, and HOT play is app-only.
* ``hot.net.il`` serves marketing pages: four-item carousels with stale posters,
  a names-only weekly top ten, no per-title pages, no sitemap entries for
  titles, and no API.

Scraping the carousels would publish a handful of years-old names as though they
were "what HOT carries", which is worse than saying nothing. So the source is
declared (it shows in the UI as a partial, subscription service, off by default)
and :meth:`HotPlugin.fetch` fails with a clear message instead of yielding a
fake catalog. HOT titles still reach Eifo where TMDB/JustWatch know them.
Replace ``fetch`` if HOT ever publishes a readable listing.
"""

from __future__ import annotations

from collections.abc import Iterator

from eifo_core.enums import SourceKind
from eifo_fetcher.sources.base import FetchContext, RawItem, SourceInfo, SourcePlugin

SOURCE_KEY = "hot"
CATALOG_URL = "https://www.hot.net.il/heb/TV/VOD/"


class HotCatalogError(RuntimeError):
    """HOT publishes no catalog this plugin can read."""


class HotPlugin(SourcePlugin):
    """Declares HOT; has nothing public to read."""

    def sources(self) -> list[SourceInfo]:
        return [
            SourceInfo(
                key=SOURCE_KEY,
                name="HOT",
                kind=SourceKind.SUBSCRIPTION,
                website_url=CATALOG_URL,
                default_enabled=False,
                partial=True,
            )
        ]

    def fetch(self, ctx: FetchContext) -> Iterator[RawItem]:
        raise HotCatalogError(
            "HOT publishes no public catalog (its web player needs a login and its VOD pages "
            "list no titles); nothing to read"
        )
        yield  # pragma: no cover - makes this a generator, so the error surfaces on iteration
