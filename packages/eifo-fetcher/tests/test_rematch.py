"""The rematch backfill: adopt, fold, or say why not - and never guess."""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from eifo_core.enums import EnrichOutcome, OfferType, SourceKind, TitleKind
from eifo_core.models import Availability, EnrichAttempt, Source, Title
from eifo_fetcher.rematch import apply_rematch, plan_rematch
from eifo_fetcher.tmdb import TmdbTitle

NOW = dt.datetime(2026, 8, 26, 3, 0, tzinfo=dt.UTC)


class StubTmdb:
    """Canned search results per query, and a record of what was asked."""

    def __init__(self, by_query: dict[str, list[TmdbTitle]] | None = None, *, fail: bool = False):
        self._by_query = by_query or {}
        self._fail = fail
        self.queries: list[str] = []

    def search(self, kind: TitleKind, query: str, *, year: int | None = None) -> list[TmdbTitle]:
        self.queries.append(query)
        if self._fail:
            raise RuntimeError("TMDB is down")
        return self._by_query.get(query, [])


def hit(**overrides: Any) -> TmdbTitle:
    values: dict[str, Any] = {
        "tmdb_id": 284053,
        "kind": TitleKind.MOVIE,
        "name": "תור: ראגנארוק",
        "original_name": "Thor: Ragnarok",
        "year": 2017,
        "overview": None,
        "poster_path": None,
    }
    values.update(overrides)
    return TmdbTitle(**values)


def add_title(session: Session, **overrides: Any) -> Title:
    values: dict[str, Any] = {"type": TitleKind.MOVIE, "name_en": "Marvel Studios Thor Ragnarok"}
    values.update(overrides)
    title = Title(**values)
    session.add(title)
    session.flush()
    return title


def add_source(session: Session, key: str = "disney_plus_il") -> Source:
    source = Source(
        key=key, name=key, kind=SourceKind.SUBSCRIPTION, website_url=f"https://{key}.example"
    )
    session.add(source)
    session.flush()
    return source


def offer(session: Session, title: Title, source: Source) -> Availability:
    row = Availability(
        title_id=title.id,
        source_id=source.id,
        offer_type=OfferType.STREAM,
        first_seen=NOW,
        last_seen=NOW,
        is_current=True,
        miss_count=0,
    )
    session.add(row)
    session.flush()
    return row


class TestPlanning:
    def test_a_confident_match_nobody_owns_is_an_adoption(self, session: Session) -> None:
        add_title(session)
        plan = plan_rematch(session, StubTmdb({"Marvel Studios Thor Ragnarok": [hit()]}))

        assert len(plan.adoptions) == 1
        assert plan.adoptions[0].hit.tmdb_id == 284053
        assert not plan.folds and not plan.ambiguous

    def test_a_match_somebody_owns_is_a_fold(self, session: Session) -> None:
        """The unmatched row was a second copy of a title all along."""
        owner = add_title(session, name_en="Thor: Ragnarok", tmdb_id=284053, year=2017)
        duplicate = add_title(session)

        plan = plan_rematch(session, StubTmdb({"Marvel Studios Thor Ragnarok": [hit()]}))

        assert len(plan.folds) == 1
        assert plan.folds[0].owner.id == owner.id
        assert plan.folds[0].duplicate.id == duplicate.id
        assert not plan.adoptions

    def test_two_qualifying_records_are_named_not_guessed_at(self, session: Session) -> None:
        add_title(session, name_en="Walt Disney Pictures Dumbo")
        results = [
            hit(tmdb_id=11360, name="דמבו", original_name="Dumbo", year=1941),
            hit(tmdb_id=329996, name="דמבו", original_name="Dumbo", year=2019),
        ]
        plan = plan_rematch(session, StubTmdb({"Walt Disney Pictures Dumbo": results}))

        assert len(plan.ambiguous) == 1
        assert {h.tmdb_id for h in plan.ambiguous[0][1]} == {11360, 329996}
        assert not plan.adoptions and not plan.folds

    def test_a_junk_name_is_never_even_searched(self, session: Session) -> None:
        """A sing-along named after its film matches that film perfectly."""
        add_title(session, name_en="Rotten To The Core Sing Along Descendants Music Video")
        tmdb = StubTmdb()

        plan = plan_rematch(session, tmdb)

        assert plan.junk_skipped == 1
        assert tmdb.queries == []

    def test_no_acceptable_hit_is_counted_and_left(self, session: Session) -> None:
        add_title(session, name_en="תוכנית בוקר מקומית")
        plan = plan_rematch(session, StubTmdb())

        assert plan.unmatched == 1
        assert not plan.adoptions and not plan.folds and not plan.ambiguous

    def test_a_search_failure_is_recorded_and_the_pass_continues(self, session: Session) -> None:
        add_title(session, name_en="First")
        add_title(session, name_en="Second")
        plan = plan_rematch(session, StubTmdb(fail=True))

        assert len(plan.errors) == 2

    def test_titles_with_an_identity_are_not_revisited(self, session: Session) -> None:
        add_title(session, name_en="Already Matched", tmdb_id=7)
        add_title(session, name_en="Already On Imdb", imdb_id="tt1")
        tmdb = StubTmdb()

        plan = plan_rematch(session, tmdb)

        assert tmdb.queries == []
        assert plan.unmatched == 0


class TestApplying:
    def test_an_adoption_sets_the_id_and_resets_the_enrich_backoff(self, session: Session) -> None:
        """The queue backed these titles off because they yielded nothing -
        and they yielded nothing because they had no identity. With one, the
        next pass must visit them now, not in however many months."""
        title = add_title(session)
        session.add(
            EnrichAttempt(
                title_id=title.id,
                attempted_at=NOW,
                outcome=EnrichOutcome.NO_MATCH,
                fruitless=4,
                due_at=NOW + dt.timedelta(days=240),
            )
        )
        session.flush()
        plan = plan_rematch(session, StubTmdb({"Marvel Studios Thor Ragnarok": [hit()]}))

        apply_rematch(session, plan)

        assert title.tmdb_id == 284053
        assert title.name_he == "תור: ראגנארוק"
        assert title.year == 2017
        assert session.scalars(select(EnrichAttempt)).all() == []

    def test_a_fold_moves_the_offer_and_removes_the_duplicate(self, session: Session) -> None:
        source_a, source_b = add_source(session, "apple_tv_store"), add_source(session)
        owner = add_title(session, name_en="Thor: Ragnarok", tmdb_id=284053, year=2017)
        duplicate = add_title(session)
        offer(session, owner, source_a)
        offer(session, duplicate, source_b)
        plan = plan_rematch(session, StubTmdb({"Marvel Studios Thor Ragnarok": [hit()]}))

        tally = apply_rematch(session, plan)

        assert tally.groups == 1
        assert session.get(Title, duplicate.id) is None
        owned = session.scalars(select(Availability).where(Availability.title_id == owner.id)).all()
        assert {row.source_id for row in owned} == {source_a.id, source_b.id}

    def test_an_ambiguous_title_is_left_exactly_as_it_was(self, session: Session) -> None:
        title = add_title(session, name_en="Walt Disney Pictures Dumbo")
        results = [
            hit(tmdb_id=11360, name="Dumbo", original_name=None, year=1941),
            hit(tmdb_id=329996, name="Dumbo", original_name=None, year=2019),
        ]
        plan = plan_rematch(session, StubTmdb({"Walt Disney Pictures Dumbo": results}))

        apply_rematch(session, plan)

        assert title.tmdb_id is None
