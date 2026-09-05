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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from eifo_core import ingest as wire
from eifo_core.enums import FetchPhase, FetchStatus
from eifo_core.settings import Settings

logger = logging.getLogger("eifo.fetch.ingest")

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

    def open_run(self, phase: FetchPhase, *, started_at: dt.datetime) -> int:
        """Record that a phase has started, and get the row's id.

        Opened at the start rather than written at the end, exactly as it was
        when the fetcher wrote the row itself: a run that never comes back is
        the one most worth having a record of, and it cannot write its own
        obituary.
        """
        payload = self._json(
            "POST",
            "/api/v1/ingest/runs",
            json={"phase": phase.value, "started_at": started_at.isoformat()},
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
        headers = {"Authorization": f"Bearer {self._token}", **kwargs.pop("headers", {})}
        try:
            response = self._http.request(method, f"{self._base}{path}", headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise IngestError(f"{self._base} could not be reached: {exc}") from exc

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
