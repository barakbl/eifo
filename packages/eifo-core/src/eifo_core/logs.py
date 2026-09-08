"""Where the two programs' output goes.

Both of them log the same way and for the same reasons, so the setup is here
rather than twice: a console handler for whoever is watching, and - when a log
directory is configured - a rotating file beside it for whoever is not.

**The file is not a nicety.** The menu-bar companion starts both the API and the
fetcher itself, and it cannot show a console that does not exist: a process it
spawned writes to a pipe nobody is reading. Until there was a file, the only
record of a nightly run was the `fetch_runs` row it managed to write, which by
definition excludes every failure that stopped it writing one.

Rotation is by size and keeps a handful of files, so a log left running for a
year is bounded without anybody remembering to prune it. It is deliberately not
clever about several processes sharing one file - each program writes its own,
which is what makes that a non-question for the ordinary arrangement of one API
and one fetcher.
"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

#: The one format both programs use, on the console and in the file.
#:
#: Matched by ``eifo_fetcher.runs``' own capture, so a line read from a run row
#: and the same line read from the file look alike - they are the same line.
LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s %(message)s"

#: Bytes per file before it rolls, and how many rolled files to keep.
#:
#: Five megabytes is around fifty thousand lines, which is a long sync with
#: room to spare; six files is a week of nightly runs on a busy catalog. The
#: point of the cap is that nobody has to remember this exists.
MAX_LOG_BYTES = 5_000_000
LOG_BACKUPS = 5

#: Loggers that are noisy in a way that is also unsafe.
#:
#: httpx logs whole request URLs, and TMDB takes its key as a query parameter -
#: so at INFO the key would be written into every log file and into anything
#: those files get pasted into. It matters more now that there are files.
_QUIETENED = ("httpx",)

logger = logging.getLogger("eifo.logs")


def configure_console(level: int | str = logging.INFO) -> None:
    """Set up logging for a program that has just started.

    Called before configuration is read, because the failure to read
    configuration is itself something worth logging.
    """
    logging.basicConfig(level=level, format=LOG_FORMAT)
    for name in _QUIETENED:
        logging.getLogger(name).setLevel(logging.WARNING)


def add_file(
    log_dir: Path | str | None,
    program: str,
    level: int | str = logging.INFO,
) -> Path | None:
    """Also write everything to ``<log_dir>/<program>.log``, if one is configured.

    Returns where it landed, or None when no directory is set or the directory
    cannot be written to. A log file that cannot be opened is reported and
    otherwise ignored: it is a place to look afterwards, and refusing to sync a
    catalog because of one would be a poor trade.

    Idempotent by path, so a program that configures itself twice - the API
    under a reloader, a test that calls this more than once - ends up with one
    handler rather than two copies of every line.
    """
    if log_dir is None:
        return None

    wanted = logging.getLevelNamesMapping()[level.upper()] if isinstance(level, str) else level
    destination = Path(log_dir) / f"{program}.log"
    root = logging.getLogger()
    if any(_writes_to(handler, destination) for handler in root.handlers):
        return destination

    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            destination,
            maxBytes=MAX_LOG_BYTES,
            backupCount=LOG_BACKUPS,
            encoding="utf-8",
        )
    except OSError as exc:
        logger.warning("could not open %s for logging: %s", destination, exc)
        return None

    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    handler.setLevel(wanted)
    root.addHandler(handler)
    # Raised only when the root would otherwise drop what the file is meant to
    # keep - a records-nothing root makes a records-nothing file. A caller that
    # has deliberately asked for more than this keeps it.
    if root.level > wanted:
        root.setLevel(wanted)

    logger.info("also logging to %s", destination)
    return destination


def attach_to(source: logging.Logger, *names: str) -> None:
    """Give these loggers the file handlers ``source`` has.

    For loggers that do not propagate. Uvicorn's are the reason this exists: it
    configures ``uvicorn.access`` with its own handler and ``propagate=False``,
    so the access log - which is the record of every request and the status it
    got, and so the single most useful thing in a server's log - is precisely
    what a root handler never sees.

    Only the file handlers are copied. Console output stays exactly as uvicorn
    arranged it, and a second console handler would print every line twice.

    A logger that already reaches the handler through an ancestor is skipped.
    Uvicorn's three are not three separate cases - ``uvicorn.error`` has no
    handlers of its own and propagates to ``uvicorn`` - so naming all three and
    attaching to all three writes every startup line into the file twice.
    Working it out here rather than asking the caller to know uvicorn's tree:
    the caller would be guessing, and would guess again when it changed.
    """
    files = [handler for handler in source.handlers if hasattr(handler, "baseFilename")]
    for name in names:
        target = logging.getLogger(name)
        for handler in files:
            if handler not in target.handlers and not _reached_through_a_parent(target, handler):
                target.addHandler(handler)


def _reached_through_a_parent(target: logging.Logger, handler: logging.Handler) -> bool:
    """Whether a record logged here would already find this handler on its way up."""
    parent = target.parent
    while target.propagate and parent is not None:
        if handler in parent.handlers:
            return True
        target, parent = parent, parent.parent
    return False


def _writes_to(handler: logging.Handler, destination: Path) -> bool:
    stream = getattr(handler, "baseFilename", None)
    return stream is not None and Path(stream) == destination.resolve()
