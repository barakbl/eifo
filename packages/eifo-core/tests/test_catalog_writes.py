"""Writing a catalog down: sources, offers, the sweep, and the volume guard.

These are the operations that change what the catalog says, and they are now
called from the API on every ordinary sync. Two of them are the only thing
standing between a misbehaving scraper and a catalog that quietly empties, so
most of what is asserted here is restraint:

* **Two strikes** - an offer is retired only after it has been missing from two
  consecutive successful syncs, so one flaky read never expires a live listing.
* **The volume guard** - a run returning far less than the last believed one is
  a broken parser, not a service that has shed nine tenths of its catalog.

Deciding *which* title a listing means is :mod:`eifo_core.match`, tested
separately, and deliberately kept a separate question.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import replace
from typing import Any

import pytest
from factories import make_source, make_title
from sqlalchemy import select
from sqlalchemy.orm import Session

from eifo_core.catalog import (
    MISS_LIMIT,
    VOLUME_GUARD_MIN_ITEMS,
    clear_backfill_requests,
    deactivate_missing_sources,
    expire_reviews,
    looks_truncated,
    register_declared_sources,
    requested_backfills,
    source_overrides,
    sweep_source,
    title_count,
    upsert_availability,
    upsert_source,
)
from eifo_core.enums import FetchPhase, FetchStatus, OfferType, SourceKind, TitleKind
from eifo_core.items import RawItem, SourceInfo
from eifo_core.models import Availability, FetchRun, MatchReview, Source, Title
from eifo_core.types import utcnow

INFO = SourceInfo(
    key="cellcom_tv",
    name="Cellcom TV",
    kind=SourceKind.SUBSCRIPTION,
    website_url="https://cellcomtv.co.il",
)


def item(**overrides: Any) -> RawItem:
    values: dict[str, Any] = {
        "source_key": INFO.key,
        "kind": TitleKind.SERIES,
        "name": "פאודה",
        "offer_type": OfferType.STREAM,
    }
    values.update(overrides)
    return RawItem(**values)


def seeded(session: Session) -> tuple[Title, Source]:
    """One title and one source, both committed, ready to be offered."""
    title = make_title()
    source = make_source(key=INFO.key)
    session.add_all([title, source])
    session.commit()
    return title, source


def offer(
    session: Session, title: Title, source: Source, *, seen_at: dt.datetime, **kw: Any
) -> bool:
    created = upsert_availability(
        session, title=title, source=source, item=item(**kw), seen_at=seen_at
    )
    session.commit()
    return created


class TestUpsertSource:
    def test_writes_a_source_that_was_not_there(self, session: Session) -> None:
        source = upsert_source(session, INFO)
        session.commit()

        assert source.key == INFO.key
        assert source.website_url == INFO.website_url

    def test_refreshes_what_the_plugin_now_says(self, session: Session) -> None:
        """The plugin is the thing that knows; the row is only what it was told."""
        upsert_source(session, INFO)
        session.commit()

        upsert_source(session, replace(INFO, name="Cellcom TV+"))
        session.commit()

        assert session.scalars(select(Source)).one().name == "Cellcom TV+"

    def test_a_source_that_comes_back_is_active_again(self, session: Session) -> None:
        """A plugin reinstated in an upgrade should not stay badged as gone."""
        source = upsert_source(session, INFO)
        source.active = False
        source.deactivated_at = utcnow()
        session.commit()

        upsert_source(session, INFO)
        session.commit()

        assert source.active is True
        assert source.deactivated_at is None

    def test_a_logo_it_no_longer_declares_is_kept(self, session: Session) -> None:
        """None means "this plugin ships no mark", not "remove the one you have"."""
        upsert_source(session, replace(INFO, logo_path="sources/c.svg"))
        session.commit()

        upsert_source(session, INFO)
        session.commit()

        assert session.scalars(select(Source)).one().logo_path == "sources/c.svg"


class TestUpsertAvailability:
    def test_a_new_offer_is_created(self, session: Session) -> None:
        title, source = seeded(session)

        assert offer(session, title, source, seen_at=utcnow()) is True
        assert len(session.scalars(select(Availability)).all()) == 1

    def test_the_same_offer_again_is_an_update(self, session: Session) -> None:
        title, source = seeded(session)
        offer(session, title, source, seen_at=utcnow())

        assert offer(session, title, source, seen_at=utcnow()) is False
        assert len(session.scalars(select(Availability)).all()) == 1

    def test_two_offer_types_are_two_rows(self, session: Session) -> None:
        """Rented and streamed are different things to be told about."""
        title, source = seeded(session)
        offer(session, title, source, seen_at=utcnow())
        offer(
            session,
            title,
            source,
            seen_at=utcnow(),
            offer_type=OfferType.RENT,
            price_minor=1990,
            price_currency="ILS",
        )

        assert len(session.scalars(select(Availability)).all()) == 2

    def test_a_price_change_is_carried_forward(self, session: Session) -> None:
        title, source = seeded(session)
        rented: dict[str, Any] = {"offer_type": OfferType.RENT, "price_currency": "ILS"}
        offer(session, title, source, seen_at=utcnow(), price_minor=1990, **rented)

        offer(session, title, source, seen_at=utcnow(), price_minor=2450, **rented)

        assert session.scalars(select(Availability)).one().price_minor == 2450


class TestTheSweep:
    """One flaky read must never expire a live listing."""

    def _missed(self, session: Session, times: int) -> Availability:
        title, source = seeded(session)
        offer(session, title, source, seen_at=utcnow() - dt.timedelta(days=10))
        for _ in range(times):
            sweep_source(session, source, run_started_at=utcnow())
            session.commit()
        return session.scalars(select(Availability)).one()

    def test_one_miss_only_strikes(self, session: Session) -> None:
        row = self._missed(session, 1)

        assert row.miss_count == 1
        assert row.is_current is True

    def test_the_second_miss_retires_it(self, session: Session) -> None:
        row = self._missed(session, MISS_LIMIT)

        assert row.is_current is False
        assert row.gone_since is not None

    def test_it_reports_how_many_it_retired(self, session: Session) -> None:
        title, source = seeded(session)
        offer(session, title, source, seen_at=utcnow() - dt.timedelta(days=10))
        sweep_source(session, source, run_started_at=utcnow())

        assert sweep_source(session, source, run_started_at=utcnow()) == 1

    def test_an_offer_this_run_saw_is_untouched(self, session: Session) -> None:
        title, source = seeded(session)
        began = utcnow() - dt.timedelta(minutes=5)
        offer(session, title, source, seen_at=utcnow())

        assert sweep_source(session, source, run_started_at=began) == 0
        assert session.scalars(select(Availability)).one().miss_count == 0

    def test_the_title_survives_its_offer_being_retired(self, session: Session) -> None:
        """A film nobody streams any more is still a film the catalog knows about."""
        self._missed(session, MISS_LIMIT)

        assert title_count(session) == 1

    def test_another_sources_offers_are_not_swept(self, session: Session) -> None:
        title, source = seeded(session)
        other = make_source(key="mako", name="Mako", kind=SourceKind.FREE)
        session.add(other)
        session.commit()
        offer(session, title, other, seen_at=utcnow() - dt.timedelta(days=10))

        assert sweep_source(session, source, run_started_at=utcnow()) == 0
        assert session.scalars(select(Availability)).one().miss_count == 0


class TestExpiringParkedListings:
    """Attention is the scarce thing in a review queue, so do not spend it on a
    listing the source has stopped carrying."""

    def _park(self, session: Session, *, created_at: dt.datetime, resolved: bool = False) -> None:
        session.add(
            MatchReview(
                source_key=INFO.key,
                raw_payload={"name": "פאודה"},
                created_at=created_at,
                resolved_at=utcnow() if resolved else None,
            )
        )
        session.commit()

    def test_a_park_older_than_this_run_is_dropped(self, session: Session) -> None:
        self._park(session, created_at=utcnow() - dt.timedelta(days=2))

        assert expire_reviews(session, INFO.key, before=utcnow()) == 1
        session.commit()
        assert session.scalars(select(MatchReview)).all() == []

    def test_a_park_this_run_rewrote_is_kept(self, session: Session) -> None:
        """A sync deletes and rewrites the park each time it sees the item again."""
        began = utcnow() - dt.timedelta(minutes=5)
        self._park(session, created_at=utcnow())

        assert expire_reviews(session, INFO.key, before=began) == 0

    def test_one_somebody_has_already_ruled_on_is_left_alone(self, session: Session) -> None:
        self._park(session, created_at=utcnow() - dt.timedelta(days=2), resolved=True)

        assert expire_reviews(session, INFO.key, before=utcnow()) == 0

    def test_another_sources_parks_are_left_alone(self, session: Session) -> None:
        self._park(session, created_at=utcnow() - dt.timedelta(days=2))

        assert expire_reviews(session, "mako", before=utcnow()) == 0


class TestTheVolumeGuard:
    """A parser that has broken looks exactly like a service that has emptied,
    and only one of those should be believed."""

    def _previous(self, session: Session, items_seen: int, **overrides: Any) -> None:
        values: dict[str, Any] = {
            "source_key": INFO.key,
            "phase": FetchPhase.SYNC,
            "status": FetchStatus.OK,
            "started_at": utcnow() - dt.timedelta(days=1),
            "finished_at": utcnow() - dt.timedelta(days=1),
            "stats": {"items_seen": items_seen},
        }
        values.update(overrides)
        session.add(FetchRun(**values))
        session.commit()

    def test_a_first_run_is_always_believed(self, session: Session) -> None:
        """There is nothing to compare it against, and it is how a catalog starts."""
        assert looks_truncated(session, INFO.key, 3) is False

    def test_a_collapse_is_not_believed(self, session: Session) -> None:
        self._previous(session, 200)

        assert looks_truncated(session, INFO.key, 5) is True

    def test_an_ordinary_shrink_is(self, session: Session) -> None:
        """Catalogs do lose titles; the guard is about the difference in scale."""
        self._previous(session, 200)

        assert looks_truncated(session, INFO.key, 150) is False

    def test_a_small_source_is_never_judged_by_this(self, session: Session) -> None:
        """Nine listings down to four says nothing at all."""
        self._previous(session, VOLUME_GUARD_MIN_ITEMS - 1)

        assert looks_truncated(session, INFO.key, 0) is False

    def test_it_compares_against_the_last_believed_run(self, session: Session) -> None:
        """A failed run is not a baseline: its total is whatever it got to."""
        self._previous(session, 200)
        self._previous(session, 2, status=FetchStatus.FAILED, started_at=utcnow())

        assert looks_truncated(session, INFO.key, 5) is True

    def test_another_sources_runs_are_not_the_baseline(self, session: Session) -> None:
        self._previous(session, 200)

        assert looks_truncated(session, "mako", 1) is False


class TestDeclaringWhatThePluginsHave:
    """A source used to exist only once it had synced, which made the operator's
    list a list of sources that had already run - so one switched off, or added
    in an upgrade, was invisible on the very screen for switching it on."""

    def _declared(self) -> dict[str, SourceInfo]:
        return {
            "on_one": replace(INFO, key="on_one", name="On"),
            "off_one": replace(INFO, key="off_one", name="Off"),
        }

    def test_every_declared_source_gets_a_row(self, session: Session) -> None:
        written = register_declared_sources(session, self._declared(), enabled=["on_one"])
        session.commit()

        assert sorted(written) == ["off_one", "on_one"]
        assert {source.key for source in session.scalars(select(Source)).all()} == {
            "on_one",
            "off_one",
        }

    def test_one_that_is_switched_off_starts_inactive(self, session: Session) -> None:
        """Which is what it is: declared, known, not currently collected."""
        register_declared_sources(session, self._declared(), enabled=["on_one"])
        session.commit()

        stored = {source.key: source for source in session.scalars(select(Source)).all()}
        assert stored["on_one"].active is True
        assert stored["off_one"].active is False
        assert stored["off_one"].deactivated_at is not None

    def test_it_does_not_touch_a_source_that_already_exists(self, session: Session) -> None:
        """The operator's own edits are not something a declaration overwrites."""
        session.add(make_source(key="on_one", name="Renamed by hand", active=False))
        session.commit()

        written = register_declared_sources(session, self._declared(), enabled=["on_one"])
        session.commit()

        assert written == ["off_one"]
        existing = session.scalars(select(Source).where(Source.key == "on_one")).one()
        assert existing.name == "Renamed by hand"
        assert existing.active is False

    def test_a_plugin_changing_its_own_default_says_so(self, session: Session) -> None:
        """The API reads that column and cannot ask the plugin directly."""
        register_declared_sources(session, self._declared(), enabled=[])
        session.commit()

        changed = {
            key: replace(info, default_enabled=False) for key, info in self._declared().items()
        }
        register_declared_sources(session, changed, enabled=[])
        session.commit()

        assert all(row.default_enabled is False for row in session.scalars(select(Source)).all())

    def test_running_it_twice_writes_nothing_the_second_time(self, session: Session) -> None:
        register_declared_sources(session, self._declared(), enabled=["on_one"])
        session.commit()

        assert register_declared_sources(session, self._declared(), enabled=["on_one"]) == []
        assert len(session.scalars(select(Source)).all()) == 2


class TestOperatorSwitches:
    def test_only_the_rows_carrying_an_answer_are_reported(self, session: Session) -> None:
        """A NULL is the absence of an answer, and means the config file decides."""
        session.add_all(
            [
                make_source(key="said_on", enabled=True),
                make_source(key="said_off", enabled=False),
                make_source(key="said_nothing"),
            ]
        )
        session.commit()

        assert source_overrides(session) == {"said_on": True, "said_off": False}

    def test_nothing_set_is_an_empty_answer_not_a_missing_one(self, session: Session) -> None:
        session.add(make_source())
        session.commit()

        assert source_overrides(session) == {}


class TestBackfillRequests:
    def _ask(self, session: Session, key: str, *, at: dt.datetime) -> None:
        session.add(make_source(key=key, backfill_requested_at=at))
        session.commit()

    def test_the_oldest_ask_comes_first(self, session: Session) -> None:
        now = utcnow()
        self._ask(session, "second", at=now)
        self._ask(session, "first", at=now - dt.timedelta(minutes=1))

        assert requested_backfills(session) == ["first", "second"]

    def test_a_source_nobody_asked_about_is_not_listed(self, session: Session) -> None:
        session.add(make_source())
        session.commit()

        assert requested_backfills(session) == []

    def test_clearing_answers_the_ask(self, session: Session) -> None:
        self._ask(session, "beta", at=utcnow())

        clear_backfill_requests(session, ["beta"])
        session.commit()

        assert requested_backfills(session) == []

    def test_it_clears_only_what_it_was_given(self, session: Session) -> None:
        """A sync of one source has answered nothing about the others."""
        self._ask(session, "alpha", at=utcnow())
        self._ask(session, "beta", at=utcnow())

        clear_backfill_requests(session, ["beta"])
        session.commit()

        assert requested_backfills(session) == ["alpha"]

    def test_clearing_nothing_is_not_an_error(self, session: Session) -> None:
        clear_backfill_requests(session, [])

        assert requested_backfills(session) == []


class TestRetiringSources:
    def test_one_nobody_declares_any_more_is_retired(self, session: Session) -> None:
        session.add(make_source())
        session.commit()

        retired = deactivate_missing_sources(session, [])
        session.commit()

        assert retired == [INFO.key]
        assert session.scalars(select(Source)).one().active is False

    def test_retiring_keeps_its_data(self, session: Session) -> None:
        """The UI badges it "no longer tracked" rather than pretending it never was."""
        title, source = seeded(session)
        offer(session, title, source, seen_at=utcnow())

        deactivate_missing_sources(session, [])
        session.commit()

        assert title_count(session) == 1
        assert len(session.scalars(select(Availability)).all()) == 1

    def test_one_still_declared_is_left_alone(self, session: Session) -> None:
        session.add(make_source())
        session.commit()

        assert deactivate_missing_sources(session, [INFO.key]) == []

    def test_one_already_retired_is_not_reported_twice(self, session: Session) -> None:
        session.add(make_source(active=False))
        session.commit()

        assert deactivate_missing_sources(session, []) == []


class TestTitleCount:
    def test_an_empty_catalog_counts_nothing(self, session: Session) -> None:
        assert title_count(session) == 0

    @pytest.mark.parametrize("many", [1, 3])
    def test_it_counts_what_is_there(self, session: Session, many: int) -> None:
        session.add_all([make_title(name_he=f"סרט {n}") for n in range(many)])
        session.commit()

        assert title_count(session) == many
