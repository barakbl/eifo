"""Artwork download and resizing.

Posters come from TMDB where possible (stable CDN, licensing-clean) and from the
source's own listing otherwise. A missing poster never fails a sync: the client
has a placeholder, and the next run retries.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from PIL import Image, UnidentifiedImageError
from sqlalchemy import select
from sqlalchemy.orm import Session

from eifo_core.models import Title
from eifo_fetcher.http import HttpClient

logger = logging.getLogger("eifo.fetch.images")

JPEG_QUALITY = 82


@dataclass(frozen=True, slots=True)
class Variant:
    """One stored rendition of an image."""

    name: str
    width: int


#: Posters stored between commits, bounding both the write lock and what an
#: interrupted run throws away.
COMMIT_EVERY = 50

POSTER_VARIANTS = (Variant("w200", 200), Variant("w500", 500))
BACKDROP_VARIANTS = (Variant("w1280", 1280),)


@dataclass(slots=True)
class ImageResult:
    """Tally for one ``images`` run."""

    downloaded: int = 0
    skipped: int = 0
    failed: int = 0

    def as_stats(self) -> dict[str, int]:
        return {"downloaded": self.downloaded, "skipped": self.skipped, "failed": self.failed}


def poster_dir(images_dir: Path, title_id: int) -> Path:
    return images_dir / "posters" / str(title_id)


def backdrop_dir(images_dir: Path, title_id: int) -> Path:
    return images_dir / "backdrops" / str(title_id)


def relative_path(images_dir: Path, path: Path) -> str:
    """Store paths relative to the images root so the root can move."""
    return str(path.relative_to(images_dir))


def save_variants(
    data: bytes,
    destination: Path,
    variants: tuple[Variant, ...],
) -> list[Path]:
    """Write resized JPEGs, returning the files written, largest first.

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
    """Downloads and stores artwork for titles that lack it."""

    def __init__(self, http: HttpClient, images_dir: Path) -> None:
        self._http = http
        self._images_dir = images_dir

    def fetch_missing(
        self,
        session: Session,
        *,
        force: bool = False,
        limit: int | None = None,
    ) -> ImageResult:
        """Fill in poster artwork for titles missing it.

        Args:
            force: re-download even where a poster is already stored.
            limit: stop after this many titles, for a quick first run.
        """
        result = ImageResult()
        for index, (title, url) in enumerate(self._pending(session, force=force, limit=limit), 1):
            if self._store_poster(title, url, force=force, result=result):
                session.flush()
            # Artwork is downloaded one title at a time; holding the write lock
            # for the whole set would block anything else for as long as that
            # takes, and lose every stored path if the run were interrupted.
            if index % COMMIT_EVERY == 0:
                session.commit()
        session.commit()
        return result

    def _pending(
        self,
        session: Session,
        *,
        force: bool,
        limit: int | None,
    ) -> list[tuple[Title, str]]:
        """Titles with a known artwork URL that still need it stored."""
        query = select(Title).where(Title.poster_source_url.is_not(None)).order_by(Title.id)
        if not force:
            query = query.where(Title.poster_path.is_(None))
        if limit is not None:
            query = query.limit(limit)

        return [
            (title, title.poster_source_url)
            for title in session.scalars(query).all()
            if title.poster_source_url
        ]

    def _store_poster(
        self,
        title: Title,
        url: str,
        *,
        force: bool,
        result: ImageResult,
    ) -> bool:
        destination = poster_dir(self._images_dir, title.id)
        largest = destination / f"{POSTER_VARIANTS[-1].name}.jpg"

        # Only a title that already claims this file may skip the download.
        #
        # Paths are keyed by title id, and ids are reused: rebuild the catalog
        # and id 13 becomes a different title while the old id 13 poster is
        # still on disk. Adopting whatever file happens to sit at the path put
        # an Israeli talk-show still on an animated film. A title with no
        # recorded poster always fetches its own.
        if largest.exists() and not force and title.poster_path is not None:
            result.skipped += 1
            return False

        try:
            data = self._http.get(url).content
            written = save_variants(data, destination, POSTER_VARIANTS)
        except (UnidentifiedImageError, OSError) as exc:
            # Bad bytes or an unwritable path: log and move on, retry next run.
            result.failed += 1
            logger.warning("could not store poster for title %s: %r", title.id, exc)
            return False
        except Exception as exc:
            result.failed += 1
            logger.warning("could not download poster for title %s: %r", title.id, exc)
            return False

        title.poster_path = relative_path(self._images_dir, written[-1])
        result.downloaded += 1
        return True
