"""Where the two programs' output goes.

The file is the point of this module. A console is only useful to somebody
watching one, and the two situations that most need a record are exactly the two
with nobody watching: a nightly run at three in the morning, and a process the
menu-bar companion started, which writes to a pipe nobody is reading.

So the tests are about the file being there, being bounded, and not being a
reason for anything else to fail.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from eifo_core import logs


@pytest.fixture(autouse=True)
def a_clean_root() -> Iterator[None]:
    """Put the root logger back however the test left it.

    This module is the one that deliberately reconfigures process-wide logging,
    so it is the one that has to tidy up: a handler left attached would go on
    writing every later test's output into a temporary directory that has been
    deleted.
    """
    root = logging.getLogger()
    handlers = list(root.handlers)
    level = root.level
    yield
    for handler in list(root.handlers):
        if handler not in handlers:
            root.removeHandler(handler)
            handler.close()
    root.handlers = handlers
    root.setLevel(level)


def lines(path: Path) -> list[str]:
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line]


class TestWritingToAFile:
    def test_nothing_is_written_when_no_directory_is_configured(self) -> None:
        """The default, and right for a terminal: the console is the record."""
        assert logs.add_file(None, "eifo-fetch") is None

    def test_the_file_is_named_after_the_program(self, tmp_path: Path) -> None:
        """One file each, which is what makes two processes sharing a directory
        a non-question rather than an interleaving problem."""
        assert logs.add_file(tmp_path, "eifo-fetch") == tmp_path / "eifo-fetch.log"
        assert logs.add_file(tmp_path, "eifo-api") == tmp_path / "eifo-api.log"

    def test_what_is_logged_lands_in_it(self, tmp_path: Path) -> None:
        logs.add_file(tmp_path, "eifo-fetch")

        logging.getLogger("eifo.fetch.test").info("syncing mako")

        assert any("syncing mako" in line for line in lines(tmp_path / "eifo-fetch.log"))

    def test_the_directory_is_made_if_it_is_not_there(self, tmp_path: Path) -> None:
        """A fresh checkout has no data/logs, and being told to create it by
        hand before the first run would be a poor introduction."""
        destination = logs.add_file(tmp_path / "not" / "yet", "eifo-fetch")

        assert destination is not None and destination.parent.is_dir()

    def test_a_line_carries_when_and_how_bad_and_from_where(self, tmp_path: Path) -> None:
        """The same format the console uses and the run row keeps, so a line
        read in one place and the same line read in another look alike."""
        logs.add_file(tmp_path, "eifo-fetch")

        logging.getLogger("eifo.fetch.test").warning("mako returned nothing")

        [line] = [ln for ln in lines(tmp_path / "eifo-fetch.log") if "mako" in ln]
        assert "WARNING" in line
        assert "eifo.fetch.test" in line
        assert line.startswith("20"), "a timestamp, so two runs can be told apart"


class TestNotBeingAProblem:
    def test_a_directory_that_cannot_be_written_is_not_fatal(self, tmp_path: Path) -> None:
        """It is a place to look afterwards. Refusing to sync a catalog because
        one could not be opened would be the tail wagging the dog."""
        blocked = tmp_path / "logs"
        blocked.write_text("this is a file, so nothing can be created inside it")

        assert logs.add_file(blocked, "eifo-fetch") is None

    def test_configuring_twice_does_not_log_everything_twice(self, tmp_path: Path) -> None:
        """A program that sets itself up more than once - the API under a
        reloader, a test - should end with one handler, not two."""
        logs.add_file(tmp_path, "eifo-fetch")
        logs.add_file(tmp_path, "eifo-fetch")

        logging.getLogger("eifo.fetch.test").info("only once")

        assert len([ln for ln in lines(tmp_path / "eifo-fetch.log") if "only once" in ln]) == 1

    def test_it_rolls_rather_than_growing_for_ever(self, tmp_path: Path) -> None:
        """These files are written by a service that runs for years."""
        logs.add_file(tmp_path, "eifo-fetch")
        handler = next(h for h in logging.getLogger().handlers if hasattr(h, "maxBytes"))

        assert handler.maxBytes == logs.MAX_LOG_BYTES  # type: ignore[attr-defined]
        assert handler.backupCount == logs.LOG_BACKUPS  # type: ignore[attr-defined]

    def test_a_root_that_records_nothing_would_make_a_file_of_nothing(self, tmp_path: Path) -> None:
        """So the level is raised to what the file was asked for, and no further."""
        logging.getLogger().setLevel(logging.ERROR)

        logs.add_file(tmp_path, "eifo-fetch", logging.INFO)
        logging.getLogger("eifo.fetch.test").info("worth keeping")

        assert any("worth keeping" in line for line in lines(tmp_path / "eifo-fetch.log"))

    def test_but_a_deliberate_debug_is_not_quietened(self, tmp_path: Path) -> None:
        """`eifo-fetch -v` asked for more than the file's floor and keeps it."""
        logging.getLogger().setLevel(logging.DEBUG)

        logs.add_file(tmp_path, "eifo-fetch", logging.INFO)

        assert logging.getLogger().level == logging.DEBUG

    def test_the_level_may_be_named_as_well_as_numbered(self, tmp_path: Path) -> None:
        """EIFO_LOG_LEVEL is a word, because that is what a person types."""
        logs.add_file(tmp_path, "eifo-api", "DEBUG")

        logging.getLogger("eifo.api.test").debug("the detail")

        assert any("the detail" in line for line in lines(tmp_path / "eifo-api.log"))


class TestSecretsStayOut:
    def test_httpx_is_quietened(self) -> None:
        """It logs whole request URLs and TMDB takes its key as a query
        parameter, so at INFO the key would be written into every log file and
        into anything those files get pasted into. It matters more now that
        there are files."""
        logs.configure_console(logging.INFO)

        assert logging.getLogger("httpx").level == logging.WARNING


class TestLoggersThatDoNotPropagate:
    """Uvicorn configures ``uvicorn.access`` with its own handler and
    ``propagate=False``, so the access log - the record of every request and the
    status it got, and the most useful thing in a server's log - is precisely
    what a root handler never sees."""

    def test_the_file_reaches_them(self, tmp_path: Path) -> None:
        logs.add_file(tmp_path, "eifo-api")
        stubborn = logging.getLogger("test.uvicorn.access")
        stubborn.propagate = False

        logs.attach_to(logging.getLogger(), "test.uvicorn.access")
        stubborn.warning('"GET /api/v1/meta HTTP/1.1" 200 OK')

        assert any("200 OK" in line for line in lines(tmp_path / "eifo-api.log"))
        stubborn.handlers.clear()

    def test_the_console_is_left_exactly_as_it_was(self, tmp_path: Path) -> None:
        """Only the file handlers are copied. A second console handler would
        print every one of uvicorn's lines twice."""
        logs.add_file(tmp_path, "eifo-api")
        stubborn = logging.getLogger("test.uvicorn.error")
        stubborn.propagate = False

        logs.attach_to(logging.getLogger(), "test.uvicorn.error")

        assert all(hasattr(handler, "baseFilename") for handler in stubborn.handlers)
        stubborn.handlers.clear()

    def test_a_logger_reached_through_its_parent_is_skipped(self, tmp_path: Path) -> None:
        """Uvicorn's three are not three separate cases: ``uvicorn.error`` has
        no handlers of its own and propagates to ``uvicorn``, so naming all
        three and attaching to all three writes every startup line twice."""
        logs.add_file(tmp_path, "eifo-api")
        parent = logging.getLogger("test.uv")
        parent.propagate = False
        child = logging.getLogger("test.uv.error")

        logs.attach_to(logging.getLogger(), "test.uv", "test.uv.error")
        child.warning("Application startup complete.")

        found = [ln for ln in lines(tmp_path / "eifo-api.log") if "startup complete" in ln]
        assert len(found) == 1
        parent.handlers.clear()
        child.handlers.clear()

    def test_attaching_twice_does_not_double_them(self, tmp_path: Path) -> None:
        """Every line of an access log printed twice is worse than no file."""
        logs.add_file(tmp_path, "eifo-api")
        stubborn = logging.getLogger("test.uvicorn.twice")
        stubborn.propagate = False

        logs.attach_to(logging.getLogger(), "test.uvicorn.twice")
        once = len(stubborn.handlers)
        logs.attach_to(logging.getLogger(), "test.uvicorn.twice")

        assert once > 0
        assert len(stubborn.handlers) == once
        stubborn.handlers.clear()
