"""``/api/v1/ingest/enrich`` - ratings and metadata, written from somewhere else.

The same division as the sync surface: what a well-behaved fetcher does with
this is tested from the other side, against the real application, in
``eifo-fetcher``. What is here is everything a well-behaved fetcher never does -
arriving without being an administrator, sending more titles than the server
will take in one batch, reporting against a run that has already finished, and
sending a score that could not have come from the provider it claims.

That last one is the reason this surface refuses anything at all. A percentage
read as a score out of ten is a parser bug, and storing it would quietly skew an
aggregate that nothing downstream can tell apart from an honest one.
"""

from __future__ import annotations

import datetime as dt
from base64 import b64encode
from typing import Any

import pytest
from fastapi.testclient import TestClient
from helpers import MakeAdmin, SignIn
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from eifo_api.security import CSRF_HEADER
from eifo_core import ingest as wire
from eifo_core.enums import (
    EnrichOutcome,
    FetchPhase,
    FetchStatus,
    RatingProvider,
    TitleKind,
)
from eifo_core.models import (
    AggregateScore,
    EnrichAttempt,
    ExternalRating,
    FetchRun,
    RatingProviderInfo,
    SeretTitle,
    Title,
)
from eifo_core.types import utcnow

DUE = "/api/v1/ingest/enrich/due"
MISLABELLED = "/api/v1/ingest/enrich/mislabelled"
PROVIDERS = "/api/v1/ingest/enrich/providers"
RESCORE = "/api/v1/ingest/enrich/rescore"
RUNS = "/api/v1/ingest/enrich/runs"
SERET_INDEX = "/api/v1/ingest/enrich/seret/index"
SERET_STATUS = "/api/v1/ingest/enrich/seret/status"
IMDB_WANTED = "/api/v1/ingest/enrich/imdb/wanted"
IMDB_RATINGS = "/api/v1/ingest/enrich/imdb/ratings"


@pytest.fixture
def operator(sign_in: SignIn, make_admin: MakeAdmin) -> str:
    """A signed-in administrator, and the CSRF token their browser would send."""
    make_admin()
    return sign_in()


@pytest.fixture
def title(session_factory: sessionmaker[Session]) -> int:
    with session_factory() as session:
        row = Title(type=TitleKind.MOVIE, name_he="פוקסטרוט", name_en="Foxtrot", year=2017)
        session.add(row)
        session.commit()
        return row.id


@pytest.fixture
def run(client: TestClient, operator: str) -> int:
    response = client.post(RUNS, json={}, headers={CSRF_HEADER: operator})
    assert response.status_code == 201
    run_id: int = response.json()["run_id"]
    return run_id


def rating(provider: RatingProvider = RatingProvider.IMDB, score: float = 8.3) -> dict[str, Any]:
    return {"provider": provider.value, "score_raw": score, "vote_count": 120, "url": None}


def report(client: TestClient, operator: str, run_id: int, *entries: Any) -> Any:
    return client.post(
        f"{RUNS}/{run_id}/findings", json=list(entries), headers={CSRF_HEADER: operator}
    )


class TestWhoMayUseIt:
    def test_a_stranger_is_not_told_it_exists(self, client: TestClient) -> None:
        assert client.get(DUE).status_code in {401, 404}

    def test_a_signed_in_non_administrator_gets_404(
        self, client: TestClient, sign_in: SignIn
    ) -> None:
        sign_in()

        assert client.get(DUE).status_code == 404

    @pytest.mark.parametrize("path", [PROVIDERS, RESCORE, RUNS, SERET_INDEX, IMDB_RATINGS])
    def test_a_stranger_cannot_write_anything(self, client: TestClient, path: str) -> None:
        assert client.post(path, json={}).status_code in {401, 403, 404}


class TestTheQueue:
    def test_it_hands_over_what_an_enricher_will_be_given(
        self, client: TestClient, operator: str, title: int
    ) -> None:
        rows = client.get(DUE).json()

        assert [row["id"] for row in rows] == [title]
        assert rows[0]["name_en"] == "Foxtrot"
        assert rows[0]["kind"] == TitleKind.MOVIE.value

    def test_it_will_not_hand_over_more_than_its_cap(
        self, client: TestClient, operator: str
    ) -> None:
        """The whole batch arrives in one answer, so the cap is what bounds it."""
        assert client.get(DUE, params={"limit": wire.MAX_DUE_PAGE + 1}).status_code == 422

    def test_force_ignores_the_schedule(
        self, client: TestClient, operator: str, title: int, session_factory: sessionmaker[Session]
    ) -> None:
        with session_factory() as session:
            session.add(
                EnrichAttempt(
                    title_id=title,
                    attempted_at=utcnow(),
                    outcome=EnrichOutcome.OK,
                    due_at=utcnow() + dt.timedelta(days=14),
                )
            )
            session.commit()

        assert client.get(DUE).json() == []
        assert len(client.get(DUE, params={"force": "true"}).json()) == 1

    def test_the_mislabelled_are_asked_for_separately(
        self, client: TestClient, operator: str, session_factory: sessionmaker[Session]
    ) -> None:
        """A repair, not a schedule: the fix is to ask TMDB about them again."""
        with session_factory() as session:
            session.add(Title(type=TitleKind.MOVIE, name_en="千と千尋の神隠し", tmdb_id=129))
            session.add(Title(type=TitleKind.MOVIE, name_en="Spirited Away", tmdb_id=130))
            session.commit()

        rows = client.get(MISLABELLED).json()

        assert [row["name_en"] for row in rows] == ["千と千尋の神隠し"]


class TestReportingFindings:
    def test_a_rating_is_stored_and_the_title_rescored(
        self,
        client: TestClient,
        operator: str,
        title: int,
        run: int,
        session_factory: sessionmaker[Session],
    ) -> None:
        answer = report(
            client,
            operator,
            run,
            {
                "title_id": title,
                "findings": [
                    {"source": "imdb", "result": {"ratings": [rating()], "metadata_patch": {}}},
                    {
                        "source": "tmdb",
                        "result": {
                            "ratings": [rating(RatingProvider.TMDB, 7.0)],
                            "metadata_patch": {},
                        },
                    },
                ],
            },
        ).json()

        assert answer["ratings_written"] == 2
        with session_factory() as session:
            assert len(session.scalars(select(ExternalRating)).all()) == 2
            assert session.scalars(select(AggregateScore)).one() is not None

    def test_a_title_nobody_could_rate_is_still_recorded_as_tried(
        self,
        client: TestClient,
        operator: str,
        title: int,
        run: int,
        session_factory: sessionmaker[Session],
    ) -> None:
        """Or the queue spends every run on exactly the same titles."""
        report(client, operator, run, {"title_id": title, "findings": []})

        with session_factory() as session:
            attempt = session.scalars(select(EnrichAttempt)).one()
        assert attempt.outcome is not EnrichOutcome.OK
        assert attempt.due_at > utcnow()

    def test_a_score_outside_its_providers_scale_is_refused(
        self,
        client: TestClient,
        operator: str,
        title: int,
        run: int,
        session_factory: sessionmaker[Session],
    ) -> None:
        """A percentage read as a score out of ten would skew the aggregate."""
        answer = report(
            client,
            operator,
            run,
            {
                "title_id": title,
                "findings": [
                    {
                        "source": "imdb",
                        "result": {"ratings": [rating(score=89.0)], "metadata_patch": {}},
                    }
                ],
            },
        ).json()

        assert answer["rejected"]
        assert "outside its" in answer["rejected"][0]["reason"]
        with session_factory() as session:
            assert session.scalars(select(ExternalRating)).all() == []

    def test_a_title_that_is_not_there_is_reported_by_index(
        self, client: TestClient, operator: str, title: int, run: int
    ) -> None:
        """So the sender is told which of a hundred, not only that one was bad."""
        answer = report(
            client,
            operator,
            run,
            {"title_id": 9999, "findings": []},
            {"title_id": title, "findings": []},
        ).json()

        assert [row["index"] for row in answer["rejected"]] == [0]
        assert answer["titles_seen"] == 1

    def test_a_finding_it_cannot_read_costs_that_finding_only(
        self,
        client: TestClient,
        operator: str,
        title: int,
        run: int,
        session_factory: sessionmaker[Session],
    ) -> None:
        answer = report(
            client,
            operator,
            run,
            {
                "title_id": title,
                "findings": [
                    {"source": "rt", "result": {"ratings": [{"provider": "who"}]}},
                    {"source": "imdb", "result": {"ratings": [rating()], "metadata_patch": {}}},
                ],
            },
        ).json()

        assert answer["rejected"]
        assert answer["ratings_written"] == 1

    def test_a_body_that_is_not_a_list_is_refused(
        self, client: TestClient, operator: str, run: int
    ) -> None:
        response = client.post(
            f"{RUNS}/{run}/findings", json={"title_id": 1}, headers={CSRF_HEADER: operator}
        )

        assert response.status_code == 400

    def test_a_batch_larger_than_the_cap_is_refused(
        self, client: TestClient, operator: str, run: int
    ) -> None:
        too_many = [{"title_id": n, "findings": []} for n in range(wire.MAX_ENRICH_CHUNK + 1)]

        response = client.post(
            f"{RUNS}/{run}/findings", json=too_many, headers={CSRF_HEADER: operator}
        )

        assert response.status_code == 413

    def test_reporting_to_a_run_that_does_not_exist_is_a_404(
        self, client: TestClient, operator: str
    ) -> None:
        assert report(client, operator, 9999).status_code == 404

    def test_reporting_to_a_finished_run_is_refused(
        self, client: TestClient, operator: str, run: int
    ) -> None:
        client.post(
            f"{RUNS}/{run}/finish",
            json={"status": FetchStatus.OK.value},
            headers={CSRF_HEADER: operator},
        )

        assert report(client, operator, run).status_code == 409


class TestFinishingARun:
    def test_the_totals_come_back_and_stay_on_the_row(
        self,
        client: TestClient,
        operator: str,
        title: int,
        run: int,
        session_factory: sessionmaker[Session],
    ) -> None:
        report(
            client,
            operator,
            run,
            {
                "title_id": title,
                "findings": [
                    {"source": "imdb", "result": {"ratings": [rating()], "metadata_patch": {}}}
                ],
            },
        )

        outcome = client.post(
            f"{RUNS}/{run}/finish",
            json={"status": FetchStatus.OK.value, "errors": []},
            headers={CSRF_HEADER: operator},
        ).json()

        assert outcome["titles_seen"] == 1
        assert outcome["ratings_written"] == 1
        with session_factory() as session:
            assert session.get(FetchRun, run).stats["ratings_written"] == 1  # type: ignore[union-attr]

    def test_what_only_the_sender_knows_is_merged_in(
        self, client: TestClient, operator: str, run: int, session_factory: sessionmaker[Session]
    ) -> None:
        """Which providers were asked is the fetcher's fact; this side sees findings."""
        client.post(
            f"{RUNS}/{run}/finish",
            json={"status": FetchStatus.OK.value, "stats": {"by_enricher": {"rt": 4}}},
            headers={CSRF_HEADER: operator},
        )

        with session_factory() as session:
            assert session.get(FetchRun, run).stats["by_enricher"] == {"rt": 4}  # type: ignore[union-attr]

    def test_the_sender_cannot_overwrite_what_this_side_counted(
        self,
        client: TestClient,
        operator: str,
        title: int,
        run: int,
        session_factory: sessionmaker[Session],
    ) -> None:
        """What was written is this side's fact, and it is the one that is true."""
        report(
            client,
            operator,
            run,
            {
                "title_id": title,
                "findings": [
                    {"source": "imdb", "result": {"ratings": [rating()], "metadata_patch": {}}}
                ],
            },
        )

        client.post(
            f"{RUNS}/{run}/finish",
            json={"status": FetchStatus.OK.value, "stats": {"ratings_written": 9999}},
            headers={CSRF_HEADER: operator},
        )

        with session_factory() as session:
            assert session.get(FetchRun, run).stats["ratings_written"] == 1  # type: ignore[union-attr]


class TestDeclaringProviders:
    def test_a_declaration_and_its_mark_land(
        self,
        client: TestClient,
        operator: str,
        settings: Any,
        session_factory: sessionmaker[Session],
    ) -> None:
        """The mark arrives as bytes because the plugin is not necessarily here."""
        response = client.post(
            PROVIDERS,
            json={
                "providers": [
                    {
                        "provider": RatingProvider.RT_CRITICS.value,
                        "label": "Tomatometer",
                        "group_key": "rt",
                        "group_name": "Rotten Tomatoes",
                        "logo": b64encode(b"<svg/>").decode("ascii"),
                        "logo_suffix": ".svg",
                    }
                ]
            },
            headers={CSRF_HEADER: operator},
        )

        assert response.json()["changed"] == ["rt_critics"]
        with session_factory() as session:
            row = session.get(RatingProviderInfo, RatingProvider.RT_CRITICS)
        assert row is not None and row.logo_path is not None
        assert (settings.images_dir / row.logo_path).read_bytes() == b"<svg/>"

    def test_declaring_the_same_thing_again_changes_nothing(
        self, client: TestClient, operator: str
    ) -> None:
        """This runs on every phase; a second identical pass must not touch a row."""
        body = {
            "providers": [
                {
                    "provider": RatingProvider.EDB.value,
                    "label": "EDB",
                    "group_key": "edb",
                    "group_name": "EDB",
                }
            ]
        }
        client.post(PROVIDERS, json=body, headers={CSRF_HEADER: operator})

        assert client.post(PROVIDERS, json=body, headers={CSRF_HEADER: operator}).json() == {
            "changed": []
        }


class TestRescoring:
    def test_it_rebuilds_from_ratings_already_stored(
        self,
        client: TestClient,
        operator: str,
        title: int,
        session_factory: sessionmaker[Session],
    ) -> None:
        """Nothing crosses the wire for this: the inputs and the arithmetic are here."""
        with session_factory() as session:
            session.add_all(
                [
                    ExternalRating(
                        title_id=title,
                        provider=RatingProvider.IMDB,
                        score_raw=9.0,
                        score_normalized=90,
                    ),
                    ExternalRating(
                        title_id=title,
                        provider=RatingProvider.TMDB,
                        score_raw=7.0,
                        score_normalized=70,
                    ),
                ]
            )
            session.commit()

        answer = client.post(RESCORE, json={}, headers={CSRF_HEADER: operator}).json()

        assert answer["aggregates_computed"] == 1
        with session_factory() as session:
            assert session.scalars(select(AggregateScore)).one().score == 85


class TestTheSeretIndex:
    def _page(self, **overrides: Any) -> dict[str, Any]:
        values: dict[str, Any] = {
            "kind": TitleKind.MOVIE.value,
            "seret_id": 4242,
            "name_he": "פוקסטרוט",
            "name_en": "Foxtrot",
            "year": 2017,
            "viewers_score": 9.1,
            "viewers_votes": 42,
            "critics_score": 6.8,
            "url": "https://www.seret.co.il/movies/s_movies.asp?MID=4242",
        }
        values.update(overrides)
        return values

    def _store(self, client: TestClient, operator: str, *pages: Any) -> Any:
        return client.post(
            SERET_INDEX, json={"pages": list(pages)}, headers={CSRF_HEADER: operator}
        )

    def test_a_crawled_page_becomes_a_row(
        self, client: TestClient, operator: str, session_factory: sessionmaker[Session]
    ) -> None:
        answer = self._store(client, operator, self._page()).json()

        assert answer["created"] == 1
        with session_factory() as session:
            row = session.scalars(select(SeretTitle)).one()
        assert row.viewers_votes == 42, "the vote count is what tells 11 votes from 11,000"

    def test_a_page_with_nothing_on_it_still_gets_a_row(
        self, client: TestClient, operator: str
    ) -> None:
        """Or the crawl pays for that id again on every single run."""
        self._store(client, operator, self._page(seret_id=8620, unreadable=True))

        assert client.get(SERET_INDEX).json() == []
        assert len(client.get(SERET_INDEX, params={"include_unreadable": "true"}).json()) == 1

    def test_only_a_newly_scorable_page_wakes_anything(
        self, client: TestClient, operator: str, title: int
    ) -> None:
        """Re-reading last month's page must not wake a title all over again."""
        first = self._store(client, operator, self._page()).json()

        again = self._store(client, operator, self._page()).json()

        assert first["newly_scorable"] == 1
        assert again["newly_scorable"] == 0

    def test_a_parked_title_the_new_page_covers_becomes_due(
        self,
        client: TestClient,
        operator: str,
        title: int,
        session_factory: sessionmaker[Session],
    ) -> None:
        """A backoff should not outlive the reason for it."""
        with session_factory() as session:
            session.add(
                EnrichAttempt(
                    title_id=title,
                    attempted_at=utcnow(),
                    outcome=EnrichOutcome.NO_MATCH,
                    fruitless=3,
                    due_at=utcnow() + dt.timedelta(days=30),
                )
            )
            session.commit()

        assert self._store(client, operator, self._page()).json()["woken"] == 1
        with session_factory() as session:
            assert session.scalars(select(EnrichAttempt)).one().due_at <= utcnow()

    def test_the_index_is_paged_by_the_whole_key(self, client: TestClient, operator: str) -> None:
        """Films and series are numbered apart, so an id alone does not order it."""
        self._store(
            client,
            operator,
            self._page(),
            self._page(kind=TitleKind.SERIES.value, name_he="טהרן", name_en="Tehran"),
        )

        rows = client.get(SERET_INDEX, params={"limit": 1}).json()
        after = client.get(
            SERET_INDEX,
            params={"limit": 5, "after_kind": rows[0]["kind"], "after_id": rows[0]["seret_id"]},
        ).json()

        assert len(rows) == 1
        assert len(after) == 1
        assert after[0]["kind"] != rows[0]["kind"]

    def test_the_status_says_what_it_holds_and_whether_it_is_wanted(
        self, client: TestClient, operator: str, title: int
    ) -> None:
        """A fresh install must not crawl 8,900 pages to enrich nothing."""
        self._store(client, operator, self._page())

        status = client.get(SERET_STATUS).json()

        assert status["pages"] == 1
        assert status["movies"] == 1
        assert status["with_viewer_score"] == 1
        assert status["catalog_titles"] == 1


class TestTheImdbBulkPass:
    def test_it_hands_over_the_ids_to_join_against(
        self, client: TestClient, operator: str, session_factory: sessionmaker[Session]
    ) -> None:
        """The small side crosses the wire; the million-row dataset does not."""
        with session_factory() as session:
            session.add(Title(type=TitleKind.MOVIE, name_en="With", imdb_id="tt1"))
            session.add(Title(type=TitleKind.MOVIE, name_en="Without"))
            session.commit()

        rows = client.get(IMDB_WANTED).json()

        assert [row["imdb_id"] for row in rows] == ["tt1"]

    def test_a_chunk_of_scores_is_written_with_its_links(
        self,
        client: TestClient,
        operator: str,
        title: int,
        session_factory: sessionmaker[Session],
    ) -> None:
        answer = client.post(
            IMDB_RATINGS,
            json={
                "ratings": [
                    {
                        "title_id": title,
                        "score_raw": 8.3,
                        "vote_count": 45_123,
                        "url": "https://www.imdb.com/title/tt1/",
                    }
                ]
            },
            headers={CSRF_HEADER: operator},
        ).json()

        assert answer["ratings_written"] == 1
        with session_factory() as session:
            stored = session.scalars(select(ExternalRating)).one()
        assert stored.url == "https://www.imdb.com/title/tt1/"
        assert stored.vote_count == 45_123

    def test_a_title_that_is_gone_is_reported_rather_than_fatal(
        self, client: TestClient, operator: str
    ) -> None:
        answer = client.post(
            IMDB_RATINGS,
            json={"ratings": [{"title_id": 9999, "score_raw": 8.3}]},
            headers={CSRF_HEADER: operator},
        ).json()

        assert answer["ratings_written"] == 0
        assert answer["rejected"]

    def test_a_chunk_larger_than_the_cap_is_refused(
        self, client: TestClient, operator: str, title: int
    ) -> None:
        too_many = [
            {"title_id": title, "score_raw": 8.3} for _ in range(wire.MAX_IMDB_WRITE_CHUNK + 1)
        ]

        response = client.post(
            IMDB_RATINGS, json={"ratings": too_many}, headers={CSRF_HEADER: operator}
        )

        assert response.status_code == 422


class TestOpeningARun:
    def test_the_row_exists_before_a_single_title_is_looked_up(
        self, client: TestClient, operator: str, run: int, session_factory: sessionmaker[Session]
    ) -> None:
        with session_factory() as session:
            stored = session.get(FetchRun, run)

        assert stored is not None
        assert stored.phase is FetchPhase.ENRICH
        assert stored.status is FetchStatus.RUNNING

    def test_a_sync_run_is_not_an_enrich_run(
        self, client: TestClient, operator: str, session_factory: sessionmaker[Session]
    ) -> None:
        with session_factory() as session:
            other = FetchRun(
                phase=FetchPhase.SYNC,
                source_key="mako",
                status=FetchStatus.RUNNING,
                started_at=utcnow(),
                stats={},
            )
            session.add(other)
            session.commit()
            other_id = other.id

        assert report(client, operator, other_id).status_code == 404
