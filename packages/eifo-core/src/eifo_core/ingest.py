"""What the fetcher sends and what the API accepts.

Until now the database was the only contract between the two services: the
fetcher wrote rows and files, the API read them, and neither had to know the
other existed. That works exactly as long as they share a disk.

They no longer have to. A fetcher can run on a laptop and fill a catalog on a
server it has no filesystem access to at all - which means there is now a second
contract, carried over HTTP, and it needs a single definition for the same
reason the schema does. Two implementations of "what a poster archive looks
like" would be two things that must never disagree, and the way they would
announce their disagreement is an upload that one side builds happily and the
other rejects with nothing useful to say about why.

So this module holds the shape and the limits, and neither service holds its
own copy. What it deliberately does not hold is the work: packing an archive
needs Pillow and a temporary directory, unpacking one needs to distrust every
byte in it, and those belong to the sides that do them.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from typing import Any

from eifo_core.enums import FetchPhase, OfferType, RatingProvider, TitleKind
from eifo_core.findings import EnrichResult, Rating, TitleView
from eifo_core.items import RawItem, TmdbTitle

#: Bumped when a change would make an older fetcher's archive wrong rather than
#: merely incomplete. The server refuses what it does not recognise instead of
#: guessing, because guessing at an unknown layout is how a poster ends up
#: filed against the wrong title.
ARCHIVE_VERSION = 1

#: The one file in the archive that is not an image.
MANIFEST_NAME = "manifest.json"

#: How the archive announces itself, and what the endpoint accepts.
ARCHIVE_MEDIA_TYPE = "application/gzip"


@dataclass(frozen=True, slots=True)
class Variant:
    """One stored rendition of an image.

    ``width`` is a ceiling, not a promise: artwork narrower than the variant is
    stored at its own size rather than enlarged, so a rendition may be smaller
    than its name suggests and never larger.
    """

    name: str
    width: int


#: Largest last. Callers rely on the order - the last variant is the one whose
#: path is recorded on the title, and the one whose bytes name the whole set.
POSTER_VARIANTS = (Variant("w200", 200), Variant("w500", 500))
BACKDROP_VARIANTS = (Variant("w1280", 1280),)

POSTER_VARIANT_NAMES = frozenset(variant.name for variant in POSTER_VARIANTS)

#: The phases a fetcher somewhere else could be running.
#:
#: All of them, now that sync and enrich write through the API too. It was one
#: phase when only artwork did, and the distinction earned its keep then: every
#: other phase opened the database directly, so it could only be running on the
#: machine holding the lock, and the lock proved what it always had.
#:
#: Nothing proves that any more. A fetcher on a laptop can be halfway through a
#: sync of this catalog while a command on the server holds a lock that means
#: nothing to it, so every phase has to be swept by the clock rather than on
#: sight. The set is kept - rather than the checks that read it being deleted -
#: because it is the thing that would have to shrink again if a phase ever went
#: back to writing locally, and an empty exception is easier to reason about
#: than a rule that has been inlined into two callers.
REMOTE_PHASES = frozenset(FetchPhase)

#: How long a run may sit RUNNING before it is taken to be gone.
#:
#: Shared, because both services now sweep and they must agree. It used to be
#: nobody's rule: the fetcher held the only lock, so anything still running
#: belonged to a process that was gone, and no threshold was needed. That
#: reasoning does not survive a fetcher on somebody else's machine - each holds
#: its own lock, and two against one catalog is a supported arrangement - so
#: "still running" stopped implying "dead" and time had to take over.
#:
#: Generously long, because it is standing in for knowledge neither side has. A
#: first pass over a large catalog's artwork is hours; a day is not. Long enough
#: to be a poor answer for a phase that does not need it, which is why it is
#: not applied to those.
ABANDONED_AFTER = dt.timedelta(hours=24)

#: Characters of the content digest that appear in a stored filename.
#:
#: Eight hex characters is 32 bits. The digest only has to be unique among the
#: renditions one title has ever had - a handful over the life of a catalog -
#: so this is about telling two versions of one poster apart, not about telling
#: apart every image in the world.
DIGEST_CHARS = 8


# --------------------------------------------------------------------- limits
#
# Every one of these exists because the endpoint accepts a compressed archive
# from a client, and a compressed archive is a request that can be very much
# larger than it looks. They are checked on the way in, before anything is
# written, and each has its own refusal so a rejected upload says which wall it
# hit rather than only that it hit one.

#: The compressed body. A batch of a hundred posters is a few megabytes.
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024

#: Everything the archive expands to. The ratio between this and the number
#: above is what stops a few kilobytes of zeros from filling a disk.
MAX_UNPACKED_BYTES = 256 * 1024 * 1024

#: Files in one archive, manifest included. Two renditions per title, so this
#: is a batch of five hundred titles with room to spare.
MAX_MEMBERS = 2_000

#: One image. Generous for a w500 JPEG; anything near it is not a poster.
MAX_FILE_BYTES = 8 * 1024 * 1024

#: Pixels in one decoded image, whatever its file size says. This is the
#: decompression bomb guard: a small file can declare enormous dimensions, and
#: the cost of finding out is paid in memory during the decode.
MAX_PIXELS = 40_000_000


@dataclass(frozen=True, slots=True)
class PosterItem:
    """One title's artwork, as the sender describes it."""

    title_id: int
    variants: tuple[str, ...]
    #: Where the artwork came from. Carried for the record rather than for the
    #: logic: the server never fetches it, and must not - a URL supplied by a
    #: client is a request it can be made to send on somebody else's behalf.
    source_url: str | None = None

    def member_name(self, variant: str) -> str:
        """Where this variant sits inside the archive."""
        return member_name(self.title_id, variant)


def member_name(title_id: int, variant: str) -> str:
    """The path of one rendition inside the archive.

    Flat and predictable, and never taken from the archive itself: the receiver
    builds this from the manifest and looks for it, rather than reading whatever
    names the tar happens to carry. A name that arrives from outside is a name
    that can contain ``..``.
    """
    return f"{title_id}/{variant}.jpg"


def stored_name(digest: str, variant: str) -> str:
    """The filename a rendition is saved under, once its bytes are known.

    Content-addressed, because ``/images`` is served ``immutable``: a poster
    rewritten at a fixed path is a poster every browser and cache that already
    has one will never see again. A new image is a new name, so the old URL
    stays true and the new one is fetched.

    All of a title's renditions share one digest - the largest one's - so that
    given any variant's path the others can be derived by changing the suffix.
    """
    return f"{digest[:DIGEST_CHARS]}-{variant}.jpg"


def poster_relpath(title_id: int, digest: str, variant: str) -> str:
    """Where a rendition is stored, relative to the images root.

    Always forward slashes: this is written to ``titles.poster_path`` and served
    back as the tail of an image URL, so a Windows separator here would be a
    backslash in a URL and a 404 for every poster in the catalog.
    """
    return f"posters/{title_id}/{stored_name(digest, variant)}"


@dataclass(frozen=True, slots=True)
class PosterManifest:
    """The archive's account of itself.

    Read before anything is extracted, so the receiver knows what it is being
    offered before it starts spending disk on it. It is a claim, not evidence:
    every file it names is still checked, and every file it does not name is
    ignored rather than trusted.
    """

    items: tuple[PosterItem, ...]
    version: int = ARCHIVE_VERSION
    kind: str = "posters"

    def to_json(self) -> str:
        return json.dumps(
            {
                "version": self.version,
                "kind": self.kind,
                "items": [
                    {
                        "title_id": item.title_id,
                        "variants": list(item.variants),
                        "source_url": item.source_url,
                    }
                    for item in self.items
                ],
            },
            ensure_ascii=False,
        )

    @classmethod
    def from_json(cls, raw: str | bytes) -> PosterManifest:
        """Parse a manifest, or say what is wrong with it.

        Raises:
            ManifestError: whenever the text is not a manifest this version
                knows how to honour. One exception type, because the caller's
                answer to all of them is the same refusal.
        """
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ManifestError(f"{MANIFEST_NAME} is not readable JSON: {exc}") from exc

        if not isinstance(payload, dict):
            raise ManifestError(f"{MANIFEST_NAME} is not an object")

        version = payload.get("version")
        if version != ARCHIVE_VERSION:
            raise ManifestError(
                f"archive version {version!r} is not {ARCHIVE_VERSION}; "
                "the sender is newer or older than this server"
            )
        if payload.get("kind") != "posters":
            raise ManifestError(f"this endpoint takes posters, not {payload.get('kind')!r}")

        raw_items = payload.get("items")
        if not isinstance(raw_items, list):
            raise ManifestError("items is not a list")

        return cls(items=tuple(_item(entry) for entry in raw_items), version=version)


def _item(entry: Any) -> PosterItem:
    if not isinstance(entry, dict):
        raise ManifestError("an item is not an object")

    title_id = entry.get("title_id")
    if not isinstance(title_id, int) or isinstance(title_id, bool) or title_id <= 0:
        raise ManifestError(f"title_id {title_id!r} is not a positive integer")

    variants = entry.get("variants")
    if not isinstance(variants, list) or not variants:
        raise ManifestError(f"title {title_id} names no variants")

    unknown = [name for name in variants if name not in POSTER_VARIANT_NAMES]
    if unknown:
        raise ManifestError(f"title {title_id} names unknown variants: {unknown}")

    source_url = entry.get("source_url")
    if source_url is not None and not isinstance(source_url, str):
        raise ManifestError(f"title {title_id} has a source_url that is not a string")

    return PosterItem(title_id=title_id, variants=tuple(variants), source_url=source_url)


class ManifestError(ValueError):
    """A manifest this server will not act on, with the reason in its message.

    The message is written to be shown to whoever sent the archive: they are
    the only person who can fix it, and "invalid manifest" would leave them
    reading their own code to find out which part.
    """


# ------------------------------------------------------------------ listings
#
# A sync used to be one process reading a catalog and writing rows. It is two
# now: a fetcher that reads, and an API that decides and writes. What crosses
# between them is a listing, and these are the shapes it crosses in.
#
# A chunk is a request and a transaction. Small enough that a failed one is
# retried alone and an interrupted run keeps what it had, large enough that the
# per-request cost is nothing beside the reading.

#: Listings per request. Matches the commit cadence the single-process pipeline
#: used, so the write lock is held for the same span it always was.
SYNC_CHUNK_SIZE = 200

#: The most a server will take in one chunk, whatever the sender thinks.
MAX_SYNC_CHUNK = 500

#: Titles handed to one enrich batch. Smaller than a sync chunk because each
#: one costs the fetcher a round of network calls before it can be reported.
ENRICH_CHUNK_SIZE = 25
MAX_ENRICH_CHUNK = 100

#: Titles the queue will hand over in one answer, and the most it ever will.
#:
#: Its own cap rather than the chunk size, because it is the other direction and
#: a different cost: a due title is seven small fields, and a thousand of them
#: is a small response, while a batch of findings is a write.
#:
#: It has to be a single answer. A title stays due until something reports on
#: it, so the queue does not move while it is being read - ask twice and the
#: same head comes back. Paging it would hand out the first hundred titles five
#: times and never reach the rest, which is exactly what a nightly run with the
#: default batch of five hundred did.
DUE_PAGE_SIZE = 100
MAX_DUE_PAGE = 1000

#: Rows of the Seret index read per request. The whole index is ~8,900 rows and
#: the crawl needs all of them before it can say what is stale, so this is
#: sized to fetch it in a handful of requests rather than to be gentle.
SERET_READ_PAGE = 2000

#: Crawled pages written per request. Small: each one cost a deliberately slow
#: round trip to somebody else's site, and a batch that fails is that much of a
#: patient crawl to do again.
SERET_WRITE_CHUNK = 200
MAX_SERET_WRITE_CHUNK = 1000

#: Titles asked about per request by the IMDb bulk pass, and ratings sent back
#: per request. Larger than anything else here because neither side does any
#: network work per row - it is one join over a file already downloaded.
IMDB_READ_PAGE = 2000
IMDB_WRITE_CHUNK = 2000
MAX_IMDB_WRITE_CHUNK = 5000


def item_to_wire(item: RawItem) -> dict[str, Any]:
    """One listing, as JSON the API can read back.

    Explicit rather than ``dataclasses.asdict``: the wire format is a contract
    and should change when somebody means it to, not when a field is renamed.
    """
    return {
        "source_key": item.source_key,
        "kind": item.kind.value,
        "name": item.name,
        "offer_type": item.offer_type.value,
        "name_alt": item.name_alt,
        "year": item.year,
        "tmdb_id": item.tmdb_id,
        "imdb_id": item.imdb_id,
        "deep_link_url": item.deep_link_url,
        "source_ref": item.source_ref,
        "poster_url": item.poster_url,
        "price_minor": item.price_minor,
        "price_currency": item.price_currency,
        "credits": [dict(entry) for entry in item.credits],
        "origin_countries": item.origin_countries,
        "extra": dict(item.extra),
    }


def item_from_wire(payload: Any) -> RawItem:
    """A listing, or a refusal naming what is wrong with it.

    Raises:
        WireError: whenever the payload is not one. The caller reports it
            against its own index in the chunk, so the sender is told which
            listing rather than only that one of two hundred was bad.
    """
    if not isinstance(payload, dict):
        raise WireError("a listing is not an object")
    try:
        return RawItem(
            source_key=str(payload["source_key"]),
            kind=TitleKind(payload["kind"]),
            name=str(payload["name"]),
            offer_type=OfferType(payload.get("offer_type", OfferType.STREAM.value)),
            name_alt=_optional_str(payload.get("name_alt")),
            year=_optional_int(payload.get("year")),
            tmdb_id=_optional_int(payload.get("tmdb_id")),
            imdb_id=_optional_str(payload.get("imdb_id")),
            deep_link_url=_optional_str(payload.get("deep_link_url")),
            source_ref=_optional_str(payload.get("source_ref")),
            poster_url=_optional_str(payload.get("poster_url")),
            price_minor=_optional_int(payload.get("price_minor")),
            price_currency=_optional_str(payload.get("price_currency")),
            credits=tuple(payload.get("credits") or ()),
            origin_countries=_optional_str(payload.get("origin_countries")),
            extra=payload.get("extra") or {},
        )
    except (KeyError, TypeError, ValueError) as exc:
        # RawItem validates itself - a blank name, a price with no currency -
        # and those refusals are as much part of the contract as the shape is.
        raise WireError(f"not a usable listing: {exc}") from exc


def tmdb_to_wire(hit: TmdbTitle) -> dict[str, Any]:
    return {
        "tmdb_id": hit.tmdb_id,
        "kind": hit.kind.value,
        "name": hit.name,
        "original_name": hit.original_name,
        "year": hit.year,
        "overview": hit.overview,
        "poster_path": hit.poster_path,
    }


def tmdb_from_wire(payload: Any) -> TmdbTitle:
    if not isinstance(payload, dict):
        raise WireError("a TMDB hit is not an object")
    try:
        return TmdbTitle(
            tmdb_id=int(payload["tmdb_id"]),
            kind=TitleKind(payload["kind"]),
            name=str(payload["name"]),
            original_name=_optional_str(payload.get("original_name")),
            year=_optional_int(payload.get("year")),
            overview=_optional_str(payload.get("overview")),
            poster_path=_optional_str(payload.get("poster_path")),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise WireError(f"not a usable TMDB hit: {exc}") from exc


# ------------------------------------------------------------------ findings


def view_to_wire(view: TitleView) -> dict[str, Any]:
    return {
        "id": view.id,
        "kind": view.kind.value,
        "name_he": view.name_he,
        "name_en": view.name_en,
        "year": view.year,
        "tmdb_id": view.tmdb_id,
        "imdb_id": view.imdb_id,
    }


def view_from_wire(payload: Any) -> TitleView:
    if not isinstance(payload, dict):
        raise WireError("a title is not an object")
    try:
        return TitleView(
            id=int(payload["id"]),
            kind=TitleKind(payload["kind"]),
            name_he=_optional_str(payload.get("name_he")),
            name_en=_optional_str(payload.get("name_en")),
            year=_optional_int(payload.get("year")),
            tmdb_id=_optional_int(payload.get("tmdb_id")),
            imdb_id=_optional_str(payload.get("imdb_id")),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise WireError(f"not a usable title: {exc}") from exc


def finding_to_wire(result: EnrichResult) -> dict[str, Any]:
    return {
        "ratings": [
            {
                "provider": rating.provider.value,
                "score_raw": rating.score_raw,
                "vote_count": rating.vote_count,
                "url": rating.url,
            }
            for rating in result.ratings
        ],
        "metadata_patch": dict(result.metadata_patch),
    }


def finding_from_wire(payload: Any) -> EnrichResult:
    if not isinstance(payload, dict):
        raise WireError("a finding is not an object")
    try:
        ratings = [
            Rating(
                provider=RatingProvider(entry["provider"]),
                score_raw=float(entry["score_raw"]),
                vote_count=_optional_int(entry.get("vote_count")),
                url=_optional_str(entry.get("url")),
            )
            for entry in payload.get("ratings") or []
        ]
    except (KeyError, TypeError, ValueError) as exc:
        raise WireError(f"not a usable rating: {exc}") from exc

    patch = payload.get("metadata_patch") or {}
    if not isinstance(patch, dict):
        raise WireError("metadata_patch is not an object")
    return EnrichResult(ratings=ratings, metadata_patch=patch)


def _optional_str(value: Any) -> str | None:
    return None if value is None else str(value)


def _optional_int(value: Any) -> int | None:
    # bool is an int in Python, and True would silently become 1.
    if value is None or isinstance(value, bool):
        return None
    return int(value)


class WireError(ValueError):
    """Something arrived that this version will not act on.

    Carries a message written for whoever sent it: they are the only person who
    can fix it, and "invalid payload" would leave them reading their own code to
    find out which field.
    """
