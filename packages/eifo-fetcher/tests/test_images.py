"""Artwork: downloading it, resizing it, and posting it to the API.

The fetcher no longer writes posters anywhere. It downloads, resizes, packs a
batch and sends it - so what is worth testing here is the packing and the
recovery, not the filing, which now belongs to ``eifo-api`` and is tested
there.

The API is stood in for by a stub, and deliberately not by importing the real
application: the two packages do not import each other, and a test that made
them would be the first place that stopped being true. What keeps the stub
honest is that it is built from :mod:`eifo_core.ingest` - the same definitions
the real endpoint reads - so a change to the contract breaks both sides at once
rather than letting them drift apart quietly.
"""

from __future__ import annotations

import io
import tarfile
from typing import Any

import httpx
import pytest
from PIL import Image, UnidentifiedImageError

from eifo_core.enums import FetchPhase, FetchStatus
from eifo_core.ingest import (
    BACKDROP_VARIANTS,
    MANIFEST_NAME,
    POSTER_VARIANTS,
    PosterManifest,
)
from eifo_core.types import utcnow
from eifo_fetcher.http import HttpClient
from eifo_fetcher.images import ImageFetcher, save_variants
from eifo_fetcher.ingest import IngestClient, IngestError


def png_bytes(width: int = 600, height: int = 900) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), "navy").save(buffer, format="PNG")
    return buffer.getvalue()


class FakeApi:
    """The ingest endpoint, as far as the fetcher can tell.

    Records what it was sent so a test can open the archive and check that what
    arrived is what the contract says should arrive.
    """

    def __init__(self, pending: list[dict[str, Any]] | None = None) -> None:
        self.pending = pending or []
        self.uploads: list[bytes] = []
        self.runs: list[dict[str, Any]] = []
        self.closed: list[dict[str, Any]] = []
        #: Set to make the next upload fail, as a server that has gone away or
        #: refused the batch would.
        self.upload_status = 200
        #: When true, storing a title does not remove it from the work list -
        #: which is what `--force` looks like, and the case where only the
        #: cursor can end the fetcher's loop.
        self.sticky = False

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def client(self) -> IngestClient:
        return IngestClient(
            "https://eifo.test", "eifo_pat_stub", http=httpx.Client(transport=self.transport())
        )

    def _handle(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer eifo_pat_stub"
        path = request.url.path

        if path.endswith("/posters/pending"):
            after = int(request.url.params.get("after", 0))
            limit = int(request.url.params.get("limit", 100))
            rows = [row for row in self.pending if row["title_id"] > after][:limit]
            return httpx.Response(200, json=rows)

        if path.endswith("/ingest/posters"):
            if self.upload_status != 200:
                return httpx.Response(self.upload_status, json={"detail": "no thank you"})
            self.uploads.append(request.content)
            manifest = self._manifest(request.content)
            # Stand in for the real endpoint's bookkeeping: a stored title is
            # no longer pending, which is what makes the fetcher's loop
            # terminate the way it does against the real one.
            stored = {item.title_id for item in manifest.items}
            if not self.sticky:
                self.pending = [row for row in self.pending if row["title_id"] not in stored]
            return httpx.Response(200, json={"stored": len(stored), "rejected": []})

        if path.endswith("/ingest/runs"):
            self.runs.append(_json(request))
            return httpx.Response(201, json={"id": len(self.runs)})

        if "/ingest/runs/" in path:
            self.closed.append(_json(request))
            return httpx.Response(200, json={"id": 1})

        raise AssertionError(f"the fetcher asked for something unexpected: {path}")

    @staticmethod
    def _manifest(archive: bytes) -> PosterManifest:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
            handle = tar.extractfile(MANIFEST_NAME)
            assert handle is not None
            return PosterManifest.from_json(handle.read())

    def members(self, index: int = 0) -> list[str]:
        with tarfile.open(fileobj=io.BytesIO(self.uploads[index]), mode="r:gz") as tar:
            return sorted(tar.getnames())


def _json(request: httpx.Request) -> dict[str, Any]:
    import json

    payload: dict[str, Any] = json.loads(request.content)
    return payload


@pytest.fixture
def poster_host(respx_mock: Any) -> Any:
    """Every poster URL the tests use, answered with a real image."""
    respx_mock.get(url__regex=r"https://image\.example/.*").mock(
        return_value=httpx.Response(200, content=png_bytes())
    )
    return respx_mock


def pending(*title_ids: int) -> list[dict[str, Any]]:
    return [
        {"title_id": title_id, "source_url": f"https://image.example/{title_id}.jpg"}
        for title_id in title_ids
    ]


class TestSaveVariants:
    def test_it_writes_every_variant_largest_last(self, tmp_path: Any) -> None:
        written = save_variants(png_bytes(), tmp_path, POSTER_VARIANTS)

        assert [path.name for path in written] == ["w200.jpg", "w500.jpg"]

    def test_it_scales_to_the_variant_width(self, tmp_path: Any) -> None:
        save_variants(png_bytes(900, 1350), tmp_path, POSTER_VARIANTS)

        with Image.open(tmp_path / "w200.jpg") as image:
            assert image.width == 200

    def test_it_never_enlarges(self, tmp_path: Any) -> None:
        # A small poster stretched to 500px is a blurry 500px poster, which is
        # worse than an honest small one.
        save_variants(png_bytes(120, 180), tmp_path, POSTER_VARIANTS)

        with Image.open(tmp_path / "w500.jpg") as image:
            assert image.width == 120

    def test_it_writes_jpeg_whatever_it_was_given(self, tmp_path: Any) -> None:
        save_variants(png_bytes(), tmp_path, POSTER_VARIANTS)

        with Image.open(tmp_path / "w500.jpg") as image:
            assert image.format == "JPEG"

    def test_backdrops_have_their_own_width(self, tmp_path: Any) -> None:
        written = save_variants(png_bytes(1920, 1080), tmp_path, BACKDROP_VARIANTS)

        assert [path.name for path in written] == ["w1280.jpg"]

    def test_bytes_that_are_not_an_image_are_refused(self, tmp_path: Any) -> None:
        with pytest.raises(UnidentifiedImageError):
            save_variants(b"this is not an image", tmp_path, POSTER_VARIANTS)


class TestWhatGetsSent:
    def test_the_archive_carries_a_manifest_and_both_renditions(self, poster_host: Any) -> None:
        api = FakeApi(pending(7))

        with HttpClient() as http, api.client() as client:
            ImageFetcher(http, client).fetch_missing()

        assert api.members() == ["7/w200.jpg", "7/w500.jpg", MANIFEST_NAME]

    def test_the_manifest_says_where_the_artwork_came_from(self, poster_host: Any) -> None:
        api = FakeApi(pending(7))

        with HttpClient() as http, api.client() as client:
            ImageFetcher(http, client).fetch_missing()

        manifest = FakeApi._manifest(api.uploads[0])
        assert manifest.items[0].source_url == "https://image.example/7.jpg"
        assert manifest.items[0].variants == ("w200", "w500")

    def test_nothing_is_left_on_this_machine(self, poster_host: Any, tmp_path: Any) -> None:
        """The staging directory is temporary; it is not this machine's poster."""
        api = FakeApi(pending(7))

        with HttpClient() as http, api.client() as client:
            ImageFetcher(http, client).fetch_missing()

        assert list(tmp_path.rglob("*.jpg")) == []

    def test_it_counts_what_the_server_says_it_stored(self, poster_host: Any) -> None:
        api = FakeApi(pending(1, 2, 3))

        with HttpClient() as http, api.client() as client:
            result = ImageFetcher(http, client).fetch_missing()

        assert result.downloaded == 3
        assert result.failed == 0


class TestBatching:
    def test_a_long_catalog_is_sent_in_batches(self, poster_host: Any) -> None:
        api = FakeApi(pending(*range(1, 8)))

        with HttpClient() as http, api.client() as client:
            result = ImageFetcher(http, client, batch_size=3).fetch_missing()

        assert [len(FakeApi._manifest(blob).items) for blob in api.uploads] == [3, 3, 1]
        assert result.downloaded == 7

    def test_limit_stops_early(self, poster_host: Any) -> None:
        api = FakeApi(pending(*range(1, 20)))

        with HttpClient() as http, api.client() as client:
            result = ImageFetcher(http, client, batch_size=3).fetch_missing(limit=5)

        assert result.downloaded == 5

    def test_it_asks_for_what_comes_after_the_last_batch(self, poster_host: Any) -> None:
        """Otherwise a title that cannot be stored is offered for ever.

        Storing does not shorten the work list here, which is what ``--force``
        looks like: every title still matches on the next call. The only thing
        that can end the loop is the cursor moving past what has been seen.
        """
        api = FakeApi(pending(1, 2, 3))
        api.sticky = True

        with HttpClient() as http, api.client() as client:
            result = ImageFetcher(http, client, batch_size=1).fetch_missing()

        assert result.downloaded == 3, "the cursor did not advance"


class TestWhenSomethingGoesWrong:
    def test_an_image_that_will_not_download_is_counted_and_skipped(self, respx_mock: Any) -> None:
        respx_mock.get("https://image.example/1.jpg").mock(return_value=httpx.Response(404))
        respx_mock.get("https://image.example/2.jpg").mock(
            return_value=httpx.Response(200, content=png_bytes())
        )
        api = FakeApi(pending(1, 2))

        with HttpClient() as http, api.client() as client:
            result = ImageFetcher(http, client).fetch_missing()

        assert result.failed == 1
        assert result.downloaded == 1

    def test_bytes_that_are_not_an_image_do_not_stop_the_batch(self, respx_mock: Any) -> None:
        respx_mock.get("https://image.example/1.jpg").mock(
            return_value=httpx.Response(200, content=b"<html>not a poster</html>")
        )
        respx_mock.get("https://image.example/2.jpg").mock(
            return_value=httpx.Response(200, content=png_bytes())
        )
        api = FakeApi(pending(1, 2))

        with HttpClient() as http, api.client() as client:
            result = ImageFetcher(http, client).fetch_missing()

        assert result.failed == 1
        assert FakeApi._manifest(api.uploads[0]).items[0].title_id == 2

    def test_a_batch_that_will_not_upload_is_unspent_work(self, poster_host: Any) -> None:
        """Not lost work: none of those titles has a poster_path yet."""
        api = FakeApi(pending(1, 2))
        api.upload_status = 503

        with HttpClient() as http, api.client() as client:
            result = ImageFetcher(http, client).fetch_missing()

        assert result.downloaded == 0
        assert result.failed == 2

    def test_a_title_the_server_rejects_is_reported_with_its_reason(self, poster_host: Any) -> None:
        api = FakeApi(pending(1))

        def rejecting(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/ingest/posters"):
                return httpx.Response(
                    200,
                    json={
                        "stored": 0,
                        "rejected": [{"title_id": 1, "reason": "w500 is not a readable image"}],
                    },
                )
            return httpx.Response(200, json=api.pending)

        client = IngestClient(
            "https://eifo.test",
            "eifo_pat_stub",
            http=httpx.Client(transport=httpx.MockTransport(rejecting)),
        )
        with HttpClient() as http, client:
            result = ImageFetcher(http, client, batch_size=1).fetch_missing(limit=1)

        assert result.failed == 1
        assert "not a readable image" in result.as_stats()["rejected"][0]  # type: ignore[index]


class TestTheClientItself:
    def test_it_says_which_setting_is_missing_rather_than_401ing_later(self) -> None:
        from eifo_core.settings import Settings

        with pytest.raises(IngestError, match="token create"):
            IngestClient.from_settings(Settings(_env_file=None))

    def test_a_revoked_token_is_explained(self) -> None:
        transport = httpx.MockTransport(lambda _r: httpx.Response(401, json={"detail": "nope"}))
        client = IngestClient(
            "https://eifo.test", "eifo_pat_x", http=httpx.Client(transport=transport)
        )

        with pytest.raises(IngestError, match="revoked"):
            client.pending_posters(limit=1)

    def test_a_404_is_read_as_the_likelier_cause(self) -> None:
        """The admin surface 404s a non-administrator, which reads as a typo."""
        transport = httpx.MockTransport(lambda _r: httpx.Response(404, json={}))
        client = IngestClient(
            "https://eifo.test", "eifo_pat_x", http=httpx.Client(transport=transport)
        )

        with pytest.raises(IngestError, match="administrator"):
            client.pending_posters(limit=1)

    def test_a_server_that_is_not_there_says_so(self) -> None:
        def refuse(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        client = IngestClient(
            "https://eifo.test",
            "eifo_pat_x",
            http=httpx.Client(transport=httpx.MockTransport(refuse)),
        )

        with pytest.raises(IngestError, match="could not be reached"):
            client.pending_posters(limit=1)

    def test_a_run_is_opened_before_the_work_and_closed_after(self) -> None:
        api = FakeApi()

        with api.client() as client:
            opened = client.open_run(FetchPhase.IMAGES, started_at=utcnow())
            client.close_run(opened, status=FetchStatus.OK, stats={"downloaded": 3})

        assert api.runs[0]["phase"] == FetchPhase.IMAGES.value
        assert api.closed[0]["status"] == FetchStatus.OK.value
        assert api.closed[0]["stats"] == {"downloaded": 3}
