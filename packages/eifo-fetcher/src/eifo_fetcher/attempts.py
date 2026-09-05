"""What happened when the fetcher could not tell anybody what happened.

A run that writes its own history has one blind spot, and moving that history
behind the API made it wider. The record of a run lives in the catalog; the
catalog is reached over HTTP; so every failure that consists of *not reaching
the catalog* - no token, a revoked token, a server that is down, a laptop with
no network at 03:00 - is precisely a failure that cannot be recorded.

What the operator sees in that case is nothing at all, which is exactly the
state ``fetch_runs`` was invented to abolish: a night when the fetcher died
looked identical to a night when nothing was scheduled.

So a run that could not reach the server writes what happened here, on the
fetcher's own disk, and the next run that *can* reach it posts that as the
failed run it was. The record arrives late, which is the best that can be done
from a machine that could not speak at the time - and late is a great deal
better than never, because "there was an attempt at 03:00 and it could not
reach me" is the sentence the operator needs.

Only on failure, never at the start. A note written on the way in would be
overwritten by the next run before that run had read it, which is how the first
version of this reported a phantom: the right number of rows, with the wrong
timestamp and no reason.

That leaves one case to somebody else, and deliberately. A fetcher killed
outright - power cut, closed lid - writes nothing here, because it had no
chance to. It has already opened its row on the server, though, and that row
stays RUNNING; the server closes it once it is old enough to be certainly
dead. Between the two, a run that vanishes leaves a trace either way.

One attempt is kept, not a queue. Five nights of the same broken token are five
copies of one fact, and the useful one is the first: when it started going
wrong.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from eifo_core.enums import FetchPhase
from eifo_core.settings import Settings
from eifo_core.types import utcnow
from eifo_fetcher.ingest import IngestError
from eifo_fetcher.lock import lock_path

logger = logging.getLogger("eifo.fetch.attempts")

#: Beside the lock file, for the same reason the lock is where it is: it
#: belongs to this machine's fetcher rather than to the catalog.
ATTEMPT_FILENAME = ".eifo-attempt.json"


@dataclass(frozen=True, slots=True)
class Attempt:
    """A run that started and could not say how it ended."""

    phase: FetchPhase
    started_at: dt.datetime
    reason: str

    def as_stats(self) -> dict[str, list[str]]:
        return {"errors": [f"could not reach the API: {self.reason}"]}


def attempt_path(settings: Settings) -> Path:
    return lock_path(settings).with_name(ATTEMPT_FILENAME)


def failed(settings: Settings, phase: FetchPhase, started_at: dt.datetime, reason: str) -> None:
    """Keep the attempt, with why it could not be reported."""
    _write(
        settings,
        {"phase": phase.value, "started_at": started_at.isoformat(), "reason": reason},
    )


def clear(settings: Settings) -> None:
    """Forget the attempt: it was reported, so there is nothing to carry."""
    attempt_path(settings).unlink(missing_ok=True)


def pending(settings: Settings) -> Attempt | None:
    """The unreported attempt, if the last run left one.

    A file that cannot be read is treated as no attempt rather than as an
    error: this is a note to self about bookkeeping, and refusing to fetch
    artwork because it is malformed would let the mechanism meant to report
    failures become one.
    """
    path = attempt_path(settings)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return Attempt(
            phase=FetchPhase(payload["phase"]),
            started_at=dt.datetime.fromisoformat(payload["started_at"]),
            reason=payload.get("reason") or "the run did not say",
        )
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError) as exc:
        logger.warning("could not read %s, ignoring it: %r", path, exc)
        path.unlink(missing_ok=True)
        return None


def _write(settings: Settings, payload: dict[str, str]) -> None:
    path = attempt_path(settings)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
    except OSError as exc:
        # A disk that will not take this note is a real problem, but not one
        # worth failing a run over: the run itself is the useful work.
        logger.warning("could not record the attempt at %s: %r", path, exc)


@contextmanager
def attempted(settings: Settings, phase: FetchPhase) -> Iterator[None]:
    """Note the attempt for the whole of it, keeping it if it cannot report.

    Wraps the client's construction as well as the run, because the commonest
    failure of all - no token configured - happens before there is a client to
    fail with, and would otherwise be the one failure that left no note.

    Nothing is written on the way in. A note written then would be clobbered by
    the following run before it had been read, and the following run would
    report its own start as the previous run's failure.
    """
    started_at = utcnow()
    try:
        yield
    except IngestError as exc:
        failed(settings, phase, started_at, str(exc))
        raise
