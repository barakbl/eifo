"""The sync pipeline: upsert, the two-strike sweep, and the volume guard."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from eifo_core.enums import FetchStatus, OfferType, SourceKind, TitleKind
from eifo_core.models import Availability, FetchRun, Source, Title
from eifo_core.settings import Settings
from eifo_core.types import utcnow
from eifo_fetcher.pipeline import (
    COMMIT_EVERY,
    MISS_LIMIT,
    deactivate_missing_sources,
    register_declared_sources,
    sync_source,
)
from eifo_fetcher.sources.base import (
    FetchContext,
    RawItem,
    SourceInfo,
    SourcePlugin,
    TooManyErrorsError,
    plausible_year,
)

INFO = SourceInfo(
    key="cellcom_tv",
    name="Cellcom TV",
    kind=SourceKind.SUBSCRIPTION,
    website_url="https://cellcomtv.co.il",
)


class StaticPlugin(SourcePlugin):
    """Yields a fixed list of items, or raises."""

    def __init__(self, items: list[RawItem] | None = None, error: Exception | None = None) -> None:
        self._items = items or []
        self._error = error

    def sources(self) -> list[SourceInfo]:
        return [INFO]

    def fetch(self, ctx: FetchContext) -> Iterator[RawItem]:
        if self._error is not None:
            raise self._error
        yield from self._items


def item(name: str, **overrides: Any) -> RawItem:
    values: dict[str, Any] = {
        "source_key": INFO.key,
        "kind": TitleKind.SERIES,
        "name": name,
        "year": 2015,
        "offer_type": OfferType.STREAM,
    }
    values.update(overrides)
    return RawItem(**values)


def run_sync(
    session: Session,
    settings: Settings,
    ctx: FetchContext,
    items: list[RawItem] | None = None,
    plugin: SourcePlugin | None = None,
) -> Any:
    return sync_source(
        session,
        plugin or StaticPlugin(items or []),
        INFO,
        ctx,
        items=items if plugin is None else None,
    )


@pytest.fixture
def sync_ctx(http: Any, settings: Settings) -> FetchContext:
    return FetchContext(source_key=INFO.key, http=http, settings=settings)


class TestUpsert:
    def test_creates_the_source_and_its_availability(
        self, session: Session, settings: Settings, sync_ctx: FetchContext
    ) -> None:
        result = run_sync(session, settings, sync_ctx, [item("פאודה"), item("שטיסל")])

        assert result.status is FetchStatus.OK
        assert result.items_seen == 2
        assert result.availability_created == 2
        assert result.titles_created == 2
        assert session.scalar(select(Source).where(Source.key == INFO.key)) is not None

    def test_records_the_deep_link(
        self, session: Session, settings: Settings, sync_ctx: FetchContext
    ) -> None:
        run_sync(session, settings, sync_ctx, [item("פאודה", deep_link_url="https://x.example/f")])

        assert session.scalars(select(Availability)).one().deep_link_url == "https://x.example/f"

    def test_records_the_price_a_rent_offer_carries(
        self, session: Session, settings: Settings, sync_ctx: FetchContext
    ) -> None:
        run_sync(
            session,
            settings,
            sync_ctx,
            [item("פאודה", offer_type=OfferType.RENT, price_minor=1990, price_currency="ILS")],
        )

        availability = session.scalars(select(Availability)).one()
        assert (availability.price_minor, availability.price_currency) == (1990, "ILS")

    def test_an_offer_with_no_price_stores_none(
        self, session: Session, settings: Settings, sync_ctx: FetchContext
    ) -> None:
        """A subscription offer costs nothing extra; it must not read as free."""
        run_sync(session, settings, sync_ctx, [item("פאודה")])

        availability = session.scalars(select(Availability)).one()
        assert (availability.price_minor, availability.price_currency) == (None, None)

    def test_a_price_change_is_picked_up_on_the_next_sync(
        self, session: Session, settings: Settings, sync_ctx: FetchContext
    ) -> None:
        rented = dict(offer_type=OfferType.RENT, price_currency="ILS")
        run_sync(session, settings, sync_ctx, [item("פאודה", price_minor=1990, **rented)])
        run_sync(session, settings, sync_ctx, [item("פאודה", price_minor=2450, **rented)])

        assert session.scalars(select(Availability)).one().price_minor == 2450

    def test_a_second_run_updates_rather_than_duplicates(
        self, session: Session, settings: Settings, sync_ctx: FetchContext
    ) -> None:
        run_sync(session, settings, sync_ctx, [item("פאודה")])
        result = run_sync(session, settings, sync_ctx, [item("פאודה")])

        assert result.availability_created == 0
        assert result.availability_updated == 1
        assert len(session.scalars(select(Availability)).all()) == 1
        assert len(session.scalars(select(Title)).all()) == 1

    def test_separate_offer_types_are_separate_rows(
        self, session: Session, settings: Settings, sync_ctx: FetchContext
    ) -> None:
        run_sync(
            session,
            settings,
            sync_ctx,
            [item("פאודה", offer_type=OfferType.RENT), item("פאודה", offer_type=OfferType.BUY)],
        )

        assert len(session.scalars(select(Availability)).all()) == 2

    def test_stores_the_artwork_url_for_later(
        self, session: Session, settings: Settings, sync_ctx: FetchContext
    ) -> None:
        run_sync(session, settings, sync_ctx, [item("פאודה", poster_url="https://i.example/p.jpg")])

        assert session.scalars(select(Title)).one().poster_source_url == "https://i.example/p.jpg"


class TestSweep:
    def _first_availability(self, session: Session) -> Availability:
        return session.scalars(select(Availability)).one()

    def test_one_miss_only_strikes(
        self, session: Session, settings: Settings, sync_ctx: FetchContext
    ) -> None:
        """A single flaky run must not retire a live catalog entry."""
        run_sync(session, settings, sync_ctx, [item("פאודה")])
        run_sync(session, settings, sync_ctx, [])

        availability = self._first_availability(session)
        assert availability.miss_count == 1
        assert availability.is_current is True
        assert availability.gone_since is None

    def test_two_misses_retire_it(
        self, session: Session, settings: Settings, sync_ctx: FetchContext
    ) -> None:
        run_sync(session, settings, sync_ctx, [item("פאודה")])
        for _ in range(MISS_LIMIT):
            run_sync(session, settings, sync_ctx, [])

        availability = self._first_availability(session)
        assert availability.is_current is False
        assert availability.gone_since is not None

    def test_the_title_survives_retirement(
        self, session: Session, settings: Settings, sync_ctx: FetchContext
    ) -> None:
        """Content that goes away is badged, never deleted."""
        run_sync(session, settings, sync_ctx, [item("פאודה")])
        for _ in range(MISS_LIMIT):
            run_sync(session, settings, sync_ctx, [])

        assert len(session.scalars(select(Title)).all()) == 1
        assert len(session.scalars(select(Availability)).all()) == 1

    def test_reappearing_clears_the_strikes(
        self, session: Session, settings: Settings, sync_ctx: FetchContext
    ) -> None:
        run_sync(session, settings, sync_ctx, [item("פאודה")])
        run_sync(session, settings, sync_ctx, [])
        run_sync(session, settings, sync_ctx, [item("פאודה")])

        availability = self._first_availability(session)
        assert availability.miss_count == 0
        assert availability.is_current is True

    def test_a_retired_entry_revives_when_it_returns(
        self, session: Session, settings: Settings, sync_ctx: FetchContext
    ) -> None:
        run_sync(session, settings, sync_ctx, [item("פאודה")])
        for _ in range(MISS_LIMIT):
            run_sync(session, settings, sync_ctx, [])
        run_sync(session, settings, sync_ctx, [item("פאודה")])

        availability = self._first_availability(session)
        assert availability.is_current is True
        assert availability.gone_since is None

    def test_a_failed_sync_never_sweeps(
        self, session: Session, settings: Settings, sync_ctx: FetchContext
    ) -> None:
        """A scraper outage must not be mistaken for content disappearing."""
        run_sync(session, settings, sync_ctx, [item("פאודה")])

        result = run_sync(
            session, settings, sync_ctx, plugin=StaticPlugin(error=RuntimeError("site down"))
        )

        assert result.status is FetchStatus.FAILED
        assert self._first_availability(session).miss_count == 0


class TestVolumeGuard:
    def _big_catalog(self, count: int) -> list[RawItem]:
        return [item(f"תוכנית {index}", year=2000 + index % 20) for index in range(count)]

    def test_a_collapse_in_volume_is_treated_as_a_broken_parser(
        self, session: Session, settings: Settings, sync_ctx: FetchContext
    ) -> None:
        run_sync(session, settings, sync_ctx, self._big_catalog(200))

        result = run_sync(session, settings, sync_ctx, self._big_catalog(5))

        assert result.status is FetchStatus.ABORTED_SUSPICIOUS

    def test_a_suspicious_run_does_not_sweep(
        self, session: Session, settings: Settings, sync_ctx: FetchContext
    ) -> None:
        run_sync(session, settings, sync_ctx, self._big_catalog(200))
        run_sync(session, settings, sync_ctx, self._big_catalog(5))

        untouched = session.scalars(select(Availability).where(Availability.miss_count > 0)).all()
        assert untouched == []

    def test_a_modest_drop_is_accepted_as_real(
        self, session: Session, settings: Settings, sync_ctx: FetchContext
    ) -> None:
        run_sync(session, settings, sync_ctx, self._big_catalog(200))

        result = run_sync(session, settings, sync_ctx, self._big_catalog(150))

        assert result.status is FetchStatus.OK

    def test_small_catalogs_are_exempt(
        self, session: Session, settings: Settings, sync_ctx: FetchContext
    ) -> None:
        """Below the floor the ratio is noise, not signal."""
        run_sync(session, settings, sync_ctx, self._big_catalog(10))

        result = run_sync(session, settings, sync_ctx, [])

        assert result.status is FetchStatus.OK

    def test_the_first_ever_run_is_never_suspicious(
        self, session: Session, settings: Settings, sync_ctx: FetchContext
    ) -> None:
        assert run_sync(session, settings, sync_ctx, [item("פאודה")]).status is FetchStatus.OK


class TestFetchRuns:
    def test_records_a_run_with_its_stats(
        self, session: Session, settings: Settings, sync_ctx: FetchContext
    ) -> None:
        run_sync(session, settings, sync_ctx, [item("פאודה"), item("שטיסל")])

        run = session.scalars(select(FetchRun)).one()
        assert run.source_key == INFO.key
        assert run.status is FetchStatus.OK
        assert run.stats["items_seen"] == 2
        assert run.stats["matched_by"] == {"created": 2}
        assert run.finished_at is not None

    def test_records_a_failure(
        self, session: Session, settings: Settings, sync_ctx: FetchContext
    ) -> None:
        run_sync(session, settings, sync_ctx, plugin=StaticPlugin(error=RuntimeError("boom")))

        assert session.scalars(select(FetchRun)).one().status is FetchStatus.FAILED

    def test_a_failure_records_what_went_wrong(
        self, session: Session, settings: Settings, sync_ctx: FetchContext
    ) -> None:
        """A row saying only that it failed leaves the one question anyone will ask."""
        run_sync(
            session, settings, sync_ctx, plugin=StaticPlugin(error=RuntimeError("browser blocked"))
        )

        errors = session.scalars(select(FetchRun)).one().stats["errors"]
        assert errors == ["fatal: RuntimeError: browser blocked"]

    def test_an_exhausted_error_budget_says_so_too(
        self, session: Session, settings: Settings, sync_ctx: FetchContext
    ) -> None:
        run_sync(
            session,
            settings,
            sync_ctx,
            plugin=StaticPlugin(error=TooManyErrorsError(INFO.key, 25)),
        )

        errors = session.scalars(select(FetchRun)).one().stats["errors"]
        assert any("TooManyErrorsError" in entry for entry in errors)

    def test_one_row_per_sync_not_one_per_outcome(
        self, session: Session, settings: Settings, sync_ctx: FetchContext
    ) -> None:
        """The row is opened at the start and closed at the end, not written twice."""
        run_sync(session, settings, sync_ctx)

        assert len(list(session.scalars(select(FetchRun)).all())) == 1

    def test_an_exhausted_error_budget_fails_the_source(
        self, session: Session, settings: Settings, sync_ctx: FetchContext
    ) -> None:
        error = TooManyErrorsError(INFO.key, 25)

        result = run_sync(session, settings, sync_ctx, plugin=StaticPlugin(error=error))

        assert result.status is FetchStatus.FAILED


class TestSourceLifecycle:
    def test_reactivates_a_previously_retired_source(
        self, session: Session, settings: Settings, sync_ctx: FetchContext
    ) -> None:
        run_sync(session, settings, sync_ctx, [item("פאודה")])
        deactivate_missing_sources(session, [])
        session.commit()

        run_sync(session, settings, sync_ctx, [item("פאודה")])

        source = session.scalar(select(Source).where(Source.key == INFO.key))
        assert source is not None
        assert source.active is True
        assert source.deactivated_at is None

    def test_retiring_a_source_keeps_its_data(
        self, session: Session, settings: Settings, sync_ctx: FetchContext
    ) -> None:
        run_sync(session, settings, sync_ctx, [item("פאודה")])

        retired = deactivate_missing_sources(session, [])
        session.commit()

        assert retired == [INFO.key]
        source = session.scalar(select(Source).where(Source.key == INFO.key))
        assert source is not None and source.active is False
        assert len(session.scalars(select(Availability)).all()) == 1
        assert len(session.scalars(select(Title)).all()) == 1

    def test_a_still_configured_source_is_left_alone(
        self, session: Session, settings: Settings, sync_ctx: FetchContext
    ) -> None:
        run_sync(session, settings, sync_ctx, [item("פאודה")])

        assert deactivate_missing_sources(session, [INFO.key]) == []


class TestRepeatedItems:
    """Sources repeat themselves; a sync must survive it.

    Paginated APIs return a title again when the underlying result set shifts
    between pages, and two listings can resolve to the same canonical title.
    """

    def test_the_same_title_twice_in_one_run(
        self, session: Session, settings: Settings, sync_ctx: FetchContext
    ) -> None:
        result = run_sync(session, settings, sync_ctx, [item("פאודה"), item("פאודה")])

        assert result.status is FetchStatus.OK
        assert len(session.scalars(select(Availability)).all()) == 1

    def test_a_repeat_counts_as_an_update_not_a_second_row(
        self, session: Session, settings: Settings, sync_ctx: FetchContext
    ) -> None:
        result = run_sync(session, settings, sync_ctx, [item("פאודה"), item("פאודה")])

        assert result.availability_created == 1
        assert result.availability_updated == 1

    def test_a_repeat_with_a_deep_link_keeps_the_link(
        self, session: Session, settings: Settings, sync_ctx: FetchContext
    ) -> None:
        run_sync(
            session,
            settings,
            sync_ctx,
            [item("פאודה"), item("פאודה", deep_link_url="https://x.example/f")],
        )

        assert session.scalars(select(Availability)).one().deep_link_url == "https://x.example/f"

    def test_repeats_across_offer_types_are_still_separate_rows(
        self, session: Session, settings: Settings, sync_ctx: FetchContext
    ) -> None:
        run_sync(
            session,
            settings,
            sync_ctx,
            [
                item("פאודה", offer_type=OfferType.STREAM),
                item("פאודה", offer_type=OfferType.RENT),
                item("פאודה", offer_type=OfferType.STREAM),
            ],
        )

        assert len(session.scalars(select(Availability)).all()) == 2


class TestFailureRecovery:
    def test_a_mid_flush_failure_still_records_the_run(
        self, session: Session, settings: Settings, sync_ctx: FetchContext
    ) -> None:
        """Without a rollback, recording the failure would itself raise."""

        class Exploding(SourcePlugin):
            def sources(self) -> list[SourceInfo]:
                return [INFO]

            def fetch(self, ctx: FetchContext) -> Iterator[RawItem]:
                yield item("פאודה")
                raise RuntimeError("connection reset mid-stream")

        result = run_sync(session, settings, sync_ctx, plugin=Exploding())

        assert result.status is FetchStatus.FAILED
        run = session.scalars(select(FetchRun)).one()
        assert run.status is FetchStatus.FAILED


class TestLockHolding:
    """SQLite allows one writer, so a sync must not hold the lock for its whole run."""

    def _distinct(self, count: int) -> list[RawItem]:
        """Items the matcher will keep separate.

        Generated names alone are not enough - "תוכנית 1" and "תוכנית 2" are
        near-identical, and the matcher is right to collapse them - so each
        item carries its own external id.
        """
        return [item(f"תוכנית {index}", tmdb_id=1000 + index) for index in range(count)]

    def test_commits_during_a_long_source_rather_than_only_at_the_end(
        self, session: Session, settings: Settings, sync_ctx: FetchContext
    ) -> None:
        """Matching makes network calls; one transaction per source would keep
        the write lock held for minutes and lock out everything else."""
        commits = 0
        original = session.commit

        def counting_commit() -> None:
            nonlocal commits
            commits += 1
            original()

        session.commit = counting_commit  # type: ignore[method-assign]

        run_sync(session, settings, sync_ctx, self._distinct(COMMIT_EVERY * 2 + 10))

        assert commits > 1

    def test_committing_as_it_goes_does_not_change_the_outcome(
        self, session: Session, settings: Settings, sync_ctx: FetchContext
    ) -> None:
        count = COMMIT_EVERY * 2 + 50

        result = run_sync(session, settings, sync_ctx, self._distinct(count))

        assert result.status is FetchStatus.OK
        assert result.items_seen == count
        assert len(session.scalars(select(Availability)).all()) == count


class TestPlausibleYears:
    """Catalogs use the year field to mean "unknown" and "not scheduled" too."""

    @pytest.mark.parametrize("value", [0, 1, 1879, 2999, 9999, -5])
    def test_a_placeholder_is_not_a_year(self, value: int) -> None:
        assert plausible_year(value) is None

    @pytest.mark.parametrize("value", [1880, 1927, 2015, 2026])
    def test_a_real_year_is_kept(self, value: int) -> None:
        assert plausible_year(value) == value

    def test_a_title_announced_for_next_year_is_kept(self) -> None:
        """Announced-but-unreleased is ordinary; a decade out is a parsing accident."""
        assert plausible_year(utcnow().year + 1) == utcnow().year + 1

    def test_a_year_far_in_the_future_is_not(self) -> None:
        assert plausible_year(utcnow().year + 20) is None

    def test_no_year_stays_no_year(self) -> None:
        assert plausible_year(None) is None

    def test_every_source_passes_through_the_same_gate(self) -> None:
        """One place rather than each parser's own business."""
        assert item("מגלים את אמריקע", year=2999).year is None
        assert item("ארץ נהדרת", year=0).year is None
        assert item("פאודה", year=2015).year == 2015

    def test_a_junk_year_does_not_cost_the_title(
        self, session: Session, settings: Settings, sync_ctx: FetchContext
    ) -> None:
        """It is the year that is wrong, not the listing."""
        result = run_sync(session, settings, sync_ctx, [item("מגלים את אמריקע", year=2999)])

        assert result.items_seen == 1
        stored = session.scalars(select(Title)).one()
        assert stored.year is None
        assert stored.name_he == "מגלים את אמריקע"


class TestASourceExistsBeforeItHasRun:
    """A source used to exist only once it had synced.

    That made the operator's source list a list of sources that had already
    run: one switched off, or one added in an upgrade, was invisible on the
    single screen whose job is showing services - so the toggle that would have
    switched it on was not there to press.
    """

    def _declared(self) -> dict[str, SourceInfo]:
        return {
            "on_one": SourceInfo(
                key="on_one",
                name="Switched On",
                kind=SourceKind.SUBSCRIPTION,
                website_url="https://on.example",
            ),
            "off_one": SourceInfo(
                key="off_one",
                name="Switched Off",
                kind=SourceKind.RENT_BUY,
                website_url="https://off.example",
            ),
        }

    def test_every_declared_source_gets_a_row(self, session: Session) -> None:
        written = register_declared_sources(session, self._declared(), enabled=["on_one"])

        assert sorted(written) == ["off_one", "on_one"]
        keys = {source.key for source in session.scalars(select(Source)).all()}
        assert keys == {"on_one", "off_one"}

    def test_one_that_is_switched_off_starts_inactive(self, session: Session) -> None:
        """Declared, known, and not currently collected - which is what it is."""
        register_declared_sources(session, self._declared(), enabled=["on_one"])

        stored = {s.key: s for s in session.scalars(select(Source)).all()}
        assert stored["on_one"].active is True
        assert stored["off_one"].active is False
        assert stored["off_one"].deactivated_at is not None

    def test_it_does_not_touch_a_source_that_already_exists(self, session: Session) -> None:
        """Including one somebody has retired: registering is not reactivating."""
        session.add(
            Source(
                key="on_one",
                name="A Name Somebody Edited",
                kind=SourceKind.FREE,
                website_url="https://elsewhere.example",
                active=False,
            )
        )
        session.flush()

        written = register_declared_sources(session, self._declared(), enabled=["on_one"])

        assert written == ["off_one"]
        existing = session.scalars(select(Source).where(Source.key == "on_one")).one()
        assert existing.active is False
        assert existing.name == "A Name Somebody Edited"

    def test_running_it_twice_writes_nothing_the_second_time(self, session: Session) -> None:
        register_declared_sources(session, self._declared(), enabled=["on_one"])

        assert register_declared_sources(session, self._declared(), enabled=["on_one"]) == []
        assert len(session.scalars(select(Source)).all()) == 2
