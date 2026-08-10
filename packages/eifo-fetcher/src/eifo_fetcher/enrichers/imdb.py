"""IMDb ratings from the official non-commercial datasets.

IMDb publishes a daily ``title.ratings.tsv.gz`` covering every rated title, so
there is nothing to scrape: one download and a single bulk update fills the
whole catalog. That is both far kinder to IMDb and far faster than per-title
requests, which is why this is a bulk pass rather than an ``Enricher``.

Licensing: the datasets are for personal and non-commercial use, which is what
Eifo is. The UI credits IMDb via ``GET /api/v1/meta``.
"""

from __future__ import annotations

import csv
import datetime as dt
import gzip
import io
import logging
from collections.abc import Iterator
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from eifo_core.enums import RatingProvider
from eifo_core.models import ExternalRating, Title
from eifo_core.types import utcnow
from eifo_fetcher.http import HttpClient
from eifo_fetcher.scores import normalise

logger = logging.getLogger("eifo.fetch.enrich.imdb")

DATASET_URL = "https://datasets.imdbws.com/title.ratings.tsv.gz"
TITLE_URL_TEMPLATE = "https://www.imdb.com/title/{imdb_id}/"

#: IMDb marks absent values with this rather than an empty column.
_NULL = "\\N"


@dataclass(slots=True)
class ImdbResult:
    """Tally for one IMDb dataset pass."""

    rows_read: int = 0
    matched: int = 0
    created: int = 0
    updated: int = 0

    def as_stats(self) -> dict[str, int]:
        return {
            "rows_read": self.rows_read,
            "matched": self.matched,
            "created": self.created,
            "updated": self.updated,
        }


@dataclass(frozen=True, slots=True)
class ImdbRating:
    """One row of the ratings dataset."""

    imdb_id: str
    average: float
    votes: int


def parse_ratings(data: bytes) -> Iterator[ImdbRating]:
    """Read the gzipped TSV, skipping rows that are not usable.

    The file is tens of megabytes and millions of rows, so it is streamed and
    filtered rather than loaded into a list.
    """
    with gzip.open(io.BytesIO(data), mode="rt", encoding="utf-8", newline="") as stream:
        for row in csv.DictReader(stream, delimiter="\t", quoting=csv.QUOTE_NONE):
            rating = _parse_row(row)
            if rating is not None:
                yield rating


def _parse_row(row: dict[str, str]) -> ImdbRating | None:
    imdb_id = (row.get("tconst") or "").strip()
    average = (row.get("averageRating") or "").strip()
    votes = (row.get("numVotes") or "").strip()

    if not imdb_id or imdb_id == _NULL or average in ("", _NULL):
        return None

    try:
        return ImdbRating(
            imdb_id=imdb_id,
            average=float(average),
            votes=int(votes) if votes and votes != _NULL else 0,
        )
    except ValueError:
        return None


class ImdbDatasetLoader:
    """Downloads the ratings dataset and applies it to the catalog in one pass."""

    def __init__(self, http: HttpClient) -> None:
        self._http = http

    def run(self, session: Session, *, url: str = DATASET_URL) -> ImdbResult:
        """Fetch the dataset and update every title we hold an ``imdb_id`` for."""
        wanted = _titles_by_imdb_id(session)
        result = ImdbResult()
        if not wanted:
            logger.info("no titles carry an imdb_id yet; nothing to join")
            return result

        logger.info("downloading %s for %d titles", url, len(wanted))
        data = self._http.get(url).content

        existing = _ratings_by_title_id(session)
        now = utcnow()

        for rating in parse_ratings(data):
            result.rows_read += 1
            title_id = wanted.get(rating.imdb_id)
            if title_id is None:
                continue

            result.matched += 1
            _apply(session, existing, title_id, rating, now, result)

        session.commit()
        logger.info(
            "imdb: %d rows read, %d matched, %d created, %d updated",
            result.rows_read,
            result.matched,
            result.created,
            result.updated,
        )
        return result


def _apply(
    session: Session,
    existing: dict[int, ExternalRating],
    title_id: int,
    rating: ImdbRating,
    now: dt.datetime,
    result: ImdbResult,
) -> None:
    normalized = normalise(RatingProvider.IMDB, rating.average)
    url = TITLE_URL_TEMPLATE.format(imdb_id=rating.imdb_id)

    stored = existing.get(title_id)
    if stored is None:
        session.add(
            ExternalRating(
                title_id=title_id,
                provider=RatingProvider.IMDB,
                score_raw=rating.average,
                score_normalized=normalized,
                vote_count=rating.votes,
                url=url,
                fetched_at=now,
            )
        )
        result.created += 1
        return

    stored.score_raw = rating.average
    stored.score_normalized = normalized
    stored.vote_count = rating.votes
    stored.url = url
    stored.fetched_at = now
    result.updated += 1


def _titles_by_imdb_id(session: Session) -> dict[str, int]:
    return {
        imdb_id: title_id
        for title_id, imdb_id in session.execute(
            select(Title.id, Title.imdb_id).where(Title.imdb_id.is_not(None))
        ).all()
        if imdb_id
    }


def _ratings_by_title_id(session: Session) -> dict[int, ExternalRating]:
    return {
        rating.title_id: rating
        for rating in session.scalars(
            select(ExternalRating).where(ExternalRating.provider == RatingProvider.IMDB)
        ).all()
    }
