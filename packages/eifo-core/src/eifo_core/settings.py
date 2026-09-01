"""Configuration.

Layering, highest priority first: ``EIFO_*`` environment variables (and ``.env``),
then ``config/eifo.toml``, then the defaults declared here. Secrets live only in
the environment so the TOML file stays committable.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    NoDecode,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

DEFAULT_CONFIG_FILE = "config/eifo.toml"


class MissingSettingsError(RuntimeError):
    """Raised at startup when required configuration is absent.

    Lists every missing setting at once so a fresh install is fixed in one pass
    rather than one restart per variable.
    """

    def __init__(self, missing: Sequence[str]) -> None:
        self.missing = list(missing)
        env_names = ", ".join(f"EIFO_{name.upper()}" for name in self.missing)
        super().__init__(
            f"Missing required configuration: {env_names}. "
            f"Set it in .env or the environment - see docs.internal/11-ops-install.md."
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
    seret_viewers: float = 1.0
    edb: float = 0.5


class ScoresConfig(BaseModel):
    weights: ScoreWeights = Field(default_factory=ScoreWeights)
    #: Ratings backed by fewer votes than this get half weight.
    low_vote_threshold: int = 50
    #: An aggregate of a single provider is misleading; require at least this many.
    min_providers: int = 2
    #: Votes a provider needs before its rating counts towards an aggregate at
    #: all, by provider. At or below the number here the rating is still stored
    #: and still shown - with its vote count and a link, so a reader can judge
    #: it - but contributes nothing to the arithmetic.
    #:
    #: Only Seret's audience score by default. It is the thinnest-voted source
    #: here by a wide margin: a film can carry a 9.1 from four people, which
    #: halving the weight does not make harmless. Everything else keeps to the
    #: damping above. Setting this section in the config file replaces the
    #: default outright rather than merging, so name every provider you want a
    #: floor for.
    min_votes: dict[str, int] = Field(default_factory=lambda: {"seret_viewers": 10})


class EnrichConfig(BaseModel):
    """How often ratings are refreshed (docs.internal/06-enrichment.md)."""

    #: Ratings older than this are refetched.
    refresh_days: int = 14
    #: Titles currently available somewhere are worth keeping fresher.
    hot_refresh_days: int = 3
    #: Titles per run, so a scheduled enrich has a bounded runtime.
    batch_size: int = 500
    #: How long to leave a title alone after nobody could rate it. Doubles with
    #: each consecutive fruitless attempt: most of a catalog this local will
    #: never carry a score, and asking every month costs the whole batch.
    retry_days: int = 30
    #: The same, after a provider failed rather than came back empty - that is
    #: the provider's problem and usually passes, so it is retried sooner.
    retry_error_days: int = 1
    #: Ceiling on those doublings, so nothing is written off for good.
    retry_max_days: int = 365
    #: Providers switched off, by enricher key.
    disabled: list[str] = Field(default_factory=list)
    #: Providers switched on that are otherwise off by default.
    enabled: list[str] = Field(default_factory=list)

    #: Requests per second per scraped provider, by enricher key.
    #:
    #: The scraped providers read somebody's website rather than an API built
    #: to be called, and how hard to lean on each is the operator's decision
    #: rather than the plugin's. Absent means the provider's own default.
    #:
    #: This replaces a ``ctx.apply_rate_limit`` call inside each enricher,
    #: which resolved to ``[sources.enrich]`` - a section that exists nowhere,
    #: so every enricher's rate limit was silently a no-op and `rt` scraped at
    #: the client-wide default with no documented way to change it.
    rate_limits: dict[str, float] = Field(default_factory=dict)

    def rate_limit_for(self, key: str, default: float | None = None) -> float | None:
        """The pace configured for one enricher, or its own default."""
        return self.rate_limits.get(key, default)


class SeretConfig(BaseModel):
    """Building the Seret index, and how hard seret.co.il is asked for it.

    Its own section rather than a ``[sources.*]`` entry, because Seret is a
    ratings provider rather than a catalog and the crawl that builds its index
    is a separate job from the nightly enrich that reads it
    (docs.internal/06-enrichment.md).
    """

    #: Pages read per crawl.
    #:
    #: Sized to disappear into a nightly run rather than to finish quickly: 300
    #: pages at the default half-a-second pace is about ten minutes. Seret
    #: publishes ~8,900 title pages, so a first index fills itself in over a
    #: month of ordinary nightly runs, and every run picks up where the last
    #: one stopped. Somebody who wants it now runs ``eifo-fetch seret index
    #: --limit 9000`` and waits five hours.
    #:
    #: The pace itself is not here - it is ``[enrich.rate_limits] seret``,
    #: beside every other scraped provider's.
    batch_size: int = 300
    #: How old a row may get before the crawl reads its page again. Months,
    #: not days: a score that has been settling for years moves slowly, and the
    #: back catalogue does not move at all.
    refresh_days: int = 120
    #: Ask Seret's autocomplete about a title the index has never heard of.
    #:
    #: It answers for recent releases only - it knows this year's films and not
    #: 2004's - which is exactly the gap between one index crawl and the next,
    #: and it costs at most two requests for a title that would otherwise get
    #: no Israeli score at all. One request per unknown title, so it is worth
    #: switching off for a first run over a large unindexed catalog.
    live_fallback: bool = True

    @field_validator("batch_size", "refresh_days")
    @classmethod
    def _at_least_one(cls, value: int) -> int:
        if value < 1:
            raise ValueError("seret batch_size and refresh_days must be at least 1")
        return value


class FetchConfig(BaseModel):
    """How the sync phase spends its time.

    See ``eifo_fetcher.prefetch`` for why only the reading is parallel and the
    writing is not.
    """

    #: How many plugins may be reading their catalogs at the same time.
    #:
    #: Plugins, not sources: a plugin owning several services fetches them one
    #: after another, because those services share an upstream API and the rate
    #: limit that goes with it. Raising this buys wall-clock on a run whose time
    #: goes on waiting for other people's servers, which is nearly all of it;
    #: it does not make any single site be asked for more per second, which the
    #: per-host rate limiter still governs. 1 restores the old serial run.
    concurrency: int = 4
    #: Listings held per source while the ingester is busy with another one.
    #:
    #: The whole point of the buffer: a plugin that has finished reading its
    #: catalog releases its worker for the next plugin instead of waiting its
    #: turn at the database. Big enough that most catalogs fit entirely, small
    #: enough that several at once are megabytes rather than hundreds of them.
    buffer_size: int = 2000

    @field_validator("concurrency")
    @classmethod
    def _at_least_one(cls, value: int) -> int:
        if value < 1:
            raise ValueError("fetch.concurrency must be at least 1")
        return value

    @field_validator("buffer_size")
    @classmethod
    def _room_for_something(cls, value: int) -> int:
        if value < 1:
            raise ValueError("fetch.buffer_size must be at least 1")
        return value


class TmdbConfig(BaseModel):
    """How hard to lean on the TMDB API.

    Separate from the scraped sources' own limits, which exist to be polite to
    somebody's website. This one is an API meant to be called, and the number
    here sets the pace of the whole nightly run.
    """

    #: Requests per second, to both the API and the image CDN. See
    #: ``eifo_fetcher.tmdb.DEFAULT_RATE_LIMIT_RPS`` for why it is what it is.
    rate_limit_rps: float = 20.0


class ScheduleConfig(BaseModel):
    """Daemon schedule; ignored when the phases are driven by system cron."""

    #: When the nightly run starts, UTC. One time for the whole thing, because
    #: the phases are a chain rather than three independent jobs: enrichment
    #: needs the titles sync creates, and artwork needs the URLs enrichment
    #: fills in. Giving each its own hour only worked while every phase
    #: reliably finished inside it, and a full sync no longer does.
    nightly: str = "03:00"

    @model_validator(mode="before")
    @classmethod
    def _accept_the_old_phase_times(cls, data: Any) -> Any:
        """Read a pre-chain config: its first phase time is when the run began."""
        if isinstance(data, dict) and "nightly" not in data and "sync" in data:
            return {**data, "nightly": data["sync"]}
        return data


class Settings(BaseSettings):
    """Runtime configuration for both services."""

    model_config = SettingsConfigDict(
        env_prefix="EIFO_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    db_url: str = "sqlite:///data/eifo.db"
    images_dir: Path = Path("data/images")
    #: Where the static web client lives; found automatically when unset.
    web_dir: Path | None = None
    public_origin: str = "http://localhost:3436"
    #: Host and port the bundled ``eifo-api`` command binds. Only the checkout
    #: path uses these; the Docker image and any real deployment sit behind a
    #: proxy and pass their own. 3436 spells EIFO on a phone keypad (E/F 3,
    #: I 4, O 6), and is what the menu-bar companion expects by default too.
    serve_host: str = "127.0.0.1"
    serve_port: int = 3436
    stale_after_hours: int = 48
    #: Bring the schema to head as the API starts, so an upgrade is a
    #: restart rather than a restart plus a remembered command. Turn it off
    #: to keep migrations a deliberate, separate step; the API then refuses
    #: to serve a database that is missing or behind.
    auto_migrate: bool = True

    # Secrets - never in the TOML file.
    secret_key: SecretStr | None = None
    tmdb_api_key: SecretStr | None = None
    #: Pinged when the nightly run starts, finishes and fails, so a run that
    #: stops happening is noticed by something other than a person wondering
    #: why the catalog looks old. Any watchdog taking a plain GET will do
    #: (healthchecks.io, Uptime Kuma's push monitors); the URL embeds a token,
    #: which is why it belongs with the secrets. Unset means no pinging.
    healthcheck_url: SecretStr | None = None
    google_client_id: SecretStr | None = None
    google_client_secret: SecretStr | None = None
    x_client_id: SecretStr | None = None
    x_client_secret: SecretStr | None = None

    #: Who may open the Manage tab and rule on the review queue, by the email
    #: address their identity provider vouches for.
    #:
    #: A list rather than a flag on the user row, and configuration rather than
    #: data: the first administrator has to come from somewhere, and "whoever
    #: signed in first" is how a public instance hands itself to a stranger.
    #: Nobody is an administrator until this is set, which is the right answer
    #: for an instance that never wanted one.
    #: ``NoDecode`` because pydantic-settings otherwise reads a list from the
    #: environment as JSON, and fails by saying the value is not valid JSON -
    #: which does not tell anybody what to type instead. The validator below
    #: accepts the line a person would actually write.
    admin_emails: Annotated[list[str], NoDecode] = Field(default_factory=list)

    sources: dict[str, SourceConfig] = Field(default_factory=dict)
    scores: ScoresConfig = Field(default_factory=ScoresConfig)
    fetch: FetchConfig = Field(default_factory=FetchConfig)
    tmdb: TmdbConfig = Field(default_factory=TmdbConfig)
    enrich: EnrichConfig = Field(default_factory=EnrichConfig)
    seret: SeretConfig = Field(default_factory=SeretConfig)
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
        config_file = Path(os.environ.get("EIFO_CONFIG_FILE", DEFAULT_CONFIG_FILE))
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            TomlConfigSettingsSource(settings_cls, toml_file=config_file),
            file_secret_settings,
        )

    @field_validator("admin_emails", mode="before")
    @classmethod
    def _accept_a_comma_separated_list(cls, value: Any) -> Any:
        """``EIFO_ADMIN_EMAILS=a@example.com,b@example.com``.

        Pydantic reads a list from the environment as JSON, which is a strange
        thing to ask of a line in a ``.env`` file - and it fails by reporting
        that the value is not valid JSON, which does not tell anybody what to
        type instead.
        """
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value

    def is_admin(self, email: str | None) -> bool:
        """Whether an address is one of the configured administrators.

        Case-insensitively: providers are inconsistent about how they present
        an address, and nobody typing one into a config file thinks about it.
        An account with no email - X does not always supply one - is nobody.
        """
        if not email:
            return False
        return email.strip().casefold() in {entry.casefold() for entry in self.admin_emails}

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
