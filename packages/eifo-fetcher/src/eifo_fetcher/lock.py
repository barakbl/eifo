"""One fetcher at a time.

Nothing stopped a second fetcher starting while the first was mid-run: the
daemon's scheduled job, a hand-run ``eifo-fetch all``, and a leftover cron entry
are three separate processes with no idea about each other. SQLite arbitrates
writes and will not corrupt anything, but that is not the same as it being
fine - the loser spends the night waiting on a busy timeout and then failing,
and both processes ask every source for the same catalog at the same time,
which is exactly the behaviour a scraper should not exhibit.

The lock is an advisory lock on a file beside the database rather than a pid
file, because the operating system releases it when the holder dies. A fetcher
killed mid-run leaves nothing to clean up and nothing to explain: the next one
simply takes the lock.

Both platforms have such a thing and neither calls it the same: ``flock`` on
POSIX, ``msvcrt.locking`` on Windows. The property that matters - released by
the OS when the process goes, whether or not it went politely - is the same in
both, so only the one call differs.
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from eifo_core.db import sqlite_path
from eifo_core.settings import Settings

logger = logging.getLogger("eifo.fetch.lock")

LOCK_FILENAME = ".eifo-fetch.lock"


if sys.platform == "win32":  # pragma: no cover - exercised on Windows only
    import msvcrt

    def _take(handle: int) -> None:
        """Lock the first byte, or raise OSError if somebody already has it."""
        msvcrt.locking(handle, msvcrt.LK_NBLCK, 1)

    def _release(handle: int) -> None:
        os.lseek(handle, 0, os.SEEK_SET)
        msvcrt.locking(handle, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _take(handle: int) -> None:
        """Lock the file, or raise OSError if somebody already has it."""
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _release(handle: int) -> None:
        """Nothing to do: closing the descriptor drops the flock."""


class AlreadyRunningError(RuntimeError):
    """Another fetcher holds the lock; this one has nothing to do."""


def lock_path(settings: Settings) -> Path:
    """Where the lock file lives: beside the database it guards writes to.

    Falls back to the images directory for a database that is not a local file,
    which only arises in tests and in deployments this project does not claim
    to support.
    """
    database = sqlite_path(settings.db_url)
    directory = database.parent if database is not None else Path(settings.images_dir)
    return directory / LOCK_FILENAME


@contextmanager
def single_flight(settings: Settings) -> Iterator[None]:
    """Hold the fetcher lock for the duration of the block.

    Raises:
        AlreadyRunningError: another fetcher is running. That is a normal state
            of affairs - a nightly run that overran into the next trigger, or an
            impatient operator - so callers report it and stop rather than
            treating it as a failure.
    """
    path = lock_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)

    try:
        try:
            # Windows locks a byte range from wherever the file pointer is, so
            # both platforms are given the same answer to "which byte".
            os.lseek(handle, 0, os.SEEK_SET)
            _take(handle)
        except OSError as exc:
            raise AlreadyRunningError(
                f"another fetcher is already running ({_holder(path)}); leaving it to it"
            ) from exc

        try:
            os.ftruncate(handle, 0)
            os.lseek(handle, 0, os.SEEK_SET)
            os.write(handle, f"pid {os.getpid()}\n".encode())
            logger.debug("holding %s", path)
            yield
        finally:
            _release(handle)
    finally:
        # The file stays: unlinking it would race with whoever opens it next.
        os.close(handle)


def _holder(path: Path) -> str:
    """Whatever the lock file says about its holder, for the error message."""
    try:
        return path.read_text(encoding="utf-8").strip() or "unknown pid"
    except OSError:
        return "unknown pid"
