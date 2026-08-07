"""The tvil-fetch CLI: migration commands and exit codes."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect

from tvil_core.settings import get_settings
from tvil_fetcher.cli import EXIT_FATAL, EXIT_OK, build_parser, main


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the CLI at a throwaway database, ignoring ambient configuration."""
    db_path = tmp_path / "cli.db"
    monkeypatch.setenv("TVIL_CONFIG_FILE", str(tmp_path / "absent.toml"))
    monkeypatch.setenv("TVIL_DB_URL", f"sqlite:///{db_path}")
    get_settings.cache_clear()
    yield db_path
    get_settings.cache_clear()


def test_db_upgrade_creates_the_schema(isolated_settings: Path) -> None:
    assert main(["db", "upgrade"]) == EXIT_OK

    engine = create_engine(f"sqlite:///{isolated_settings}")
    try:
        tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
    assert {"titles", "sources", "availability"} <= tables


def test_db_upgrade_is_idempotent(isolated_settings: Path) -> None:
    assert main(["db", "upgrade"]) == EXIT_OK
    assert main(["db", "upgrade"]) == EXIT_OK


def test_db_current_reports_the_revision(
    isolated_settings: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    main(["db", "upgrade"])

    assert main(["db", "current"]) == EXIT_OK

    assert capsys.readouterr().out.strip() == "0001_initial"


def test_db_current_on_a_fresh_database(
    isolated_settings: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["db", "current"]) == EXIT_OK

    assert "no migrations applied" in capsys.readouterr().out


def test_db_downgrade_reverts_the_schema(isolated_settings: Path) -> None:
    main(["db", "upgrade"])

    assert main(["db", "downgrade", "base"]) == EXIT_OK

    engine = create_engine(f"sqlite:///{isolated_settings}")
    try:
        assert "titles" not in set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_unexpected_failure_exits_fatally(
    isolated_settings: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk on fire")

    monkeypatch.setattr("tvil_core.migrate.upgrade", _boom)

    assert main(["db", "upgrade"]) == EXIT_FATAL


class TestParser:
    def test_requires_a_subcommand(self) -> None:
        with pytest.raises(SystemExit):
            build_parser().parse_args([])

    def test_requires_a_db_action(self) -> None:
        with pytest.raises(SystemExit):
            build_parser().parse_args(["db"])

    def test_upgrade_defaults_to_head(self) -> None:
        args = build_parser().parse_args(["db", "upgrade"])

        assert args.revision == "head"

    def test_upgrade_accepts_an_explicit_revision(self) -> None:
        args = build_parser().parse_args(["db", "upgrade", "0001_initial"])

        assert args.revision == "0001_initial"

    def test_reports_a_version(self) -> None:
        with pytest.raises(SystemExit) as exc_info:
            build_parser().parse_args(["--version"])

        assert exc_info.value.code == 0
