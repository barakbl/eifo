"""``eifo-api`` - the thin wrapper that starts uvicorn.

Only the argument handling is worth testing: uvicorn itself is not ours, so
every test here stops at "what were we about to ask it for".
"""

from __future__ import annotations

from typing import Any

import pytest

from eifo_api import __main__ as command


@pytest.fixture
def settings_from_env(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Set environment, then read settings fresh - they are cached per process."""
    from eifo_core import settings as settings_module

    def apply(**env: str) -> None:
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        settings_module.get_settings.cache_clear()

    yield apply
    settings_module.get_settings.cache_clear()


@pytest.fixture
def asked(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Capture what would have been handed to uvicorn.run."""
    captured: dict[str, Any] = {}

    def fake_run(app: str, **kwargs: Any) -> None:
        captured["app"] = app
        captured.update(kwargs)

    monkeypatch.setattr(command.uvicorn, "run", fake_run)
    return captured


class TestHowManyWorkers:
    def test_one_process_is_the_default_and_asks_for_no_workers_at_all(
        self, asked: dict[str, Any]
    ) -> None:
        """``workers=1`` and ``workers=None`` are different paths in uvicorn.

        None is the single-process server this command has always run, and it
        is what an install that never asked for workers must keep getting.
        """
        command.main([])

        assert asked["workers"] is None

    def test_asking_for_more_passes_them_through(
        self, asked: dict[str, Any], settings_from_env: Any
    ) -> None:
        """Two processes, two interpreter locks: a sync writing through the
        API stops being something the web app waits behind."""
        settings_from_env(EIFO_AUTO_MIGRATE="false")

        command.main(["--workers", "2"])

        assert asked["workers"] == 2

    def test_reloading_and_workers_together_are_refused(self, asked: dict[str, Any]) -> None:
        """uvicorn honours one and drops the other without saying which."""
        with pytest.raises(SystemExit):
            command.main(["--reload", "--workers", "2"])

        assert asked == {}

    def test_the_setting_is_what_the_flag_defaults_to(
        self, asked: dict[str, Any], settings_from_env: Any
    ) -> None:
        """So a deployment sets it once in config rather than in the command."""
        settings_from_env(EIFO_SERVE_WORKERS="3", EIFO_AUTO_MIGRATE="false")

        command.main([])

        assert asked["workers"] == 3


class TestWorkersAndMigrations:
    """Every worker runs the lifespan, so every worker would migrate."""

    def test_workers_with_auto_migrate_on_is_refused(
        self, asked: dict[str, Any], settings_from_env: Any
    ) -> None:
        """A pending migration applied by several processes at once is not a
        thing to discover on the one deploy that has one waiting."""
        settings_from_env(EIFO_AUTO_MIGRATE="true")

        with pytest.raises(SystemExit):
            command.main(["--workers", "2"])

        assert asked == {}

    def test_workers_are_allowed_once_the_schema_is_somebody_elses_job(
        self, asked: dict[str, Any], settings_from_env: Any
    ) -> None:
        settings_from_env(EIFO_AUTO_MIGRATE="false")

        command.main(["--workers", "2"])

        assert asked["workers"] == 2
