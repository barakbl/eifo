"""Title matching: normalisation, the strategy chain, and the review band."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from tvil_core.enums import TitleKind
from tvil_core.models import MatchReview, Title
from tvil_core.types import utcnow
from tvil_fetcher.match import (
    MatchMethod,
    TitleMatcher,
    is_hebrew,
    names_of,
    normalise,
    similarity,
    years_match,
)
from tvil_fetcher.sources.base import RawItem
from tvil_fetcher.tmdb import TmdbTitle


def item(**overrides: Any) -> RawItem:
    values: dict[str, Any] = {
        "source_key": "mako",
        "kind": TitleKind.SERIES,
        "name": "פאודה",
        "year": 2015,
    }
    values.update(overrides)
    return RawItem(**values)


class FakeTmdb:
    """Stands in for TmdbClient, returning canned search results."""

    def __init__(self, results: list[TmdbTitle] | None = None, *, fail: bool = False) -> None:
        self._results = results or []
        self._fail = fail
        self.queries: list[str] = []

    def search(self, kind: TitleKind, query: str, *, year: int | None = None) -> list[TmdbTitle]:
        self.queries.append(query)
        if self._fail:
            raise RuntimeError("TMDB is down")
        return self._results


def tmdb_title(**overrides: Any) -> TmdbTitle:
    values: dict[str, Any] = {
        "tmdb_id": 4321,
        "kind": TitleKind.SERIES,
        "name": "Fauda",
        "original_name": "פאודה",
        "year": 2015,
        "overview": "An undercover unit.",
        "poster_path": "/poster.jpg",
    }
    values.update(overrides)
    return TmdbTitle(**values)


class TestNormalise:
    @pytest.mark.parametrize(
        ("left", "right"),
        [
            ("The Crown", "crown"),
            ("Breaking  Bad", "breaking bad"),
            # An apostrophe joins words rather than separating them.
            ("Marvel's Daredevil", "Marvels Daredevil"),
            ("WALL·E", "wall e"),
        ],
    )
    def test_folds_away_meaningless_differences(self, left: str, right: str) -> None:
        assert normalise(left) == normalise(right)

    def test_strips_the_hebrew_definite_article(self) -> None:
        assert normalise("הקומדי סטור") == normalise("קומדי סטור")

    def test_keeps_distinct_titles_distinct(self) -> None:
        assert normalise("Fauda") != normalise("Fargo")

    def test_an_article_only_name_survives(self) -> None:
        assert normalise("The") == "the"


class TestSimilarity:
    def test_identical_names_score_full(self) -> None:
        assert similarity("Fauda", "fauda") == 100.0

    def test_near_names_score_high(self) -> None:
        assert similarity("Shtisel", "Shtissel") >= 90.0

    def test_unrelated_names_score_low(self) -> None:
        assert similarity("Fauda", "Tehran") < 50.0


class TestYearsMatch:
    def test_exact_years_match(self) -> None:
        assert years_match(2015, 2015)

    def test_one_year_apart_is_tolerated(self) -> None:
        assert years_match(2015, 2016)

    def test_far_apart_years_do_not_match(self) -> None:
        assert not years_match(2015, 2019)

    def test_a_missing_year_never_vetoes(self) -> None:
        assert years_match(None, 2015)
        assert years_match(2015, None)


class TestNamesOf:
    def test_splits_by_script(self) -> None:
        assert names_of(item(name="פאודה", name_alt="Fauda")) == ("פאודה", "Fauda")

    def test_order_does_not_matter(self) -> None:
        assert names_of(item(name="Fauda", name_alt="פאודה")) == ("פאודה", "Fauda")

    def test_a_single_latin_name_leaves_hebrew_empty(self) -> None:
        assert names_of(item(name="Fargo", name_alt=None)) == (None, "Fargo")

    def test_detects_hebrew(self) -> None:
        assert is_hebrew("פאודה")
        assert not is_hebrew("Fauda")


class TestExternalId:
    def test_matches_on_tmdb_id(self, session: Session) -> None:
        existing = Title(type=TitleKind.SERIES, name_en="Fauda", tmdb_id=99, year=2015)
        session.add(existing)
        session.flush()

        result = TitleMatcher(session).match(item(tmdb_id=99))

        assert result.method is MatchMethod.EXTERNAL_ID
        assert result.title is existing

    def test_matches_on_imdb_id(self, session: Session) -> None:
        existing = Title(type=TitleKind.SERIES, name_en="Fauda", imdb_id="tt4565380", year=2015)
        session.add(existing)
        session.flush()

        result = TitleMatcher(session).match(item(imdb_id="tt4565380"))

        assert result.method is MatchMethod.EXTERNAL_ID
        assert result.title is existing

    def test_an_unknown_tmdb_id_creates_a_title_carrying_it(self, session: Session) -> None:
        result = TitleMatcher(session).match(item(tmdb_id=1234))

        assert result.method is MatchMethod.TMDB
        assert result.title is not None
        assert result.title.tmdb_id == 1234


class TestTmdbLookup:
    def test_accepts_a_confident_hit(self, session: Session) -> None:
        tmdb = FakeTmdb([tmdb_title()])

        result = TitleMatcher(session, tmdb=tmdb).match(item(name="פאודה"))

        assert result.method is MatchMethod.TMDB
        assert result.title is not None
        assert result.title.tmdb_id == 4321
        assert result.title.name_he == "פאודה"
        assert result.title.name_en == "Fauda"

    def test_rejects_a_hit_from_the_wrong_year(self, session: Session) -> None:
        tmdb = FakeTmdb([tmdb_title(year=1998)])

        result = TitleMatcher(session, tmdb=tmdb).match(item(name="פאודה", year=2015))

        assert result.method is not MatchMethod.TMDB

    def test_rejects_a_hit_whose_name_is_unrelated(self, session: Session) -> None:
        tmdb = FakeTmdb([tmdb_title(name="Tehran", original_name="טהרן")])

        result = TitleMatcher(session, tmdb=tmdb).match(item(name="פאודה"))

        assert result.method is not MatchMethod.TMDB

    def test_folds_into_an_existing_title_with_the_same_tmdb_id(self, session: Session) -> None:
        existing = Title(type=TitleKind.SERIES, name_en="Fauda", tmdb_id=4321, year=2015)
        session.add(existing)
        session.flush()
        tmdb = FakeTmdb([tmdb_title()])

        result = TitleMatcher(session, tmdb=tmdb).match(item(name="פאודה"))

        assert result.title is existing
        assert session.scalars(select(Title)).all() == [existing]

    def test_a_tmdb_outage_does_not_fail_the_item(self, session: Session) -> None:
        result = TitleMatcher(session, tmdb=FakeTmdb(fail=True)).match(item())

        assert result.method is MatchMethod.CREATED
        assert result.title is not None


class TestLocalFuzzy:
    def test_matches_a_near_identical_stored_name(self, session: Session) -> None:
        existing = Title(type=TitleKind.SERIES, name_he="שטיסל", year=2013)
        session.add(existing)
        session.flush()

        result = TitleMatcher(session).match(item(name="שטיסל ", year=2013))

        assert result.method is MatchMethod.FUZZY
        assert result.title is existing

    def test_does_not_match_across_kinds(self, session: Session) -> None:
        session.add(Title(type=TitleKind.MOVIE, name_he="פאודה", year=2015))
        session.flush()

        result = TitleMatcher(session).match(item(kind=TitleKind.SERIES, name="פאודה"))

        assert result.method is MatchMethod.CREATED

    def test_does_not_match_a_different_year(self, session: Session) -> None:
        session.add(Title(type=TitleKind.SERIES, name_he="פאודה", year=1999))
        session.flush()

        result = TitleMatcher(session).match(item(name="פאודה", year=2015))

        assert result.method is MatchMethod.CREATED


class TestCreateAndReview:
    def test_creates_a_title_when_nothing_is_close(self, session: Session) -> None:
        result = TitleMatcher(session).match(item(name="תוכנית חדשה לגמרי", year=2026))

        assert result.method is MatchMethod.CREATED
        assert result.title is not None
        assert result.title.name_he == "תוכנית חדשה לגמרי"

    def test_creates_a_title_with_no_year(self, session: Session) -> None:
        """Mako's catalog carries no years; that must not block ingestion."""
        result = TitleMatcher(session).match(item(name="חתונה ממבט ראשון", year=None))

        assert result.method is MatchMethod.CREATED
        assert result.title is not None
        assert result.title.year is None

    def test_parks_an_ambiguous_near_miss(self, session: Session) -> None:
        """A season-numbered variant is the classic "a human should decide" case."""
        session.add(Title(type=TitleKind.SERIES, name_en="Srugim", year=2008))
        session.flush()

        result = TitleMatcher(session).match(item(name="Srugim 2", name_alt=None, year=2008))

        assert result.method is MatchMethod.REVIEW
        assert result.title is None

    def test_a_parked_item_records_what_it_nearly_matched(self, session: Session) -> None:
        existing = Title(type=TitleKind.SERIES, name_en="Srugim", year=2008)
        session.add(existing)
        session.flush()

        TitleMatcher(session).match(item(name="Srugim 2", year=2008))

        review = session.scalars(select(MatchReview)).one()
        assert review.source_key == "mako"
        assert review.raw_payload["name"] == "Srugim 2"
        assert review.candidates["closest"]["title_id"] == existing.id
        assert review.candidates["closest"]["similarity"] >= 75

    def test_stores_the_artwork_url_for_the_image_phase(self, session: Session) -> None:
        result = TitleMatcher(session).match(item(poster_url="https://img.example/p.jpg"))

        assert result.title is not None
        assert result.title.poster_source_url == "https://img.example/p.jpg"


class TestHonouringReviewDecisions:
    """A ruling in the review queue has to survive the next sync.

    Without this the queue regrows every night no matter how diligently it is
    worked, and the answer a human gave is silently discarded.
    """

    @staticmethod
    def _park(session: Session, existing_name: str = "Srugim") -> tuple[Title, MatchReview]:
        existing = Title(type=TitleKind.SERIES, name_en=existing_name, year=2008)
        session.add(existing)
        session.flush()

        TitleMatcher(session).match(item(name="Srugim 2", year=2008))
        return existing, session.scalars(select(MatchReview)).one()

    def test_a_resolved_item_attaches_to_the_chosen_title_next_sync(self, session: Session) -> None:
        existing, review = self._park(session)
        review.resolved_title_id = existing.id
        review.resolved_at = utcnow()
        session.flush()

        result = TitleMatcher(session).match(item(name="Srugim 2", year=2008))

        assert result.method is MatchMethod.RESOLVED
        assert result.title is existing

    def test_a_resolved_item_is_not_parked_again(self, session: Session) -> None:
        existing, review = self._park(session)
        review.resolved_title_id = existing.id
        review.resolved_at = utcnow()
        session.flush()

        TitleMatcher(session).match(item(name="Srugim 2", year=2008))

        assert len(session.scalars(select(MatchReview)).all()) == 1

    def test_a_skipped_item_becomes_a_title_of_its_own(self, session: Session) -> None:
        """Skipping means "not that one", so it stops being a near-miss."""
        _existing, review = self._park(session)
        review.resolved_at = utcnow()
        session.flush()

        result = TitleMatcher(session).match(item(name="Srugim 2", year=2008))

        assert result.method is MatchMethod.CREATED
        assert result.title is not None
        assert result.title.name_en == "Srugim 2"
        assert len(session.scalars(select(MatchReview)).all()) == 1

    def test_an_unresolved_review_does_not_count_as_a_decision(self, session: Session) -> None:
        """Still waiting for a human means still waiting."""
        self._park(session)

        result = TitleMatcher(session).match(item(name="Srugim 2", year=2008))

        assert result.method is MatchMethod.REVIEW

    def test_a_decision_about_another_item_is_not_applied(self, session: Session) -> None:
        existing, review = self._park(session)
        review.resolved_title_id = existing.id
        review.resolved_at = utcnow()
        session.flush()

        result = TitleMatcher(session).match(item(name="Srugim 3", year=2008))

        assert result.method is MatchMethod.REVIEW

    def test_a_decision_from_another_source_is_not_applied(self, session: Session) -> None:
        """Two services can list different things under the same name."""
        existing, review = self._park(session)
        review.resolved_title_id = existing.id
        review.resolved_at = utcnow()
        session.flush()

        result = TitleMatcher(session).match(
            item(source_key="disney_plus_il", name="Srugim 2", year=2008)
        )

        assert result.method is MatchMethod.REVIEW

    def test_a_resolution_to_a_since_deleted_title_is_ignored(self, session: Session) -> None:
        existing, review = self._park(session)
        review.resolved_title_id = existing.id
        review.resolved_at = utcnow()
        session.flush()
        session.delete(existing)
        session.flush()

        result = TitleMatcher(session).match(item(name="Srugim 2", year=2008))

        assert result.method is MatchMethod.CREATED


class TestReparkingIsIdempotent:
    """A re-sync must not pile up duplicate rows for an unresolved near-miss.

    The prior fix kept *resolved* decisions; this keeps the *open* queue from
    regrowing with duplicates every night, which would make it unworkable.
    """

    @staticmethod
    def _seed_ambiguous(session: Session) -> None:
        session.add(Title(type=TitleKind.SERIES, name_en="Srugim", year=2008))
        session.flush()

    def test_reparking_the_same_item_replaces_its_open_review(self, session: Session) -> None:
        self._seed_ambiguous(session)

        TitleMatcher(session).match(item(name="Srugim 2", year=2008))
        TitleMatcher(session).match(item(name="Srugim 2", year=2008))
        TitleMatcher(session).match(item(name="Srugim 2", year=2008))

        reviews = session.scalars(select(MatchReview)).all()
        assert len(reviews) == 1
        assert reviews[0].raw_payload["name"] == "Srugim 2"

    def test_it_heals_duplicates_a_previous_run_left(self, session: Session) -> None:
        """Two stale unresolved rows collapse to one on the next park."""
        self._seed_ambiguous(session)
        for _ in range(2):
            session.add(
                MatchReview(
                    source_key="mako",
                    raw_payload={"name": "Srugim 2", "kind": "series"},
                    candidates={},
                )
            )
        session.flush()

        TitleMatcher(session).match(item(name="Srugim 2", year=2008))

        assert len(session.scalars(select(MatchReview)).all()) == 1

    def test_a_resolved_review_is_not_replaced_by_reparking(self, session: Session) -> None:
        """Only open rows are swept; a human's decision must survive a re-sync."""
        self._seed_ambiguous(session)
        TitleMatcher(session).match(item(name="Srugim 2", year=2008))
        review = session.scalars(select(MatchReview)).one()
        review.resolved_at = utcnow()  # skipped
        session.flush()

        # Re-syncing now honours the skip (creates its own title) and leaves the
        # resolved row alone rather than deleting or duplicating it.
        result = TitleMatcher(session).match(item(name="Srugim 2", year=2008))

        assert result.method is MatchMethod.CREATED
        assert len(session.scalars(select(MatchReview)).all()) == 1
        assert session.scalars(select(MatchReview)).one().resolved_at is not None

    def test_a_different_source_keeps_its_own_open_review(self, session: Session) -> None:
        self._seed_ambiguous(session)
        TitleMatcher(session).match(item(name="Srugim 2", year=2008))
        TitleMatcher(session).match(item(source_key="disney_plus_il", name="Srugim 2", year=2008))

        assert len(session.scalars(select(MatchReview)).all()) == 2


class TestStats:
    def test_counts_each_method(self, session: Session) -> None:
        matcher = TitleMatcher(session)
        matcher.match(item(name="ראשון", year=2001))
        matcher.match(item(name="שני", year=2002))
        matcher.match(item(name="ראשון", year=2001))

        assert matcher.stats.as_dict() == {"created": 2, "fuzzy": 1}
