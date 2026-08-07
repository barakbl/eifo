"""Availability for every service JustWatch tracks in Israel, via TMDB.

TMDB's watch-provider data is powered by JustWatch and covers the Israeli
catalogs of both the international services and the local operators. One crawl
therefore populates many sources, which is why :meth:`sources` returns a list
and each :class:`RawItem` carries its own ``source_key``.

Two consequences of using this dataset, both required by its licence and shape:
the UI must credit JustWatch (the API serves that string from ``/meta``), and
there are no deep links — the JustWatch export does not include them, so items
link to the service's own site instead.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from tvil_core.enums import OfferType, SourceKind, TitleKind
from tvil_fetcher.sources.base import FetchContext, RawItem, SourceInfo, SourcePlugin
from tvil_fetcher.tmdb import TmdbClient, image_url


@dataclass(frozen=True, slots=True)
class ProviderSource:
    """A TVIL source backed by a JustWatch/TMDB provider."""

    key: str
    name: str
    kind: SourceKind
    website_url: str
    #: Names TMDB may use for this provider, compared case-insensitively.
    provider_names: tuple[str, ...]


#: The Israeli market as of 2026. Provider ids are resolved by name at runtime
#: rather than hard-coded, because TMDB renumbers and renames providers and a
#: stale id silently yields an empty catalog.
PROVIDER_SOURCES: tuple[ProviderSource, ...] = (
    ProviderSource(
        key="netflix_il",
        name="Netflix",
        kind=SourceKind.SUBSCRIPTION,
        website_url="https://www.netflix.com/il/",
        provider_names=("Netflix", "Netflix Standard with Ads"),
    ),
    ProviderSource(
        key="disney_plus_il",
        name="Disney+",
        kind=SourceKind.SUBSCRIPTION,
        website_url="https://www.disneyplus.com/",
        provider_names=("Disney Plus", "Disney+"),
    ),
    ProviderSource(
        key="prime_video_il",
        name="Prime Video",
        kind=SourceKind.SUBSCRIPTION,
        website_url="https://www.primevideo.com/",
        provider_names=("Amazon Prime Video", "Prime Video"),
    ),
    ProviderSource(
        key="apple_tv_plus",
        name="Apple TV+",
        kind=SourceKind.SUBSCRIPTION,
        website_url="https://tv.apple.com/il",
        provider_names=("Apple TV+", "Apple TV Plus"),
    ),
    ProviderSource(
        key="yes_plus",
        name="yes+",
        kind=SourceKind.SUBSCRIPTION,
        website_url="https://www.yes.co.il/",
        provider_names=("yes+", "Yes Plus", "yes VOD"),
    ),
    ProviderSource(
        key="sting_tv",
        name="Sting TV",
        kind=SourceKind.SUBSCRIPTION,
        website_url="https://www.stingtv.co.il/",
        provider_names=("Sting TV", "STINGTV"),
    ),
    ProviderSource(
        key="hot",
        name="HOT",
        kind=SourceKind.SUBSCRIPTION,
        website_url="https://www.hot.net.il/",
        provider_names=("HOT", "Hot Israel", "NEXT TV"),
    ),
    ProviderSource(
        key="cellcom_tv",
        name="Cellcom TV",
        kind=SourceKind.SUBSCRIPTION,
        website_url="https://cellcomtv.co.il/",
        provider_names=("Cellcom tv", "Cellcom TV"),
    ),
    ProviderSource(
        key="partner_tv",
        name="Partner TV",
        kind=SourceKind.SUBSCRIPTION,
        website_url="https://www.partner.co.il/tv/",
        provider_names=("Partner TV", "Partner"),
    ),
)

#: Pages per provider per media type. 500 pages is TMDB's ceiling; the default
#: keeps a full sync to a sane number of requests and is raised in config.
DEFAULT_MAX_PAGES = 50


class TmdbProvidersPlugin(SourcePlugin):
    """Harvests many Israeli services from TMDB's watch-provider data."""

    def __init__(self, tmdb: TmdbClient | None = None) -> None:
        self._tmdb = tmdb

    def sources(self) -> list[SourceInfo]:
        return [
            SourceInfo(
                key=source.key,
                name=source.name,
                kind=source.kind,
                website_url=source.website_url,
            )
            for source in PROVIDER_SOURCES
        ]

    def fetch(self, ctx: FetchContext) -> Iterator[RawItem]:
        """Yield everything the requested provider currently offers."""
        tmdb = self._tmdb or _client_from(ctx)
        source = _source_for(ctx.source_key)
        if source is None:
            ctx.record_error(f"{ctx.source_key} is not a TMDB provider source")
            return

        max_pages = _max_pages(ctx)

        for kind in (TitleKind.MOVIE, TitleKind.SERIES):
            provider_id = self._resolve_provider_id(tmdb, source, kind, ctx)
            if provider_id is None:
                continue
            yield from self._fetch_kind(tmdb, source, kind, provider_id, max_pages, ctx)

    def _resolve_provider_id(
        self,
        tmdb: TmdbClient,
        source: ProviderSource,
        kind: TitleKind,
        ctx: FetchContext,
    ) -> int | None:
        """Find the provider TMDB currently uses for this service."""
        try:
            providers = tmdb.watch_providers(kind)
        except Exception as exc:
            ctx.record_error(f"could not list {kind.value} providers", exc=exc)
            return None

        wanted = {name.casefold() for name in source.provider_names}
        for provider in providers:
            name = str(provider.get("provider_name", ""))
            if name.casefold() in wanted:
                return int(provider["provider_id"])

        # Not an error: a service may simply carry no titles of this kind in the
        # region, and the market shifts faster than this table does.
        ctx.logger.info(
            "no TMDB %s provider in region %s matches %s",
            kind.value,
            tmdb.region,
            source.provider_names,
        )
        return None

    def _fetch_kind(
        self,
        tmdb: TmdbClient,
        source: ProviderSource,
        kind: TitleKind,
        provider_id: int,
        max_pages: int,
        ctx: FetchContext,
    ) -> Iterator[RawItem]:
        seen = 0
        for hit in tmdb.discover_by_provider(kind, provider_id, max_pages=max_pages):
            seen += 1
            ctx.record_success()
            yield RawItem(
                source_key=source.key,
                kind=kind,
                name=hit.name,
                name_alt=hit.original_name if hit.original_name != hit.name else None,
                year=hit.year,
                tmdb_id=hit.tmdb_id,
                offer_type=OfferType.STREAM,
                # JustWatch's export has no per-title deep links; send viewers
                # to the service itself rather than inventing a URL.
                deep_link_url=source.website_url,
                poster_url=image_url(hit.poster_path) if hit.poster_path else None,
                extra={"provider_id": provider_id},
            )

        if seen >= max_pages * 20:
            ctx.logger.warning(
                "%s %s hit the %d-page cap; catalog may be truncated",
                source.key,
                kind.value,
                max_pages,
            )


def _source_for(key: str) -> ProviderSource | None:
    return next((source for source in PROVIDER_SOURCES if source.key == key), None)


def _client_from(ctx: FetchContext) -> TmdbClient:
    """Build a TMDB client, failing loudly if the key is missing."""
    ctx.settings.require("tmdb_api_key")
    assert ctx.settings.tmdb_api_key is not None
    return TmdbClient(ctx.http, ctx.settings.tmdb_api_key.get_secret_value())


def _max_pages(ctx: FetchContext) -> int:
    configured = ctx.config.max_pages
    return configured if configured is not None else DEFAULT_MAX_PAGES
