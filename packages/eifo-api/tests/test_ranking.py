"""What comes first, and why.

Every case here is the one that broke: searching ``הסנדק`` used to answer with
"הסנדקית מאמא גראס" and put The Godfather - the exact title typed, two million
votes - eighth and last, because ``bm25`` ranks by document length and the
document included both overviews. The film with the fullest synopsis was the
one the search buried.

So the fixture is that shelf, near enough: one exact title, its two sequels, a
film whose name merely starts with the same letters and has no synopsis at all,
and one that mentions it only in passing.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from seed import NOW, Seeded
from sqlalchemy.orm import Session, sessionmaker

from eifo_api.search import strip_article
from eifo_core.enums import OfferType, RatingProvider, TitleKind
from eifo_core.models import Availability, ExternalRating, Title

#: (name_he, name_en, year, imdb votes, overview length)
SHELF = [
    ("הסנדק", "The Godfather", 1972, 2_252_984, 600),
    ("הסנדק: חלק שני", "The Godfather Part II", 1974, 1_511_194, 500),
    ("הסנדק: חלק שלישי", "The Godfather Part III", 1990, 460_181, 400),
    # No votes and no synopsis: the shape bm25 used to reward above everything.
    ("הסנדקית מאמא גראס", None, 2020, None, 0),
    ("טעות בסנדק", "Jane Austen's Mafia!", 1998, 27_000, 300),
    ("הנשיא", "The President", 2020, 900, 0),
]


@pytest.fixture
def shelf(session_factory: sessionmaker[Session], catalog: Seeded) -> dict[str, int]:
    """The titles above, by Hebrew name.

    Each is put on a service, because a search answers with what can be
    watched: a title nobody carries is filtered out before ranking ever sees
    it, and a fixture of those would test an empty list very thoroughly.
    """
    ids: dict[str, int] = {}
    with session_factory() as session:
        for name_he, name_en, year, votes, overview in SHELF:
            title = Title(
                type=TitleKind.MOVIE,
                name_he=name_he,
                name_en=name_en,
                year=year,
                # Length is the whole point: it is what used to decide the order.
                overview_he="ג" * overview or None,
            )
            session.add(title)
            session.flush()
            ids[name_he] = title.id
            session.add(
                Availability(
                    title_id=title.id,
                    source_id=catalog.netflix,
                    offer_type=OfferType.STREAM,
                    first_seen=NOW,
                    last_seen=NOW,
                )
            )
            if votes is not None:
                session.add(
                    ExternalRating(
                        title_id=title.id,
                        provider=RatingProvider.IMDB,
                        score_raw=8.0,
                        score_normalized=80,
                        vote_count=votes,
                    )
                )
        session.commit()
    return ids


def names(client: TestClient, query: str, **params: str) -> list[str]:
    response = client.get("/api/v1/suggest", params={"q": query, **params})
    assert response.status_code == 200
    return [title["name_he"] or title["name_en"] for title in response.json()["titles"]]


class TestTheLadder:
    def test_the_exact_title_comes_first(self, client: TestClient, shelf: dict[str, int]) -> None:
        assert names(client, "הסנדק")[0] == "הסנדק"

    def test_then_the_titles_that_begin_with_it(
        self, client: TestClient, shelf: dict[str, int]
    ) -> None:
        """The sequels, in the order their audiences rank them."""
        assert names(client, "הסנדק")[:3] == [
            "הסנדק",
            "הסנדק: חלק שני",
            "הסנדק: חלק שלישי",
        ]

    def test_a_word_that_merely_starts_the_same_ranks_below_all_of_them(
        self, client: TestClient, shelf: dict[str, int]
    ) -> None:
        """הסנדקית is not הסנדק.

        The distinction is where the match ends: הסנדק is the whole of the
        first word of "הסנדק: חלק שני" and only the front of "הסנדקית מאמא
        גראס". This is the case that used to come first, on the strength of
        having no synopsis to be normalised against.
        """
        ranked = names(client, "הסנדק")

        assert ranked.index("הסנדקית מאמא גראס") > ranked.index("הסנדק: חלק שלישי")

    def test_a_match_inside_the_title_ranks_below_one_that_starts_it(
        self, client: TestClient, shelf: dict[str, int]
    ) -> None:
        ranked = names(client, "סנדק")

        assert ranked.index("הסנדק") < ranked.index("טעות בסנדק")

    def test_length_of_the_synopsis_decides_nothing(
        self, client: TestClient, shelf: dict[str, int]
    ) -> None:
        """The whole of the old bug, stated once.

        The exact title has the longest overview in the fixture and the one it
        used to lose to has none at all.
        """
        assert names(client, "הסנדק")[0] == "הסנדק"


class TestPopularityInsideARung:
    def test_two_titles_that_match_equally_are_ordered_by_fame(
        self, client: TestClient, shelf: dict[str, int]
    ) -> None:
        # Both begin with the query on a word boundary; 1.5M votes against 460k.
        ranked = names(client, "הסנדק:")

        assert ranked.index("הסנדק: חלק שני") < ranked.index("הסנדק: חלק שלישי")

    def test_fame_never_lifts_a_worse_match_past_a_better_one(
        self, client: TestClient, shelf: dict[str, int]
    ) -> None:
        """The promise of a strict ladder.

        Type a title in full and it is first, however obscure - which is the
        thing people notice when it is not true.
        """
        ranked = names(client, "הסנדקית מאמא גראס")

        assert ranked[0] == "הסנדקית מאמא גראס"


class TestEnglishNames:
    def test_a_leading_article_does_not_demote_a_title(
        self, client: TestClient, shelf: dict[str, int]
    ) -> None:
        """ "godfather" and "The Godfather" are the same request.

        Without this the second is a match in the middle of a name, and any
        obscure film actually called "Godfather" outranks it.
        """
        assert names(client, "godfather")[0] == "הסנדק"

    def test_typing_the_article_too_works_the_same_way(
        self, client: TestClient, shelf: dict[str, int]
    ) -> None:
        assert names(client, "the godfather")[0] == "הסנדק"

    def test_the_better_of_the_two_names_is_the_one_that_counts(
        self, client: TestClient, shelf: dict[str, int]
    ) -> None:
        """A title is one work with two names.

        Somebody who typed the English one should not be marked down for the
        Hebrew one being different.
        """
        assert names(client, "president")[0] == "הנשיא"


class TestHebrewPrefixes:
    def test_the_noun_finds_the_title_that_carries_the_article(
        self, client: TestClient, shelf: dict[str, int]
    ) -> None:
        """Hebrew glues ה to the front of the word.

        So הסנדק is one token and somebody typing the noun itself matched
        almost nothing - a prefix wildcard only grows a term at the end.
        """
        assert "הסנדק" in names(client, "סנדק")

    def test_the_article_still_finds_it_too(
        self, client: TestClient, shelf: dict[str, int]
    ) -> None:
        assert names(client, "הסנדק")[0] == "הסנדק"


class TestTheGridSortsTheSameWay:
    def _grid(self, client: TestClient, query: str, **params: str) -> list[str]:
        response = client.get(
            "/api/v1/titles",
            params={"q": query, "sort": "relevance", **params},
        )
        assert response.status_code == 200
        return [item["name_he"] or item["name_en"] for item in response.json()["items"]]

    def test_relevance_uses_the_ladder(self, client: TestClient, shelf: dict[str, int]) -> None:
        """One ranking, so the grid cannot disagree with the dropdown above it."""
        assert self._grid(client, "הסנדק")[0] == "הסנדק"

    def test_asking_for_the_opposite_order_gives_the_opposite_order(
        self, client: TestClient, shelf: dict[str, int]
    ) -> None:
        """Relevance is several columns pointing different ways.

        Reversing it means reversing all of them, and getting that wrong is
        invisible until somebody notices the grid opening on its worst match -
        which is exactly what a parameter called `descending` bought.
        """
        best = self._grid(client, "הסנדק")
        worst = self._grid(client, "הסנדק", order="desc")

        assert worst[0] != best[0]
        assert worst[0] == best[-1]


class TestStripArticle:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("The Godfather", "godfather"),
            ("the godfather", "godfather"),
            ("An Education", "education"),
            ("A Serious Man", "serious man"),
            ("Godfather", "godfather"),
            ("הסנדק", "הסנדק"),
            ("  The Thing  ", "thing"),
        ],
    )
    def test_names_meet_in_the_middle(self, name: str, expected: str) -> None:
        assert strip_article(name) == expected

    @pytest.mark.parametrize("name", ["The", "the", "A", "an"])
    def test_a_name_that_is_only_an_article_keeps_it(self, name: str) -> None:
        """ "The" is a title somebody may have meant.

        And an empty needle is worse than useless: instr() finds it at position
        one in every row in the catalog.
        """
        assert strip_article(name) == name.lower()
