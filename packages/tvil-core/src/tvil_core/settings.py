"""Configuration.

Layering, highest priority first: ``TVIL_*`` environment variables (and ``.env``),
then ``config/tvil.toml``, then the defaults declared here. Secrets live only in
the environment so the TOML file stays committable.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

DEFAULT_CONFIG_FILE = "config/tvil.toml"


class MissingSettingsError(RuntimeError):
    """Raised at startup when required configuration is absent.

    Lists every missing setting at once so a fresh install is fixed in one pass
    rather than one restart per variable.
    """

    def __init__(self, missing: Sequence[str]) -> None:
        self.missing = list(missing)
        env_names = ", ".join(f"TVIL_{name.upper()}" for name in self.missing)
        super().__init__(
            f"Missing required configuration: {env_names}. "
            f"Set it in .env or the environment — see docs.internal/11-ops-install.md."
        )


class SourceConfig(BaseModel):
    """Per-source switches. Absent from the file means "use these defaults"."""

    enabled: bool = True
    rate_limit_rps: float | None = None
    #: Cap on paginated result pages per media type; None uses the plugin default.
    max_pages: int | None = None


class ScoreWeights(BaseModel):
    """Weights for the aggregate score (docs.internal/06-enrichment.md)."""

    imdb: float = 3.0
    rt_critics: float = 2.0
    rt_audience: float = 1.0
    tmdb: float = 1.0
    seret_critics: float = 2.0
    seret_viewers: float = 1.5
    edb: float = 0.5


class ScoresConfig(BaseModel):
    weights: ScoreWeights = Field(default_factory=ScoreWeights)
    #: Ratings backed by fewer votes than this get half weight.
    low_vote_threshold: int = 50
    #: An aggregate of a single provider is misleading; require at least this many.
    min_providers: int = 2


class EnrichConfig(BaseModel):
    """How often ratings are refreshed (docs.internal/06-enrichment.md)."""

    #: Ratings older than this are refetched.
    refresh_days: int = 14
    #: Titles currently available somewhere are worth keeping fresher.
    hot_refresh_days: int = 3
    #: Titles per run, so a scheduled enrich has a bounded runtime.
    batch_size: int = 500
    #: Providers switched off, by enricher key.
    disabled: list[str] = Field(default_factory=list)


class ScheduleConfig(BaseModel):
    """Daemon schedule; ignored when the phases are driven by system cron."""

    sync: str = "03:00"
    enrich: str = "04:30"
    images: str = "05:30"


class Settings(BaseSettings):
    """Runtime configuration for both services."""

    model_config = SettingsConfigDict(
        env_prefix="TVIL_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    db_url: str = "sqlite:///data/tvil.db"
    images_dir: Path = Path("data/images")
    public_origin: str = "http://localhost:8000"
    stale_after_hours: int = 48

    # Secrets — never in the TOML file.
    secret_key: SecretStr | None = None
    tmdb_api_key: SecretStr | None = None
    google_client_id: SecretStr | None = None
    google_client_secret: SecretStr | None = None
    x_client_id: SecretStr | None = None
    x_client_secret: SecretStr | None = None

    sources: dict[str, SourceConfig] = Field(default_factory=dict)
    scores: ScoresConfig = Field(default_factory=ScoresConfig)
    enrich: EnrichConfig = Field(default_factory=EnrichConfig)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Insert the TOML file below the environment in the precedence chain."""
        config_file = Path(os.environ.get("TVIL_CONFIG_FILE", DEFAULT_CONFIG_FILE))
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            TomlConfigSettingsSource(settings_cls, toml_file=config_file),
            file_secret_settings,
        )

    def enabled_source_keys(self) -> list[str]:
        """Source keys currently switched on, in configuration order."""
        return [key for key, cfg in self.sources.items() if cfg.enabled]

    def source_config(self, key: str) -> SourceConfig:
        """Configuration for one source, falling back to defaults."""
        return self.sources.get(key, SourceConfig())

    def require(self, *names: str) -> None:
        """Fail fast unless every named setting has a value.

        Raises:
            MissingSettingsError: listing all missing settings at once.
            AttributeError: if a name is not a known setting (a programming error).
        """
        missing = [name for name in names if _is_unset(getattr(self, name))]
        if missing:
            raise MissingSettingsError(missing)


def _is_unset(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, SecretStr):
        return not value.get_secret_value().strip()
    if isinstance(value, str):
        return not value.strip()
    return False


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings, loaded once.

    Tests that manipulate the environment should call ``get_settings.cache_clear()``
    or construct :class:`Settings` directly.
    """
    return Settings()
