"""``/api/v1/ingest/enrich`` - ratings and metadata, written from somewhere else.

The same shape as :mod:`~eifo_api.routers.syncing`, and deliberately so. An
enricher is a pure reader: it is handed a snapshot of a title, looks it up on
somebody's website, and returns what it found. Everything about what that means
for the catalog - which score may overwrite which, whether a name is in the
wrong script, when the title next falls due - happens here, because it is a
question about the catalog.

**Which titles are due is asked, not decided.** The queue is a property of the
catalog: least recently attempted first, with a backoff that lengthens for
titles nobody can rate. A fetcher cannot know any of that, and should not have
to - it asks what to look up and reports what it found.

**A finding is per title and per enricher.** One provider being down costs that
provider's answer for that title; the rest of the batch is written, and the
title is still recorded as attempted so the queue keeps moving. A run where
every provider fails on every title would otherwise be a run that changes
nothing and comes back to exactly the same titles tomorrow.
"""

from __future__ import annotations

import logging
from base64 import b64decode
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import func, literal, select, tuple_
from sqlalchemy.orm import Session

from eifo_api.deps import AdminDep, CsrfDep, SessionDep, SettingsDep
from eifo_api.schemas import (
    EnrichBegun,
    EnrichChunkOut,
    EnrichFinish,
    EnrichOutcomeOut,
    EnrichStart,
    ImdbRatings,
    ProviderDeclaration,
    ProvidersOut,
    RescoreOut,
    SeretEntryOut,
    SeretIndexOut,
    SeretPage,
    SeretPages,
    SeretStatus,
    TitleDue,
    WantedImdb,
)
from eifo_core import ingest as wire
from eifo_core.enriching import (
    RejectionSink,
    apply_offer_facts,
    apply_patch,
    mislabelled_names,
    outcome_of,
    recompute,
    recompute_all_aggregates,
    record_attempt,
    store_ratings,
    titles_due,
    view_of,
)
from eifo_core.enums import FetchPhase, FetchStatus, RatingProvider, TitleKind
from eifo_core.findings import Rating
from eifo_core.models import FetchRun, SeretTitle, Title
from eifo_core.providers import DeclaredProvider, register_declared_providers
from eifo_core.seret import SeretEntry, index_status, wake_titles_newly_covered
from eifo_core.settings import Settings
from eifo_core.types import utcnow

logger = logging.getLogger("eifo.api.ingest.enrich")

router = APIRouter(prefix="/ingest/enrich", tags=["ingest"])


@router.get("/due", response_model=list[TitleDue], summary="Titles worth looking up now")
def due(
    _admin: AdminDep,
    session: SessionDep,
    settings: SettingsDep,
    limit: Annotated[int, Query(ge=1, le=wire.MAX_DUE_PAGE)] = wire.DUE_PAGE_SIZE,
    force: Annotated[bool, Query()] = False,
) -> list[TitleDue]:
    """What to enrich next, oldest attempt first.

    One answer, not a page of one. The queue does not move until something
    reports on what it handed out, so a second ask returns the same head - and a
    caller that treated this as pageable would enrich the first hundred titles
    over and over and never reach the rest.

    ``force`` ignores the schedule and walks the catalog from the start, which
    is how a change to what an enricher extracts gets applied to titles that
    are not otherwise due for weeks.
    """
    rows = titles_due(session, settings, force=force, limit=limit)
    return [TitleDue(**wire.view_to_wire(view_of(title))) for title in rows]


@router.get(
    "/mislabelled", response_model=list[TitleDue], summary="English names in the wrong script"
)
def mislabelled(
    _admin: AdminDep,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> list[TitleDue]:
    """Titles whose English name is not in Latin script.

    A repair, not a schedule: these are titles a source filed a Hebrew name
    under ``name_en``, and the fix is to ask TMDB about them again. Which ones
    is a question about the catalog, so it is asked here.
    """
    rows = mislabelled_names(session, limit=limit)
    return [TitleDue(**wire.view_to_wire(view_of(title))) for title in rows]


@router.post("/providers", response_model=ProvidersOut, summary="Declare what credits a score")
def providers(
    _admin: AdminDep,
    _csrf: CsrfDep,
    session: SessionDep,
    settings: SettingsDep,
    body: ProviderDeclaration,
) -> ProvidersOut:
    """Record what each rating provider calls itself, and publish its mark.

    From everything installed, not from tonight's selection: a provider skipped
    for one run still has thousands of scores in the catalog, and they still
    have to be credited on the page.

    The mark arrives as bytes because the file ships with the plugin, and the
    plugin is not necessarily on this machine. Where it lands is decided here,
    content-addressed, beside the artwork that lands the same way.
    """
    declared = [
        DeclaredProvider(
            provider=entry.provider,
            label=entry.label,
            group_key=entry.group_key,
            group_name=entry.group_name,
            website_url=entry.website_url,
            position=entry.position,
            logo=b64decode(entry.logo) if entry.logo else None,
            logo_suffix=entry.logo_suffix,
        )
        for entry in body.providers
    ]
    changed = register_declared_providers(session, declared, images_dir=Path(settings.images_dir))
    session.commit()
    return ProvidersOut(changed=changed)


@router.get("/seret/index", response_model=list[SeretEntryOut], summary="The stored Seret index")
def seret_index(
    _admin: AdminDep,
    session: SessionDep,
    after_kind: Annotated[TitleKind | None, Query()] = None,
    after_id: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=5000)] = 2000,
    include_unreadable: Annotated[bool, Query()] = False,
) -> list[SeretEntryOut]:
    """Every usable row of the index, a page at a time.

    Rows with no name are left out: an id whose page carried no title node
    cannot be matched against anything, and keeping it would only make the
    lookup the fetcher builds from this larger.

    The cursor is the whole key, both halves of it. Seret numbers films and
    series separately, so the same id names two different pages and one of them
    would fall down the crack between two pages of this - silently, and only
    for the ids that happen to land on a boundary.
    """
    key = (SeretTitle.kind, SeretTitle.seret_id)
    query = select(SeretTitle).order_by(*key)
    if not include_unreadable:
        # An id whose page carried no title node cannot be matched against
        # anything, so the lookup leaves it out - but the crawl still wants it,
        # or it would pay for that page again on every run.
        query = query.where(SeretTitle.unreadable.is_(False))
    if after_kind is not None:
        query = query.where(tuple_(*key) > tuple_(literal(after_kind), literal(after_id)))
    return [
        SeretEntryOut(
            kind=row.kind,
            seret_id=row.seret_id,
            name_he=row.name_he,
            name_en=row.name_en,
            year=row.year,
            imdb_id=row.imdb_id,
            viewers_score=row.viewers_score,
            viewers_votes=row.viewers_votes,
            critics_score=row.critics_score,
            url=row.url,
            indexed_at=row.indexed_at,
            unreadable=row.unreadable,
        )
        for row in session.scalars(query.limit(limit)).all()
    ]


@router.post("/seret/index", response_model=SeretIndexOut, summary="Store crawled Seret pages")
def store_seret(
    _admin: AdminDep,
    _csrf: CsrfDep,
    session: SessionDep,
    body: SeretPages,
) -> SeretIndexOut:
    """Write what a crawl read, including the pages that had nothing on them.

    A page that carried no title node still gets a row, marked unreadable:
    without one the crawl would pay for that id again on every single run, and
    there are enough withdrawn ids for that to matter.

    Which pages are newly scorable is worked out here rather than sent, and it
    has to be: the answer is "could the row we are about to overwrite score
    anything", and only this side has ever seen that row. A crawl re-reading a
    page it read last month must not wake anything on the strength of scores
    that were already there.
    """
    created = updated = 0
    newly_scorable: list[SeretEntry] = []
    now = utcnow()

    for page in body.pages:
        row = session.get(SeretTitle, {"kind": page.kind, "seret_id": page.seret_id})
        if row is None:
            row = SeretTitle(kind=page.kind, seret_id=page.seret_id)
            session.add(row)
            created += 1
            had_scores = False
        else:
            updated += 1
            had_scores = row.viewers_score is not None or row.critics_score is not None

        gains_scores = page.viewers_score is not None or page.critics_score is not None
        if gains_scores and not had_scores and not page.unreadable:
            newly_scorable.append(_entry(page))

        row.indexed_at = now
        row.unreadable = page.unreadable
        row.name_he = page.name_he
        row.name_en = page.name_en
        row.year = page.year
        row.imdb_id = page.imdb_id
        row.viewers_score = page.viewers_score
        row.viewers_votes = page.viewers_votes
        row.critics_score = page.critics_score
        row.url = page.url
    session.flush()

    # A title that had no Seret page and now has one is due again: it was put
    # to sleep having found nothing, and the thing that was missing has just
    # arrived.
    woken = wake_titles_newly_covered(session, newly_scorable)
    session.commit()
    return SeretIndexOut(
        created=created,
        updated=updated,
        newly_scorable=len(newly_scorable),
        woken=woken,
    )


def _entry(page: SeretPage) -> SeretEntry:
    """One crawled page as the matcher wants it.

    Converted rather than passed through: the schema is a wire shape with
    validation on it, and what resolves a title to a page is a core type with
    the matching rules attached. Keeping them apart is what stops a change to
    the request body quietly changing how a score finds its film.
    """
    return SeretEntry(
        kind=page.kind,
        seret_id=page.seret_id,
        name_he=page.name_he,
        name_en=page.name_en,
        year=page.year,
        imdb_id=page.imdb_id,
        viewers_score=page.viewers_score,
        viewers_votes=page.viewers_votes,
        critics_score=page.critics_score,
        url=page.url,
    )


@router.get("/seret/status", response_model=SeretStatus, summary="What the Seret index holds")
def seret_status(_admin: AdminDep, session: SessionDep, settings: SettingsDep) -> SeretStatus:
    """Counts for ``eifo-fetch seret status``, and the guard the crawl reads.

    ``catalog_titles`` is here because the decision it feeds cannot be made on
    the fetcher's side: whether tonight's enrich should crawl part of somebody
    else's sitemap depends on there being titles for the scores to attach to,
    and a fetcher with no database cannot count them.
    """
    del settings  # symmetry with the other handlers; nothing here is configured
    return SeretStatus(
        **index_status(session),
        catalog_titles=session.scalar(select(func.count()).select_from(Title)) or 0,
    )


@router.get("/imdb/wanted", response_model=list[WantedImdb], summary="Titles carrying an IMDb id")
def imdb_wanted(
    _admin: AdminDep,
    session: SessionDep,
    after: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=5000)] = 2000,
) -> list[WantedImdb]:
    """Which titles the bulk pass should look for, a page at a time.

    The dataset has over a million rows and the catalog has tens of thousands
    of titles, so the join has to happen where the small side is - on the
    fetcher, which is the one holding the download. It asks for the ids, keeps
    the rows that match, and sends back only those.
    """
    rows = session.execute(
        select(Title.id, Title.imdb_id)
        .where(Title.imdb_id.is_not(None), Title.id > after)
        .order_by(Title.id)
        .limit(limit)
    ).all()
    return [WantedImdb(title_id=title_id, imdb_id=imdb_id) for title_id, imdb_id in rows]


@router.post("/imdb/ratings", response_model=EnrichChunkOut, summary="Store bulk IMDb ratings")
def imdb_ratings(
    _admin: AdminDep,
    _csrf: CsrfDep,
    session: SessionDep,
    body: ImdbRatings,
) -> EnrichChunkOut:
    """Write a chunk of the dataset's scores.

    Not through the per-title path: this writes the rating and nothing else,
    and the aggregates are rebuilt in one pass afterwards rather than once per
    title. That is why the bulk pass exists at all.
    """
    written = 0
    rejected: list[dict[str, Any]] = []
    for index, entry in enumerate(body.ratings):
        title = session.get(Title, entry.title_id)
        if title is None:
            rejected.append({"index": index, "reason": f"no such title: {entry.title_id}"})
            continue
        written += store_ratings(
            session,
            title,
            [
                Rating(
                    provider=RatingProvider.IMDB,
                    score_raw=entry.score_raw,
                    vote_count=entry.vote_count,
                    url=entry.url,
                )
            ],
            _reject(rejected, index),
        )
    session.commit()
    return EnrichChunkOut(titles_seen=len(body.ratings), ratings_written=written, rejected=rejected)


@router.post("/rescore", response_model=RescoreOut, summary="Recompute every aggregate")
def rescore(
    _admin: AdminDep, _csrf: CsrfDep, session: SessionDep, settings: SettingsDep
) -> RescoreOut:
    """Rebuild every title's weighted score from the ratings already stored.

    Nothing crosses the wire for this one: the inputs are all in the catalog
    and so is the arithmetic. The fetcher asks for it because it is the thing
    that knows a bulk pass just rewrote thousands of ratings underneath.
    """
    computed = recompute_all_aggregates(session, settings)
    session.commit()
    logger.info("rescored %d title(s)", computed)
    return RescoreOut(aggregates_computed=computed)


@router.post("/runs", response_model=EnrichBegun, status_code=201, summary="Begin an enrich run")
def begin(
    _admin: AdminDep,
    _csrf: CsrfDep,
    session: SessionDep,
    body: EnrichStart,
) -> EnrichBegun:
    """Open the run before any title is looked up."""
    run = FetchRun(
        phase=FetchPhase.ENRICH,
        status=FetchStatus.RUNNING,
        started_at=body.started_at or utcnow(),
        stats={},
    )
    session.add(run)
    session.commit()
    logger.info("enrich run %d begun", run.id)
    return EnrichBegun(run_id=run.id, started_at=run.started_at)


@router.post(
    "/runs/{run_id}/findings",
    response_model=EnrichChunkOut,
    summary="Report what the enrichers found",
)
async def take_findings(
    run_id: int,
    request: Request,
    _admin: AdminDep,
    _csrf: CsrfDep,
    session: SessionDep,
    settings: SettingsDep,
) -> EnrichChunkOut:
    """Store a batch of findings and mark those titles attempted.

    The body is a JSON array, one entry per title: the title's id, what each
    enricher returned for it, and which of them failed. A title appears once
    however many enrichers looked at it, because "has this title been tried"
    is one fact and the queue is keyed on it.

    Async only long enough to read the body, for the same reason as the sync
    side: storing a chunk is a rating write, a patch, a rescore and a flush for
    every title in it, and on the event loop that serves nothing else while it
    runs. The rest of this router is plain ``def``, which FastAPI already hands
    to a worker thread; this one was async for the body and took the CPU work
    onto the loop with it.
    """
    payload = await _body(request)
    return await run_in_threadpool(_store_findings, session, settings, run_id, payload)


def _store_findings(
    session: Session, settings: Settings, run_id: int, payload: list[Any]
) -> EnrichChunkOut:
    """The chunk itself. Runs in a worker thread; see :func:`take_findings`."""
    run = _running(session, run_id)

    tally = _Tally.of(run)
    rejected: list[dict[str, Any]] = []

    for index, entry in enumerate(payload):
        title_id = entry.get("title_id") if isinstance(entry, dict) else None
        title = session.get(Title, title_id) if isinstance(title_id, int) else None
        if title is None:
            rejected.append({"index": index, "reason": f"no such title: {title_id!r}"})
            continue

        tally.titles_seen += 1
        written = 0
        errored = bool(entry.get("errored"))

        for finding in entry.get("findings") or []:
            source = str(finding.get("source") or "unknown")
            try:
                result = wire.finding_from_wire(finding.get("result"))
            except wire.WireError as exc:
                rejected.append({"index": index, "reason": f"{source}: {exc}"})
                errored = True
                continue

            written += store_ratings(session, title, result.ratings, _reject(rejected, index))
            if apply_patch(session, title, result, source=source):
                tally.metadata_updated += 1
            # What a service charges and where its page is, attached to offers
            # the harvester already found. Counted with the metadata because it
            # is the same kind of thing: a fact about the title filled in by
            # somebody who knows it better than whoever first reported it.
            tally.metadata_updated += apply_offer_facts(session, title, result)

        tally.ratings_written += written
        if recompute(session, title, settings):
            tally.aggregates_computed += 1
        # Before the flush, so a title nothing could rate still says so: the
        # queue has to learn from the attempts that found nothing, or it spends
        # every run on the same titles.
        record_attempt(
            session, title, settings, outcome=outcome_of(title, written=written, errored=errored)
        )
        session.flush()

    tally.onto(run)
    session.commit()

    logger.info(
        "enrich chunk: %d title(s), %d rating(s), %d patched%s",
        len(payload),
        tally.ratings_written,
        tally.metadata_updated,
        f", {len(rejected)} rejected" if rejected else "",
    )
    return EnrichChunkOut(
        titles_seen=tally.titles_seen,
        ratings_written=tally.ratings_written,
        rejected=rejected,
    )


@router.post(
    "/runs/{run_id}/finish", response_model=EnrichOutcomeOut, summary="Finish an enrich run"
)
def finish(
    run_id: int,
    _admin: AdminDep,
    _csrf: CsrfDep,
    session: SessionDep,
    body: EnrichFinish,
) -> EnrichOutcomeOut:
    """Close the run with what it managed."""
    run = _running(session, run_id)
    tally = _Tally.of(run)

    run.status = body.status
    run.finished_at = utcnow()
    # The sender's stats first, so anything this side counted wins the clash:
    # what was written is this side's fact, and a fetcher must not be able to
    # report a different number for it.
    run.stats = {
        **dict(body.stats),
        **(run.stats or {}),
        "errors": list(body.errors),
        "error_count": len(body.errors),
    }
    run.log = "\n".join(part for part in (run.log, body.log) if part) or None
    session.commit()

    return EnrichOutcomeOut(
        status=body.status,
        titles_seen=tally.titles_seen,
        ratings_written=tally.ratings_written,
        metadata_updated=tally.metadata_updated,
        aggregates_computed=tally.aggregates_computed,
        errors=list(body.errors),
    )


@dataclass(slots=True)
class _Tally:
    """A run's totals, carried on the row between one chunk and the next.

    On the row rather than in memory, for the same reason the sync side does
    it: the API keeps nothing between requests, and a total held in a worker's
    process would be lost by a restart and wrong the moment there were two.
    """

    titles_seen: int = 0
    ratings_written: int = 0
    metadata_updated: int = 0
    aggregates_computed: int = 0

    @classmethod
    def of(cls, run: FetchRun) -> _Tally:
        stats = run.stats or {}
        return cls(
            titles_seen=int(stats.get("titles_seen", 0)),
            ratings_written=int(stats.get("ratings_written", 0)),
            metadata_updated=int(stats.get("metadata_updated", 0)),
            aggregates_computed=int(stats.get("aggregates_computed", 0)),
        )

    def onto(self, run: FetchRun) -> None:
        run.stats = {
            **(run.stats or {}),
            "titles_seen": self.titles_seen,
            "ratings_written": self.ratings_written,
            "metadata_updated": self.metadata_updated,
            "aggregates_computed": self.aggregates_computed,
        }


def _reject(into: list[dict[str, Any]], index: int) -> RejectionSink:
    """A sink that files a rejected score against the title it came with.

    Built per title rather than closed over the loop variable: the sink
    outlives the iteration that made it, and a closure would give every
    rejection the last index in the batch.
    """

    def sink(message: str, exc: Exception) -> None:
        into.append({"index": index, "reason": f"{message}: {exc}"})

    return sink


async def _body(request: Request) -> list[Any]:
    payload = await request.json()
    if not isinstance(payload, list):
        raise HTTPException(status_code=400, detail="Expected a JSON array of findings.")
    if len(payload) > wire.MAX_ENRICH_CHUNK:
        raise HTTPException(
            status_code=413,
            detail=f"A batch may hold at most {wire.MAX_ENRICH_CHUNK} titles.",
        )
    return payload


def _running(session: Session, run_id: int) -> FetchRun:
    run = session.get(FetchRun, run_id)
    if run is None or run.phase is not FetchPhase.ENRICH:
        raise HTTPException(status_code=404, detail="No such enrich run.")
    if run.status is not FetchStatus.RUNNING:
        raise HTTPException(
            status_code=409, detail=f"That run is already {run.status.value}; begin another."
        )
    return run


__all__ = ["router"]
