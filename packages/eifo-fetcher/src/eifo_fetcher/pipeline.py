"""The sync pipeline: fetch, resolve, ship.

What is left of it. Reading a service's catalog is still here - that is the
part that needs plugins, an HTTP client and somebody's connection - but nothing
after that is. The listings go to ``/api/v1/ingest/sync``, which matches them
against the catalog, writes the rows, sweeps what has stopped being offered and
judges whether to believe the run at all.

The two rules that keep a misbehaving scraper from emptying a catalog are still
enforced, and still on every run; they simply live where the catalog does now:

* **Two strikes** - availability is only retired after an item has been missing
  from two consecutive *successful* syncs, so one flaky run never expires a
  catalog.
* **The volume guard** - a sync returning far less than the previous successful
  run is treated as a broken parser, not as mass removal: the run is recorded as
  ``aborted_suspicious`` and no sweep happens.

**TMDB stays on this side.** The API answers a chunk by naming the listings it
could not place from the catalog alone; only those are looked up here, and
offered again with the hit attached. Resolving everything up front would be one
request instead of two and thousands of needless lookups a night, since most
listings are already bound to a title by the source's own reference.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

from eifo_core import ingest as wire
from eifo_core.enums import FetchStatus
from eifo_core.items import RawItem, SourceInfo, TmdbTitle
from eifo_core.types import utcnow
from eifo_fetcher.ingest import IngestClient, IngestError
from eifo_fetcher.progress import ProgressTicker
from eifo_fetcher.runs import RunLogCapture, capturing, new_capture
from eifo_fetcher.sources.base import (
    FetchContext,
    SourcePlugin,
    TooManyErrorsError,
)
from eifo_fetcher.tmdb import TmdbClient

logger = logging.getLogger("eifo.fetch.pipeline")

#: Listings per request. The API commits a chunk as one transaction, so this is
#: also how much work an interrupted run leaves behind.
CHUNK_SIZE = wire.SYNC_CHUNK_SIZE


@dataclass
class SyncResult:
    """What one source's sync did."""

    source_key: str
    status: FetchStatus
    items_seen: int = 0
    availability_created: int = 0
    availability_updated: int = 0
    titles_created: int = 0
    retired: int = 0
    reviews_expired: int = 0
    errors: list[str] = field(default_factory=list)
    matched_by: dict[str, int] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status is FetchStatus.OK

    def as_stats(self) -> dict[str, Any]:
        return {
            "items_seen": self.items_seen,
            "reviews_expired": self.reviews_expired,
            "availability_created": self.availability_created,
            "availability_updated": self.availability_updated,
            "titles_created": self.titles_created,
            "retired": self.retired,
            "matched_by": self.matched_by,
            "errors": self.errors,
            "error_count": len(self.errors),
        }

    @classmethod
    def of(cls, payload: dict[str, Any]) -> SyncResult:
        """The outcome the API reported, as this side's result type."""
        return cls(
            source_key=str(payload["source_key"]),
            status=FetchStatus(payload["status"]),
            items_seen=int(payload.get("items_seen", 0)),
            availability_created=int(payload.get("availability_created", 0)),
            availability_updated=int(payload.get("availability_updated", 0)),
            titles_created=int(payload.get("titles_created", 0)),
            retired=int(payload.get("retired", 0)),
            reviews_expired=int(payload.get("reviews_expired", 0)),
            errors=list(payload.get("errors") or []),
            matched_by=dict(payload.get("matched_by") or {}),
        )


def sync_source(
    api: IngestClient,
    plugin: SourcePlugin,
    info: SourceInfo,
    ctx: FetchContext,
    *,
    tmdb: TmdbClient | None = None,
    items: Iterable[RawItem] | None = None,
    capture: RunLogCapture | None = None,
    chunk_size: int = CHUNK_SIZE,
) -> SyncResult:
    """Read one source end to end and ship it, chunk by chunk.

    Args:
        tmdb: used only for the listings the API could not place. Without one
            the matcher on the far side falls back to external ids and local
            fuzzy comparison, exactly as it does without a key today.
        items: pre-fetched listings, from the prefetcher or from a test.
        capture: a log capture already collecting for this source. The
            prefetcher opens one before it starts reading, which is how lines
            logged during the fetch reach the row for the source that logged
            them rather than whichever row happened to be open at the time.
    """
    started_at = utcnow()
    run_id = api.begin_sync(info, started_at=started_at)

    # Everything this source says goes into its own row. "mako returned
    # nothing" used to be answerable only by running it again and watching.
    captured = capture if capture is not None else new_capture()
    status = FetchStatus.OK
    fatal: str | None = None

    with capturing(captured):
        try:
            stream = items if items is not None else plugin.fetch(ctx)
            _ship(api, run_id, stream, info, ctx, tmdb, chunk_size)
        except TooManyErrorsError as exc:
            logger.error("%s", exc)
            status = FetchStatus.FAILED
            fatal = f"{type(exc).__name__}: {exc}"
        except Exception as exc:
            logger.exception("source %r failed", info.key)
            status = FetchStatus.FAILED
            # Whatever ended the run goes into the row. Without this a failed
            # sync records errors: [] - it says that it failed and nothing
            # about why, which is the one question anyone reading it will have.
            fatal = f"{type(exc).__name__}: {exc}"

    errors = list(ctx.errors)
    if fatal is not None:
        errors.append(f"fatal: {fatal}")

    try:
        outcome = api.finish_sync(run_id, status=status, errors=errors, log=captured.text())
    except IngestError as exc:
        # The work is done and mostly stored; only the verdict is missing. The
        # run stays open, which is what an unfinished run should look like, and
        # the server closes it once it is old enough to be certainly dead.
        logger.warning("%s: could not finish the run: %s", info.key, exc)
        return SyncResult(
            source_key=info.key, status=FetchStatus.FAILED, errors=[*errors, str(exc)]
        )

    return SyncResult.of(outcome)


def _ship(
    api: IngestClient,
    run_id: int,
    items: Iterable[RawItem],
    info: SourceInfo,
    ctx: FetchContext,
    tmdb: TmdbClient | None,
    chunk_size: int,
) -> None:
    """Send the stream in chunks, resolving whatever the API hands back."""
    ticker = ProgressTicker()
    seen = 0
    chunk: list[RawItem] = []

    for item in items:
        chunk.append(item)
        seen += 1
        if len(chunk) >= chunk_size:
            _send(api, run_id, chunk, info, tmdb)
            chunk = []
            if ticker.due(seen):
                # A catalog of twenty thousand listings is twenty minutes in
                # which the only thing telling this apart from a hang is that
                # it keeps saying where it has got to.
                logger.info("%s: %d listing(s) sent", info.key, seen)

    if chunk:
        _send(api, run_id, chunk, info, tmdb)
    logger.info("%s: %d listing(s) sent in total", info.key, seen)


def _send(
    api: IngestClient,
    run_id: int,
    chunk: list[RawItem],
    info: SourceInfo,
    tmdb: TmdbClient | None,
) -> None:
    """One chunk, and the second pass for whatever it could not place."""
    answer = api.offer(run_id, [{"item": wire.item_to_wire(item)} for item in chunk])
    _report(info.key, answer)

    pending = answer.get("needs_tmdb") or []
    if not pending:
        return
    if tmdb is None:
        # No key configured. The far side will fall back to fuzzy matching,
        # which is what a keyless install has always done.
        logger.debug("%s: %d listing(s) need TMDB and no key is set", info.key, len(pending))

    resolved = [_resolve(entry, tmdb) for entry in pending]
    second = api.offer(run_id, resolved)
    _report(info.key, second)


def _resolve(entry: dict[str, Any], tmdb: TmdbClient | None) -> dict[str, Any]:
    """Look one listing up, and pair the hit with it.

    A listing nothing is found for is still sent back, and marked as having been
    looked for. That flag is the whole handshake: without it "I found nothing"
    reads as "I have not looked", and the far side defers the same listing for
    ever rather than falling through to fuzzy matching, parking or creating.
    """
    listing = {key: value for key, value in entry.items() if key != "index"}
    hit = _search(listing, tmdb) if tmdb is not None else None
    return {
        "item": listing,
        "tmdb": None if hit is None else wire.tmdb_to_wire(hit),
        "resolved": True,
    }


def _search(listing: dict[str, Any], tmdb: TmdbClient) -> TmdbTitle | None:
    try:
        item = wire.item_from_wire(listing)
    except wire.WireError:  # pragma: no cover - it came from us
        return None
    for query in filter(None, (item.name, item.name_alt)):
        try:
            for candidate in tmdb.search(item.kind, query, year=item.year):
                return candidate
        except Exception:
            logger.exception("TMDB search failed for %r", query)
            return None
    return None


def _report(source_key: str, answer: dict[str, Any]) -> None:
    """Say what the far side made of a chunk, and complain about what it would not take."""
    for rejection in answer.get("rejected") or []:
        logger.warning(
            "%s: listing %s was not stored: %s",
            source_key,
            rejection.get("index"),
            rejection.get("reason"),
        )


def iter_with_error_capture(
    items: Iterator[RawItem],
    ctx: FetchContext,
) -> Iterator[RawItem]:
    """Yield items, turning per-item parse failures into recorded errors."""
    while True:
        try:
            item = next(items)
        except StopIteration:
            return
        except TooManyErrorsError:
            raise
        except Exception as exc:
            ctx.record_error("failed to parse an item", exc=exc)
            continue
        ctx.record_success()
        yield item
