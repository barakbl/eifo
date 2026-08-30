"""Settings layering and fail-fast validation."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr, ValidationError

from eifo_core.settings import (
    MissingSettingsError,
    Settings,
    SourceConfig,
    get_settings,
)


def _write_config(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "eifo.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_defaults_apply_without_any_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EIFO_CONFIG_FILE", "does-not-exist.toml")

    settings = Settings(_env_file=None)

    assert settings.db_url == "sqlite:///data/eifo.db"
    assert settings.images_dir == Path("data/images")
    assert settings.stale_after_hours == 48
    assert settings.sources == {}


def test_toml_file_overrides_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _write_config(
        tmp_path,
        """
        db_url = "sqlite:///from-toml.db"
        stale_after_hours = 12

        [sources.cellcom_tv]
        enabled = true
        rate_limit_rps = 0.5

        [sources.free_tv]
        enabled = false

        [scores.weights]
        imdb = 5.0
        """,
    )
    monkeypatch.setenv("EIFO_CONFIG_FILE", str(config))

    settings = Settings(_env_file=None)

    assert settings.db_url == "sqlite:///from-toml.db"
    assert settings.stale_after_hours == 12
    assert settings.sources["cellcom_tv"].rate_limit_rps == 0.5
    assert settings.scores.weights.imdb == 5.0
    # Unspecified weights keep their defaults.
    assert settings.scores.weights.seret_critics == 2.0


def test_environment_beats_the_toml_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _write_config(tmp_path, 'db_url = "sqlite:///from-toml.db"')
    monkeypatch.setenv("EIFO_CONFIG_FILE", str(config))
    monkeypatch.setenv("EIFO_DB_URL", "sqlite:///from-env.db")

    assert Settings(_env_file=None).db_url == "sqlite:///from-env.db"


def test_nested_settings_come_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EIFO_CONFIG_FILE", "does-not-exist.toml")
    monkeypatch.setenv("EIFO_SCORES__LOW_VOTE_THRESHOLD", "10")

    assert Settings(_env_file=None).scores.low_vote_threshold == 10


def test_enabled_source_keys_skips_disabled_sources() -> None:
    settings = Settings(
        _env_file=None,
        sources={
            "netflix_il": SourceConfig(enabled=True),
            "free_tv": SourceConfig(enabled=False),
            "mako": SourceConfig(enabled=True),
        },
    )

    assert settings.enabled_source_keys() == ["netflix_il", "mako"]


def test_source_config_falls_back_to_defaults() -> None:
    settings = Settings(_env_file=None)

    config = settings.source_config("never-configured")

    assert config.enabled is True
    assert config.rate_limit_rps is None


class TestRequire:
    def test_passes_when_every_setting_has_a_value(self) -> None:
        settings = Settings(_env_file=None, secret_key=SecretStr("s3cret"))

        settings.require("secret_key", "db_url")

    def test_lists_every_missing_setting_at_once(self) -> None:
        settings = Settings(_env_file=None)

        with pytest.raises(MissingSettingsError) as exc_info:
            settings.require("secret_key", "tmdb_api_key")

        assert exc_info.value.missing == ["secret_key", "tmdb_api_key"]
        message = str(exc_info.value)
        assert "EIFO_SECRET_KEY" in message
        assert "EIFO_TMDB_API_KEY" in message

    def test_blank_secret_counts_as_missing(self) -> None:
        settings = Settings(_env_file=None, tmdb_api_key=SecretStr("   "))

        with pytest.raises(MissingSettingsError):
            settings.require("tmdb_api_key")

    def test_unknown_setting_is_a_programming_error(self) -> None:
        with pytest.raises(AttributeError):
            Settings(_env_file=None).require("no_such_setting")


def test_get_settings_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("EIFO_CONFIG_FILE", "does-not-exist.toml")
    monkeypatch.setenv("EIFO_PUBLIC_ORIGIN", "https://first.example")

    first = get_settings()
    monkeypatch.setenv("EIFO_PUBLIC_ORIGIN", "https://second.example")
    second = get_settings()

    assert first is second
    assert second.public_origin == "https://first.example"
    get_settings.cache_clear()


class TestScheduleConfig:
    """The phases became one chain; configs written for three still work."""

    def test_the_nightly_run_has_a_default_time(self) -> None:
        assert Settings(_env_file=None).schedule.nightly == "03:00"

    def test_a_config_written_before_the_chain_keeps_its_start_time(self) -> None:
        """Its first phase time is when the run began, so that is when it still begins."""
        settings = Settings(
            _env_file=None,
            schedule={"sync": "02:15", "enrich": "04:30", "images": "05:30"},
        )

        assert settings.schedule.nightly == "02:15"

    def test_an_explicit_nightly_time_wins(self) -> None:
        settings = Settings(_env_file=None, schedule={"nightly": "01:00", "sync": "02:15"})

        assert settings.schedule.nightly == "01:00"


class TestAdministrators:
    """Nobody is one until an instance says so, by address, in configuration.

    Configuration rather than a flag on a row, because the first administrator
    has to come from somewhere and "whoever signed in first" is how a public
    instance hands itself to a stranger.
    """

    def test_an_instance_that_never_wanted_one_has_none(self) -> None:
        settings = Settings(_env_file=None)

        assert settings.admin_emails == []
        assert settings.is_admin("anybody@example.com") is False

    def test_a_listed_address_is_one(self) -> None:
        settings = Settings(_env_file=None, admin_emails=["ops@example.com"])

        assert settings.is_admin("ops@example.com") is True
        assert settings.is_admin("someone.else@example.com") is False

    def test_case_does_not_decide_it(self) -> None:
        """Providers are inconsistent, and nobody typing one thinks about it."""
        settings = Settings(_env_file=None, admin_emails=["Ops@Example.com"])

        assert settings.is_admin("ops@example.COM") is True

    def test_an_account_with_no_address_is_nobody(self) -> None:
        """X does not always supply one, and an absent address matches nothing."""
        settings = Settings(_env_file=None, admin_emails=["ops@example.com"])

        assert settings.is_admin(None) is False
        assert settings.is_admin("") is False
        assert settings.is_admin("   ") is False

    def test_a_comma_separated_line_is_what_a_dotenv_file_holds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pydantic reads a list as JSON, which is a strange thing to ask of .env."""
        monkeypatch.setenv("EIFO_ADMIN_EMAILS", "one@example.com, two@example.com")

        settings = Settings(_env_file=None)

        assert settings.admin_emails == ["one@example.com", "two@example.com"]
        assert settings.is_admin("two@example.com") is True

    def test_a_list_still_works(self) -> None:
        assert Settings(_env_file=None, admin_emails=["a@b.test"]).admin_emails == ["a@b.test"]


class TestTheExampleConfig:
    """The file people copy to make their own.

    A setting that exists and is written down nowhere is a setting nobody
    finds - which is how the Manage tab shipped invisible to the person who
    asked for it. These keep the example honest about what can be set.
    """

    def _example(self) -> dict[str, Any]:
        path = Path(__file__).resolve().parents[3] / "config" / "eifo.example.toml"
        return tomllib.loads(path.read_text(encoding="utf-8"))

    def test_it_is_valid_configuration(self) -> None:
        """Not merely valid TOML: every key has to be one Settings accepts."""
        Settings(_env_file=None, **self._example())

    def test_it_names_the_settings_that_turn_a_surface_on(self) -> None:
        """The ones nobody can guess, because nothing in the UI hints at them."""
        example = self._example()

        assert "admin_emails" in example
        assert example["admin_emails"] == [], "the example must not grant anybody access"

    def test_it_holds_no_secrets(self) -> None:
        """They come from the environment; this file is committable."""
        example = self._example()

        for name in ("secret_key", "tmdb_api_key", "google_client_secret", "x_client_secret"):
            assert name not in example


class TestFetchConfig:
    """How much of the sync phase runs at once (``eifo_fetcher.prefetch``)."""

    def test_it_reads_several_catalogs_at_once_by_default(self) -> None:
        assert Settings(_env_file=None).fetch.concurrency > 1

    def test_the_serial_run_is_still_available(self) -> None:
        assert Settings(_env_file=None, fetch={"concurrency": 1}).fetch.concurrency == 1

    def test_no_readers_at_all_is_refused(self) -> None:
        """A run that reads nothing is not a slower run, it is no run."""
        with pytest.raises(ValidationError):
            Settings(_env_file=None, fetch={"concurrency": 0})

    def test_a_buffer_with_no_room_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            Settings(_env_file=None, fetch={"buffer_size": 0})
