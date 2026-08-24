"""One fetcher at a time.

The lock is what keeps a scheduled run, a hand-run ``eifo-fetch all`` and a
leftover cron entry from asking every source for the same catalog at once.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from eifo_core.settings import Settings
from eifo_fetcher.lock import LOCK_FILENAME, AlreadyRunningError, lock_path, single_flight


class TestWhereTheLockLives:
    def test_beside_the_database_it_guards(self, settings: Settings, tmp_path: Path) -> None:
        assert lock_path(settings) == tmp_path / LOCK_FILENAME

    def test_a_database_that_is_not_a_local_file_falls_back(self, tmp_path: Path) -> None:
        """Only arises in tests and in deployments this project does not claim to support."""
        settings = Settings(
            _env_file=None,
            db_url="sqlite://",
            images_dir=tmp_path / "images",
        )

        assert lock_path(settings) == tmp_path / "images" / LOCK_FILENAME

    def test_a_missing_directory_is_created(self, tmp_path: Path) -> None:
        settings = Settings(
            _env_file=None,
            db_url=f"sqlite:///{tmp_path / 'nested' / 'deeper' / 'eifo.db'}",
            images_dir=tmp_path / "images",
        )

        with single_flight(settings):
            assert lock_path(settings).exists()


class TestExclusion:
    def test_a_second_holder_is_turned_away(self, settings: Settings) -> None:
        with single_flight(settings), pytest.raises(AlreadyRunningError), single_flight(settings):
            pass  # pragma: no cover - the context manager raises on entry

    def test_the_lock_is_released_on_the_way_out(self, settings: Settings) -> None:
        with single_flight(settings):
            pass

        with single_flight(settings):
            pass  # would raise if the first had not let go

    def test_the_lock_is_released_when_the_block_raises(self, settings: Settings) -> None:
        """A fetcher that fell over must not lock the catalog out of tonight's run."""
        with pytest.raises(RuntimeError), single_flight(settings):
            raise RuntimeError("sync exploded")

        with single_flight(settings):
            pass

    def test_the_holder_is_named(self, settings: Settings) -> None:
        """So an operator wondering what is running has somewhere to start."""
        holder = f"pid {os.getpid()}"
        with (
            single_flight(settings),
            pytest.raises(AlreadyRunningError, match=holder),
            single_flight(settings),
        ):
            pass  # pragma: no cover - the context manager raises on entry

    def test_a_leftover_lock_file_is_not_a_lock(self, settings: Settings) -> None:
        """The kernel releases an flock when its holder dies; nothing to clean up."""
        lock_path(settings).write_text("pid 999999\n", encoding="utf-8")

        with single_flight(settings):
            pass
