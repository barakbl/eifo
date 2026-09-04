"""Scores gathered by the service that reported them.

Two providers report two figures each - Rotten Tomatoes measures critics and
the crowd, Seret does the same - and as a chip apiece they read as two sites
disagreeing rather than as one site having measured two things. On a page whose
whole business is telling raters apart, that is the wrong claim to make.

None of the naming is decided here any more. The fetcher writes what each
plugin declares into ``rating_providers`` and this reads it, so these tests are
about what the API does with a table it did not write - including when the
table has nothing to say.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from seed import Seeded
from sqlalchemy import delete
from sqlalchemy.orm import Session, sessionmaker

from eifo_core.enums import RatingProvider
from eifo_core.models import ExternalRating, RatingProviderInfo

RT_URL = "https://www.rottentomatoes.com/m/foxtrot_2018"


@pytest.fixture
def both_rt_scores(session_factory: sessionmaker[Session], catalog: Seeded) -> None:
    """Give Foxtrot the audience score to go beside its Tomatometer."""
    with session_factory() as session:
        session.add(
            ExternalRating(
                title_id=catalog.foxtrot,
                provider=RatingProvider.RT_AUDIENCE,
                score_raw=78.0,
                score_normalized=78,
                vote_count=1520,
                url=RT_URL,
            )
        )
        session.commit()


def groups(client: TestClient, title_id: int) -> dict[str, dict]:
    body = client.get(f"/api/v1/titles/{title_id}").json()
    return {group["key"]: group for group in body["rating_groups"]}


class TestOneChipPerService:
    def test_two_figures_from_one_service_are_one_group(
        self, client: TestClient, catalog: Seeded, both_rt_scores: None
    ) -> None:
        rt = groups(client, catalog.foxtrot)["rt"]

        assert rt["name"] == "Rotten Tomatoes"
        assert [score["provider"] for score in rt["scores"]] == ["rt_critics", "rt_audience"]

    def test_each_figure_keeps_its_own_name_and_scale(
        self, client: TestClient, catalog: Seeded, both_rt_scores: None
    ) -> None:
        critics, audience = groups(client, catalog.foxtrot)["rt"]["scores"]

        assert (critics["provider_name"], critics["score_display"]) == ("Tomatometer", "94%")
        assert (audience["provider_name"], audience["score_display"]) == ("Audience", "78%")

    def test_critics_come_before_the_crowd(
        self, client: TestClient, catalog: Seeded, both_rt_scores: None
    ) -> None:
        """The order the plugin declared, which is how the site prints them.

        Asserted against the stored positions rather than against insertion
        order: the audience row is written second here, so an implementation
        that simply kept database order would pass by accident.
        """
        scores = groups(client, catalog.foxtrot)["rt"]["scores"]

        assert [score["provider"] for score in scores] == ["rt_critics", "rt_audience"]

    def test_a_lone_figure_is_a_group_of_one(self, client: TestClient, catalog: Seeded) -> None:
        imdb = groups(client, catalog.fauda)["imdb"]

        assert imdb["name"] == "IMDb"
        assert len(imdb["scores"]) == 1

    def test_the_chip_links_to_the_title_rather_than_the_service(
        self, client: TestClient, catalog: Seeded, both_rt_scores: None
    ) -> None:
        """A chip is one link, and the useful one is this title's own page."""
        assert groups(client, catalog.foxtrot)["rt"]["url"] == RT_URL

    def test_the_flat_list_is_still_sent(
        self, client: TestClient, catalog: Seeded, both_rt_scores: None
    ) -> None:
        """The aggregate's working reads by rater, because a weight is per rater."""
        body = client.get(f"/api/v1/titles/{catalog.foxtrot}").json()

        assert len(body["ratings"]) == 2
        assert {r["provider"] for r in body["ratings"]} == {"rt_critics", "rt_audience"}


class TestWhatThePluginsSaid:
    def test_a_group_carries_the_mark_the_fetcher_published(
        self, client: TestClient, catalog: Seeded, session_factory: sessionmaker[Session]
    ) -> None:
        with session_factory() as session:
            row = session.get(RatingProviderInfo, RatingProvider.IMDB)
            assert row is not None
            row.logo_path = "providers/imdb-abc12345.svg"
            session.commit()

        assert groups(client, catalog.fauda)["imdb"]["logo_url"] == (
            "/images/providers/imdb-abc12345.svg"
        )

    def test_no_mark_is_an_ordinary_answer(self, client: TestClient, catalog: Seeded) -> None:
        """The chip says the service's name instead, as every chip once did."""
        imdb = groups(client, catalog.fauda)["imdb"]

        assert imdb["logo_url"] is None
        assert imdb["name"] == "IMDb"

    def test_a_provider_nothing_has_declared_is_still_credited(
        self,
        client: TestClient,
        catalog: Seeded,
        session_factory: sessionmaker[Session],
    ) -> None:
        """A score on the page with no source against it is a rumour.

        This is the state between an upgrade and the first enrich on a
        deployment whose plugins have changed: the provider key is a worse name
        than "IMDb" and a far better one than nothing.
        """
        with session_factory() as session:
            session.execute(delete(RatingProviderInfo))
            session.commit()

        imdb = groups(client, catalog.fauda)["imdb"]
        assert imdb["name"] == "imdb"
        assert imdb["scores"][0]["provider_name"] == "imdb"

    def test_an_undeclared_provider_gets_a_chip_of_its_own(
        self,
        client: TestClient,
        catalog: Seeded,
        session_factory: sessionmaker[Session],
        both_rt_scores: None,
    ) -> None:
        """Not one chip holding everything nobody has described.

        Grouping is a claim that two figures came from the same place. With
        nothing declared there is no evidence for that claim about any pair, so
        the honest fallback is the shape the page had before grouping existed.
        """
        with session_factory() as session:
            session.execute(delete(RatingProviderInfo))
            session.commit()

        keys = set(groups(client, catalog.foxtrot))
        assert keys == {"rt_critics", "rt_audience"}
