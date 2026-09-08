"""``/api/v1/ingest`` - the surface a fetcher writes through.

Two things are being checked, and the second is the one that matters. The first
is that a well-formed batch of posters lands where it should and is recorded
against the right titles. The second is that this endpoint - the only one in
the product that accepts a compressed archive and writes files out of it -
cannot be talked into writing them anywhere else, or into spending a machine's
whole disk finding out that it will not.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import tarfile
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from helpers import MakeAdmin, SignIn
from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from eifo_api.routers import ingest
from eifo_api.security import CSRF_HEADER
from eifo_core import ingest as wire
from eifo_core.enums import FetchPhase, FetchStatus, TitleKind
from eifo_core.models import FetchRun, Title
from eifo_core.types import utcnow

POSTERS = "/api/v1/ingest/posters"
PENDING = f"{POSTERS}/pending"
RUNS = "/api/v1/ingest/runs"


def jpeg(width: int = 500, height: int = 750, colour: str = "navy") -> bytes:
    """A real JPEG, because the endpoint decodes what it is given."""
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), colour).save(buffer, format="JPEG")
    return buffer.getvalue()


def png(width: int = 500, height: int = 750) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), "navy").save(buffer, format="PNG")
    return buffer.getvalue()


def build_archive(
    files: dict[str, bytes],
    manifest: str | bytes | None,
    *,
    extra: Callable[[tarfile.TarFile], None] | None = None,
) -> bytes:
    """A .tar.gz with exactly the members asked for, however wrong."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        if manifest is not None:
            raw = manifest.encode("utf-8") if isinstance(manifest, str) else manifest
            info = tarfile.TarInfo(wire.MANIFEST_NAME)
            info.size = len(raw)
            tar.addfile(info, io.BytesIO(raw))
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        if extra is not None:
            extra(tar)
    return buffer.getvalue()


def good_batch(*title_ids: int) -> bytes:
    """The archive a well-behaved fetcher sends."""
    manifest = wire.PosterManifest(
        items=tuple(
            wire.PosterItem(
                title_id=title_id,
                variants=("w200", "w500"),
                source_url=f"https://image.example/{title_id}.jpg",
            )
            for title_id in title_ids
        )
    )
    files = {
        wire.member_name(title_id, variant.name): jpeg(variant.width, variant.width * 3 // 2)
        for title_id in title_ids
        for variant in wire.POSTER_VARIANTS
    }
    return build_archive(files, manifest.to_json())


@pytest.fixture
def operator(sign_in: SignIn, make_admin: MakeAdmin) -> str:
    """A signed-in administrator, and the CSRF token their browser would send.

    The fetcher presents an API token instead and needs no CSRF header at all;
    a cookie is simply the shortest way to get an administrator in a test.
    """
    make_admin()
    return sign_in()


@pytest.fixture
def titles(session_factory: sessionmaker[Session]) -> list[int]:
    """Three titles with artwork to fetch and none fetched."""
    with session_factory() as session:
        rows = [
            Title(
                type=TitleKind.MOVIE,
                name_en=f"Title {index}",
                poster_source_url=f"https://image.example/{index}.jpg",
            )
            for index in range(3)
        ]
        session.add_all(rows)
        session.commit()
        return [row.id for row in rows]


class TestWhoMayUseIt:
    def test_a_stranger_is_not_told_it_exists(self, client: TestClient) -> None:
        # 404 rather than 401 or 403, exactly as the operator's surface does.
        assert client.get(PENDING).status_code in {401, 404}

    def test_a_signed_in_non_administrator_gets_404(
        self, client: TestClient, sign_in: SignIn
    ) -> None:
        sign_in()

        assert client.get(PENDING).status_code == 404

    def test_an_administrator_gets_the_work_list(
        self, client: TestClient, operator: str, titles: list[int]
    ) -> None:
        response = client.get(PENDING)

        assert response.status_code == 200
        assert [row["title_id"] for row in response.json()] == titles


class TestTheWorkList:
    def test_it_offers_only_titles_that_have_none(
        self,
        client: TestClient,
        operator: str,
        titles: list[int],
        session_factory: sessionmaker[Session],
    ) -> None:
        with session_factory() as session:
            session.get(Title, titles[0]).poster_path = "posters/1/abc-w500.jpg"  # type: ignore[union-attr]
            session.commit()

        assert [row["title_id"] for row in client.get(PENDING).json()] == titles[1:]

    def test_force_offers_them_all_again(
        self,
        client: TestClient,
        operator: str,
        titles: list[int],
        session_factory: sessionmaker[Session],
    ) -> None:
        with session_factory() as session:
            session.get(Title, titles[0]).poster_path = "posters/1/abc-w500.jpg"  # type: ignore[union-attr]
            session.commit()

        rows = client.get(PENDING, params={"force": "true"}).json()

        assert [row["title_id"] for row in rows] == titles

    def test_after_walks_past_what_has_been_seen(
        self, client: TestClient, operator: str, titles: list[int]
    ) -> None:
        """Without this a caller that cannot store what it downloads loops."""
        rows = client.get(PENDING, params={"after": titles[0]}).json()

        assert [row["title_id"] for row in rows] == titles[1:]

    def test_a_title_with_no_source_url_is_not_offered(
        self, client: TestClient, operator: str, session_factory: sessionmaker[Session]
    ) -> None:
        with session_factory() as session:
            session.add(Title(type=TitleKind.MOVIE, name_en="No artwork anywhere"))
            session.commit()

        assert client.get(PENDING).json() == []


class TestStoringABatch:
    def test_it_writes_both_renditions_and_records_the_largest(
        self,
        client: TestClient,
        operator: str,
        titles: list[int],
        session_factory: sessionmaker[Session],
        settings: object,
    ) -> None:
        response = client.post(
            POSTERS, content=good_batch(titles[0]), headers={CSRF_HEADER: operator}
        )

        assert response.status_code == 200
        assert response.json() == {"stored": 1, "rejected": []}

        with session_factory() as session:
            stored = session.get(Title, titles[0]).poster_path  # type: ignore[union-attr]
        assert stored is not None and stored.endswith("-w500.jpg")

        images = Path(settings.images_dir)  # type: ignore[attr-defined]
        assert (images / stored).is_file()
        # Both variants land, sharing one digest, so either can be derived from
        # the other by changing the suffix.
        assert (images / stored.replace("w500", "w200")).is_file()

    def test_the_path_is_named_for_the_bytes(
        self,
        client: TestClient,
        operator: str,
        titles: list[int],
        session_factory: sessionmaker[Session],
    ) -> None:
        """So that /images can go on saying `immutable` and not be lying."""
        client.post(POSTERS, content=good_batch(titles[0]), headers={CSRF_HEADER: operator})
        with session_factory() as session:
            first = session.get(Title, titles[0]).poster_path  # type: ignore[union-attr]

        different = build_archive(
            {
                wire.member_name(titles[0], "w200"): jpeg(200, 300, "crimson"),
                wire.member_name(titles[0], "w500"): jpeg(500, 750, "crimson"),
            },
            wire.PosterManifest(
                items=(wire.PosterItem(title_id=titles[0], variants=("w200", "w500")),)
            ).to_json(),
        )
        client.post(POSTERS, content=different, headers={CSRF_HEADER: operator})

        with session_factory() as session:
            second = session.get(Title, titles[0]).poster_path  # type: ignore[union-attr]
        assert first != second, "new artwork must not reuse the old URL"

    def test_sending_the_same_batch_twice_changes_nothing(
        self,
        client: TestClient,
        operator: str,
        titles: list[int],
        session_factory: sessionmaker[Session],
    ) -> None:
        # A retried upload is the normal case after a dropped connection, and
        # it must not produce a second copy under a second name.
        archive = good_batch(titles[0])
        client.post(POSTERS, content=archive, headers={CSRF_HEADER: operator})
        with session_factory() as session:
            first = session.get(Title, titles[0]).poster_path  # type: ignore[union-attr]

        client.post(POSTERS, content=archive, headers={CSRF_HEADER: operator})

        with session_factory() as session:
            assert session.get(Title, titles[0]).poster_path == first  # type: ignore[union-attr]

    def test_a_batch_of_several(
        self,
        client: TestClient,
        operator: str,
        titles: list[int],
        session_factory: sessionmaker[Session],
    ) -> None:
        response = client.post(
            POSTERS, content=good_batch(*titles), headers={CSRF_HEADER: operator}
        )

        assert response.json()["stored"] == 3
        with session_factory() as session:
            paths = session.scalars(select(Title.poster_path)).all()
        assert all(path is not None for path in paths)


class TestOneBadTitleDoesNotSpoilTheBatch:
    """A rejection is per title, and it says which and why.

    The alternative - failing the whole upload - would mean one unreadable
    image among a hundred good ones cost all hundred, and would do it again on
    every run, because the bad one is never stored and so is always offered.
    """

    def test_a_title_that_does_not_exist(
        self, client: TestClient, operator: str, titles: list[int]
    ) -> None:
        response = client.post(
            POSTERS, content=good_batch(titles[0], 999_999), headers={CSRF_HEADER: operator}
        )

        body = response.json()
        assert body["stored"] == 1
        assert body["rejected"] == [{"title_id": 999_999, "reason": "no such title"}]

    def test_a_file_the_manifest_promised_and_did_not_send(
        self, client: TestClient, operator: str, titles: list[int]
    ) -> None:
        archive = build_archive(
            {wire.member_name(titles[0], "w200"): jpeg(200, 300)},
            wire.PosterManifest(
                items=(wire.PosterItem(title_id=titles[0], variants=("w200", "w500")),)
            ).to_json(),
        )

        body = client.post(POSTERS, content=archive, headers={CSRF_HEADER: operator}).json()

        assert body["stored"] == 0
        assert "w500 is missing" in body["rejected"][0]["reason"]

    def test_something_that_is_not_an_image(
        self, client: TestClient, operator: str, titles: list[int]
    ) -> None:
        archive = build_archive(
            {wire.member_name(titles[0], "w500"): b"this is not an image"},
            wire.PosterManifest(
                items=(wire.PosterItem(title_id=titles[0], variants=("w500",)),)
            ).to_json(),
        )

        body = client.post(POSTERS, content=archive, headers={CSRF_HEADER: operator}).json()

        assert "not a readable image" in body["rejected"][0]["reason"]

    def test_an_image_that_is_not_a_jpeg(
        self, client: TestClient, operator: str, titles: list[int]
    ) -> None:
        # Only JPEG is stored, because only JPEG is what the path says it is.
        archive = build_archive(
            {wire.member_name(titles[0], "w500"): png(500, 750)},
            wire.PosterManifest(
                items=(wire.PosterItem(title_id=titles[0], variants=("w500",)),)
            ).to_json(),
        )

        body = client.post(POSTERS, content=archive, headers={CSRF_HEADER: operator}).json()

        assert "only JPEG is stored" in body["rejected"][0]["reason"]

    def test_a_rendition_wider_than_the_variant_it_claims_to_be(
        self, client: TestClient, operator: str, titles: list[int]
    ) -> None:
        """A w200 that is 1600px wide is not a thumbnail, whatever it is called."""
        archive = build_archive(
            {wire.member_name(titles[0], "w200"): jpeg(1600, 2400)},
            wire.PosterManifest(
                items=(wire.PosterItem(title_id=titles[0], variants=("w200",)),)
            ).to_json(),
        )

        body = client.post(POSTERS, content=archive, headers={CSRF_HEADER: operator}).json()

        assert "wider than" in body["rejected"][0]["reason"]

    def test_a_rejected_title_keeps_no_poster_path(
        self,
        client: TestClient,
        operator: str,
        titles: list[int],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Which is what puts it back on the next run's work list."""
        archive = build_archive(
            {wire.member_name(titles[0], "w500"): b"nonsense"},
            wire.PosterManifest(
                items=(wire.PosterItem(title_id=titles[0], variants=("w500",)),)
            ).to_json(),
        )

        client.post(POSTERS, content=archive, headers={CSRF_HEADER: operator})

        with session_factory() as session:
            assert session.get(Title, titles[0]).poster_path is None  # type: ignore[union-attr]


class TestArchivesThatAreNotWelcome:
    """The reason this endpoint is written the way it is."""

    def test_a_member_that_tries_to_climb_out_of_the_directory(
        self, client: TestClient, operator: str, titles: list[int], tmp_path: Path
    ) -> None:
        """The classic. It cannot work here, and the test says why.

        Nothing is extracted by the name the archive carries: the receiver
        builds each name from the manifest and looks it up. A member called
        ``../../etc/thing`` is simply never asked for.
        """
        archive = build_archive(
            {
                "../../escaped.jpg": jpeg(200, 300),
                wire.member_name(titles[0], "w500"): jpeg(500, 750),
            },
            wire.PosterManifest(
                items=(wire.PosterItem(title_id=titles[0], variants=("w500",)),)
            ).to_json(),
        )

        response = client.post(POSTERS, content=archive, headers={CSRF_HEADER: operator})

        assert response.json()["stored"] == 1
        assert not (tmp_path / "escaped.jpg").exists()
        assert not Path("/tmp/escaped.jpg").exists()

    def test_a_symlink_where_a_poster_should_be(
        self, client: TestClient, operator: str, titles: list[int]
    ) -> None:
        """A link is not a file, and writing through one writes somewhere else."""

        def add_symlink(tar: tarfile.TarFile) -> None:
            info = tarfile.TarInfo(wire.member_name(titles[0], "w500"))
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/passwd"
            tar.addfile(info)

        archive = build_archive(
            {},
            wire.PosterManifest(
                items=(wire.PosterItem(title_id=titles[0], variants=("w500",)),)
            ).to_json(),
            extra=add_symlink,
        )

        response = client.post(POSTERS, content=archive, headers={CSRF_HEADER: operator})

        assert response.status_code == 400
        assert "not a regular file" in response.json()["detail"]

    def test_an_archive_that_expands_to_far_more_than_it_weighs(
        self, client: TestClient, operator: str, titles: list[int], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A few kilobytes of zeros that would otherwise fill a disk.

        The cap is lowered rather than the bomb enlarged: building a genuine
        256MB expansion would make this test cost more than it is worth, and
        what is being checked is that the counter is consulted at all.
        """
        monkeypatch.setattr(wire, "MAX_UNPACKED_BYTES", 1024)
        archive = build_archive(
            {wire.member_name(titles[0], "w500"): b"\0" * 500_000},
            wire.PosterManifest(
                items=(wire.PosterItem(title_id=titles[0], variants=("w500",)),)
            ).to_json(),
        )

        response = client.post(POSTERS, content=archive, headers={CSRF_HEADER: operator})

        assert response.status_code == 413
        assert "expands to more than" in response.json()["detail"]

    def test_a_body_larger_than_the_cap(
        self, client: TestClient, operator: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(wire, "MAX_ARCHIVE_BYTES", 512)

        response = client.post(POSTERS, content=good_batch(1), headers={CSRF_HEADER: operator})

        assert response.status_code == 413
        assert "Send fewer titles" in response.json()["detail"]

    def test_a_manifest_naming_more_files_than_are_allowed(
        self, client: TestClient, operator: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(wire, "MAX_MEMBERS", 8)
        manifest = wire.PosterManifest(
            items=tuple(wire.PosterItem(title_id=n, variants=("w200", "w500")) for n in range(1, 9))
        )

        response = client.post(
            POSTERS, content=build_archive({}, manifest.to_json()), headers={CSRF_HEADER: operator}
        )

        assert response.status_code == 413
        assert "manifest names more than 8 files" in response.json()["detail"]

    def test_an_archive_holding_more_files_than_are_allowed(
        self, client: TestClient, operator: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Counted while scanning, not after.

        ``getmember`` reads the whole archive on first use, so an archive of a
        million empty entries - which compresses to almost nothing and passes
        every size check above - would be read in full and held as a million
        objects before anybody asked what was in it.
        """
        monkeypatch.setattr(wire, "MAX_MEMBERS", 4)
        archive = build_archive({f"junk/{n}": b"" for n in range(20)}, manifest=None)

        response = client.post(POSTERS, content=archive, headers={CSRF_HEADER: operator})

        assert response.status_code == 413
        assert "archive holds more than 4 files" in response.json()["detail"]

    def test_an_empty_body(self, client: TestClient, operator: str) -> None:
        response = client.post(POSTERS, content=b"", headers={CSRF_HEADER: operator})

        assert response.status_code == 400
        assert "empty" in response.json()["detail"]

    def test_something_that_is_not_a_tarball(self, client: TestClient, operator: str) -> None:
        response = client.post(POSTERS, content=b"just some bytes", headers={CSRF_HEADER: operator})

        assert response.status_code == 400
        assert "readable .tar.gz" in response.json()["detail"]

    def test_an_archive_with_no_manifest(
        self, client: TestClient, operator: str, titles: list[int]
    ) -> None:
        archive = build_archive({wire.member_name(titles[0], "w500"): jpeg()}, manifest=None)

        response = client.post(POSTERS, content=archive, headers={CSRF_HEADER: operator})

        assert response.status_code == 400
        assert wire.MANIFEST_NAME in response.json()["detail"]

    def test_a_manifest_from_a_version_this_server_does_not_know(
        self, client: TestClient, operator: str
    ) -> None:
        raw = json.dumps({"version": 99, "kind": "posters", "items": []})

        response = client.post(
            POSTERS, content=build_archive({}, raw), headers={CSRF_HEADER: operator}
        )

        assert response.status_code == 400
        assert "newer or older" in response.json()["detail"]

    def test_files_the_manifest_does_not_name_are_ignored(
        self,
        client: TestClient,
        operator: str,
        titles: list[int],
        settings: object,
    ) -> None:
        """A stray .DS_Store is untidy, not dangerous."""
        archive = build_archive(
            {
                wire.member_name(titles[0], "w500"): jpeg(500, 750),
                ".DS_Store": b"junk",
            },
            wire.PosterManifest(
                items=(wire.PosterItem(title_id=titles[0], variants=("w500",)),)
            ).to_json(),
        )

        response = client.post(POSTERS, content=archive, headers={CSRF_HEADER: operator})

        assert response.json()["stored"] == 1
        assert not (Path(settings.images_dir) / ".DS_Store").exists()  # type: ignore[attr-defined]


class TestRecordingARun:
    def test_a_run_is_visible_before_it_finishes(
        self, client: TestClient, operator: str, session_factory: sessionmaker[Session]
    ) -> None:
        """The whole reason the row is opened at the start.

        A fetcher stopped by a closed laptop lid cannot write its own obituary.
        If the row only appeared at the end, the runs most worth knowing about
        would leave no trace at all.
        """
        response = client.post(
            RUNS, json={"phase": FetchPhase.IMAGES.value}, headers={CSRF_HEADER: operator}
        )

        assert response.status_code == 201
        with session_factory() as session:
            run = session.get(FetchRun, response.json()["id"])
            assert run is not None
            assert run.status is FetchStatus.RUNNING
            assert run.finished_at is None

    def test_closing_one_records_what_it_did(
        self, client: TestClient, operator: str, session_factory: sessionmaker[Session]
    ) -> None:
        run_id = client.post(
            RUNS, json={"phase": FetchPhase.IMAGES.value}, headers={CSRF_HEADER: operator}
        ).json()["id"]

        response = client.patch(
            f"{RUNS}/{run_id}",
            json={
                "status": FetchStatus.OK.value,
                "stats": {"downloaded": 12, "failed": 0},
                "log": "…and then it finished",
            },
            headers={CSRF_HEADER: operator},
        )

        assert response.status_code == 200
        with session_factory() as session:
            run = session.get(FetchRun, run_id)
            assert run is not None
            assert run.status is FetchStatus.OK
            assert run.stats == {"downloaded": 12, "failed": 0}
            assert run.finished_at is not None

    def test_the_fetcher_s_own_clock_is_believed_for_when_it_started(
        self, client: TestClient, operator: str, session_factory: sessionmaker[Session]
    ) -> None:
        # The work began on the fetcher's machine, which is no longer
        # guaranteed to be this one.
        response = client.post(
            RUNS,
            json={"phase": FetchPhase.IMAGES.value, "started_at": "2026-01-02T03:04:05Z"},
            headers={CSRF_HEADER: operator},
        )

        with session_factory() as session:
            run = session.get(FetchRun, response.json()["id"])
            assert run is not None
            assert run.started_at.year == 2026
            assert run.started_at.hour == 3

    def test_closing_a_run_that_is_not_there(self, client: TestClient, operator: str) -> None:
        response = client.patch(
            f"{RUNS}/424242",
            json={"status": FetchStatus.OK.value},
            headers={CSRF_HEADER: operator},
        )

        assert response.status_code == 404

    def test_a_log_longer_than_the_cap_is_refused(self, client: TestClient, operator: str) -> None:
        """A cap only one side enforces is a cap on well-behaved senders."""
        run_id = client.post(
            RUNS, json={"phase": FetchPhase.IMAGES.value}, headers={CSRF_HEADER: operator}
        ).json()["id"]

        response = client.patch(
            f"{RUNS}/{run_id}",
            json={"status": FetchStatus.OK.value, "log": "x" * 500_000},
            headers={CSRF_HEADER: operator},
        )

        assert response.status_code == 422

    def test_a_non_administrator_cannot_invent_a_run(
        self, client: TestClient, sign_in: SignIn
    ) -> None:
        csrf = sign_in()

        response = client.post(
            RUNS, json={"phase": FetchPhase.IMAGES.value}, headers={CSRF_HEADER: csrf}
        )

        assert response.status_code == 404


class TestARunNobodyCameBackFrom:
    """The fetcher used to sweep these itself, reasoning from the lock.

    It held the only one, so anything still RUNNING belonged to a process that
    was gone. That stops being true the moment fetchers run on other people's
    machines: each holds its own lock, and two against one catalog is now a
    supported arrangement. Time is what implies death instead.
    """

    def _running_since(self, session_factory: sessionmaker[Session], hours: int) -> int:
        with session_factory() as session:
            run = FetchRun(
                phase=FetchPhase.IMAGES,
                status=FetchStatus.RUNNING,
                started_at=utcnow() - dt.timedelta(hours=hours),
                stats={},
            )
            session.add(run)
            session.commit()
            return run.id

    def test_a_day_old_run_is_marked_crashed_when_the_next_one_opens(
        self, client: TestClient, operator: str, session_factory: sessionmaker[Session]
    ) -> None:
        abandoned = self._running_since(session_factory, hours=30)

        client.post(RUNS, json={"phase": FetchPhase.IMAGES.value}, headers={CSRF_HEADER: operator})

        with session_factory() as session:
            run = session.get(FetchRun, abandoned)
            assert run is not None
            assert run.status is FetchStatus.CRASHED
            assert run.finished_at is not None
            assert "without recording an outcome" in run.stats["errors"][0]

    def test_a_run_that_may_still_be_going_is_left_alone(
        self, client: TestClient, operator: str, session_factory: sessionmaker[Session]
    ) -> None:
        """A first pass over a large catalog's artwork is hours, not minutes.

        Sweeping on "another fetcher started" would kill a live run on somebody
        else's machine, which is the failure this rule exists to avoid.
        """
        live = self._running_since(session_factory, hours=3)

        client.post(RUNS, json={"phase": FetchPhase.IMAGES.value}, headers={CSRF_HEADER: operator})

        with session_factory() as session:
            run = session.get(FetchRun, live)
            assert run is not None
            assert run.status is FetchStatus.RUNNING

    def test_another_phase_is_not_swept(
        self, client: TestClient, operator: str, session_factory: sessionmaker[Session]
    ) -> None:
        """Only the phase being opened; the rest are somebody else's business."""
        with session_factory() as session:
            other = FetchRun(
                phase=FetchPhase.SYNC,
                status=FetchStatus.RUNNING,
                started_at=utcnow() - dt.timedelta(hours=30),
                stats={},
            )
            session.add(other)
            session.commit()
            other_id = other.id

        client.post(RUNS, json={"phase": FetchPhase.IMAGES.value}, headers={CSRF_HEADER: operator})

        with session_factory() as session:
            assert session.get(FetchRun, other_id).status is FetchStatus.RUNNING  # type: ignore[union-attr]


class TestTheServerKeepsAnsweringWhileABatchIsFiled:
    """The third of the three, and the same reasoning as the other two.

    Streaming the archive belongs on the event loop. Unpacking gzip and tar
    over a batch of images, decoding each one and writing the renditions does
    not: on the loop it serves nothing else until the batch is done.
    """

    def test_another_request_is_served_while_a_batch_is_still_filing(
        self,
        client: TestClient,
        operator: str,
        titles: list[int],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        filing = threading.Event()
        release = threading.Event()
        real = ingest._file_posters

        def blocking(*args: Any, **kwargs: Any) -> Any:
            filing.set()
            # Bounded, so a regression fails this test rather than hanging the
            # whole suite.
            release.wait(timeout=30)
            return real(*args, **kwargs)

        monkeypatch.setattr(ingest, "_file_posters", blocking)

        answers: list[Any] = []
        batch = threading.Thread(
            target=lambda: answers.append(
                client.post(POSTERS, content=good_batch(titles[0]), headers={CSRF_HEADER: operator})
            )
        )
        probed: list[Any] = []
        probe = threading.Thread(target=lambda: probed.append(client.get("/api/v1/meta")))

        batch.start()
        try:
            assert filing.wait(timeout=10), "the batch never reached the filing"
            probe.start()
            probe.join(timeout=10)
            served = not probe.is_alive()
        finally:
            release.set()
            probe.join(timeout=15)
            batch.join(timeout=15)

        assert served, "the event loop was blocked: nothing was served while a batch filed"
        assert probed[0].status_code == 200
        assert answers[0].status_code == 200
