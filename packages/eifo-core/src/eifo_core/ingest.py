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
#: first pass over a large catalog's artwork is hours; a day is not.
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
