"""The artwork pipeline: variants, idempotency, and tolerated failures."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

import httpx
import pytest
import respx
from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from tvil_core.enums import TitleKind
from tvil_core.models import Title
from tvil_fetcher.http import HttpClient
from tvil_fetcher.images import (
    BACKDROP_VARIANTS,
    POSTER_VARIANTS,
    ImageFetcher,
    save_variants,
)

POSTER_URL = "https://img.example/poster.jpg"


def png_bytes(width: int = 900, height: int = 1350) -> bytes:
    """A real image of a given size, as a source site would serve."""
    buffer = BytesIO()
    Image.new("RGB", (width, height), color=(30, 90, 160)).save(buffer, format="PNG")
    return buffer.getvalue()


def add_title(session: Session, **overrides: object) -> Title:
    values: dict[str, object] = {
        "type": TitleKind.SERIES,
        "name_he": "פאודה",
        "year": 2015,
        "poster_source_url": POSTER_URL,
    }
    values.update(overrides)
    title = Title(**values)
    session.add(title)
    session.commit()
    return title


class TestSaveVariants:
    def test_writes_every_poster_size(self, tmp_path: Path) -> None:
        written = save_variants(png_bytes(), tmp_path, POSTER_VARIANTS)

        assert [path.name for path in written] == ["w200.jpg", "w500.jpg"]
        assert all(path.exists() for path in written)

    def test_resizes_to_the_requested_width(self, tmp_path: Path) -> None:
        save_variants(png_bytes(900, 1350), tmp_path, POSTER_VARIANTS)

        with Image.open(tmp_path / "w200.jpg") as image:
            assert image.width == 200
            assert image.height == 300  # aspect ratio preserved

    def test_never_upscales_a_small_source(self, tmp_path: Path) -> None:
        save_variants(png_bytes(120, 180), tmp_path, POSTER_VARIANTS)

        with Image.open(tmp_path / "w500.jpg") as image:
            assert image.width == 120

    def test_writes_jpeg_without_alpha(self, tmp_path: Path) -> None:
        save_variants(png_bytes(), tmp_path, POSTER_VARIANTS)

        with Image.open(tmp_path / "w500.jpg") as image:
            assert image.format == "JPEG"
            assert image.mode == "RGB"

    def test_supports_backdrop_sizes(self, tmp_path: Path) -> None:
        written = save_variants(png_bytes(1920, 1080), tmp_path, BACKDROP_VARIANTS)

        assert [path.name for path in written] == ["w1280.jpg"]

    def test_rejects_bytes_that_are_not_an_image(self, tmp_path: Path) -> None:
        with pytest.raises(Exception, match=r"cannot identify|image"):
            save_variants(b"this is not an image", tmp_path, POSTER_VARIANTS)


class TestImageFetcher:
    @respx.mock
    def test_downloads_and_records_the_stored_path(
        self, session_factory: sessionmaker[Session], http: HttpClient, tmp_path: Path
    ) -> None:
        respx.get(POSTER_URL).mock(return_value=httpx.Response(200, content=png_bytes()))
        with session_factory() as session:
            title = add_title(session)

        result = ImageFetcher(http, tmp_path / "images").fetch_missing(session_factory())

        assert result.downloaded == 1
        with session_factory() as session:
            stored = session.get(Title, title.id)
            assert stored is not None
            assert stored.poster_path == f"posters/{title.id}/w500.jpg"
            assert (tmp_path / "images" / stored.poster_path).exists()

    @respx.mock
    def test_skips_titles_that_already_have_artwork(
        self, session_factory: sessionmaker[Session], http: HttpClient, tmp_path: Path
    ) -> None:
        route = respx.get(POSTER_URL).mock(return_value=httpx.Response(200, content=png_bytes()))
        with session_factory() as session:
            add_title(session)
        fetcher = ImageFetcher(http, tmp_path / "images")

        fetcher.fetch_missing(session_factory())
        second = fetcher.fetch_missing(session_factory())

        assert second.downloaded == 0
        assert route.call_count == 1

    @respx.mock
    def test_force_re_downloads(
        self, session_factory: sessionmaker[Session], http: HttpClient, tmp_path: Path
    ) -> None:
        route = respx.get(POSTER_URL).mock(return_value=httpx.Response(200, content=png_bytes()))
        with session_factory() as session:
            add_title(session)
        fetcher = ImageFetcher(http, tmp_path / "images")

        fetcher.fetch_missing(session_factory())
        fetcher.fetch_missing(session_factory(), force=True)

        assert route.call_count == 2

    @respx.mock
    def test_ignores_titles_with_no_artwork_url(
        self, session_factory: sessionmaker[Session], http: HttpClient, tmp_path: Path
    ) -> None:
        with session_factory() as session:
            add_title(session, poster_source_url=None)

        result = ImageFetcher(http, tmp_path / "images").fetch_missing(session_factory())

        assert result == type(result)()  # untouched tally

    @respx.mock
    def test_a_download_failure_is_counted_not_raised(
        self, session_factory: sessionmaker[Session], http: HttpClient, tmp_path: Path
    ) -> None:
        """A missing poster must never fail a run; the next one retries."""
        respx.get(POSTER_URL).mock(return_value=httpx.Response(404))
        with session_factory() as session:
            title = add_title(session)

        result = ImageFetcher(http, tmp_path / "images").fetch_missing(session_factory())

        assert result.failed == 1
        with session_factory() as session:
            stored = session.get(Title, title.id)
            assert stored is not None and stored.poster_path is None

    @respx.mock
    def test_a_corrupt_image_is_counted_not_raised(
        self, session_factory: sessionmaker[Session], http: HttpClient, tmp_path: Path
    ) -> None:
        respx.get(POSTER_URL).mock(return_value=httpx.Response(200, content=b"garbage"))
        with session_factory() as session:
            add_title(session)

        result = ImageFetcher(http, tmp_path / "images").fetch_missing(session_factory())

        assert result.failed == 1

    @respx.mock
    def test_limit_stops_early(
        self, session_factory: sessionmaker[Session], http: HttpClient, tmp_path: Path
    ) -> None:
        respx.get(POSTER_URL).mock(return_value=httpx.Response(200, content=png_bytes()))
        with session_factory() as session:
            for index in range(3):
                add_title(session, name_he=f"תוכנית {index}")

        result = ImageFetcher(http, tmp_path / "images").fetch_missing(session_factory(), limit=2)

        assert result.downloaded == 2

    @respx.mock
    def test_recovers_the_path_when_the_file_is_already_on_disk(
        self, session_factory: sessionmaker[Session], http: HttpClient, tmp_path: Path
    ) -> None:
        """A run interrupted after writing but before committing self-heals."""
        respx.get(POSTER_URL).mock(return_value=httpx.Response(200, content=png_bytes()))
        with session_factory() as session:
            title = add_title(session)
        images_dir = tmp_path / "images"
        ImageFetcher(http, images_dir).fetch_missing(session_factory())

        with session_factory() as session:
            stored = session.get(Title, title.id)
            assert stored is not None
            stored.poster_path = None
            session.commit()

        result = ImageFetcher(http, images_dir).fetch_missing(session_factory())

        assert result.skipped == 1
        with session_factory() as session:
            recovered = session.get(Title, title.id)
            assert recovered is not None
            assert recovered.poster_path == f"posters/{title.id}/w500.jpg"


class TestPendingSelection:
    @respx.mock
    def test_only_titles_missing_artwork_are_fetched(
        self, session_factory: sessionmaker[Session], http: HttpClient, tmp_path: Path
    ) -> None:
        respx.get(POSTER_URL).mock(return_value=httpx.Response(200, content=png_bytes()))
        with session_factory() as session:
            add_title(session, name_he="עם תמונה", poster_path="posters/9/w500.jpg")
            add_title(session, name_he="בלי תמונה")

        result = ImageFetcher(http, tmp_path / "images").fetch_missing(session_factory())

        assert result.downloaded == 1
        with session_factory() as session:
            assert len(session.scalars(select(Title)).all()) == 2
