"""Plugin discovery, configuration gating, and the source-plugin contract."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy.orm import Session

from eifo_core.enums import OfferType, SourceKind, TitleKind
from eifo_core.models import Source
from eifo_core.settings import Settings, SourceConfig
from eifo_fetcher.registry import (
    declared_sources,
    discover_plugins,
    enabled_sources,
    plugins_for,
    source_overrides,
)
from eifo_fetcher.sources.base import (
    FetchContext,
    RawItem,
    SourceInfo,
    SourcePlugin,
    TooManyErrorsError,
)


def info(key: str) -> SourceInfo:
    return SourceInfo(
        key=key,
        name=key.replace("_", " ").title(),
        kind=SourceKind.SUBSCRIPTION,
        website_url=f"https://{key}.example",
    )


class FakePlugin(SourcePlugin):
    def __init__(self, *keys: str) -> None:
        self._keys = keys

    def sources(self) -> list[SourceInfo]:
        return [info(key) for key in self._keys]

    def fetch(self, ctx: FetchContext) -> Iterator[RawItem]:
        yield RawItem(source_key=ctx.source_key, kind=TitleKind.MOVIE, name="x", year=2020)


class TestDeclaredSources:
    def test_collects_keys_from_every_plugin(self) -> None:
        declared = declared_sources([FakePlugin("a", "b"), FakePlugin("c")])

        assert sorted(declared) == ["a", "b", "c"]

    def test_a_duplicate_key_is_an_error(self) -> None:
        """Letting one silently win would make catalogs depend on import order."""
        with pytest.raises(ValueError, match="declared by both"):
            declared_sources([FakePlugin("a"), FakePlugin("a")])


class TestEnabledSources:
    def test_a_disabled_source_is_excluded(self) -> None:
        settings = Settings(_env_file=None, sources={"b": SourceConfig(enabled=False)})

        enabled = enabled_sources([FakePlugin("a", "b")], settings)

        assert sorted(enabled) == ["a"]

    def test_an_unconfigured_source_defaults_to_enabled(self) -> None:
        """Adding a plugin should be one file, not a file plus a config edit."""
        enabled = enabled_sources([FakePlugin("brand_new")], Settings(_env_file=None))

        assert sorted(enabled) == ["brand_new"]


class TestPluginsFor:
    def test_pairs_plugins_with_only_the_requested_sources(self) -> None:
        plugin = FakePlugin("a", "b")

        pairs = plugins_for([plugin, FakePlugin("c")], ["a"])

        assert len(pairs) == 1
        assert [source.key for source in pairs[0][1]] == ["a"]

    def test_plugins_owning_nothing_requested_are_skipped(self) -> None:
        assert plugins_for([FakePlugin("a")], ["zzz"]) == []


class TestBuiltinPlugins:
    def test_the_shipped_plugins_declare_unique_keys(self) -> None:
        declared = declared_sources(discover_plugins())

        assert "mako" in declared
        assert "netflix_il" in declared
        assert "prime_video_il" in declared

    def test_every_declared_source_has_a_website(self) -> None:
        for source in declared_sources(discover_plugins()).values():
            assert source.website_url.startswith("https://")


class TestFetchContext:
    def test_reports_the_configured_source(self, ctx: FetchContext) -> None:
        assert ctx.config.enabled is True

    def test_records_errors_without_raising(self, ctx: FetchContext) -> None:
        ctx.record_error("bad item")

        assert ctx.error_count == 1
        assert ctx.errors == ["bad item"]

    def test_includes_the_exception_when_given_one(self, ctx: FetchContext) -> None:
        ctx.record_error("bad item", exc=ValueError("nope"))

        assert "ValueError" in ctx.errors[0]

    def test_a_success_resets_the_streak(self, ctx: FetchContext) -> None:
        for _ in range(ctx.max_consecutive_errors - 1):
            ctx.record_error("x")
        ctx.record_success()

        for _ in range(ctx.max_consecutive_errors - 1):
            ctx.record_error("x")  # must not trip the limit

        assert ctx.error_count == 2 * (ctx.max_consecutive_errors - 1)

    def test_a_long_failure_streak_aborts_the_source(self, ctx: FetchContext) -> None:
        with pytest.raises(TooManyErrorsError):
            for _ in range(ctx.max_consecutive_errors):
                ctx.record_error("x")

    def test_stored_errors_are_capped_but_the_count_is_exact(self, ctx: FetchContext) -> None:
        for _ in range(ctx.max_recorded_errors + 5):
            ctx.record_success()
            ctx.record_error("x")

        assert len(ctx.errors) == ctx.max_recorded_errors
        assert ctx.error_count == ctx.max_recorded_errors + 5

    def test_applies_a_configured_rate_limit_to_a_named_host(
        self, http: object, settings: Settings
    ) -> None:
        settings = Settings(
            _env_file=None,
            sources={"slow": SourceConfig(rate_limit_rps=0.5)},
        )
        ctx = FetchContext(source_key="slow", http=http, settings=settings)  # type: ignore[arg-type]

        ctx.apply_rate_limit("slow.example")

        waited = ctx.http.rate_limiter.wait("slow.example", sleep=lambda _s: None, now=lambda: 0.0)
        assert waited == 0.0  # first call is free
        assert ctx.http.rate_limiter.wait(
            "slow.example", sleep=lambda _s: None, now=lambda: 0.0
        ) == pytest.approx(2.0)


class TestRawItem:
    def test_rejects_a_blank_name(self) -> None:
        with pytest.raises(ValueError, match="name"):
            RawItem(source_key="s", kind=TitleKind.MOVIE, name="   ")

    def test_rejects_a_blank_source_key(self) -> None:
        with pytest.raises(ValueError, match="source_key"):
            RawItem(source_key="", kind=TitleKind.MOVIE, name="x")

    def test_rejects_a_price_without_a_currency(self) -> None:
        """A bare number is not a price: 1990 of what?"""
        with pytest.raises(ValueError, match="currency"):
            RawItem(source_key="s", kind=TitleKind.MOVIE, name="x", price_minor=1990)

    def test_rejects_a_currency_without_a_price(self) -> None:
        with pytest.raises(ValueError, match="currency"):
            RawItem(source_key="s", kind=TitleKind.MOVIE, name="x", price_currency="ILS")

    def test_rejects_a_negative_price(self) -> None:
        with pytest.raises(ValueError, match="negative"):
            RawItem(
                source_key="s", kind=TitleKind.MOVIE, name="x", price_minor=-1, price_currency="ILS"
            )

    def test_defaults_to_a_streaming_offer(self) -> None:
        assert RawItem(source_key="s", kind=TitleKind.MOVIE, name="x").offer_type is (
            OfferType.STREAM
        )


class TestAnOperatorsOverride:
    """The Manage tab's switch, which the fetcher reads on its next run.

    Nullable on purpose: NULL means "the config file decides". A boolean
    defaulting to true would have frozen every source at whatever the file said
    the moment somebody first used one toggle.
    """

    def test_without_one_the_config_file_still_decides(self) -> None:
        settings = Settings(_env_file=None, sources={"mako": SourceConfig(enabled=False)})

        enabled = enabled_sources([FakePlugin("mako", "kan")], settings, overrides={})

        assert "mako" not in enabled
        assert "kan" in enabled

    def test_it_can_switch_a_source_off(self) -> None:
        settings = Settings(_env_file=None)

        enabled = enabled_sources([FakePlugin("mako", "kan")], settings, overrides={"kan": False})

        assert "kan" not in enabled
        assert "mako" in enabled

    def test_it_can_switch_one_back_on_that_the_file_disabled(self) -> None:
        settings = Settings(_env_file=None, sources={"mako": SourceConfig(enabled=False)})

        enabled = enabled_sources([FakePlugin("mako", "kan")], settings, overrides={"mako": True})

        assert "mako" in enabled

    def test_no_overrides_at_all_behaves_as_it_always_did(self) -> None:
        settings = Settings(_env_file=None, sources={"mako": SourceConfig(enabled=False)})

        assert enabled_sources([FakePlugin("mako", "kan")], settings) == enabled_sources(
            [FakePlugin("mako", "kan")], settings, overrides={}
        )


class TestReadingTheOverrides:
    def test_only_the_rows_that_carry_an_answer(self, session: Session) -> None:
        """A NULL is the absence of an answer, not an answer of "off"."""
        session.add_all(
            [
                Source(
                    key="mako",
                    name="Mako",
                    kind=SourceKind.FREE,
                    website_url="https://mako.test",
                    enabled=False,
                ),
                Source(
                    key="kan",
                    name="Kan",
                    kind=SourceKind.FREE,
                    website_url="https://kan.test",
                    enabled=True,
                ),
                Source(
                    key="netflix_il",
                    name="Netflix",
                    kind=SourceKind.SUBSCRIPTION,
                    website_url="https://netflix.test",
                ),
            ]
        )
        session.commit()

        assert source_overrides(session) == {"mako": False, "kan": True}

    def test_nothing_set_is_an_empty_answer(self, session: Session) -> None:
        assert source_overrides(session) == {}


class TestWhatASourceDoesWhenTheFileSaysNothing:
    """Absence is not the same as `enabled = false`.

    A source the config file has never heard of falls back to what its plugin
    declares, which is how a new plugin starts working without an edit to every
    deployment. Almost every source declares itself on; one that costs a
    request per title declares itself off, so upgrading does not quietly hand
    somebody a much longer nightly run.
    """

    def _plugin(self) -> FakePlugin:
        return FakePlugin("cheap", "expensive")

    def _sources(self) -> dict[str, SourceInfo]:
        return {
            "cheap": info("cheap"),
            "expensive": SourceInfo(
                key="expensive",
                name="Expensive",
                kind=SourceKind.RENT_BUY,
                website_url="https://expensive.example",
                default_enabled=False,
            ),
        }

    def test_a_source_that_declares_itself_off_stays_off(self) -> None:
        plugin = _DeclaringPlugin(self._sources())

        assert set(enabled_sources([plugin], Settings(_env_file=None))) == {"cheap"}

    def test_the_file_still_wins_when_it_says_anything(self) -> None:
        plugin = _DeclaringPlugin(self._sources())
        settings = Settings(_env_file=None, sources={"expensive": SourceConfig(enabled=True)})

        assert "expensive" in enabled_sources([plugin], settings)

    def test_and_an_operator_switch_wins_over_both(self) -> None:
        plugin = _DeclaringPlugin(self._sources())

        enabled = enabled_sources([plugin], Settings(_env_file=None), overrides={"expensive": True})

        assert "expensive" in enabled

    def test_the_apple_store_is_the_one_that_declares_itself_off(self) -> None:
        """Named rather than inferred: this is a promise about a nightly run."""
        declared = declared_sources(discover_plugins())

        assert declared["apple_tv_store"].default_enabled is False
        assert declared["netflix_il"].default_enabled is True


class _DeclaringPlugin(SourcePlugin):
    """A plugin whose sources carry their own default_enabled."""

    def __init__(self, sources: dict[str, SourceInfo]) -> None:
        self._sources = sources

    def sources(self) -> list[SourceInfo]:
        return list(self._sources.values())

    def fetch(self, ctx: FetchContext) -> Iterator[RawItem]:
        return iter(())
