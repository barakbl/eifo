"""The IMDb dataset loader: TSV parsing and the bulk join."""

from __future__ import annotations

import gzip
import io
from typing import Any

import httpx
import respx
from sqlalchemy import select
from sqlalchemy.orm import Session

from tvil_core.enums import RatingProvider, TitleKind
from tvil_core.models import ExternalRating, Title
from tvil_fetcher.enrichers.imdb import (
    DATASET_URL,
    ImdbDatasetLoader,
    parse_ratings,
)
from tvil_fetcher.http import HttpClient

HEADER = "tconst\taverageRating\tnumVotes"


def dataset(*rows: str) -> bytes:
    """A gzipped TSV shaped like IMDb's real export."""
    body = "\n".join([HEADER, *rows]) + "\n"
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb") as stream:
        stream.write(body.encode("utf-8"))
    return buffer.getvalue()


def add_title(session: Session, imdb_id: str | None, **overrides: Any) -> Title:
    values: dict[str, Any] = {
        "type": TitleKind.SERIES,
        "name_he": "פאודה",
        "year": 2015,
        "imdb_id": imdb_id,
    }
    values.update(overrides)
    title = Title(**values)
    session.add(title)
    session.commit()
    return title


class TestParseRatings:
    def test_reads_the_real_column_layout(self) -> None:
        [rating] = list(parse_ratings(dataset("tt4565380\t8.3\t45123")))

        assert rating.imdb_id == "tt4565380"
        assert rating.average == 8.3
        assert rating.votes == 45123

    def test_skips_imdbs_null_marker(self) -> None:
        rows = list(parse_ratings(dataset("tt0000001\t\\N\t100", "tt0000002\t7.5\t50")))

        assert [rating.imdb_id for rating in rows] == ["tt0000002"]

    def test_skips_unparsable_numbers(self) -> None:
        rows = list(parse_ratings(dataset("tt0000001\tnot-a-number\t100")))

        assert rows == []

    def test_tolerates_a_missing_vote_count(self) -> None:
        [rating] = list(parse_ratings(dataset("tt0000001\t7.5\t\\N")))

        assert rating.votes == 0

    def test_reads_a_large_file_lazily(self) -> None:
        """The real dataset is millions of rows, so it must stream."""
        rows = (f"tt{index:07d}\t7.0\t100" for index in range(5000))
        parsed = parse_ratings(dataset(*rows))

        first = next(parsed)

        assert first.imdb_id == "tt0000000"


class TestBulkJoin:
    @respx.mock
    def test_creates_ratings_for_matching_titles(self, session: Session, http: HttpClient) -> None:
        add_title(session, "tt4565380")
        respx.get(DATASET_URL).mock(
            return_value=httpx.Response(200, content=dataset("tt4565380\t8.3\t45123"))
        )

        result = ImdbDatasetLoader(http).run(session)

        rating = session.scalars(select(ExternalRating)).one()
        assert rating.provider is RatingProvider.IMDB
        assert rating.score_raw == 8.3
        assert rating.score_normalized == 83
        assert rating.vote_count == 45123
        assert rating.url == "https://www.imdb.com/title/tt4565380/"
        assert result.created == 1

    @respx.mock
    def test_updates_an_existing_rating(self, session: Session, http: HttpClient) -> None:
        title = add_title(session, "tt4565380")
        session.add(
            ExternalRating(
                title_id=title.id,
                provider=RatingProvider.IMDB,
                score_raw=7.0,
                score_normalized=70,
                vote_count=10,
            )
        )
        session.commit()
        respx.get(DATASET_URL).mock(
            return_value=httpx.Response(200, content=dataset("tt4565380\t8.3\t45123"))
        )

        result = ImdbDatasetLoader(http).run(session)

        rating = session.scalars(select(ExternalRating)).one()
        assert rating.score_raw == 8.3
        assert result.updated == 1
        assert result.created == 0

    @respx.mock
    def test_ignores_rows_for_titles_we_do_not_hold(
        self, session: Session, http: HttpClient
    ) -> None:
        add_title(session, "tt4565380")
        respx.get(DATASET_URL).mock(
            return_value=httpx.Response(
                200, content=dataset("tt9999999\t9.9\t1", "tt4565380\t8.3\t45123")
            )
        )

        result = ImdbDatasetLoader(http).run(session)

        assert result.rows_read == 2
        assert result.matched == 1
        assert len(session.scalars(select(ExternalRating)).all()) == 1

    @respx.mock
    def test_titles_without_an_imdb_id_are_skipped(
        self, session: Session, http: HttpClient
    ) -> None:
        add_title(session, None)
        respx.get(DATASET_URL).mock(
            return_value=httpx.Response(200, content=dataset("tt4565380\t8.3\t1"))
        )

        result = ImdbDatasetLoader(http).run(session)

        assert result.matched == 0

    @respx.mock
    def test_no_download_when_nothing_could_match(self, session: Session, http: HttpClient) -> None:
        """Skips tens of megabytes when no title carries an imdb_id."""
        route = respx.get(DATASET_URL).mock(return_value=httpx.Response(200, content=b""))

        result = ImdbDatasetLoader(http).run(session)

        assert route.call_count == 0
        assert result.rows_read == 0
