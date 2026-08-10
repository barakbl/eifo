"""Settings layering and fail-fast validation."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr

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
