"""Artwork download and resizing.

Posters come from TMDB where possible (stable CDN, licensing-clean) and from the
source's own listing otherwise. A missing poster never fails a sync: the client
has a placeholder, and the next run retries.

**Nothing here writes to the catalog.** It downloads, resizes, packs a batch and
posts it to ``/api/v1/ingest/posters``, which is what lets this run somewhere
other than where the catalog lives - on a laptop, over a domestic connection,
against a server whose disk it cannot see. The renditions are made here rather
than there because the alternative is sending the originals: three times the
bytes over the slow link, and the resizing done on the machine that is most
likely to be the small one.

The work is batched, and that is the whole failure story. A batch that uploads
is done; a batch that does not is work the next run picks up, because what is
outstanding is derived from the catalog - titles with no ``poster_path`` - and
never from anything remembered here.
"""

from __future__ import annotations

import logging
import tarfile
import tempfile
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from eifo_core.ingest import (
    BACKDROP_VARIANTS,
    MANIFEST_NAME,
    POSTER_VARIANTS,
    PosterItem,
    PosterManifest,
    Variant,
    member_name,
)
from eifo_fetcher.http import HttpClient
from eifo_fetcher.ingest import IngestClient, IngestError, PendingPoster

logger = logging.getLogger("eifo.fetch.images")

JPEG_QUALITY = 82

#: Titles per upload. Small enough that an interrupted run has lost at most
#: this much work and that the far end is never holding a large body; big
#: enough that the per-request overhead is nothing next to the downloads.
BATCH_SIZE = 100

__all__ = [
    "BACKDROP_VARIANTS",
    "BATCH_SIZE",
    "POSTER_VARIANTS",
    "ImageFetcher",
    "ImageResult",
    "Variant",
    "save_variants",
]


@dataclass(slots=True)
class ImageResult:
    """Tally for one ``images`` run."""

    downloaded: int = 0
    skipped: int = 0
    failed: int = 0
    #: What the server would not take, in its words. Kept on the result so it
    #: reaches the run's stats: a poster rejected at the far end is invisible
    #: here otherwise, and "downloaded 100, stored 98" with no reason is the
    #: kind of number somebody has to go and investigate by hand.
    rejected: list[str] = field(default_factory=list)

    def as_stats(self) -> dict[str, object]:
        stats: dict[str, object] = {
            "downloaded": self.downloaded,
            "skipped": self.skipped,
            "failed": self.failed,
        }
        if self.rejected:
            stats["rejected"] = self.rejected[:50]
        return stats


def save_variants(
    data: bytes,
    destination: Path,
    variants: tuple[Variant, ...],
) -> list[Path]:
    """Write resized JPEGs, returning the files written, largest last.

    Raises:
        UnidentifiedImageError: if the bytes are not a readable image.
    """
    destination.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    with Image.open(BytesIO(data)) as source:
        # Drop EXIF and any alpha channel; JPEG has no use for either.
        image = source.convert("RGB")

        for variant in variants:
            path = destination / f"{variant.name}.jpg"
            resized = _fit_width(image, variant.width)
            resized.save(path, format="JPEG", quality=JPEG_QUALITY, optimize=True)
            written.append(path)

    return written


def _fit_width(image: Image.Image, width: int) -> Image.Image:
    """Scale to a target width, never enlarging beyond the original."""
    if image.width <= width:
        return image
    height = round(image.height * width / image.width)
    return image.resize((width, height), Image.Resampling.LANCZOS)


class ImageFetcher:
    """Downloads artwork for titles that lack it and posts it to the API."""

    def __init__(
        self,
        http: HttpClient,
        api: IngestClient,
        *,
        batch_size: int = BATCH_SIZE,
    ) -> None:
        self._http = http
        self._api = api
        self._batch_size = batch_size

    def fetch_missing(self, *, force: bool = False, limit: int | None = None) -> ImageResult:
        """Fill in poster artwork for every title the API says is missing it.

        Args:
            force: re-download even where a poster is already stored.
            limit: stop after this many titles, for a quick first run.

        Walks the catalog by title id rather than asking the same question
        repeatedly. The server offers what is outstanding *after* a given id,
        so a title that cannot be stored is passed over for the rest of this
        run and retried on the next one - rather than being re-offered
        immediately and turning one bad image into an endless loop.
        """
        result = ImageResult()
        cursor = 0
        remaining = limit

        while remaining is None or remaining > 0:
            want = self._batch_size if remaining is None else min(self._batch_size, remaining)
            pending = self._api.pending_posters(limit=want, force=force, after=cursor)
            if not pending:
                break

            self._do_batch(pending, result)
            cursor = pending[-1].title_id
            if remaining is not None:
                remaining -= len(pending)

        return result

    def _do_batch(self, pending: list[PendingPoster], result: ImageResult) -> None:
        """Download and resize one batch into a staging directory, then send it.

        The staging directory is temporary and is gone before this returns.
        Nothing about a poster survives on this machine: it is not this
        machine's poster.
        """
        with tempfile.TemporaryDirectory(prefix="eifo-posters-") as work:
            staging = Path(work)
            items = [
                item
                for poster in pending
                if (item := self._stage(poster, staging, result)) is not None
            ]
            if not items:
                return

            archive = staging / "batch.tar.gz"
            _pack(staging, items, archive)
            self._send(archive, items, result)

    def _stage(
        self, poster: PendingPoster, staging: Path, result: ImageResult
    ) -> PosterItem | None:
        """One title's renditions, written where the packer will find them."""
        try:
            data = self._http.get(poster.source_url).content
            save_variants(data, staging / str(poster.title_id), POSTER_VARIANTS)
        except (UnidentifiedImageError, OSError) as exc:
            # Bad bytes or an unwritable path: log and move on, retry next run.
            result.failed += 1
            logger.warning("could not render poster for title %s: %r", poster.title_id, exc)
            return None
        except Exception as exc:
            result.failed += 1
            logger.warning("could not download poster for title %s: %r", poster.title_id, exc)
            return None

        return PosterItem(
            title_id=poster.title_id,
            variants=tuple(variant.name for variant in POSTER_VARIANTS),
            source_url=poster.source_url,
        )

    def _send(self, archive: Path, items: list[PosterItem], result: ImageResult) -> None:
        try:
            outcome = self._api.upload_posters(archive)
        except IngestError as exc:
            # The whole batch is unspent work, not lost work: none of these
            # titles has a poster_path, so the next run is offered them again.
            result.failed += len(items)
            logger.warning("could not upload %d posters: %s", len(items), exc)
            return

        result.downloaded += int(outcome.get("stored", 0))
        for rejection in outcome.get("rejected", []):
            result.failed += 1
            reason = f"title {rejection.get('title_id')}: {rejection.get('reason')}"
            result.rejected.append(reason)
            logger.warning("the API would not store %s", reason)


def _pack(staging: Path, items: list[PosterItem], archive: Path) -> None:
    """Build the archive the ingest endpoint expects.

    Members are added under the names :func:`eifo_core.ingest.member_name`
    gives them, not under whatever the staging directory happens to be called.
    The receiver looks each one up by that name and ignores everything else, so
    a name that drifted would simply not arrive.
    """
    manifest = PosterManifest(items=tuple(items))
    manifest_path = staging / MANIFEST_NAME
    manifest_path.write_text(manifest.to_json(), encoding="utf-8")

    with tarfile.open(archive, mode="w:gz") as tar:
        tar.add(manifest_path, arcname=MANIFEST_NAME)
        for item in items:
            for variant in item.variants:
                name = member_name(item.title_id, variant)
                tar.add(staging / str(item.title_id) / f"{variant}.jpg", arcname=name)
