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

from eifo_core.enums import FetchPhase, FetchStatus, TitleKind
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


class FakeTmdb:
    """TMDB's details endpoint, as far as the fallback asks it anything."""

    def __init__(self, posters: dict[str, str | None] | None = None, *, fail: bool = False) -> None:
        #: Poster path per language; a language left out has none.
        self.posters = posters if posters is not None else {"en-US": "/tmdb.jpg"}
        self.fail = fail
        self.asked: list[tuple[TitleKind, int, str]] = []

    def details(self, kind: TitleKind, tmdb_id: int, *, language: str) -> dict[str, Any]:
        self.asked.append((kind, tmdb_id, language))
        if self.fail:
            raise httpx.ConnectError("TMDB is down")
        return {"id": tmdb_id, "poster_path": self.posters.get(language)}


TMDB_POSTER = "https://image.tmdb.org/t/p/w500/tmdb.jpg"


def gone(title_id: int = 1, **extra: Any) -> list[dict[str, Any]]:
    """A title whose own artwork is a dead link, and which TMDB knows."""
    row = {
        "title_id": title_id,
        "source_url": f"https://cdn.example/{title_id}",
        "kind": "movie",
        "tmdb_id": 500 + title_id,
    }
    return [row | extra]


class TestWhenTheArtworkIsGone:
    """Lev VOD's dead CDN links: retried every night, failing every night."""

    @pytest.fixture
    def tmdb_images(self, respx_mock: Any) -> Any:
        respx_mock.get(url__regex=r"https://image\.tmdb\.org/.*").mock(
            return_value=httpx.Response(200, content=png_bytes())
        )
        return respx_mock

    @pytest.mark.parametrize("status", [403, 404, 410])
    def test_tmdbs_poster_is_used_instead(self, tmdb_images: Any, status: int) -> None:
        tmdb_images.get("https://cdn.example/1").mock(return_value=httpx.Response(status))
        api = FakeApi(gone(1))
        tmdb = FakeTmdb()

        with HttpClient() as http, api.client() as client:
            result = ImageFetcher(http, client, tmdb=tmdb).fetch_missing()  # type: ignore[arg-type]

        assert (result.downloaded, result.failed, result.from_tmdb) == (1, 0, 1)
        assert result.as_stats()["from_tmdb"] == 1
        assert tmdb.asked == [(TitleKind.MOVIE, 501, "en-US")]
        # The record says where the artwork really came from.
        assert FakeApi._manifest(api.uploads[0]).items[0].source_url == TMDB_POSTER

    def test_hebrew_artwork_when_tmdb_has_no_english(self, tmdb_images: Any) -> None:
        tmdb_images.get("https://cdn.example/1").mock(return_value=httpx.Response(403))
        api = FakeApi(gone(1))
        tmdb = FakeTmdb({"he-IL": "/tmdb.jpg"})

        with HttpClient() as http, api.client() as client:
            result = ImageFetcher(http, client, tmdb=tmdb).fetch_missing()  # type: ignore[arg-type]

        assert result.from_tmdb == 1
        assert [language for *_, language in tmdb.asked] == ["en-US", "he-IL"]

    def test_a_host_having_a_bad_night_is_not_gone(self, respx_mock: Any) -> None:
        """A 5xx is retried tomorrow; the source's own artwork is still wanted."""
        respx_mock.get("https://cdn.example/1").mock(return_value=httpx.Response(503))
        api = FakeApi(gone(1))
        tmdb = FakeTmdb()

        with HttpClient(attempts=1) as http, api.client() as client:
            result = ImageFetcher(http, client, tmdb=tmdb).fetch_missing()  # type: ignore[arg-type]

        assert (result.failed, result.from_tmdb) == (1, 0)
        assert tmdb.asked == []

    @pytest.mark.parametrize("status", [408, 429])
    def test_asking_for_patience_is_not_gone(self, respx_mock: Any, status: int) -> None:
        respx_mock.get("https://cdn.example/1").mock(return_value=httpx.Response(status))
        api = FakeApi(gone(1))
        tmdb = FakeTmdb()

        with HttpClient(attempts=1) as http, api.client() as client:
            result = ImageFetcher(http, client, tmdb=tmdb).fetch_missing()  # type: ignore[arg-type]

        assert result.failed == 1
        assert tmdb.asked == []

    @pytest.mark.parametrize(
        ("row", "tmdb"),
        [
            ({"tmdb_id": None}, FakeTmdb()),
            ({"kind": "podcast"}, FakeTmdb()),
            ({}, FakeTmdb({})),
            ({}, FakeTmdb(fail=True)),
            ({}, None),
        ],
        ids=["no tmdb id", "unknown kind", "tmdb has no poster", "tmdb down", "no tmdb key"],
    )
    def test_still_a_failure_when_tmdb_cannot_help(
        self, respx_mock: Any, row: dict[str, Any], tmdb: FakeTmdb | None
    ) -> None:
        respx_mock.get("https://cdn.example/1").mock(return_value=httpx.Response(403))
        api = FakeApi(gone(1, **row))

        with HttpClient() as http, api.client() as client:
            result = ImageFetcher(http, client, tmdb=tmdb).fetch_missing()  # type: ignore[arg-type]

        assert (result.downloaded, result.failed, result.from_tmdb) == (0, 1, 0)
        assert api.uploads == []

    def test_a_dead_tmdb_poster_is_not_tried_twice(self, respx_mock: Any) -> None:
        """When the dead link *is* TMDB's, there is nothing to fall back to."""
        respx_mock.get(TMDB_POSTER).mock(return_value=httpx.Response(404))
        api = FakeApi(gone(1, source_url=TMDB_POSTER))

        with HttpClient() as http, api.client() as client:
            result = ImageFetcher(http, client, tmdb=FakeTmdb()).fetch_missing()  # type: ignore[arg-type]

        assert result.failed == 1
        assert respx_mock.calls.call_count == 1

    def test_the_rest_of_the_batch_is_untouched(self, tmdb_images: Any, poster_host: Any) -> None:
        tmdb_images.get("https://cdn.example/1").mock(return_value=httpx.Response(403))
        api = FakeApi(gone(1) + pending(2))

        with HttpClient() as http, api.client() as client:
            result = ImageFetcher(http, client, tmdb=FakeTmdb()).fetch_missing()  # type: ignore[arg-type]

        assert (result.downloaded, result.from_tmdb) == (2, 1)
        sources = {
            item.title_id: item.source_url for item in FakeApi._manifest(api.uploads[0]).items
        }
        assert sources == {1: TMDB_POSTER, 2: "https://image.example/2.jpg"}
