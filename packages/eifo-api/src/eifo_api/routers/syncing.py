"""``/api/v1/ingest/sync`` - a catalog sync, written from somewhere else.

A sync used to be one process: it read a service's catalog, decided what each
listing meant, and wrote the rows. It is two now. The fetcher reads - which is
the part that needs the network, the plugins and somebody's domestic
connection - and everything from "which title is this" onwards happens here,
because that is where the catalog is and a decision about it cannot be made
anywhere else.

**A run is three conversations, and it has to be.** The listings arrive in
chunks, but two of the things a sync does are properties of the whole run
rather than of any chunk: the volume guard compares this run's total against
the last believed one, and the sweep retires what this run did not see. Neither
question can be answered until the last chunk has landed, so the run is opened
first, fed, and finished.

**Chunks come in twos.** A first pass matches what can be matched from the
catalog alone - the source's own reference, an external id - and answers with
the listings it could not place. The fetcher resolves only those against TMDB
and sends the answers back. Matching everything up front would have been one
request instead of two, and would have made thousands of TMDB calls a night for
listings that were already bound to a title and never needed asking about.

Everything is per-listing, including the failures: one unusable listing is
reported against its index and the other hundred and ninety-nine are stored,
because the alternative costs a whole catalog for one bad row and will do it
again every night.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import select
from sqlalchemy.orm import Session

from eifo_api.deps import AdminDep, CsrfDep, SessionDep
from eifo_api.schemas import (
    BackfillsDone,
    SourceDeclaration,
    SourceRegistry,
    SyncBegun,
    SyncChunkOut,
    SyncFinish,
    SyncOutcome,
    SyncStart,
)
from eifo_core import ingest as wire
from eifo_core.catalog import (
    clear_backfill_requests,
    deactivate_missing_sources,
    expire_reviews,
    looks_truncated,
    register_declared_sources,
    requested_backfills,
    source_overrides,
    sweep_source,
    title_count,
    upsert_availability,
    upsert_source,
)
from eifo_core.enums import FetchPhase, FetchStatus
from eifo_core.fts import ensure_search_triggers
from eifo_core.items import RawItem, SourceInfo, TmdbTitle
from eifo_core.match import KnownTitles, MatchStats, TitleMatcher, TmdbUnavailableError
from eifo_core.models import FetchRun, Source
from eifo_core.people import apply_credits
from eifo_core.types import utcnow

logger = logging.getLogger("eifo.api.ingest.sync")

router = APIRouter(prefix="/ingest/sync", tags=["ingest"])

#: How much of this side's own narration to keep on a run row.
#:
#: A tenth of what the fetcher is allowed, because this is one line per chunk
#: rather than one per listing, and because the fetcher's log is the one that
#: explains a failure. Enough for a thousand chunks, which is a source of two
#: hundred thousand listings.
MAX_SERVER_LOG_CHARS = 6_000


@dataclass(slots=True)
class _Held:
    """What a run has accumulated so far, between one chunk and the next.

    Kept on the run row rather than in memory: the API has no session of its
    own between requests, and a total that lived in a worker's process would be
    lost the moment anything restarted - or be wrong the moment there were two.
    """

    items_seen: int = 0
    availability_created: int = 0
    availability_updated: int = 0
    titles_created: int = 0
    matched_by: dict[str, int] = field(default_factory=dict)

    @classmethod
    def of(cls, run: FetchRun) -> _Held:
        stats = run.stats or {}
        return cls(
            items_seen=int(stats.get("items_seen", 0)),
            availability_created=int(stats.get("availability_created", 0)),
            availability_updated=int(stats.get("availability_updated", 0)),
            titles_created=int(stats.get("titles_created", 0)),
            matched_by=dict(stats.get("matched_by") or {}),
        )

    def onto(self, run: FetchRun) -> None:
        run.stats = {
            **(run.stats or {}),
            "items_seen": self.items_seen,
            "availability_created": self.availability_created,
            "availability_updated": self.availability_updated,
            "titles_created": self.titles_created,
            "matched_by": self.matched_by,
        }


@router.post("/sources", response_model=SourceRegistry, summary="Declare what this fetcher has")
def register(
    request: Request,
    _admin: AdminDep,
    _csrf: CsrfDep,
    session: SessionDep,
    body: SourceDeclaration,
) -> SourceRegistry:
    """Write a row for every plugin, and answer with what is switched on.

    Both halves of one exchange, because they are two sides of one question. A
    source used to exist only once it had synced, which made the operator's
    list a list of sources that had already run: one switched off, or added in
    an upgrade, was invisible on the very screen whose job is showing services,
    so the toggle that would have switched it on was not there to press.

    What comes back is the override an administrator has set, which wins over
    the configuration file. Read per run rather than held, so a source switched
    off at midnight is off tonight without anyone restarting a daemon.

    It is also where the search index's triggers are checked, and that moved
    here for the same reason everything else did. A rebuild of ``titles`` drops
    them silently, and every title written afterwards is invisible to search
    with no sign anything is wrong - so the check belongs to whatever is about
    to write titles, and that is no longer the fetcher. Here rather than on the
    run that follows, because every sync makes this exchange and does it first,
    including one that turns out to have no source to read.
    """
    ensure_search_triggers(request.app.state.engine)

    declared = {
        entry.source_key: SourceInfo(
            key=entry.source_key,
            name=entry.name,
            kind=entry.kind,
            website_url=entry.website_url,
            logo_path=entry.logo_path,
            default_enabled=entry.default_enabled,
        )
        for entry in body.sources
    }
    added = register_declared_sources(session, declared, enabled=body.enabled)
    retired = deactivate_missing_sources(session, declared) if body.retire_missing else []
    overrides = source_overrides(session)
    session.commit()

    if added:
        logger.info("sources now known: %s", ", ".join(sorted(added)))
    if retired:
        logger.info("sources no longer declared: %s", ", ".join(sorted(retired)))
    return SourceRegistry(added=added, retired=retired, overrides=overrides)


@router.get("/backfills", response_model=list[str], summary="Sources an operator asked for")
def backfills(_admin: AdminDep, session: SessionDep) -> list[str]:
    """Keys somebody switched on in the Manage tab, oldest ask first."""
    return requested_backfills(session)


@router.post("/backfills/clear", status_code=204, summary="Mark those asks answered")
def clear_backfills(
    _admin: AdminDep,
    _csrf: CsrfDep,
    session: SessionDep,
    body: BackfillsDone,
) -> None:
    """Cleared after the sync, never before it.

    A fetcher that dies partway leaves the ask standing and the next tick tries
    again. The cost of running one twice is a repeated sync; the cost of
    clearing one too early is a source that was asked for and never arrives.
    """
    clear_backfill_requests(session, body.keys)
    session.commit()


@router.post("/runs", response_model=SyncBegun, status_code=201, summary="Begin a source's sync")
def begin(
    _admin: AdminDep,
    _csrf: CsrfDep,
    session: SessionDep,
    body: SyncStart,
) -> SyncBegun:
    """Register the source and open its run.

    The source row is written here rather than being assumed to exist: a plugin
    is the thing that knows what a service is called and where it lives, and the
    API cannot ask it. A service added in an upgrade has to become visible to
    the operator without anybody editing a table.

    """
    info = SourceInfo(
        key=body.source_key,
        name=body.name,
        kind=body.kind,
        website_url=body.website_url,
        logo_path=body.logo_path,
        default_enabled=body.default_enabled,
    )
    source = upsert_source(session, info)
    session.flush()

    # Opened before any listing arrives, so a sync that dies mid-flight leaves
    # a row saying it started rather than no row at all.
    run = FetchRun(
        phase=FetchPhase.SYNC,
        source_key=body.source_key,
        status=FetchStatus.RUNNING,
        started_at=body.started_at or utcnow(),
        stats={},
    )
    session.add(run)
    session.commit()

    logger.info("%s: sync run %d begun", body.source_key, run.id)
    return SyncBegun(run_id=run.id, source_id=source.id, started_at=run.started_at)


@router.post("/runs/{run_id}/items", response_model=SyncChunkOut, summary="Offer a chunk")
async def take_chunk(
    run_id: int,
    request: Request,
    _admin: AdminDep,
    _csrf: CsrfDep,
    session: SessionDep,
) -> SyncChunkOut:
    """Match and store one chunk, saying which listings still need TMDB.

    The body is a JSON array of listings, optionally paired with a TMDB hit the
    sender has already looked up - which is what the second pass sends back.

    Async only long enough to read the body. Matching a chunk is seconds of
    unbroken CPU, and on the event loop that is not a slow endpoint, it is a
    stopped server: nothing else is served while it runs, so the healthcheck
    times out, the web app hangs, and requests pile up holding sessions until
    the connection pool is exhausted. That is exactly how the Oracle box went
    dark on 2026-09-07. In a worker thread a heavy sync is merely a heavy sync.
    """
    payload = await _body(request)
    return await run_in_threadpool(_match_chunk, session, run_id, payload)


def _match_chunk(session: Session, run_id: int, payload: list[Any]) -> SyncChunkOut:
    """The chunk itself: match, write, tally. Runs in a worker thread.

    The session is used from a thread other than the one that made it, which is
    safe because it is used by this thread alone and the SQLite pool is built
    with ``check_same_thread=False``.
    """
    run = _running(session, run_id)
    source = _source_of(session, run)

    items: list[RawItem] = []
    hits: list[TmdbTitle | None] = []
    asked: list[bool] = []
    rejected: list[dict[str, Any]] = []

    for index, entry in enumerate(payload):
        listing = entry.get("item") if isinstance(entry, dict) and "item" in entry else entry
        try:
            items.append(wire.item_from_wire(listing))
        except wire.WireError as exc:
            rejected.append({"index": index, "reason": str(exc)})
            continue
        raw_hit = entry.get("tmdb") if isinstance(entry, dict) else None
        hits.append(None if raw_hit is None else _hit(raw_hit, rejected, index))
        # "I have looked this one up" and "I have not been asked yet" are
        # different answers and the difference is the whole handshake. Without
        # it a listing nobody could find is deferred a second time, handed back
        # again, and quietly dropped - which is every listing on an install with
        # no TMDB key.
        asked.append(bool(entry.get("resolved")) if isinstance(entry, dict) else False)

    stats = _Held.of(run)
    before = title_count(session)
    before_offers = (stats.availability_created, stats.availability_updated)
    # One matcher, but its resolver changes per listing: each one may or may
    # not have arrived with a hit, and the matcher must be told the difference.
    stats_seen = MatchStats()
    unresolved: list[int] = []
    written: dict[Any, Any] = {}
    # Read the catalog once for the chunk rather than once per listing. The
    # fuzzy comparison needs every title of a kind, so a 200-listing chunk was
    # reading 39,000 rows two hundred times - minutes of pure CPU on a small
    # box, for a catalog that had not changed between listings.
    known = KnownTitles(session)

    for index, item in enumerate(items):
        matcher = TitleMatcher(
            session,
            tmdb=_Resolver(hits[index], asked=asked[index]),
            stats=stats_seen,
            known=known,
        )
        try:
            match = matcher.match(item)
        except TmdbUnavailableError:
            # Nothing local claims this listing and no hit came with it, so the
            # matcher reached for TMDB and there is none here. Hand it back
            # rather than guess: the fetcher looks it up and offers it again.
            unresolved.append(index)
            continue

        # Counted here rather than on the way in: a deferred listing is offered
        # again once it has been looked up, and counting it both times would
        # inflate the total the volume guard is about to judge the run by.
        stats.items_seen += 1
        if match.title is None:
            continue

        if item.poster_url and not match.title.poster_source_url:
            match.title.poster_source_url = item.poster_url
        if item.origin_countries and not match.title.origin_countries:
            match.title.origin_countries = item.origin_countries
        if item.credits:
            apply_credits(session, match.title, item.credits, source=source.key)

        created = upsert_availability(
            session,
            title=match.title,
            source=source,
            item=item,
            seen_at=run.started_at,
            written=written,
        )
        if created:
            stats.availability_created += 1
        else:
            stats.availability_updated += 1

    created_titles = title_count(session) - before
    stats.titles_created += created_titles
    for method, count in stats_seen.as_dict().items():
        stats.matched_by[method] = stats.matched_by.get(method, 0) + count

    summary = _summarise(
        run.source_key or "?",
        listings=len(items),
        titles=created_titles,
        offers_created=stats.availability_created - before_offers[0],
        offers_updated=stats.availability_updated - before_offers[1],
        deferred=len(unresolved),
        methods=stats_seen.as_dict(),
    )
    logger.info("%s", summary)
    _note(run, summary)

    stats.onto(run)
    session.commit()

    return SyncChunkOut(
        stored=len(items) - len(unresolved),
        needs_tmdb=[wire.item_to_wire(items[index]) | {"index": index} for index in unresolved],
        rejected=rejected,
        matched_by=stats_seen.as_dict(),
    )


@router.post("/runs/{run_id}/finish", response_model=SyncOutcome, summary="Finish a source's sync")
def finish(
    run_id: int,
    _admin: AdminDep,
    _csrf: CsrfDep,
    session: SessionDep,
    body: SyncFinish,
) -> SyncOutcome:
    """Believe the run or not, then sweep, then close it.

    The order matters and it is the whole point of finishing separately. A run
    that returned far less than the last believed one is assumed to be a broken
    parser rather than a service that has shed nine tenths of its catalog, and
    nothing is retired on the strength of it - because retiring is how a
    parser bug becomes a catalog that quietly empties.
    """
    run = _running(session, run_id)
    source = _source_of(session, run)
    stats = _Held.of(run)

    status = body.status
    errors = list(body.errors)
    truncated = looks_truncated(session, run.source_key or "", stats.items_seen)
    if status is FetchStatus.OK and truncated:
        status = FetchStatus.ABORTED_SUSPICIOUS
        message = (
            f"{run.source_key} returned {stats.items_seen} items, far below its previous "
            "run; assuming a broken parser and skipping the sweep"
        )
        logger.error("%s", message)
        errors.append(message)

    retired = 0
    expired = 0
    if status is FetchStatus.OK:
        retired = sweep_source(session, source, run_started_at=run.started_at)
        expired = expire_reviews(session, source.key, before=run.started_at)
        logger.info("%s: %d retired, %d parked listings expired", source.key, retired, expired)

    run.status = status
    run.finished_at = utcnow()
    run.stats = {
        **(run.stats or {}),
        "retired": retired,
        "reviews_expired": expired,
        "errors": errors,
        "error_count": len(errors),
    }
    run.log = _joined(run.log, body.log)
    session.commit()

    return SyncOutcome(
        source_key=source.key,
        status=status,
        items_seen=stats.items_seen,
        availability_created=stats.availability_created,
        availability_updated=stats.availability_updated,
        titles_created=stats.titles_created,
        retired=retired,
        reviews_expired=expired,
        errors=errors,
        matched_by=stats.matched_by,
    )


class _Resolver:
    """Hands the matcher the TMDB hit its caller already looked up.

    The matcher asks TMDB through a search protocol. On this side there is no
    TMDB - the key lives with the fetcher, which is where the network is - so
    the "search" is a lookup of what arrived in the request.

    Three states, and the third is the one that has to be spelled out. A hit is
    a hit. Nothing, from a sender that has not been asked yet, is a deferral:
    the matcher stops, the listing goes back, the fetcher looks it up. Nothing,
    from a sender that *has* looked, is an honest empty search - and the matcher
    must be allowed to carry on to fuzzy matching, parking or creating, exactly
    as it does on an install with no TMDB key at all. Conflating the last two
    hands a listing back for ever, and it is every listing on a keyless install.
    """

    def __init__(self, hit: TmdbTitle | None = None, *, asked: bool = False) -> None:
        self._hit = hit
        self._asked = asked

    def search(self, kind: Any, query: str, *, year: int | None = None) -> list[TmdbTitle]:
        if self._hit is None:
            if self._asked:
                return []
            raise TmdbUnavailableError(query)
        return [self._hit] if self._hit.kind == kind else []


def _hit(raw: Any, rejected: list[dict[str, Any]], index: int) -> TmdbTitle | None:
    try:
        return wire.tmdb_from_wire(raw)
    except wire.WireError as exc:
        rejected.append({"index": index, "reason": str(exc)})
        return None


async def _body(request: Request) -> list[Any]:
    payload = await request.json()
    if not isinstance(payload, list):
        raise HTTPException(status_code=400, detail="Expected a JSON array of listings.")
    if len(payload) > wire.MAX_SYNC_CHUNK:
        raise HTTPException(
            status_code=413,
            detail=f"A chunk may hold at most {wire.MAX_SYNC_CHUNK} listings.",
        )
    return payload


def _running(session: Session, run_id: int) -> FetchRun:
    run = session.get(FetchRun, run_id)
    if run is None or run.phase is not FetchPhase.SYNC:
        raise HTTPException(status_code=404, detail="No such sync run.")
    if run.status is not FetchStatus.RUNNING:
        raise HTTPException(
            status_code=409,
            detail=f"That run is already {run.status.value}; begin another.",
        )
    return run


def _source_of(session: Session, run: FetchRun) -> Source:
    source = session.scalar(select(Source).where(Source.key == run.source_key))
    if source is None:  # pragma: no cover - begin() writes it
        raise HTTPException(status_code=404, detail="That run's source is gone.")
    return source


def _summarise(
    source_key: str,
    *,
    listings: int,
    titles: int,
    offers_created: int,
    offers_updated: int,
    deferred: int,
    methods: dict[str, int],
) -> str:
    """What one chunk turned into, as a sentence.

    Deliberately about outcomes rather than about matching. "Is it finding
    anything" is the question somebody watching a long sync is trying to
    answer, and a breakdown of match methods answers a different, later one -
    so the counts come first and the methods follow in brackets.
    """
    found = [
        f"{count} {label}"
        for count, label in (
            (titles, "new title(s)"),
            (offers_created, "new offer(s)"),
            (offers_updated, "already listed"),
            (deferred, "awaiting TMDB"),
        )
        if count
    ]
    how = ", ".join(f"{count} {method}" for method, count in sorted(methods.items()))
    tail = f" [{how}]" if how else ""
    return f"{source_key}: {listings} listing(s) in - {', '.join(found) or 'nothing'}{tail}"


def _note(run: FetchRun, line: str) -> None:
    """Append one of this side's decisions to the run's own log.

    So that one row holds the whole story. Half of a sync happens here now -
    what each listing matched to, what was parked, what was handed back - and
    none of it was reachable from the row: it went to the server's log, where
    it is interleaved with every other request the service was serving.

    Bounded, because a source with twenty thousand listings is a hundred of
    these and a misbehaving one could be far more. The head is kept rather than
    the tail, which is the opposite of what the fetcher keeps: the fetcher's
    interesting part is where it stopped, and this side's is what it was
    deciding while things still looked normal.
    """
    existing = run.log or ""
    if len(existing) >= MAX_SERVER_LOG_CHARS:
        return
    run.log = f"{existing}{utcnow().isoformat(timespec='seconds')} {line}\n"


def _joined(existing: str | None, addition: str | None) -> str | None:
    """The run's log: this side's decisions, then what the fetcher said.

    Not interleaved, and it cannot be. The fetcher keeps the tail of its own
    output and sends it when the run finishes, which is the only moment it has
    to send it - so the two halves arrive at different times and the row reads
    as two sections rather than one conversation.
    """
    parts = [part for part in (existing, addition) if part]
    return "\n".join(parts) if parts else None


__all__ = ["router"]
