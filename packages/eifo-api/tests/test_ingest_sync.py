"""``/api/v1/ingest/sync`` - a catalog sync, written from somewhere else.

What a well-behaved fetcher does with this surface is tested from the other
side, against the real application, in ``eifo-fetcher``. What is here is
everything a well-behaved fetcher never does: arriving without being an
administrator, sending a chunk larger than the server will take, offering
listings against a run that has already finished, and sending something that is
not a listing at all.

Those are the tests that matter for a surface reachable over a network. The
happy path has a fetcher on the other end of it that gets it right; this one
has whatever turns up.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest
from fastapi.testclient import TestClient
from helpers import MakeAdmin, SignIn
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from eifo_api.routers import syncing
from eifo_api.security import CSRF_HEADER
from eifo_core import ingest as wire
from eifo_core.enums import FetchPhase, FetchStatus, SourceKind, TitleKind
from eifo_core.models import Availability, FetchRun, Source, Title
from eifo_core.types import utcnow

SOURCES = "/api/v1/ingest/sync/sources"
BACKFILLS = "/api/v1/ingest/sync/backfills"
RUNS = "/api/v1/ingest/sync/runs"

DECLARED: dict[str, Any] = {
    "source_key": "cellcom_tv",
    "name": "Cellcom TV",
    "kind": SourceKind.SUBSCRIPTION.value,
    "website_url": "https://cellcomtv.co.il",
}


def listing(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "source_key": DECLARED["source_key"],
        "kind": TitleKind.SERIES.value,
        "name": "פאודה",
        "offer_type": "stream",
        "year": 2015,
    }
    values.update(overrides)
    return values


@pytest.fixture
def operator(sign_in: SignIn, make_admin: MakeAdmin) -> str:
    """A signed-in administrator, and the CSRF token their browser would send.

    The fetcher presents an API token instead and needs no CSRF header at all;
    a cookie is simply the shortest way to get an administrator in a test.
    """
    make_admin()
    return sign_in()


@pytest.fixture
def run(client: TestClient, operator: str) -> int:
    """An open sync run, which is what listings are offered against."""
    response = client.post(RUNS, json=DECLARED, headers={CSRF_HEADER: operator})
    assert response.status_code == 201
    run_id: int = response.json()["run_id"]
    return run_id


def offer(client: TestClient, operator: str, run_id: int, *entries: Any) -> Any:
    return client.post(
        f"{RUNS}/{run_id}/items", json=list(entries), headers={CSRF_HEADER: operator}
    )


class TestWhoMayUseIt:
    def test_a_stranger_is_not_told_it_exists(self, client: TestClient) -> None:
        """404 rather than 401 or 403, exactly as the operator's surface does."""
        assert client.get(BACKFILLS).status_code in {401, 404}

    def test_a_signed_in_non_administrator_gets_404(
        self, client: TestClient, sign_in: SignIn
    ) -> None:
        sign_in()

        assert client.get(BACKFILLS).status_code == 404

    def test_a_stranger_cannot_declare_sources(self, client: TestClient) -> None:
        assert client.post(SOURCES, json={"sources": [DECLARED]}).status_code in {401, 403, 404}

    def test_a_stranger_cannot_open_a_run(self, client: TestClient) -> None:
        assert client.post(RUNS, json=DECLARED).status_code in {401, 403, 404}


class TestDeclaringSources:
    def test_a_declaration_becomes_a_row_and_an_answer(
        self, client: TestClient, operator: str, session_factory: sessionmaker[Session]
    ) -> None:
        response = client.post(
            SOURCES,
            json={"sources": [DECLARED], "enabled": [DECLARED["source_key"]]},
            headers={CSRF_HEADER: operator},
        )

        assert response.status_code == 200
        assert response.json()["added"] == [DECLARED["source_key"]]
        with session_factory() as session:
            assert session.scalars(select(Source)).one().name == "Cellcom TV"

    def test_the_operators_switches_come_back(
        self, client: TestClient, operator: str, session_factory: sessionmaker[Session]
    ) -> None:
        """Read per run rather than held, so a source switched off at midnight
        is off tonight without anybody restarting a daemon."""
        client.post(SOURCES, json={"sources": [DECLARED]}, headers={CSRF_HEADER: operator})
        with session_factory() as session:
            session.scalars(select(Source)).one().enabled = False
            session.commit()

        answer = client.post(
            SOURCES, json={"sources": [DECLARED]}, headers={CSRF_HEADER: operator}
        ).json()

        assert answer["overrides"] == {DECLARED["source_key"]: False}

    def test_retiring_happens_only_when_it_is_asked_for(
        self, client: TestClient, operator: str
    ) -> None:
        """A fetcher syncing one source is not evidence the others are gone."""
        client.post(
            SOURCES,
            json={"sources": [DECLARED], "enabled": [DECLARED["source_key"]]},
            headers={CSRF_HEADER: operator},
        )

        kept = client.post(SOURCES, json={"sources": []}, headers={CSRF_HEADER: operator}).json()
        retired = client.post(
            SOURCES,
            json={"sources": [], "retire_missing": True},
            headers={CSRF_HEADER: operator},
        ).json()

        assert kept["retired"] == []
        assert retired["retired"] == [DECLARED["source_key"]]


class TestBackfills:
    def test_an_ask_is_listed_and_then_answered(
        self, client: TestClient, operator: str, session_factory: sessionmaker[Session]
    ) -> None:
        client.post(SOURCES, json={"sources": [DECLARED]}, headers={CSRF_HEADER: operator})
        with session_factory() as session:
            session.scalars(select(Source)).one().backfill_requested_at = utcnow()
            session.commit()

        assert client.get(BACKFILLS).json() == [DECLARED["source_key"]]

        cleared = client.post(
            f"{BACKFILLS}/clear",
            json={"keys": [DECLARED["source_key"]]},
            headers={CSRF_HEADER: operator},
        )

        assert cleared.status_code == 204
        assert client.get(BACKFILLS).json() == []


class TestOpeningARun:
    def test_the_run_exists_before_a_single_listing_arrives(
        self, client: TestClient, operator: str, run: int, session_factory: sessionmaker[Session]
    ) -> None:
        """A sync that dies mid-flight must leave a row saying it started."""
        with session_factory() as session:
            stored = session.get(FetchRun, run)

        assert stored is not None
        assert stored.phase is FetchPhase.SYNC
        assert stored.status is FetchStatus.RUNNING

    def test_the_source_is_written_from_what_the_plugin_says(
        self, client: TestClient, operator: str, run: int, session_factory: sessionmaker[Session]
    ) -> None:
        """A service added in an upgrade has to become visible without anybody
        editing a table, and the API cannot ask a plugin anything."""
        with session_factory() as session:
            assert session.scalars(select(Source)).one().key == DECLARED["source_key"]

    def test_the_fetchers_clock_is_the_one_that_counts(
        self, client: TestClient, operator: str, session_factory: sessionmaker[Session]
    ) -> None:
        """The work began on its machine, which is no longer guaranteed to be this one."""
        began = "2026-01-02T03:04:05+00:00"

        answer = client.post(
            RUNS, json={**DECLARED, "started_at": began}, headers={CSRF_HEADER: operator}
        ).json()

        assert answer["started_at"].startswith("2026-01-02T03:04:05")


class TestOfferingAChunk:
    def test_a_listing_becomes_a_title_and_an_offer(
        self, client: TestClient, operator: str, run: int, session_factory: sessionmaker[Session]
    ) -> None:
        response = offer(client, operator, run, {"item": listing(), "resolved": True})

        assert response.status_code == 200
        with session_factory() as session:
            assert len(session.scalars(select(Title)).all()) == 1
            assert len(session.scalars(select(Availability)).all()) == 1

    def test_a_listing_it_cannot_place_is_handed_back(
        self, client: TestClient, operator: str, run: int
    ) -> None:
        """There is no TMDB key on this side; the sender has the network."""
        answer = offer(client, operator, run, {"item": listing()}).json()

        assert [row["name"] for row in answer["needs_tmdb"]] == ["פאודה"]
        assert answer["stored"] == 0

    def test_the_second_pass_is_not_deferred_again(
        self, client: TestClient, operator: str, run: int
    ) -> None:
        """ "I looked and found nothing" and "I have not looked" are different
        answers, and conflating them hands a listing back for ever - which on an
        install with no TMDB key is every listing."""
        offer(client, operator, run, {"item": listing()})

        answer = offer(
            client, operator, run, {"item": listing(), "tmdb": None, "resolved": True}
        ).json()

        assert answer["needs_tmdb"] == []
        assert answer["stored"] == 1

    def test_a_deferred_listing_is_not_counted_twice(
        self, client: TestClient, operator: str, run: int, session_factory: sessionmaker[Session]
    ) -> None:
        """It crosses the wire twice, and the volume guard judges the run by this."""
        offer(client, operator, run, {"item": listing()})
        offer(client, operator, run, {"item": listing(), "resolved": True})

        with session_factory() as session:
            assert session.get(FetchRun, run).stats["items_seen"] == 1  # type: ignore[union-attr]

    def test_one_bad_listing_is_reported_and_the_rest_are_stored(
        self, client: TestClient, operator: str, run: int, session_factory: sessionmaker[Session]
    ) -> None:
        """The alternative costs a whole catalog for one bad row, every night."""
        answer = offer(
            client,
            operator,
            run,
            {"item": listing(name="  "), "resolved": True},
            {"item": listing(name="שטיסל", source_ref="s-2"), "resolved": True},
        ).json()

        assert [row["index"] for row in answer["rejected"]] == [0]
        with session_factory() as session:
            assert len(session.scalars(select(Title)).all()) == 1

    def test_a_body_that_is_not_a_list_is_refused(
        self, client: TestClient, operator: str, run: int
    ) -> None:
        response = client.post(
            f"{RUNS}/{run}/items", json={"item": listing()}, headers={CSRF_HEADER: operator}
        )

        assert response.status_code == 400
        assert "array" in response.json()["detail"]

    def test_a_chunk_larger_than_the_cap_is_refused(
        self, client: TestClient, operator: str, run: int
    ) -> None:
        """Named rather than truncated: a sender that thinks it stored two
        thousand listings and stored five hundred is worse off than one told no."""
        too_many = [{"item": listing(source_ref=f"s-{n}")} for n in range(wire.MAX_SYNC_CHUNK + 1)]

        response = client.post(
            f"{RUNS}/{run}/items", json=too_many, headers={CSRF_HEADER: operator}
        )

        assert response.status_code == 413
        assert str(wire.MAX_SYNC_CHUNK) in response.json()["detail"]

    def test_a_run_that_does_not_exist_is_a_404(self, client: TestClient, operator: str) -> None:
        assert offer(client, operator, 9999, {"item": listing()}).status_code == 404

    def test_an_enrich_run_is_not_a_sync_run(
        self, client: TestClient, operator: str, session_factory: sessionmaker[Session]
    ) -> None:
        """The id spaces are shared, so the phase has to be checked."""
        with session_factory() as session:
            other = FetchRun(
                phase=FetchPhase.ENRICH,
                status=FetchStatus.RUNNING,
                started_at=utcnow(),
                stats={},
            )
            session.add(other)
            session.commit()
            other_id = other.id

        assert offer(client, operator, other_id, {"item": listing()}).status_code == 404


class TestFinishingARun:
    def _finish(self, client: TestClient, operator: str, run_id: int, **body: Any) -> Any:
        return client.post(
            f"{RUNS}/{run_id}/finish",
            json={"status": FetchStatus.OK.value, **body},
            headers={CSRF_HEADER: operator},
        )

    def test_it_reports_what_the_run_did(self, client: TestClient, operator: str, run: int) -> None:
        offer(client, operator, run, {"item": listing(), "resolved": True})

        outcome = self._finish(client, operator, run).json()

        assert outcome["status"] == FetchStatus.OK.value
        assert outcome["items_seen"] == 1
        assert outcome["titles_created"] == 1

    def test_a_collapse_in_volume_is_not_believed(
        self,
        client: TestClient,
        operator: str,
        session_factory: sessionmaker[Session],
    ) -> None:
        """Assumed to be a broken parser rather than a service that has shed
        nine tenths of its catalog - and nothing is retired on the strength of it."""
        with session_factory() as session:
            session.add(
                FetchRun(
                    source_key=DECLARED["source_key"],
                    phase=FetchPhase.SYNC,
                    status=FetchStatus.OK,
                    started_at=utcnow(),
                    finished_at=utcnow(),
                    stats={"items_seen": 500},
                )
            )
            session.commit()
        opened = client.post(RUNS, json=DECLARED, headers={CSRF_HEADER: operator}).json()
        offer(client, operator, opened["run_id"], {"item": listing(), "resolved": True})

        outcome = self._finish(client, operator, opened["run_id"]).json()

        assert outcome["status"] == FetchStatus.ABORTED_SUSPICIOUS.value
        assert any("far below" in error for error in outcome["errors"])

    def test_the_row_holds_what_both_sides_said(
        self, client: TestClient, operator: str, run: int, session_factory: sessionmaker[Session]
    ) -> None:
        offer(client, operator, run, {"item": listing(), "resolved": True})

        self._finish(client, operator, run, log="the fetcher read 1 listing")

        with session_factory() as session:
            log = session.get(FetchRun, run).log or ""  # type: ignore[union-attr]
        assert "listing(s) in" in log, "what this side decided"
        assert "the fetcher read 1 listing" in log, "what the fetcher said"

    def test_finishing_twice_is_refused_rather_than_ignored(
        self, client: TestClient, operator: str, run: int
    ) -> None:
        """A second finish would sweep a second time against a run already judged."""
        self._finish(client, operator, run)

        again = self._finish(client, operator, run)

        assert again.status_code == 409
        assert "begin another" in again.json()["detail"]

    def test_offering_to_a_finished_run_is_refused(
        self, client: TestClient, operator: str, run: int
    ) -> None:
        self._finish(client, operator, run)

        assert offer(client, operator, run, {"item": listing()}).status_code == 409


class TestTheServerKeepsAnsweringWhileAChunkIsMatched:
    """The difference between a slow endpoint and a stopped server.

    Matching a chunk is seconds of unbroken CPU. Run on the event loop it
    serves nothing else meanwhile: the healthcheck times out, the web app
    hangs, and requests queue up holding database sessions until the pool is
    exhausted. That is how the deployed server went dark on 2026-09-07 - it
    was not down, it was matching.
    """

    def test_another_request_is_served_while_a_chunk_is_still_matching(
        self,
        client: TestClient,
        operator: str,
        run: int,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        matching = threading.Event()
        release = threading.Event()
        real = syncing.TitleMatcher

        class Blocking(real):  # type: ignore[valid-type,misc]
            """Holds the chunk open until the test says otherwise."""

            def match(self, item: Any) -> Any:
                matching.set()
                # Bounded, so a regression fails this test rather than hanging
                # the whole suite.
                release.wait(timeout=30)
                return super().match(item)

        monkeypatch.setattr(syncing, "TitleMatcher", Blocking)

        answers: list[Any] = []
        chunk = threading.Thread(
            target=lambda: answers.append(
                offer(client, operator, run, {"item": listing(), "resolved": True})
            )
        )
        probed: list[Any] = []
        probe = threading.Thread(target=lambda: probed.append(client.get("/api/v1/meta")))

        chunk.start()
        try:
            assert matching.wait(timeout=10), "the chunk never reached the matcher"
            # The chunk is now parked inside the handler, waiting on us. If it
            # were waiting on the event loop this could not be answered until
            # we let go - and we are not going to.
            probe.start()
            probe.join(timeout=10)
            served = not probe.is_alive()
        finally:
            release.set()
            probe.join(timeout=15)
            chunk.join(timeout=15)

        assert served, "the event loop was blocked: nothing was served while a chunk matched"
        assert probed[0].status_code == 200
        assert answers[0].status_code == 200
