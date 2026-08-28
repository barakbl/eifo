"""Title matching: normalisation, the strategy chain, and the review band."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from eifo_core.enums import OfferType, SourceKind, TitleKind
from eifo_core.models import Availability, MatchReview, Source, Title, TmdbAlias
from eifo_core.types import utcnow
from eifo_fetcher.match import (
    REVIEW_YEAR_TOLERANCE,
    MatchMethod,
    TitleMatcher,
    is_hebrew,
    latin_script,
    names_of,
    normalise,
    similarity,
    years_match,
)
from eifo_fetcher.sources.base import RawItem
from eifo_fetcher.tmdb import TmdbTitle


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

    def test_a_vowel_sign_is_a_letter_not_a_decoration(self) -> None:
        """In Malayalam, Devanagari and Tamil the vowel signs are combining marks.

        Stripping every combining character folded "ജോജി" (Joji) and "ജോ & ജോ"
        (Jo & Jo) to the same two consonants - two different films at a
        similarity of 100, saved from being merged only by their external ids.
        """
        assert normalise("ജോജി") != normalise("ജോ & ജോ")
        assert similarity("ജോജി", "ജോ & ജോ") < 90

    def test_latin_diacritics_are_still_folded(self) -> None:
        assert normalise("Amélie") == normalise("Amelie")

    def test_hebrew_pointing_is_still_folded(self) -> None:
        assert normalise("הַגּוֹבֶה") == normalise("הגובה")

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


class TestASourcesOwnIdForAListing:
    """Disney+ lists both Beauty and the Beast films as "Beauty And The Beast".

    No year, no id the matcher used, and the same slug. Matched on the name
    alone both listings landed on whichever title was reached first; the other
    was never seen and retired two syncs later as though it had left Disney+.
    The 1991 film went missing while its sing-along cut stayed.
    """

    def _bind(
        self,
        session: Session,
        title: Title,
        ref: str,
        *,
        source_key: str = "disney_plus_il",
    ) -> None:
        source = session.scalar(select(Source).where(Source.key == source_key))
        if source is None:
            source = Source(
                key=source_key,
                name=source_key,
                kind=SourceKind.SUBSCRIPTION,
                website_url=f"https://{source_key}.example",
            )
            session.add(source)
            session.flush()
        session.add(
            Availability(
                title_id=title.id,
                source_id=source.id,
                offer_type=OfferType.STREAM,
                source_ref=ref,
            )
        )
        session.flush()

    def _listing(self, ref: str) -> RawItem:
        """As Disney+ presents it: a slug-derived name, no year, and an id."""
        return item(
            source_key="disney_plus_il",
            kind=TitleKind.MOVIE,
            name="Beauty And The Beast",
            year=None,
            source_ref=ref,
        )

    def test_a_bound_listing_stays_bound(self, session: Session) -> None:
        """No name can overrule the catalogue's own answer about what a thing is."""
        film = Title(type=TitleKind.MOVIE, name_en="Beauty And The Beast", year=1991)
        session.add(film)
        session.flush()
        self._bind(session, film, "1260017283")

        result = TitleMatcher(session).match(self._listing("1260017283"))

        assert result.title is film

    def test_a_second_listing_does_not_take_the_first_ones_title(self, session: Session) -> None:
        """The bug: both landed on one title and the other retired unseen."""
        remake = Title(type=TitleKind.MOVIE, name_en="Beauty And The Beast", year=2017)
        session.add(remake)
        session.flush()
        self._bind(session, remake, "1260018151")

        result = TitleMatcher(session).match(self._listing("1260017283"))

        assert result.title is not remake

    def test_the_conflict_is_parked_rather_than_guessed(self, session: Session) -> None:
        """At most one of the two is right and the data cannot say which."""
        remake = Title(type=TitleKind.MOVIE, name_en="Beauty And The Beast", year=2017)
        session.add(remake)
        session.flush()
        self._bind(session, remake, "1260018151")

        TitleMatcher(session).match(self._listing("1260017283"))

        parked = session.scalars(select(MatchReview)).all()
        assert len(parked) == 1
        assert parked[0].raw_payload["name"] == "Beauty And The Beast"

    def test_a_source_publishing_no_id_is_unaffected(self, session: Session) -> None:
        """Most listings carry nothing to bind, and must match as they always did."""
        held = Title(type=TitleKind.SERIES, name_en="Fauda", tmdb_id=99, year=2015)
        session.add(held)
        session.flush()

        result = TitleMatcher(session).match(item(tmdb_id=99))

        assert result.title is held


class TestExternalId:
    def test_matches_on_tmdb_id(self, session: Session) -> None:
        existing = Title(type=TitleKind.SERIES, name_en="Fauda", tmdb_id=99, year=2015)
        session.add(existing)
        session.flush()

        result = TitleMatcher(session).match(item(tmdb_id=99))

        assert result.method is MatchMethod.EXTERNAL_ID
        assert result.title is existing

    def test_a_film_and_a_series_may_share_a_tmdb_id(self, session: Session) -> None:
        """TMDB numbers the two separately, so movie 105 (Back to the Future)
        and series 105 (Sex and the City) are different works.

        Matching on the number alone gave the film to the series: it was never
        created, and its rent-and-buy offers were filed against a show that has
        nothing to do with it.
        """
        series = Title(type=TitleKind.SERIES, name_en="Sex and the City", tmdb_id=105, year=1998)
        session.add(series)
        session.flush()

        result = TitleMatcher(session).match(
            item(kind=TitleKind.MOVIE, name="Back to the Future", year=1985, tmdb_id=105)
        )

        assert result.title is not series
        assert result.title.type is TitleKind.MOVIE

    def test_the_same_id_in_the_same_namespace_still_matches(self, session: Session) -> None:
        """The qualifier narrows the lookup; it does not break it."""
        film = Title(type=TitleKind.MOVIE, name_en="Back to the Future", tmdb_id=105, year=1985)
        session.add(film)
        session.flush()

        result = TitleMatcher(session).match(
            item(kind=TitleKind.MOVIE, name="Back to the Future", year=1985, tmdb_id=105)
        )

        assert result.method is MatchMethod.EXTERNAL_ID
        assert result.title is film

    def test_an_alias_does_not_reach_across_namespaces(self, session: Session) -> None:
        """An alias keyed on the bare number would shadow the other kind's id
        exactly as a title used to."""
        held = Title(type=TitleKind.SERIES, name_en="Some Show", tmdb_id=1, year=2010)
        session.add(held)
        session.flush()
        session.add(TmdbAlias(type=TitleKind.SERIES, tmdb_id=105, title_id=held.id))
        session.flush()

        result = TitleMatcher(session).match(
            item(kind=TitleKind.MOVIE, name="Back to the Future", year=1985, tmdb_id=105)
        )

        assert result.title is not held

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
        """A film is never silently folded into a series, or the other way round.

        It is asked about instead: one catalog filing a one-off documentary as a
        film while another files it as a series is the single largest family of
        duplicates in the deployed catalog, and neither answer is safe to guess.
        """
        session.add(Title(type=TitleKind.MOVIE, name_he="פאודה", year=2015))
        session.flush()

        result = TitleMatcher(session).match(item(kind=TitleKind.SERIES, name="פאודה"))

        assert result.title is None
        assert result.method is MatchMethod.REVIEW

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


class TestTmdbDuplicatesItsOwnRecords:
    """TMDB does carry the same work twice - and a name is not how you tell.

    This used to fold an unowned id into any stored title whose name matched
    after normalise(), which strips the leading article: "The Strays" and
    "Strays" are one string, and so are "An Intrusion" and "Intrusion". Sampled
    against IMDb ids, 39 of 40 aliases the rule had written joined two
    genuinely different films. The offer went to the wrong title with them, and
    an alias is permanent - the id resolves there every night afterwards.

    So the fold now needs the candidate to hold no id of its own, which is the
    rule the TMDB-search path a few lines below already applies. Two records
    that really are one work still merge; they merge in dedupe, which weighs
    more than a name and writes the alias itself as part of the merge.
    """

    def test_a_second_record_folds_into_a_title_holding_no_id(self, session: Session) -> None:
        """The case the alias was built for: we hold the work, TMDB named it twice."""
        held = Title(type=TitleKind.SERIES, name_en="The Pacific", year=2010)
        session.add(held)
        session.flush()

        result = TitleMatcher(session).match(item(name="The Pacific", year=2010, tmdb_id=327352))

        assert result.title is held
        assert result.method is MatchMethod.ALIAS
        assert session.scalars(select(Title)).all() == [held]

    def test_the_losing_id_is_remembered(self, session: Session) -> None:
        """Or the merge undoes itself: the feed offers that id again tomorrow."""
        held = Title(type=TitleKind.SERIES, name_en="The Pacific", year=2010)
        session.add(held)
        session.flush()
        TitleMatcher(session).match(item(name="The Pacific", year=2010, tmdb_id=327352))

        alias = session.get(TmdbAlias, (TitleKind.SERIES, 327352))
        assert alias is not None and alias.title_id == held.id

    def test_the_alias_resolves_on_the_next_sync(self, session: Session) -> None:
        held = Title(type=TitleKind.SERIES, name_en="The Pacific", year=2010)
        session.add(held)
        session.flush()
        matcher = TitleMatcher(session)
        matcher.match(item(name="The Pacific", year=2010, tmdb_id=327352))

        again = matcher.match(item(name="The Pacific", year=2010, tmdb_id=327352))

        assert again.title is held
        assert again.method is MatchMethod.ALIAS
        assert len(session.scalars(select(Title)).all()) == 1

    def test_a_title_that_already_has_an_id_is_not_folded_into(self, session: Session) -> None:
        """Two ids on two records is TMDB saying these are two works.

        "The Strays" (2023) was folded into "Strays" (2023) - a British thriller
        into a talking-dog comedy - because the article is what normalise()
        throws away.
        """
        comedy = Title(type=TitleKind.MOVIE, name_en="Strays", year=2023, tmdb_id=912908)
        session.add(comedy)
        session.flush()

        result = TitleMatcher(session).match(
            item(kind=TitleKind.MOVIE, name="The Strays", year=2023, tmdb_id=1063422)
        )

        assert result.title is not comedy
        assert result.method is MatchMethod.TMDB
        assert session.scalars(select(TmdbAlias)).all() == []

    def test_the_same_id_arriving_again_is_not_an_alias_of_itself(self, session: Session) -> None:
        """A title already holding the id is matched by it, not folded into."""
        held = Title(type=TitleKind.SERIES, name_en="The Pacific", year=2010, tmdb_id=16997)
        session.add(held)
        session.flush()

        result = TitleMatcher(session).match(item(name="The Pacific", year=2010, tmdb_id=16997))

        assert result.title is held
        assert result.method is MatchMethod.EXTERNAL_ID

    def test_a_merely_similar_name_is_left_as_its_own_title(self, session: Session) -> None:
        """An item carrying its own id is asserting an identity, and TMDB is usually right."""
        session.add(Title(type=TitleKind.SERIES, name_en="Love Island Ari", year=2024, tmdb_id=1))
        session.flush()

        result = TitleMatcher(session).match(item(name="Love Island Adel", year=2024, tmdb_id=2))

        assert result.method is MatchMethod.TMDB
        assert len(session.scalars(select(Title)).all()) == 2


class TestATitleWeAlreadyHoldWithoutAnId:
    """A local source creates a Hebrew title; a later one resolves it via TMDB."""

    def _held(self, session: Session) -> Title:
        held = Title(type=TitleKind.SERIES, name_he="יניב", year=2023)
        session.add(held)
        session.flush()
        return held

    def test_the_tmdb_hit_is_folded_into_it(self, session: Session) -> None:
        held = self._held(session)
        tmdb = FakeTmdb([tmdb_title(tmdb_id=331371, name="יניב", original_name="Yaniv", year=2023)])

        result = TitleMatcher(session, tmdb=tmdb).match(
            item(source_key="reshet13", name="יניב", year=2023)
        )

        assert result.title is held
        assert len(session.scalars(select(Title)).all()) == 1

    def test_it_gains_the_anchor_enrichment_needs(self, session: Session) -> None:
        """The id-less Hebrew listings are exactly the ones nothing could enrich."""
        held = self._held(session)
        tmdb = FakeTmdb([tmdb_title(tmdb_id=331371, name="יניב", original_name="Yaniv", year=2023)])

        TitleMatcher(session, tmdb=tmdb).match(item(source_key="reshet13", name="יניב", year=2023))

        assert held.tmdb_id == 331371

    def test_a_title_that_already_has_an_id_is_not_overwritten(self, session: Session) -> None:
        held = self._held(session)
        held.tmdb_id = 999
        session.flush()
        tmdb = FakeTmdb([tmdb_title(tmdb_id=331371, name="יניב", original_name="Yaniv", year=2023)])

        TitleMatcher(session, tmdb=tmdb).match(item(source_key="reshet13", name="יניב", year=2023))

        assert held.tmdb_id == 999


class TestYearsCatalogsDisagreeAbout:
    def test_tmdb_is_searched_again_without_the_year(self, session: Session) -> None:
        """Its year filter is exact, and a series is dated by whichever season a catalog carries."""
        tmdb = FakeTmdb([tmdb_title(tmdb_id=51157, name="חטופים", year=2010)])

        result = TitleMatcher(session, tmdb=tmdb).match(item(name="חטופים", year=2010))

        assert result.method is MatchMethod.TMDB
        assert tmdb.queries == ["חטופים"]

    def test_a_wide_year_gap_is_asked_about_rather_than_duplicated(self, session: Session) -> None:
        """חטופים is 2010 in one catalog and 2012 in another - the same show."""
        session.add(Title(type=TitleKind.SERIES, name_he="חטופים", year=2010))
        session.flush()

        result = TitleMatcher(session).match(item(name="חטופים", year=2012))

        assert result.title is None
        assert result.method is MatchMethod.REVIEW

    def test_a_gap_beyond_asking_about_is_simply_a_new_title(self, session: Session) -> None:
        session.add(Title(type=TitleKind.SERIES, name_he="חטופים", year=2010))
        session.flush()

        result = TitleMatcher(session).match(
            item(name="חטופים", year=2010 + REVIEW_YEAR_TOLERANCE + 5)
        )

        assert result.method is MatchMethod.CREATED

    def test_a_year_gap_is_never_enough_to_match_on(self, session: Session) -> None:
        """Widening the matching rule would make the year useless against remakes."""
        session.add(Title(type=TitleKind.SERIES, name_he="חטופים", year=2010))
        session.flush()

        result = TitleMatcher(session).match(item(name="חטופים", year=2012))

        assert result.method is not MatchMethod.FUZZY


class TestWhichColumnANameBelongsIn:
    """There is a Hebrew column and an English one, and nothing else."""

    # The dotless i is the point of that last one: Latin, and nowhere near ASCII.
    @pytest.mark.parametrize(
        "name",
        ["Fauda", "Amélie", "Cien años de soledad", "Canım Annem"],  # noqa: RUF001
    )
    def test_latin_names_are_english(self, name: str) -> None:
        """Accented Latin is an English-column name in every sense that matters here."""
        assert latin_script(name) is True

    @pytest.mark.parametrize(
        "name",
        ["千と千尋の神隠し", "פאודה", "दिलवाले दुल्हनिया ले जायेंगे", "லியோ", "어비스", "Улицы"],
    )
    def test_other_scripts_are_not(self, name: str) -> None:
        assert latin_script(name) is False

    @pytest.mark.parametrize("name", ["2046", "", "!!!"])
    def test_a_name_with_no_letters_is_not_english_either(self, name: str) -> None:
        assert latin_script(name) is False

    def test_a_japanese_name_is_not_filed_as_english(self, session: Session) -> None:
        """Calling anything non-Hebrew English is how Spirited Away became unfindable."""
        matcher = TitleMatcher(session)
        matcher.match(item(name="千と千尋の神隠し", name_alt="Spirited Away", year=2001))

        stored = session.scalars(select(Title)).one()
        assert stored.name_en == "Spirited Away"
        assert stored.name_he is None

    def test_a_title_with_only_a_third_script_name_still_gets_stored(
        self, session: Session
    ) -> None:
        """A row must carry a name; the enricher replaces it with a real English one."""
        TitleMatcher(session).match(item(name="千と千尋の神隠し", year=2001))

        stored = session.scalars(select(Title)).one()
        assert stored.name_en == "千と千尋の神隠し"

    def test_an_israeli_film_does_not_get_hebrew_in_both_columns(self, session: Session) -> None:
        """TMDB answers a Hebrew request in Hebrew and calls the original title English."""
        tmdb = FakeTmdb(
            [tmdb_title(tmdb_id=359314, name="ארץ פצועה", original_name="ארץ פצועה", year=2016)]
        )

        TitleMatcher(session, tmdb=tmdb).match(item(name="ארץ פצועה", year=2016))

        stored = session.scalars(select(Title)).one()
        assert stored.name_he == "ארץ פצועה"
        assert stored.name_en is None


class TestReadingPastDecoration:
    """The fallback that fires only after the plain ratio has given up.

    Sources decorate names - "Marvel Studios Thor Ragnarok", "Star Wars The
    Force Awakens Episode VII" - and the plain ratio prices that decoration at
    about twenty points, which left films everyone has heard of sitting in the
    catalog with no identity at all. Token scores can see past it, but they are
    generous, so every acceptance here carries a guard; measured against 2,059
    titles whose right answer was already known, the guards were the difference
    between 99.3% correct and no errors at all.
    """

    def _match(self, session: Session, tmdb: FakeTmdb, raw: RawItem) -> Any:
        return TitleMatcher(session, tmdb=tmdb).match(raw)

    def test_our_decoration_around_their_name_is_a_match(self, session: Session) -> None:
        tmdb = FakeTmdb(
            [
                tmdb_title(
                    tmdb_id=284053,
                    kind=TitleKind.MOVIE,
                    name="תור: ראגנארוק",
                    original_name="Thor: Ragnarok",
                    year=2017,
                )
            ]
        )

        result = self._match(
            session,
            tmdb,
            item(kind=TitleKind.MOVIE, name="Marvel Studios Thor Ragnarok", year=None),
        )

        assert result.method is MatchMethod.TMDB
        assert result.title is not None and result.title.tmdb_id == 284053

    def test_our_name_inside_theirs_is_not_one_on_its_own(self, session: Session) -> None:
        """ "Air Crash Investigation" is not its own spin-off.

        A fragment of a longer name is the untrustworthy direction: nothing
        says the extra words are decoration rather than a different work.
        """
        tmdb = FakeTmdb(
            [
                tmdb_title(
                    tmdb_id=120324,
                    name="Air Crash Investigation: Special Report",
                    original_name=None,
                    year=2018,
                )
            ]
        )

        result = self._match(session, tmdb, item(name="Air Crash Investigation", year=None))

        assert result.method is MatchMethod.CREATED
        assert result.title is not None and result.title.tmdb_id is None

    def test_unless_the_year_corroborates_it(self, session: Session) -> None:
        """A fragment plus an agreeing year is two independent signals."""
        tmdb = FakeTmdb(
            [
                tmdb_title(
                    tmdb_id=421920,
                    kind=TitleKind.MOVIE,
                    name="טבאלוגה: הסרט",
                    original_name="Tabaluga",
                    year=2018,
                )
            ]
        )

        result = self._match(session, tmdb, item(kind=TitleKind.MOVIE, name="טבאלוגה", year=2019))

        assert result.method is MatchMethod.TMDB
        assert result.title is not None and result.title.tmdb_id == 421920

    def test_two_qualifying_records_is_not_a_match(self, session: Session) -> None:
        """Both Dumbos qualify and nothing says which - guessing between a
        remake and its original is how a catalog quietly lies.

        The decoration is what routes this through the fallback at all: a bare
        "Dumbo" clears the plain bar and takes the first candidate, which is
        today's behaviour and deliberately untouched.
        """
        tmdb = FakeTmdb(
            [
                tmdb_title(
                    tmdb_id=11360,
                    kind=TitleKind.MOVIE,
                    name="דמבו",
                    original_name="Dumbo",
                    year=1941,
                ),
                tmdb_title(
                    tmdb_id=329996,
                    kind=TitleKind.MOVIE,
                    name="דמבו",
                    original_name="Dumbo",
                    year=2019,
                ),
            ]
        )

        result = self._match(
            session, tmdb, item(kind=TitleKind.MOVIE, name="Walt Disney Pictures Dumbo", year=None)
        )

        assert result.method is MatchMethod.CREATED
        assert result.title is not None and result.title.tmdb_id is None

    def test_an_acronym_gets_no_token_credit(self, session: Session) -> None:
        """ "T O T's" against "O.T.T." is a perfect token score and a different
        film; names of one-and-two-letter tokens score on the plain ratio only."""
        tmdb = FakeTmdb(
            [
                tmdb_title(
                    tmdb_id=99, kind=TitleKind.MOVIE, name="O.T.T.", original_name=None, year=1982
                )
            ]
        )

        result = self._match(session, tmdb, item(kind=TitleKind.MOVIE, name="T O T's", year=None))

        assert result.method is MatchMethod.CREATED
        assert result.title is not None and result.title.tmdb_id is None

    def test_the_plain_bar_still_answers_first(self, session: Session) -> None:
        """The fallback is additive: an exact name never reaches it."""
        tmdb = FakeTmdb([tmdb_title()])

        result = self._match(session, tmdb, item(name="פאודה", year=2015))

        assert result.method is MatchMethod.TMDB
        assert result.title is not None and result.title.tmdb_id == 4321
