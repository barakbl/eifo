"""Availability for every service JustWatch tracks in Israel, via TMDB.

TMDB's watch-provider data is powered by JustWatch and covers the Israeli
catalogs of both the international services and the local operators. One crawl
therefore populates many sources, which is why :meth:`sources` returns a list
and each :class:`RawItem` carries its own ``source_key``.

Two consequences of using this dataset, both required by its licence and shape:
the UI must credit JustWatch (the API serves that string from ``/meta``), and
there are no deep links - the JustWatch export does not include them, so items
link to the service's own site instead.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from eifo_core.enums import OfferType, SourceKind, TitleKind
from eifo_core.types import utcnow
from eifo_fetcher.sources.base import (
    FUTURE_YEAR_ALLOWANCE,
    FetchContext,
    RawItem,
    SourceInfo,
    SourcePlugin,
)
from eifo_fetcher.tmdb import MAX_PAGE, TmdbClient, TmdbTitle, image_url

#: Where a viewer is sent for a specific title.
#:
#: The JustWatch export behind this data carries no per-provider deep links, so
#: a service's own homepage was used at first - a "Watch" button that did not
#: take you to the thing you clicked. TMDB publishes a per-title watch page
#: instead, which names every service carrying it in the region. The slug is
#: optional: TMDB redirects the bare id to the canonical URL.
WATCH_URL_TEMPLATE = "https://www.themoviedb.org/{media}/{tmdb_id}/watch?locale={region}"


@dataclass(frozen=True, slots=True)
class ProviderSource:
    """A Eifo source backed by a JustWatch/TMDB provider."""

    key: str
    name: str
    kind: SourceKind
    website_url: str
    #: Names TMDB may use for this provider, compared case-insensitively.
    provider_names: tuple[str, ...]
    #: Offer types to confirm per title against TMDB's own per-title data.
    #:
    #: Empty - every subscription service - means "assume it streams", which is
    #: what being on the provider already implies and costs no extra request.
    #: A storefront is different: it rents some films, sells others, and does
    #: both for most, and nothing in a discover listing says which. Naming the
    #: types here buys one request per title and an offer that is true.
    verified_offer_types: tuple[OfferType, ...] = ()
    #: See :attr:`~eifo_fetcher.sources.base.SourceInfo.default_enabled`.
    default_enabled: bool = True
    #: Read this catalog one release year at a time rather than in one listing.
    #:
    #: TMDB stops paging at 500 pages, which is 10,000 titles - a limit on the
    #: query, not on the provider. A listing bigger than that cannot be finished
    #: however many pages are asked for, and the store is 17,799 films: a single
    #: listing reaches the popular half and no more. Asked a year at a time the
    #: biggest slice is about a thousand, so every one of them is reachable.
    walk_by_year: bool = False
    #: Pages per listing when the config file says nothing. A sliced catalog
    #: needs enough to finish its biggest year, not enough to bound an endless
    #: one, so it sets its own.
    default_max_pages: int | None = None


#: Services TMDB actually reports for region IL, verified against the live API
#: in August 2026. Provider ids are resolved by name at runtime rather than
#: hard-coded, because TMDB renumbers and renames providers and a stale id fails
#: silently as an empty catalog.
#:
#: **The Israeli operators are not here, and cannot be.** JustWatch - whose data
#: this is - does not track yes+, Sting TV, HOT, Cellcom TV or Partner TV at
#: all: they appear nowhere in TMDB's region-IL provider list, so every one of
#: them returned zero. Reaching them needs a dedicated plugin per operator, the
#: way Mako has one. See docs.internal/03-sources.md.
PROVIDER_SOURCES: tuple[ProviderSource, ...] = (
    ProviderSource(
        key="netflix_il",
        name="Netflix",
        kind=SourceKind.SUBSCRIPTION,
        website_url="https://www.netflix.com/il/",
        provider_names=("Netflix",),
    ),
    ProviderSource(
        key="prime_video_il",
        name="Prime Video",
        kind=SourceKind.SUBSCRIPTION,
        website_url="https://www.primevideo.com/",
        provider_names=("Amazon Prime Video",),
    ),
    ProviderSource(
        key="apple_tv_plus",
        name="Apple TV",
        kind=SourceKind.SUBSCRIPTION,
        website_url="https://tv.apple.com/il",
        # TMDB lists no "Apple TV+"; "Apple TV" is the subscription and
        # "Apple TV Store" is the separate rent-and-buy storefront.
        provider_names=("Apple TV",),
    ),
    ProviderSource(
        key="apple_tv_store",
        name="Apple TV Store",
        kind=SourceKind.RENT_BUY,
        website_url="https://tv.apple.com/il",
        # The storefront beside the subscription above, and by a distance the
        # largest catalog TMDB reports for Israel: 17,799 films against the
        # subscription's 110. Films only - TMDB lists no series provider for it
        # in this region, and _resolve_provider_id says so and moves on.
        provider_names=("Apple TV Store",),
        verified_offer_types=(OfferType.RENT, OfferType.BUY),
        walk_by_year=True,
        # Enough to finish the biggest year, which is a little over a thousand
        # films; the cap is per slice, and no slice comes near this.
        default_max_pages=MAX_PAGE,
        # Opt-in: a full sync costs a request per film on top of the listing,
        # and nobody upgrading should discover that by watching their nightly
        # run get longer. One toggle in the Manage tab turns it on.
        default_enabled=False,
    ),
    ProviderSource(
        key="hbo_max_il",
        name="HBO Max",
        kind=SourceKind.SUBSCRIPTION,
        website_url="https://www.hbomax.com/",
        provider_names=("HBO Max",),
    ),
    ProviderSource(
        key="mubi_il",
        name="MUBI",
        kind=SourceKind.SUBSCRIPTION,
        website_url="https://mubi.com/",
        provider_names=("MUBI",),
    ),
    ProviderSource(
        key="crunchyroll_il",
        name="Crunchyroll",
        kind=SourceKind.SUBSCRIPTION,
        website_url="https://www.crunchyroll.com/",
        provider_names=("Crunchyroll",),
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
                default_enabled=source.default_enabled,
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

        max_pages = _max_pages(ctx, source)

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

    def _offers_for(
        self,
        tmdb: TmdbClient,
        source: ProviderSource,
        kind: TitleKind,
        provider_id: int,
        hit: TmdbTitle,
        ctx: FetchContext,
    ) -> Iterator[RawItem]:
        """The offers this provider makes for one title.

        One item for a subscription service. For a storefront, one per way it
        actually sells the film - which is asked rather than assumed, and when
        the asking fails nothing is yielded at all. An invented offer is worse
        than a missing one: it sends somebody to a shop that is not selling it.
        """
        if not source.verified_offer_types:
            ctx.record_success()
            yield self._item(source, kind, hit, OfferType.STREAM, provider_id, tmdb.region)
            return

        try:
            offered = tmdb.title_watch_providers(kind, hit.tmdb_id)
        except Exception as exc:
            # "We could not ask", which is not the same as "it is not sold" -
            # and the difference matters because a title that yields no offer
            # is a title the sweep counts as missing. Recording it as an error
            # is what lets a run whose every lookup is failing give up, instead
            # of reporting an empty shop and retiring the catalog two nights
            # later. A scattered failure resets on the next title that reads.
            ctx.record_error(f"could not read offers for {hit.tmdb_id}", exc=exc)
            return

        ctx.record_success()
        for offer_type in source.verified_offer_types:
            if _carries(offered, offer_type, provider_id):
                yield self._item(source, kind, hit, offer_type, provider_id, tmdb.region)

    def _item(
        self,
        source: ProviderSource,
        kind: TitleKind,
        hit: TmdbTitle,
        offer_type: OfferType,
        provider_id: int,
        region: str,
    ) -> RawItem:
        return RawItem(
            source_key=source.key,
            kind=kind,
            name=hit.name,
            name_alt=hit.original_name if hit.original_name != hit.name else None,
            year=hit.year,
            tmdb_id=hit.tmdb_id,
            offer_type=offer_type,
            deep_link_url=watch_url(kind, hit.tmdb_id, region),
            poster_url=image_url(hit.poster_path) if hit.poster_path else None,
            extra={"provider_id": provider_id},
        )

    def _fetch_kind(
        self,
        tmdb: TmdbClient,
        source: ProviderSource,
        kind: TitleKind,
        provider_id: int,
        max_pages: int,
        ctx: FetchContext,
    ) -> Iterator[RawItem]:
        seen: set[int] = set()
        truncated = False

        for slice_filters in _slices(source):
            reported = 0

            def note_total(total: int, _slice: dict[str, Any] = slice_filters) -> None:
                nonlocal reported
                reported = total

            for hit in tmdb.discover_by_provider(
                kind,
                provider_id,
                max_pages=max_pages,
                filters=slice_filters,
                on_total=note_total,
            ):
                # A film belongs to one release year, but a slice boundary is
                # not worth trusting with a request per title.
                if hit.tmdb_id in seen:
                    continue
                seen.add(hit.tmdb_id)
                # Success is recorded once the title is actually read, not on
                # having been listed - see _offers_for.
                yield from self._offers_for(tmdb, source, kind, provider_id, hit, ctx)

            if reported > max_pages * 20:
                truncated = True
                ctx.logger.warning(
                    "%s %s: %s reports %d titles, more than %d pages can reach",
                    source.key,
                    kind.value,
                    _describe(slice_filters),
                    reported,
                    max_pages,
                )

        ctx.logger.info("%s %s: read %d titles", source.key, kind.value, len(seen))
        if truncated:
            ctx.logger.warning("%s %s: catalog may be truncated", source.key, kind.value)


def watch_url(kind: TitleKind, tmdb_id: int, region: str) -> str:
    """The page listing where this specific title can be watched."""
    media = "movie" if kind is TitleKind.MOVIE else "tv"
    return WATCH_URL_TEMPLATE.format(media=media, tmdb_id=tmdb_id, region=region)


def _source_for(key: str) -> ProviderSource | None:
    return next((source for source in PROVIDER_SOURCES if source.key == key), None)


def _client_from(ctx: FetchContext) -> TmdbClient:
    """Build a TMDB client, failing loudly if the key is missing."""
    ctx.settings.require("tmdb_api_key")
    assert ctx.settings.tmdb_api_key is not None
    return TmdbClient(
        ctx.http,
        ctx.settings.tmdb_api_key.get_secret_value(),
        rate_limit_rps=ctx.settings.tmdb.rate_limit_rps,
    )


#: TMDB's names for the ways a title can be offered, by our own offer type.
_BUCKETS: dict[OfferType, str] = {
    OfferType.STREAM: "flatrate",
    OfferType.RENT: "rent",
    OfferType.BUY: "buy",
    OfferType.FREE: "free",
}


def _carries(offered: dict[str, Any], offer_type: OfferType, provider_id: int) -> bool:
    """Whether this provider offers the title that particular way.

    Matched on provider id rather than name: the id is what was resolved from
    the region's provider list, and TMDB renames providers more often than it
    renumbers them.
    """
    bucket = offered.get(_BUCKETS[offer_type])
    if not isinstance(bucket, list):
        return False
    return any(
        isinstance(entry, dict) and entry.get("provider_id") == provider_id for entry in bucket
    )


#: Below this the store holds a few hundred films in total, so one query for
#: all of them costs a request instead of seventy that mostly answer nothing.
CATALOG_START_YEAR = 1950


def _slices(source: ProviderSource) -> Iterator[dict[str, Any]]:
    """The listings to read, which is one unless the catalog is too big for one.

    Sliced by release year because that is the partition TMDB offers that
    actually divides a film catalog evenly enough: every film has at most one
    release year, so the slices do not overlap, and the biggest is about a
    thousand films against a limit of ten thousand.

    A film TMDB holds no release date for falls in no slice and is not read.
    There were 29 of them in the Apple TV Store when this was written, out of
    17,799, and no discover filter selects them.
    """
    if not source.walk_by_year:
        yield {}
        return

    yield {"primary_release_date.lte": f"{CATALOG_START_YEAR - 1}-12-31"}
    for year in range(CATALOG_START_YEAR, utcnow().year + FUTURE_YEAR_ALLOWANCE + 1):
        yield {"primary_release_year": year}


def _describe(slice_filters: dict[str, Any]) -> str:
    """A slice, for a log line."""
    if not slice_filters:
        return "the whole catalog"
    year = slice_filters.get("primary_release_year")
    return str(year) if year else f"up to {CATALOG_START_YEAR - 1}"


def _max_pages(ctx: FetchContext, source: ProviderSource) -> int:
    """Pages per listing: the config file, else whatever the source asks for.

    The cap bounds one listing, and for a sliced catalog that is one release
    year rather than a whole storefront - so a source that slices sets its own
    default rather than inheriting a number chosen to bound a different shape.
    """
    configured = ctx.config.max_pages
    if configured is not None:
        return configured
    return source.default_max_pages or DEFAULT_MAX_PAGES
