"""The sync pipeline: upsert, the two-strike sweep, and the volume guard."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from typing import Any

import pytest
from live import LiveApi
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from eifo_api.routers.syncing import MAX_SERVER_LOG_CHARS
from eifo_core.catalog import MISS_LIMIT, deactivate_missing_sources, register_declared_sources
from eifo_core.enums import FetchStatus, OfferType, SourceKind, TitleKind
from eifo_core.models import Availability, FetchRun, Source, Title
from eifo_core.settings import Settings
from eifo_core.types import utcnow
from eifo_fetcher.ingest import IngestClient
from eifo_fetcher.pipeline import CHUNK_SIZE, sync_source
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
    api: IngestClient,
    ctx: FetchContext,
    items: list[RawItem] | None = None,
    plugin: SourcePlugin | None = None,
) -> Any:
    return sync_source(
        api,
        plugin or StaticPlugin(items or []),
        INFO,
        ctx,
        items=items if plugin is None else None,
    )


@pytest.fixture
def session_factory(live_api: LiveApi) -> sessionmaker[Session]:
    """The catalog the API writes to, which is the only one a sync touches now.

    Overriding the package fixture rather than adding a second name: every
    assertion below is about what a sync left in the catalog, and there is now
    exactly one process that puts it there.
    """
    return live_api.session_factory


@pytest.fixture
def sync_ctx(http: Any, settings: Settings) -> FetchContext:
    return FetchContext(source_key=INFO.key, http=http, settings=settings)


class TestUpsert:
    def test_creates_the_source_and_its_availability(
        self, api: IngestClient, session: Session, sync_ctx: FetchContext
    ) -> None:
        result = run_sync(api, sync_ctx, [item("פאודה"), item("שטיסל")])

        assert result.status is FetchStatus.OK
        assert result.items_seen == 2
        assert result.availability_created == 2
        assert result.titles_created == 2
        assert session.scalar(select(Source).where(Source.key == INFO.key)) is not None

    def test_records_the_deep_link(
        self, api: IngestClient, session: Session, sync_ctx: FetchContext
    ) -> None:
        run_sync(api, sync_ctx, [item("פאודה", deep_link_url="https://x.example/f")])

        assert session.scalars(select(Availability)).one().deep_link_url == "https://x.example/f"

    def test_records_the_price_a_rent_offer_carries(
        self, api: IngestClient, session: Session, sync_ctx: FetchContext
    ) -> None:
        run_sync(
            api,
            sync_ctx,
            [item("פאודה", offer_type=OfferType.RENT, price_minor=1990, price_currency="ILS")],
        )

        availability = session.scalars(select(Availability)).one()
        assert (availability.price_minor, availability.price_currency) == (1990, "ILS")

    def test_an_offer_with_no_price_stores_none(
        self, api: IngestClient, session: Session, sync_ctx: FetchContext
    ) -> None:
        """A subscription offer costs nothing extra; it must not read as free."""
        run_sync(api, sync_ctx, [item("פאודה")])

        availability = session.scalars(select(Availability)).one()
        assert (availability.price_minor, availability.price_currency) == (None, None)

    def test_a_price_change_is_picked_up_on_the_next_sync(
        self, api: IngestClient, session: Session, sync_ctx: FetchContext
    ) -> None:
        rented = dict(offer_type=OfferType.RENT, price_currency="ILS")
        run_sync(api, sync_ctx, [item("פאודה", price_minor=1990, **rented)])
        run_sync(api, sync_ctx, [item("פאודה", price_minor=2450, **rented)])

        assert session.scalars(select(Availability)).one().price_minor == 2450

    def test_a_second_run_updates_rather_than_duplicates(
        self, api: IngestClient, session: Session, sync_ctx: FetchContext
    ) -> None:
        run_sync(api, sync_ctx, [item("פאודה")])
        result = run_sync(api, sync_ctx, [item("פאודה")])

        assert result.availability_created == 0
        assert result.availability_updated == 1
        assert len(session.scalars(select(Availability)).all()) == 1
        assert len(session.scalars(select(Title)).all()) == 1

    def test_separate_offer_types_are_separate_rows(
        self, api: IngestClient, session: Session, sync_ctx: FetchContext
    ) -> None:
        run_sync(
            api,
            sync_ctx,
            [item("פאודה", offer_type=OfferType.RENT), item("פאודה", offer_type=OfferType.BUY)],
        )

        assert len(session.scalars(select(Availability)).all()) == 2

    def test_stores_the_artwork_url_for_later(
        self, api: IngestClient, session: Session, sync_ctx: FetchContext
    ) -> None:
        run_sync(api, sync_ctx, [item("פאודה", poster_url="https://i.example/p.jpg")])

        assert session.scalars(select(Title)).one().poster_source_url == "https://i.example/p.jpg"


class TestSweep:
    def _first_availability(self, session: Session) -> Availability:
        return session.scalars(select(Availability)).one()

    def test_one_miss_only_strikes(
        self, api: IngestClient, session: Session, sync_ctx: FetchContext
    ) -> None:
        """A single flaky run must not retire a live catalog entry."""
        run_sync(api, sync_ctx, [item("פאודה")])
        run_sync(api, sync_ctx, [])

        availability = self._first_availability(session)
        assert availability.miss_count == 1
        assert availability.is_current is True
        assert availability.gone_since is None

    def test_two_misses_retire_it(
        self, api: IngestClient, session: Session, sync_ctx: FetchContext
    ) -> None:
        run_sync(api, sync_ctx, [item("פאודה")])
        for _ in range(MISS_LIMIT):
            run_sync(api, sync_ctx, [])

        availability = self._first_availability(session)
        assert availability.is_current is False
        assert availability.gone_since is not None

    def test_the_title_survives_retirement(
        self, api: IngestClient, session: Session, sync_ctx: FetchContext
    ) -> None:
        """Content that goes away is badged, never deleted."""
        run_sync(api, sync_ctx, [item("פאודה")])
        for _ in range(MISS_LIMIT):
            run_sync(api, sync_ctx, [])

        assert len(session.scalars(select(Title)).all()) == 1
        assert len(session.scalars(select(Availability)).all()) == 1

    def test_reappearing_clears_the_strikes(
        self, api: IngestClient, session: Session, sync_ctx: FetchContext
    ) -> None:
        run_sync(api, sync_ctx, [item("פאודה")])
        run_sync(api, sync_ctx, [])
        run_sync(api, sync_ctx, [item("פאודה")])

        availability = self._first_availability(session)
        assert availability.miss_count == 0
        assert availability.is_current is True

    def test_a_retired_entry_revives_when_it_returns(
        self, api: IngestClient, session: Session, sync_ctx: FetchContext
    ) -> None:
        run_sync(api, sync_ctx, [item("פאודה")])
        for _ in range(MISS_LIMIT):
            run_sync(api, sync_ctx, [])
        run_sync(api, sync_ctx, [item("פאודה")])

        availability = self._first_availability(session)
        assert availability.is_current is True
        assert availability.gone_since is None

    def test_a_failed_sync_never_sweeps(
        self, api: IngestClient, session: Session, sync_ctx: FetchContext
    ) -> None:
        """A scraper outage must not be mistaken for content disappearing."""
        run_sync(api, sync_ctx, [item("פאודה")])

        result = run_sync(api, sync_ctx, plugin=StaticPlugin(error=RuntimeError("site down")))

        assert result.status is FetchStatus.FAILED
        assert self._first_availability(session).miss_count == 0


class TestVolumeGuard:
    def _big_catalog(self, count: int) -> list[RawItem]:
        return [item(f"תוכנית {index}", year=2000 + index % 20) for index in range(count)]

    def test_a_collapse_in_volume_is_treated_as_a_broken_parser(
        self, api: IngestClient, session: Session, sync_ctx: FetchContext
    ) -> None:
        run_sync(api, sync_ctx, self._big_catalog(200))

        result = run_sync(api, sync_ctx, self._big_catalog(5))

        assert result.status is FetchStatus.ABORTED_SUSPICIOUS

    def test_a_suspicious_run_does_not_sweep(
        self, api: IngestClient, session: Session, sync_ctx: FetchContext
    ) -> None:
        run_sync(api, sync_ctx, self._big_catalog(200))
        run_sync(api, sync_ctx, self._big_catalog(5))

        untouched = session.scalars(select(Availability).where(Availability.miss_count > 0)).all()
        assert untouched == []

    def test_a_modest_drop_is_accepted_as_real(
        self, api: IngestClient, session: Session, sync_ctx: FetchContext
    ) -> None:
        run_sync(api, sync_ctx, self._big_catalog(200))

        result = run_sync(api, sync_ctx, self._big_catalog(150))

        assert result.status is FetchStatus.OK

    def test_small_catalogs_are_exempt(
        self, api: IngestClient, session: Session, sync_ctx: FetchContext
    ) -> None:
        """Below the floor the ratio is noise, not signal."""
        run_sync(api, sync_ctx, self._big_catalog(10))

        result = run_sync(api, sync_ctx, [])

        assert result.status is FetchStatus.OK

    def test_the_first_ever_run_is_never_suspicious(
        self, api: IngestClient, session: Session, sync_ctx: FetchContext
    ) -> None:
        assert run_sync(api, sync_ctx, [item("פאודה")]).status is FetchStatus.OK


class TestFetchRuns:
    def test_records_a_run_with_its_stats(
        self, api: IngestClient, session: Session, sync_ctx: FetchContext
    ) -> None:
        run_sync(api, sync_ctx, [item("פאודה"), item("שטיסל")])

        run = session.scalars(select(FetchRun)).one()
        assert run.source_key == INFO.key
        assert run.status is FetchStatus.OK
        assert run.stats["items_seen"] == 2
        assert run.stats["matched_by"] == {"created": 2}
        assert run.finished_at is not None

    def test_records_a_failure(
        self, api: IngestClient, session: Session, sync_ctx: FetchContext
    ) -> None:
        run_sync(api, sync_ctx, plugin=StaticPlugin(error=RuntimeError("boom")))

        assert session.scalars(select(FetchRun)).one().status is FetchStatus.FAILED

    def test_a_failure_records_what_went_wrong(
        self, api: IngestClient, session: Session, sync_ctx: FetchContext
    ) -> None:
        """A row saying only that it failed leaves the one question anyone will ask."""
        run_sync(api, sync_ctx, plugin=StaticPlugin(error=RuntimeError("browser blocked")))

        errors = session.scalars(select(FetchRun)).one().stats["errors"]
        assert errors == ["fatal: RuntimeError: browser blocked"]

    def test_an_exhausted_error_budget_says_so_too(
        self, api: IngestClient, session: Session, sync_ctx: FetchContext
    ) -> None:
        run_sync(
            api,
            sync_ctx,
            plugin=StaticPlugin(error=TooManyErrorsError(INFO.key, 25)),
        )

        errors = session.scalars(select(FetchRun)).one().stats["errors"]
        assert any("TooManyErrorsError" in entry for entry in errors)

    def test_one_row_per_sync_not_one_per_outcome(
        self, api: IngestClient, session: Session, sync_ctx: FetchContext
    ) -> None:
        """The row is opened at the start and closed at the end, not written twice."""
        run_sync(api, sync_ctx)

        assert len(list(session.scalars(select(FetchRun)).all())) == 1

    def test_an_exhausted_error_budget_fails_the_source(
        self, api: IngestClient, session: Session, sync_ctx: FetchContext
    ) -> None:
        error = TooManyErrorsError(INFO.key, 25)

        result = run_sync(api, sync_ctx, plugin=StaticPlugin(error=error))

        assert result.status is FetchStatus.FAILED


class TestSourceLifecycle:
    def test_reactivates_a_previously_retired_source(
        self, api: IngestClient, session: Session, sync_ctx: FetchContext
    ) -> None:
        run_sync(api, sync_ctx, [item("פאודה")])
        deactivate_missing_sources(session, [])
        session.commit()

        run_sync(api, sync_ctx, [item("פאודה")])

        source = session.scalar(select(Source).where(Source.key == INFO.key))
        assert source is not None
        assert source.active is True
        assert source.deactivated_at is None

    def test_retiring_a_source_keeps_its_data(
        self, api: IngestClient, session: Session, sync_ctx: FetchContext
    ) -> None:
        run_sync(api, sync_ctx, [item("פאודה")])

        retired = deactivate_missing_sources(session, [])
        session.commit()

        assert retired == [INFO.key]
        source = session.scalar(select(Source).where(Source.key == INFO.key))
        assert source is not None and source.active is False
        assert len(session.scalars(select(Availability)).all()) == 1
        assert len(session.scalars(select(Title)).all()) == 1

    def test_a_still_configured_source_is_left_alone(
        self, api: IngestClient, session: Session, sync_ctx: FetchContext
    ) -> None:
        run_sync(api, sync_ctx, [item("פאודה")])

        assert deactivate_missing_sources(session, [INFO.key]) == []


class TestRepeatedItems:
    """Sources repeat themselves; a sync must survive it.

    Paginated APIs return a title again when the underlying result set shifts
    between pages, and two listings can resolve to the same canonical title.
    """

    def test_the_same_title_twice_in_one_run(
        self, api: IngestClient, session: Session, sync_ctx: FetchContext
    ) -> None:
        result = run_sync(api, sync_ctx, [item("פאודה"), item("פאודה")])

        assert result.status is FetchStatus.OK
        assert len(session.scalars(select(Availability)).all()) == 1

    def test_a_repeat_counts_as_an_update_not_a_second_row(
        self, api: IngestClient, session: Session, sync_ctx: FetchContext
    ) -> None:
        result = run_sync(api, sync_ctx, [item("פאודה"), item("פאודה")])

        assert result.availability_created == 1
        assert result.availability_updated == 1

    def test_a_repeat_with_a_deep_link_keeps_the_link(
        self, api: IngestClient, session: Session, sync_ctx: FetchContext
    ) -> None:
        run_sync(
            api,
            sync_ctx,
            [item("פאודה"), item("פאודה", deep_link_url="https://x.example/f")],
        )

        assert session.scalars(select(Availability)).one().deep_link_url == "https://x.example/f"

    def test_repeats_across_offer_types_are_still_separate_rows(
        self, api: IngestClient, session: Session, sync_ctx: FetchContext
    ) -> None:
        run_sync(
            api,
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
        self, api: IngestClient, session: Session, sync_ctx: FetchContext
    ) -> None:
        """Without a rollback, recording the failure would itself raise."""

        class Exploding(SourcePlugin):
            def sources(self) -> list[SourceInfo]:
                return [INFO]

            def fetch(self, ctx: FetchContext) -> Iterator[RawItem]:
                yield item("פאודה")
                raise RuntimeError("connection reset mid-stream")

        result = run_sync(api, sync_ctx, plugin=Exploding())

        assert result.status is FetchStatus.FAILED
        run = session.scalars(select(FetchRun)).one()
        assert run.status is FetchStatus.FAILED


class TestSendingItInChunks:
    """A chunk is a request and a transaction, and that is what bounds the damage.

    SQLite allows one writer, so a whole source in one transaction held the write
    lock for minutes and locked out everything else. The lock is on the far side
    now, and the same rule governs it: the run arrives in chunks, each committed
    on its own, so what an interrupted sync loses is at most one of them.
    """

    def _distinct(self, count: int) -> list[RawItem]:
        """Items the matcher will keep separate.

        Generated names alone are not enough - "תוכנית 1" and "תוכנית 2" are
        near-identical, and the matcher is right to collapse them - so each
        item carries its own external id.
        """
        return [item(f"תוכנית {index}", tmdb_id=1000 + index) for index in range(count)]

    def test_a_long_source_arrives_as_several_chunks_rather_than_one(
        self, live_api: LiveApi, sync_ctx: FetchContext
    ) -> None:
        chunks: list[int] = []
        original = live_api.api.offer

        def counting_offer(run_id: int, entries: list[Any]) -> Any:
            chunks.append(len(entries))
            return original(run_id, entries)

        live_api.api.offer = counting_offer  # type: ignore[method-assign]

        run_sync(live_api.api, sync_ctx, self._distinct(CHUNK_SIZE * 2 + 10))

        assert len(chunks) == 3
        assert chunks == [CHUNK_SIZE, CHUNK_SIZE, 10]

    def test_arriving_in_chunks_does_not_change_the_outcome(
        self, api: IngestClient, session: Session, sync_ctx: FetchContext
    ) -> None:
        count = CHUNK_SIZE * 2 + 50

        result = run_sync(api, sync_ctx, self._distinct(count))

        assert result.status is FetchStatus.OK
        assert result.items_seen == count
        assert len(session.scalars(select(Availability)).all()) == count

    def test_what_the_last_chunk_could_not_do_does_not_undo_the_rest(
        self, live_api: LiveApi, sync_ctx: FetchContext
    ) -> None:
        """The point of a chunk being a transaction, stated as a test."""
        original = live_api.api.offer
        calls = {"n": 0}

        def failing_offer(run_id: int, entries: list[Any]) -> Any:
            calls["n"] += 1
            if calls["n"] > 1:
                raise RuntimeError("the connection dropped")
            return original(run_id, entries)

        live_api.api.offer = failing_offer  # type: ignore[method-assign]

        result = run_sync(live_api.api, sync_ctx, self._distinct(CHUNK_SIZE * 2))

        assert result.status is FetchStatus.FAILED
        with live_api.session() as session:
            assert len(session.scalars(select(Availability)).all()) == CHUNK_SIZE


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
        self, api: IngestClient, session: Session, sync_ctx: FetchContext
    ) -> None:
        """It is the year that is wrong, not the listing."""
        result = run_sync(api, sync_ctx, [item("מגלים את אמריקע", year=2999)])

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


LETTERS = "אבגדהוזחטיכלמנסעפצקרשת"


def unrelated_name(n: int) -> str:
    """A name nothing else this generates is near enough to be mistaken for.

    ``סדרה 1`` and ``סדרה 2`` are one character apart, which is well inside what
    the matcher treats as the same title under two spellings - so a list of them
    is a list of near-duplicates, and says nothing about counting.
    """
    digest = hashlib.sha1(str(n).encode()).hexdigest()[:10]
    return "".join(LETTERS[int(char, 16) % len(LETTERS)] for char in digest)


def latest_log(session: Session) -> str:
    run = session.scalars(select(FetchRun).order_by(FetchRun.id.desc())).first()
    assert run is not None
    return run.log or ""


class TestSayingHowFarItHasGot:
    """A catalog of twenty thousand listings is twenty minutes in which the only
    thing telling a working sync apart from a hung one is that it keeps saying
    where it has got to.

    Both sides say it now, and the row holds both. The fetcher says how much it
    has sent, because that is all it knows; the API says what each chunk turned
    into, because that is all *it* knows. Neither half is the whole answer, and
    before this the second half went to the service's own log where it was
    interleaved with every other request being served.
    """

    def test_a_long_sync_reports_on_the_way_through(
        self,
        api: IngestClient,
        session: Session,
        sync_ctx: FetchContext,
        fetcher_logs_at_info: None,
    ) -> None:
        result = run_sync(api, sync_ctx, [item(unrelated_name(n)) for n in range(250)])

        said = latest_log(session)
        assert result.items_seen == 250
        assert f"{CHUNK_SIZE} listing(s) sent" in said
        assert "250 listing(s) sent in total" in said

    def test_it_says_what_it_is_finding_and_not_only_how_much(
        self,
        api: IngestClient,
        session: Session,
        sync_ctx: FetchContext,
        fetcher_logs_at_info: None,
    ) -> None:
        """Whether any of it is new is the question somebody is watching to answer."""
        run_sync(api, sync_ctx, [item(unrelated_name(n)) for n in range(120)])

        assert "120 listing(s) in - 120 new title(s), 120 new offer(s)" in latest_log(session)

    def test_a_second_run_says_it_found_nothing_new(
        self,
        api: IngestClient,
        session: Session,
        sync_ctx: FetchContext,
        fetcher_logs_at_info: None,
    ) -> None:
        listings = [item(unrelated_name(n)) for n in range(120)]
        run_sync(api, sync_ctx, listings)
        run_sync(api, sync_ctx, listings)

        assert "120 listing(s) in - 120 already listed" in latest_log(session)

    def test_a_short_sync_still_proves_itself_alive(
        self,
        api: IngestClient,
        session: Session,
        sync_ctx: FetchContext,
        fetcher_logs_at_info: None,
    ) -> None:
        """A source with a dozen listings still leaves a row that says what it did."""
        run_sync(api, sync_ctx, [item(unrelated_name(n)) for n in range(12)])

        assert "12 listing(s) in - 12 new title(s)" in latest_log(session)

    def test_a_stretch_of_parked_listings_does_not_silence_it(
        self,
        api: IngestClient,
        session: Session,
        sync_ctx: FetchContext,
        fetcher_logs_at_info: None,
    ) -> None:
        """The run that most looks stuck is the one parking listing after listing."""
        # Near-identical names and a different year each: the matcher can neither
        # dismiss the resemblance nor accept it, which is what parking is for.
        listings = [item(f"סדרה {n}", year=1900 + n) for n in range(250)]

        result = run_sync(api, sync_ctx, listings)

        said = latest_log(session)
        assert result.matched_by.get("review", 0) > 100
        assert "review" in said
        assert "250 listing(s) sent in total" in said

    def test_the_row_holds_what_both_sides_said(
        self,
        api: IngestClient,
        session: Session,
        sync_ctx: FetchContext,
        fetcher_logs_at_info: None,
    ) -> None:
        """One row, the whole story - which is the point of appending here at all."""
        run_sync(api, sync_ctx, [item(unrelated_name(n)) for n in range(12)])

        said = latest_log(session)
        assert "listing(s) in" in said, "what the API decided"
        assert "listing(s) sent in total" in said, "what the fetcher reported"

    def test_this_sides_narration_is_bounded(
        self,
        api: IngestClient,
        session: Session,
        sync_ctx: FetchContext,
        fetcher_logs_at_info: None,
    ) -> None:
        """These rows are never deleted, so the budget is per run for ever."""
        run_sync(api, sync_ctx, [item(unrelated_name(n)) for n in range(1200)])

        server_lines = [
            line for line in latest_log(session).splitlines() if "listing(s) in" in line
        ]
        assert server_lines
        assert sum(len(line) for line in server_lines) < MAX_SERVER_LOG_CHARS * 2
