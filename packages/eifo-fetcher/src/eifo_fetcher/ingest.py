"""Talking to the API instead of to the database.

The fetcher used to open ``eifo.db`` and the images directory and write to
both. This is the replacement: it asks the API what needs doing, sends back
what it did, and never touches either. What that buys is a fetcher that does
not have to live on the same machine as the catalog - one running on a laptop,
on a domestic connection, filling a catalog on a server somewhere else.

It is deliberately thin. There is no caching, no queue and no retry logic of
its own: :class:`~eifo_fetcher.http.HttpClient` already retries the statuses
worth retrying, and anything this cannot deliver is work the next run picks up,
because the server's idea of what is outstanding is derived from the catalog
rather than from anything this remembers.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from eifo_core import ingest as wire
from eifo_core.enums import FetchPhase, FetchStatus, TitleKind
from eifo_core.findings import TitleView
from eifo_core.items import SourceInfo
from eifo_core.seret import SeretEntry, SeretLookup
from eifo_core.settings import Settings

logger = logging.getLogger("eifo.fetch.ingest")


def _readable(path: str, params: Any) -> str:
    """The path with its query, for a log line somebody has to scan quickly.

    The parameters are what tell two otherwise identical calls apart - which
    page of the Seret index, how far through the artwork queue - so a line
    without them says a request happened and nothing about which.
    """
    if not isinstance(params, dict) or not params:
        return path
    query = "&".join(f"{key}={value}" for key, value in params.items())
    return f"{path}?{query}"


def _elapsed(began: float) -> str:
    """How long it took, at a precision worth reading.

    Milliseconds below a second, because most of these are a few tens of them
    and "0.0s" says nothing; seconds above, because an upload of a hundred
    posters is measured in them and three decimal places would be noise.
    """
    seconds = time.monotonic() - began
    return f"{seconds * 1000:.0f}ms" if seconds < 1 else f"{seconds:.1f}s"


#: How long to wait on an upload. Generous next to the rest of the fetcher's
#: requests, because this one carries a batch of images and the far end decodes
#: every one of them before it answers.
UPLOAD_TIMEOUT_SECONDS = 300.0


class IngestError(RuntimeError):
    """The API would not take something, with the reason it gave.

    Carries the server's own words wherever there are any. A refusal from this
    endpoint is nearly always something the operator has to fix - a token that
    has been revoked, a batch that is too big, an image that is not one - and
    replacing what the server said with "upload failed" would throw away the
    only part that says which.
    """


@dataclass(frozen=True, slots=True)
class PendingPoster:
    """One title the API says still needs its artwork."""

    title_id: int
    source_url: str


@dataclass(frozen=True, slots=True)
class StoredSeretPage:
    """One row of the Seret index as the crawl has to see it.

    :class:`~eifo_core.seret.SeretEntry` and two facts it has no business
    carrying. An entry is what a page *says*, and is the same shape whether it
    came from the store or from a page fetched a second ago; when it was last
    read and whether it turned out to have nothing on it are properties of the
    stored row, and only the crawl - which is deciding what to fetch again -
    ever asks about them.
    """

    entry: SeretEntry
    #: When the store last read this page. None where it has never said.
    indexed_at: dt.datetime | None = None
    #: The page answered but carried no title node.
    unreadable: bool = False


class IngestClient:
    """The fetcher's half of ``/api/v1/ingest``."""

    def __init__(self, base_url: str, token: str, *, http: httpx.Client | None = None) -> None:
        self._base = base_url.rstrip("/")
        self._token = token
        # Injectable so tests can drive the real application in-process rather
        # than over a socket, which is the only way this module gets tested
        # against the thing it actually talks to.
        self._http = http or httpx.Client(timeout=UPLOAD_TIMEOUT_SECONDS)
        self._owned = http is None

    @classmethod
    def from_settings(cls, settings: Settings, *, http: httpx.Client | None = None) -> IngestClient:
        """Build one, or say which setting is missing.

        Raises:
            IngestError: when no token is configured. Named rather than
                implied, because the failure otherwise arrives as a 401 from
                a URL the operator never typed.
        """
        token = settings.api_token.get_secret_value().strip() if settings.api_token else ""
        if not token:
            raise IngestError(
                "No API token configured. The fetcher writes through the API now, "
                "so it needs one: run `eifo-fetch token create fetcher` on the machine "
                "with the database and set EIFO_API_TOKEN to what it prints."
            )
        return cls(settings.api_url(), token, http=http)

    def close(self) -> None:
        if self._owned:
            self._http.close()

    def __enter__(self) -> IngestClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ work

    def pending_posters(
        self, *, limit: int, force: bool = False, after: int = 0
    ) -> list[PendingPoster]:
        """Titles whose artwork the catalog is still missing.

        ``after`` walks the catalog by title id. Without it a caller that
        cannot store what it downloaded would be handed the same titles again
        on the very next call, which is a loop rather than a retry.
        """
        payload = self._json(
            "GET",
            "/api/v1/ingest/posters/pending",
            params={"limit": limit, "force": str(force).lower(), "after": after},
        )
        return [
            PendingPoster(title_id=row["title_id"], source_url=row["source_url"]) for row in payload
        ]

    def upload_posters(self, archive: Path) -> dict[str, Any]:
        """Send one batch and return what the server made of it.

        The archive is streamed from disk rather than read into memory: it is
        already on disk because it had to be built there, and a batch is
        megabytes on a machine that may be a Raspberry Pi at the other end.
        """
        with archive.open("rb") as body:
            response = self._request(
                "POST",
                "/api/v1/ingest/posters",
                content=body,
                headers={"Content-Type": wire.ARCHIVE_MEDIA_TYPE},
            )
        result: dict[str, Any] = response.json()
        return result

    # ------------------------------------------------------------------ runs

    def open_run(
        self, phase: FetchPhase, *, started_at: dt.datetime, source_key: str | None = None
    ) -> int:
        """Record that a phase has started, and get the row's id.

        Opened at the start rather than written at the end, exactly as it was
        when the fetcher wrote the row itself: a run that never comes back is
        the one most worth having a record of, and it cannot write its own
        obituary.

        Args:
            source_key: what to file the row under. The bulk passes inside the
                enrich phase - the IMDb join, the Seret crawl - are long jobs
                that fail on their own and so want rows of their own, and a
                name is what tells the three of them apart in the panel.
        """
        payload = self._json(
            "POST",
            "/api/v1/ingest/runs",
            json={
                "phase": phase.value,
                "source_key": source_key,
                "started_at": started_at.isoformat(),
            },
        )
        return int(payload["id"])

    def close_run(
        self,
        run_id: int,
        *,
        status: FetchStatus,
        stats: dict[str, Any],
        log: str | None = None,
    ) -> None:
        """Record how a phase ended."""
        self._json(
            "PATCH",
            f"/api/v1/ingest/runs/{run_id}",
            json={
                "status": status.value,
                "finished_at": dt.datetime.now(dt.UTC).isoformat(),
                "stats": stats,
                "log": log,
            },
        )

    # -------------------------------------------------------------- plumbing

    def _json(self, method: str, path: str, **kwargs: Any) -> Any:
        return self._request(method, path, **kwargs).json()

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Make one call, and say so - the asking and the answer, on one line.

        At INFO, and every call, because this is now the only thing the fetcher
        does: a phase that has gone quiet is either waiting on somebody else's
        website or waiting on the catalog, and until this line existed there was
        no way to tell those apart from the outside. The elapsed time is what
        makes it worth reading - a chunk taking four seconds and a chunk taking
        four minutes are different problems on different machines.

        The token is a header and never printed. The query string is, because
        this endpoint's parameters are limits and cursors; nothing secret is
        ever passed to it, unlike the TMDB client whose whole logger is
        quietened for exactly that reason.
        """
        headers = {"Authorization": f"Bearer {self._token}", **kwargs.pop("headers", {})}
        target = f"{self._base}{path}"
        began = time.monotonic()

        try:
            response = self._http.request(method, target, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            # Logged here as well as raised, because the raise is caught in
            # several places and turned into a tally or a warning - and a run
            # that could not reach the catalog should say which call it was on.
            logger.info(
                "%s %s -> could not be reached after %s (%s)",
                method,
                _readable(path, kwargs.get("params")),
                _elapsed(began),
                exc,
            )
            raise IngestError(f"{self._base} could not be reached: {exc}") from exc

        logger.info(
            "%s %s -> %d %s in %s",
            method,
            _readable(path, kwargs.get("params")),
            response.status_code,
            response.reason_phrase or "",
            _elapsed(began),
        )
        if response.is_success:
            return response
        raise IngestError(self._refusal(response, path))

    def _refusal(self, response: httpx.Response, path: str) -> str:
        """The server's reason, or a useful guess at what it meant.

        404 gets translated. The admin surface answers 404 rather than 403 to
        anybody who is not an administrator - which is right for a stranger
        probing it and unhelpful here, where "not found" would send somebody
        looking for a typo in a URL this module wrote itself.
        """
        detail = ""
        try:
            body = response.json()
            if isinstance(body, dict):
                detail = str(body.get("detail", ""))
        except ValueError:
            detail = response.text[:200]

        if response.status_code == 401:
            return "The API rejected the token. It may have been revoked; issue another."
        if response.status_code == 404 and not detail:
            return (
                f"{self._base}{path} answered 404. That is also what this API says to a "
                "token whose account is not an administrator, which is the likelier cause."
            )
        said = detail or response.reason_phrase
        return f"{self._base}{path} answered {response.status_code}: {said}"

    # ------------------------------------------------------------------ sync

    def register_sources(
        self,
        declared: Mapping[str, SourceInfo],
        *,
        enabled: Iterable[str],
        retire_missing: bool = False,
    ) -> dict[str, Any]:
        """Tell the catalog what this fetcher can do, and hear what is on.

        One exchange rather than two, because they are two halves of one
        question: a plugin is the only thing that knows a service exists, and
        an administrator's switch is the only thing that knows whether to run
        it.
        """
        payload: dict[str, Any] = self._json(
            "POST",
            "/api/v1/ingest/sync/sources",
            json={
                "sources": [
                    {
                        "source_key": info.key,
                        "name": info.name,
                        "kind": info.kind.value,
                        "website_url": info.website_url,
                        "logo_path": info.logo_path,
                        "default_enabled": info.default_enabled,
                    }
                    for info in declared.values()
                ],
                "enabled": list(enabled),
                "retire_missing": retire_missing,
            },
        )
        return payload

    def requested_backfills(self) -> list[str]:
        """Sources an operator has asked for, oldest ask first."""
        rows: list[str] = self._json("GET", "/api/v1/ingest/sync/backfills")
        return rows

    def clear_backfills(self, keys: Iterable[str]) -> None:
        self._request("POST", "/api/v1/ingest/sync/backfills/clear", json={"keys": list(keys)})

    def begin_sync(self, info: SourceInfo, *, started_at: dt.datetime) -> int:
        """Open a run for one source, and get the id to feed it by."""
        payload = self._json(
            "POST",
            "/api/v1/ingest/sync/runs",
            json={
                "source_key": info.key,
                "name": info.name,
                "kind": info.kind.value,
                "website_url": info.website_url,
                "logo_path": info.logo_path,
                "default_enabled": info.default_enabled,
                "started_at": started_at.isoformat(),
            },
        )
        return int(payload["run_id"])

    def offer(self, run_id: int, entries: list[dict[str, Any]]) -> dict[str, Any]:
        """Offer one chunk of listings and hear what became of them."""
        payload: dict[str, Any] = self._json(
            "POST", f"/api/v1/ingest/sync/runs/{run_id}/items", json=entries
        )
        return payload

    def finish_sync(
        self,
        run_id: int,
        *,
        status: FetchStatus,
        errors: list[str],
        log: str | None = None,
    ) -> dict[str, Any]:
        """Say there are no more listings, and hear how the run is judged."""
        payload: dict[str, Any] = self._json(
            "POST",
            f"/api/v1/ingest/sync/runs/{run_id}/finish",
            json={"status": status.value, "errors": errors, "log": log},
        )
        return payload

    # ---------------------------------------------------------------- enrich

    def titles_due(self, *, limit: int, force: bool = False) -> list[TitleView]:
        """What to look up next. The queue is the catalog's business, not ours.

        Asked once for the whole batch. The queue is not pageable - a title is
        due until it has been reported on, so asking again before reporting
        hands back the same titles - and ``limit`` is capped at the far end.
        """
        rows = self._json(
            "GET",
            "/api/v1/ingest/enrich/due",
            params={"limit": min(limit, wire.MAX_DUE_PAGE), "force": str(force).lower()},
        )
        return [wire.view_from_wire(row) for row in rows]

    def offers_missing_price(self, *, source_key: str, limit: int) -> list[TitleView]:
        """Titles a service offers with no price on them yet.

        The worklist a price pass walks instead of the ratings queue. Which
        titles those are is a question about the catalog - which offers are
        current, which of them carry a figure - so it is asked, like everything
        else this client asks rather than works out.
        """
        rows = self._json(
            "GET",
            "/api/v1/ingest/enrich/offers/wanted",
            params={"source": source_key, "limit": min(limit, wire.MAX_DUE_PAGE)},
        )
        return [wire.view_from_wire(row) for row in rows]

    def begin_enrich(self, *, started_at: dt.datetime) -> int:
        payload = self._json(
            "POST", "/api/v1/ingest/enrich/runs", json={"started_at": started_at.isoformat()}
        )
        return int(payload["run_id"])

    def report(self, run_id: int, entries: list[dict[str, Any]]) -> dict[str, Any]:
        """Report what the enrichers found for a batch of titles."""
        payload: dict[str, Any] = self._json(
            "POST", f"/api/v1/ingest/enrich/runs/{run_id}/findings", json=entries
        )
        return payload

    def finish_enrich(
        self,
        run_id: int,
        *,
        status: FetchStatus,
        errors: list[str],
        stats: dict[str, Any] | None = None,
        log: str | None = None,
    ) -> dict[str, Any]:
        """Close the run, adding what only this side knows about it.

        ``stats`` is merged into what the catalog counted rather than replacing
        it: the two sides know different halves - which providers were asked
        here, what was actually stored there - and a run row wants both.
        """
        payload: dict[str, Any] = self._json(
            "POST",
            f"/api/v1/ingest/enrich/runs/{run_id}/finish",
            json={
                "status": status.value,
                "errors": errors,
                "stats": stats or {},
                "log": log,
            },
        )
        return payload

    def mislabelled(self, *, limit: int) -> list[TitleView]:
        """Titles whose English name is not in Latin script.

        A repair rather than a schedule, and asked for rather than worked out:
        which titles a source filed a Hebrew name under ``name_en`` is a
        question about the catalog.
        """
        rows = self._json("GET", "/api/v1/ingest/enrich/mislabelled", params={"limit": limit})
        return [wire.view_from_wire(row) for row in rows]

    def declare_providers(self, providers: list[dict[str, Any]]) -> list[str]:
        """Say what credits each score, and hear which rows that changed.

        The marks go with it, as bytes. The file ships with the plugin and the
        plugin is over here; where it lands is the catalog's business, so it is
        decided there, beside the artwork that lands the same way.
        """
        payload = self._json(
            "POST", "/api/v1/ingest/enrich/providers", json={"providers": providers}
        )
        changed: list[str] = list(payload.get("changed") or [])
        return changed

    def rescore(self) -> int:
        """Rebuild every aggregate, and hear how many were rebuilt.

        Nothing crosses the wire for this one: the inputs are all in the
        catalog and so is the arithmetic. It is asked for because this side is
        the one that knows a bulk pass just rewrote thousands of ratings
        underneath.
        """
        payload = self._json("POST", "/api/v1/ingest/enrich/rescore", json={})
        return int(payload["aggregates_computed"])

    # ------------------------------------------------------------ seret index

    def seret_status(self) -> dict[str, int]:
        """What the stored index holds, and whether the catalog needs it."""
        payload: dict[str, int] = self._json("GET", "/api/v1/ingest/enrich/seret/status")
        return payload

    def seret_index(self, *, include_unreadable: bool = False) -> list[StoredSeretPage]:
        """The whole stored index, however many requests that takes.

        All of it, not a page: the crawl has to know what it has already read
        before it can say what is stale, and the enricher has to resolve any
        title against any page. Neither question can be answered from a slice.
        """
        pages: list[StoredSeretPage] = []
        cursor: tuple[str, int] | None = None
        while True:
            params: dict[str, Any] = {
                "limit": wire.SERET_READ_PAGE,
                "include_unreadable": str(include_unreadable).lower(),
            }
            if cursor is not None:
                params["after_kind"], params["after_id"] = cursor
            rows = self._json("GET", "/api/v1/ingest/enrich/seret/index", params=params)
            if not rows:
                return pages
            pages.extend(_stored_page(row) for row in rows)
            # Both halves of the key: Seret numbers films and series apart, so
            # the id alone does not order the index and paging by it would drop
            # whichever of a colliding pair fell after the boundary.
            cursor = (str(rows[-1]["kind"]), int(rows[-1]["seret_id"]))
            if len(rows) < wire.SERET_READ_PAGE:
                return pages

    def seret_lookup(self) -> SeretLookup:
        """The stored index as the enricher resolves titles through.

        Rows that carried no title node are left out of the request rather than
        filtered here: they cannot be matched against anything, and asking for
        them would be a tenth of the index fetched to be discarded.
        """
        return SeretLookup(page.entry for page in self.seret_index() if page.entry.names())

    def store_seret_pages(self, pages: list[dict[str, Any]]) -> dict[str, Any]:
        """Write one batch of crawled pages, and hear what it changed."""
        payload: dict[str, Any] = self._json(
            "POST", "/api/v1/ingest/enrich/seret/index", json={"pages": pages}
        )
        return payload

    # -------------------------------------------------------------- imdb bulk

    def imdb_wanted(self) -> dict[str, int]:
        """Every ``imdb_id`` the catalog holds, by the title it belongs to.

        The join happens on this side because the small side is here: the
        dataset is over a million rows and the catalog is tens of thousands of
        titles, and this is the machine already holding the download.
        """
        wanted: dict[str, int] = {}
        after = 0
        while True:
            rows = self._json(
                "GET",
                "/api/v1/ingest/enrich/imdb/wanted",
                params={"after": after, "limit": wire.IMDB_READ_PAGE},
            )
            if not rows:
                return wanted
            for row in rows:
                wanted[str(row["imdb_id"])] = int(row["title_id"])
                after = max(after, int(row["title_id"]))
            if len(rows) < wire.IMDB_READ_PAGE:
                return wanted

    def store_imdb_ratings(self, ratings: list[dict[str, Any]]) -> dict[str, Any]:
        """Write one chunk of the dataset's scores."""
        payload: dict[str, Any] = self._json(
            "POST", "/api/v1/ingest/enrich/imdb/ratings", json={"ratings": ratings}
        )
        return payload


def _stored_page(row: Mapping[str, Any]) -> StoredSeretPage:
    """One row of the index, with the two facts only the store knows."""
    return StoredSeretPage(
        entry=SeretEntry(
            kind=TitleKind(row["kind"]),
            seret_id=int(row["seret_id"]),
            name_he=row.get("name_he"),
            name_en=row.get("name_en"),
            year=row.get("year"),
            imdb_id=row.get("imdb_id"),
            viewers_score=row.get("viewers_score"),
            viewers_votes=row.get("viewers_votes"),
            critics_score=row.get("critics_score"),
            url=row.get("url"),
        ),
        indexed_at=_when(row.get("indexed_at")),
        unreadable=bool(row.get("unreadable")),
    )


def _when(value: Any) -> dt.datetime | None:
    """A timestamp the far side sent, or None if it sent nothing usable.

    Unparseable is treated as never read rather than as an error: the only
    thing this decides is whether to fetch a page again, and fetching one page
    too many is a cheaper mistake than a crawl that will not start.
    """
    if not isinstance(value, str):
        return None
    try:
        return dt.datetime.fromisoformat(value)
    except ValueError:
        return None
