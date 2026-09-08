"""``/api/v1/ingest`` - where a fetcher that has no disk here puts its work.

The fetcher used to open the database and the images directory itself, which
is the simplest possible arrangement and works exactly as long as the two run
on one machine. This router is the other arrangement: a fetcher on somebody's
laptop, downloading posters over their domestic connection, filling a catalog
on a server it cannot reach the filesystem of.

Three things it needs, and they are the three things here: what still needs
doing, somewhere to put the result, and a way to say a run happened. Nothing
else about the catalog is reachable through it.

**Everything arriving here is hostile until it has been checked.** That is not
a statement about whoever holds the token; it is that this endpoint takes a
compressed archive, and a compressed archive is a request that can be very much
larger than it looks, name files outside the directory it is unpacked into,
carry symlinks pointing at ``/etc``, or hold a hundred-megapixel image in nine
kilobytes. Each of those has its own guard below, and each guard has its own
refusal, so a rejected upload says which wall it hit.

The whole router is behind :func:`~eifo_api.deps.require_admin`, which 404s
rather than 403s - the same treatment the operator's surface gets, for the same
reason.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
import tarfile
import tempfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from PIL import Image, UnidentifiedImageError
from sqlalchemy import select
from sqlalchemy.orm import Session

from eifo_api.deps import AdminDep, CsrfDep, SessionDep, SettingsDep
from eifo_api.schemas import (
    IngestResult,
    PendingPoster,
    RejectedPoster,
    RunClose,
    RunOpen,
    RunOut,
)
from eifo_core import ingest as wire
from eifo_core.enums import FetchPhase, FetchStatus
from eifo_core.models import FetchRun, Title
from eifo_core.settings import Settings
from eifo_core.types import utcnow

logger = logging.getLogger("eifo.api.ingest")

router = APIRouter(prefix="/ingest", tags=["ingest"])

#: Titles offered in one call to the work list. The fetcher downloads every one
#: of them before it uploads anything, so this is also how much work it commits
#: to before it can report any of it.
MAX_PENDING = 500
DEFAULT_PENDING = 100


@router.get(
    "/posters/pending",
    response_model=list[PendingPoster],
    summary="Titles whose artwork still needs downloading",
)
def pending_posters(
    _admin: AdminDep,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=MAX_PENDING)] = DEFAULT_PENDING,
    force: Annotated[bool, Query()] = False,
    after: Annotated[int, Query(ge=0, description="Only titles past this id")] = 0,
) -> list[PendingPoster]:
    """What to fetch next.

    The same question the fetcher used to answer with a ``SELECT`` of its own,
    asked over the wire instead. It stays here rather than being reimplemented
    on the other side because "which titles are missing artwork" is a fact
    about the catalog, and the catalog is here.

    ``force`` re-offers titles that already have a poster, which is how a
    change to how artwork is rendered gets applied to a catalog that is
    otherwise complete.

    ``after`` is what lets the caller walk the whole catalog. Without it a
    ``force`` run would ask the same question and get the same first hundred
    titles for ever, because storing their artwork does not stop them matching
    - and even without ``force``, a title whose image will not decode would be
    re-offered on every pass of the same run rather than retried on the next
    one.
    """
    query = select(Title).where(Title.poster_source_url.is_not(None)).order_by(Title.id)
    if not force:
        query = query.where(Title.poster_path.is_(None))
    if after:
        query = query.where(Title.id > after)

    return [
        PendingPoster(title_id=title.id, source_url=title.poster_source_url)
        for title in session.scalars(query.limit(limit)).all()
        if title.poster_source_url
    ]


@router.post(
    "/posters",
    response_model=IngestResult,
    summary="Store a batch of downloaded posters",
)
async def store_posters(
    request: Request,
    _admin: AdminDep,
    _csrf: CsrfDep,
    session: SessionDep,
    settings: SettingsDep,
) -> IngestResult:
    """Take a batch of renditions and file them against their titles.

    The body is the gzipped tar itself rather than a multipart form. The sender
    is a script, so the form encoding would buy nothing and cost a dependency
    (``python-multipart``) on every install - including the small server this
    is meant to make possible.

    One batch is one transaction. A title whose files are rejected leaves the
    rest of the batch alone and comes back on the next work list, because its
    ``poster_path`` was never set - which is the same recovery an interrupted
    run gets, and means a bad image can never wedge the pipeline.

    Streaming the body is the only part that belongs on the event loop.
    Unpacking is gzip and tar over a batch of images and filing them is that
    many writes, which on the loop would stop the server serving anything at
    all until the batch was done - the same way a sync chunk used to.
    """
    with tempfile.TemporaryDirectory(prefix="eifo-ingest-") as work:
        staging = Path(work)
        body = await _spooled(request, staging / "upload.tar.gz")
        stored, rejected = await run_in_threadpool(
            _unpack_and_file, session, settings, body, staging / "unpacked"
        )

    logger.info("stored artwork for %d titles, rejected %d", stored, len(rejected))
    return IngestResult(stored=stored, rejected=rejected)


def _unpack_and_file(
    session: Session, settings: Settings, body: Path, unpacked: Path
) -> tuple[int, list[RejectedPoster]]:
    """Unpack the archive and file what it holds. Runs in a worker thread."""
    manifest = _unpack(body, unpacked)
    stored, rejected = _file_posters(session, manifest, unpacked, Path(settings.images_dir))
    session.commit()
    return stored, rejected


async def _spooled(request: Request, destination: Path) -> Path:
    """Write the body to disk, refusing it the moment it grows too large.

    Read in chunks and checked as it goes, rather than taken whole and measured
    afterwards: a declared ``Content-Length`` is a claim, and the only honest
    way to know how big a body is turns out to be counting it. Refusing at the
    limit means an oversized upload costs the limit, not the upload.
    """
    total = 0
    with destination.open("wb") as sink:
        async for chunk in request.stream():
            total += len(chunk)
            if total > wire.MAX_ARCHIVE_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail=(
                        f"The archive is larger than {wire.MAX_ARCHIVE_BYTES} bytes. "
                        "Send fewer titles per batch."
                    ),
                )
            sink.write(chunk)

    if total == 0:
        raise HTTPException(status_code=400, detail="The archive is empty.")
    return destination


def _unpack(body: Path, into: Path) -> wire.PosterManifest:
    """Extract the archive, distrusting every member of it.

    The manifest is read first and is the only thing that decides what gets
    extracted. Names are built from it and looked up; they are never taken
    from the tar. That is what makes the traversal question moot rather than
    merely handled - there is no path here that came from outside.
    """
    into.mkdir(parents=True, exist_ok=True)

    try:
        with tarfile.open(body, mode="r:gz") as tar:
            index = _index(tar)
            manifest = _read_manifest(tar, index)
            _extract_named(tar, index, manifest, into)
    except tarfile.TarError as exc:
        raise HTTPException(status_code=400, detail=f"Not a readable .tar.gz: {exc}") from exc

    return manifest


def _index(tar: tarfile.TarFile) -> dict[str, tarfile.TarInfo]:
    """The archive's members, by name, refusing one that has too many.

    A single bounded pass, and the bound is the point. ``getmember`` scans the
    whole archive on first use, so an archive of a million empty entries - which
    compresses to almost nothing and passes every size check above - would be
    read in full and held as a million objects before anybody asked what was in
    it. Counting while scanning is what makes the cap a cap rather than a thing
    checked after the damage.
    """
    index: dict[str, tarfile.TarInfo] = {}
    for member in tar:
        if len(index) >= wire.MAX_MEMBERS:
            raise HTTPException(
                status_code=413,
                detail=f"The archive holds more than {wire.MAX_MEMBERS} files.",
            )
        index[member.name] = member
    return index


def _read_manifest(tar: tarfile.TarFile, index: dict[str, tarfile.TarInfo]) -> wire.PosterManifest:
    member = index.get(wire.MANIFEST_NAME)
    if member is None:
        raise HTTPException(status_code=400, detail=f"The archive has no {wire.MANIFEST_NAME}.")

    if not member.isfile() or member.size > wire.MAX_FILE_BYTES:
        raise HTTPException(status_code=400, detail=f"{wire.MANIFEST_NAME} is not a sane file.")

    handle = tar.extractfile(member)
    if handle is None:  # pragma: no cover - isfile() already ruled this out
        raise HTTPException(status_code=400, detail=f"{wire.MANIFEST_NAME} could not be read.")

    try:
        return wire.PosterManifest.from_json(handle.read())
    except wire.ManifestError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _extract_named(
    tar: tarfile.TarFile,
    index: dict[str, tarfile.TarInfo],
    manifest: wire.PosterManifest,
    into: Path,
) -> None:
    """Pull out exactly the files the manifest names, and nothing else.

    Anything else in the archive is ignored rather than refused: a sender that
    packs a stray ``.DS_Store`` has not done anything dangerous, and the files
    that matter are the ones asked for by name.
    """
    wanted = [
        wire.member_name(item.title_id, variant)
        for item in manifest.items
        for variant in item.variants
    ]
    if len(wanted) + 1 > wire.MAX_MEMBERS:
        raise HTTPException(
            status_code=413,
            detail=f"The manifest names more than {wire.MAX_MEMBERS} files.",
        )

    unpacked = 0
    for name in wanted:
        member = index.get(name)
        if member is None:
            # Left for the filing pass to report against its title, so the
            # sender is told which title is short rather than only that one is.
            continue

        if not member.isfile():
            raise HTTPException(
                status_code=400,
                detail=f"{name} is not a regular file; symlinks and devices are not accepted.",
            )
        if member.size > wire.MAX_FILE_BYTES:
            raise HTTPException(
                status_code=413, detail=f"{name} is larger than {wire.MAX_FILE_BYTES} bytes."
            )

        unpacked += member.size
        if unpacked > wire.MAX_UNPACKED_BYTES:
            raise HTTPException(
                status_code=413,
                detail=(
                    f"The archive expands to more than {wire.MAX_UNPACKED_BYTES} bytes. "
                    "Send fewer titles per batch."
                ),
            )

        source = tar.extractfile(member)
        if source is None:  # pragma: no cover - isfile() already ruled this out
            continue
        # Built from the manifest, so it cannot escape `into` however the
        # archive names things.
        target = into / name
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("wb") as sink:
            shutil.copyfileobj(source, sink, length=1024 * 1024)


@dataclass(slots=True)
class _Checked:
    """One rendition that has survived being looked at."""

    variant: str
    data: bytes


def _file_posters(
    session: Session,
    manifest: wire.PosterManifest,
    unpacked: Path,
    images_dir: Path,
) -> tuple[int, list[RejectedPoster]]:
    """Verify each title's renditions, store them, and record the largest."""
    stored = 0
    rejected: list[RejectedPoster] = []

    for item in manifest.items:
        title = session.get(Title, item.title_id)
        if title is None:
            rejected.append(RejectedPoster(title_id=item.title_id, reason="no such title"))
            continue

        try:
            checked = _verify(item, unpacked)
        except _RejectedError as exc:
            rejected.append(RejectedPoster(title_id=item.title_id, reason=str(exc)))
            continue

        # Named for the largest rendition's bytes, and all of them share it, so
        # that from any variant's path the others are a suffix change away.
        digest = hashlib.sha256(checked[-1].data).hexdigest()
        for rendition in checked:
            relative = wire.poster_relpath(item.title_id, digest, rendition.variant)
            destination = images_dir / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(rendition.data)

        title.poster_path = wire.poster_relpath(item.title_id, digest, checked[-1].variant)
        stored += 1

    return stored, rejected


class _RejectedError(Exception):
    """One title's artwork will not be stored, and this is why."""


def _verify(item: wire.PosterItem, unpacked: Path) -> list[_Checked]:
    """Every rendition of one title, decoded and measured.

    Ordered smallest first, so the caller can take the last as the largest -
    the same ordering promise :data:`eifo_core.ingest.POSTER_VARIANTS` makes.
    """
    by_name = {variant.name: variant for variant in wire.POSTER_VARIANTS}
    ordered = [name for name in (v.name for v in wire.POSTER_VARIANTS) if name in item.variants]
    if not ordered:
        raise _RejectedError("no usable variants")

    checked: list[_Checked] = []
    for name in ordered:
        path = unpacked / item.member_name(name)
        if not path.is_file():
            raise _RejectedError(f"{name} is missing from the archive")

        data = path.read_bytes()
        width = _decoded_width(data, name)
        if width > by_name[name].width:
            raise _RejectedError(
                f"{name} is {width}px wide, wider than the {by_name[name].width}px it claims"
            )
        checked.append(_Checked(variant=name, data=data))

    return checked


def _decoded_width(data: bytes, name: str) -> int:
    """The image's width, having proved it is an image at all.

    Opened twice on purpose. ``verify()`` checks the structure but leaves the
    file unusable and does not decode the pixels, so a truncated or malformed
    image passes it and fails later - somewhere with less to say about what
    went wrong. The second pass loads it properly, which is also where a
    declared-enormous image spends the memory it was hoping to spend.
    """
    try:
        with Image.open(BytesIO(data)) as probe:
            probe.verify()
        with Image.open(BytesIO(data)) as image:
            if image.format != "JPEG":
                raise _RejectedError(f"{name} is {image.format}, and only JPEG is stored")
            if image.width * image.height > wire.MAX_PIXELS:
                raise _RejectedError(f"{name} decodes to more than {wire.MAX_PIXELS} pixels")
            image.load()
            return image.width
    except _RejectedError:
        raise
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise _RejectedError(f"{name} is not a readable image: {exc}") from exc


# ------------------------------------------------------------------------ runs
#
# A fetcher with no database still has to be able to say that a run happened,
# and in particular that one started and never finished. That was the whole
# point of opening the row at the start rather than writing it at the end, and
# it would be lost if a remote fetcher simply reported its totals at the finish.


@router.post("/runs", response_model=RunOut, status_code=201, summary="Record that a run started")
def open_run(
    _admin: AdminDep,
    _csrf: CsrfDep,
    session: SessionDep,
    body: RunOpen,
) -> RunOut:
    """Open a ``fetch_runs`` row and hand back its id.

    Committed immediately, which is the entire point: a row nobody can see
    until the phase ends is no better than the row that used to be written
    afterwards, and tells you nothing about the run that never came back.
    """
    _sweep_abandoned(session, body.phase)
    run = FetchRun(
        phase=body.phase,
        source_key=body.source_key,
        status=FetchStatus.RUNNING,
        started_at=body.started_at or utcnow(),
        stats={},
    )
    session.add(run)
    session.commit()
    return _run_out(run)


def _sweep_abandoned(session: Session, phase: FetchPhase) -> None:
    """Close rows from a fetcher that never came back.

    The fetcher has always done this for itself, and reasoned from the lock:
    it holds the only one, so anything still RUNNING belongs to a process that
    is gone. That reasoning does not survive this change. Fetchers now run on
    other people's machines, each holding its own lock, and two of them against
    one catalog is a supported arrangement rather than a mistake - so "still
    running" no longer implies "dead".

    Time does imply it. A run that has been open for a day is not a run that is
    still going, whoever started it, and marking it is what stops the panel
    filling with rows that will never close.
    """
    stale = session.scalars(
        select(FetchRun).where(
            FetchRun.phase == phase,
            FetchRun.status == FetchStatus.RUNNING,
            FetchRun.started_at < utcnow() - wire.ABANDONED_AFTER,
        )
    ).all()

    for run in stale:
        run.status = FetchStatus.CRASHED
        run.finished_at = utcnow()
        run.stats = {**(run.stats or {}), "errors": ["run ended without recording an outcome"]}
        logger.warning(
            "%s run started %s never finished; marking it crashed", run.phase, run.started_at
        )


@router.patch("/runs/{run_id}", response_model=RunOut, summary="Record how a run ended")
def close_run(
    run_id: int,
    _admin: AdminDep,
    _csrf: CsrfDep,
    session: SessionDep,
    body: RunClose,
) -> RunOut:
    """Close a run that this fetcher opened."""
    run = session.get(FetchRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="No such run.")

    run.status = body.status
    run.finished_at = body.finished_at or utcnow()
    run.stats = body.stats
    run.log = body.log
    session.commit()
    return _run_out(run)


def _run_out(run: FetchRun) -> RunOut:
    duration = (
        (run.finished_at - run.started_at).total_seconds() if run.finished_at is not None else None
    )
    return RunOut(
        id=run.id,
        source_key=run.source_key,
        phase=run.phase,
        status=run.status,
        started_at=run.started_at,
        finished_at=run.finished_at,
        duration_seconds=duration,
        stats=run.stats or {},
        has_log=bool(run.log),
    )


__all__ = ["router"]
