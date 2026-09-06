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
import gzip
import io
import logging
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from eifo_core import ingest as wire
from eifo_core.enums import RatingProvider
from eifo_fetcher.enrichers.base import ICONS_DIR, ProviderInfo
from eifo_fetcher.http import HttpClient
from eifo_fetcher.ingest import IngestClient
from eifo_fetcher.progress import ProgressTicker

#: Dataset rows between progress lines. The file has over a million of them
#: and they cost nothing each, so the sync loops' hundred would be noise.
PROGRESS_EVERY_ROWS = 250_000
#: The first line, early enough to prove the parse started at all.
PROGRESS_FIRST_ROWS = 50_000

logger = logging.getLogger("eifo.fetch.enrich.imdb")

DATASET_URL = "https://datasets.imdbws.com/title.ratings.tsv.gz"
TITLE_URL_TEMPLATE = "https://www.imdb.com/title/{imdb_id}/"

#: IMDb marks absent values with this rather than an empty column.
_NULL = "\\N"

#: Refusals kept for the run row. The same reasoning as FetchContext's cap: if
#: the parse has gone wrong every row fails the same way, and the first handful
#: says so as well as a million would.
MAX_REPORTED_REJECTIONS = 20


@dataclass(slots=True)
class ImdbResult:
    """Tally for one IMDb dataset pass.

    ``written`` where this used to say created and updated. The pass no longer
    writes the rows itself, and the far side answers how many scores it stored
    rather than how many of them were new - which is the number that was ever
    worth reporting, since a dataset refreshed daily is almost entirely updates.
    """

    rows_read: int = 0
    matched: int = 0
    written: int = 0
    #: What the catalog would not take, in its words. A score outside IMDb's
    #: scale is a parse that has gone wrong, and it is invisible from here
    #: unless the refusal is carried back.
    rejected: list[str] = field(default_factory=list)

    def as_stats(self) -> dict[str, Any]:
        return {
            "rows_read": self.rows_read,
            "matched": self.matched,
            "written": self.written,
            "errors": self.rejected,
            "error_count": len(self.rejected),
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

    #: Declared here even though this is not an ``Enricher``: it is what
    #: produces the IMDb score, so it is what should say how the score credits
    #: itself. The one figure IMDb publishes is its own group.
    provider_info = (
        ProviderInfo(
            provider=RatingProvider.IMDB,
            label="IMDb",
            group_key="imdb",
            group_name="IMDb",
            icon=ICONS_DIR / "imdb.svg",
            website_url="https://www.imdb.com",
        ),
    )

    def __init__(self, http: HttpClient) -> None:
        self._http = http

    def run(
        self,
        api: IngestClient,
        *,
        url: str = DATASET_URL,
        chunk_size: int = wire.IMDB_WRITE_CHUNK,
    ) -> ImdbResult:
        """Fetch the dataset and update every title the catalog holds an id for.

        The join happens here rather than at the catalog, and that is the whole
        shape of this pass: the dataset is over a million rows and the catalog
        is tens of thousands of titles, so the small side crosses the wire and
        the large one stays on the machine that has just downloaded it. Asking
        the catalog about a million ids would be the same work done the
        expensive way round.
        """
        wanted = api.imdb_wanted()
        result = ImdbResult()
        if not wanted:
            logger.info("no titles carry an imdb_id yet; nothing to join")
            return result

        logger.info("downloading %s for %d titles", url, len(wanted))
        data = self._http.get(url).content
        logger.info("downloaded %.1fMB; joining it against the catalog", len(data) / 1_000_000)

        # The dataset runs to well over a million rows and none of them are
        # slow, so this reports on a far coarser scale than the loops that make
        # network calls - often enough to prove the pass is moving, rarely
        # enough that it does not drown the run it belongs to.
        ticker = ProgressTicker(every=PROGRESS_EVERY_ROWS, first=PROGRESS_FIRST_ROWS)
        batch: list[dict[str, Any]] = []

        for rating in parse_ratings(data):
            result.rows_read += 1
            title_id = wanted.get(rating.imdb_id)
            if title_id is not None:
                result.matched += 1
                batch.append(_to_wire(title_id, rating))
                if len(batch) >= chunk_size:
                    _send(api, batch, result)
                    batch = []

            # Outside the match, so a stretch of rows this catalog holds nothing
            # for still counts as progress - which is most of the dataset.
            if ticker.due(result.rows_read):
                logger.info(
                    "imdb: %s rows read, %s matched",
                    f"{result.rows_read:,}",
                    f"{result.matched:,}",
                )

        if batch:
            _send(api, batch, result)

        logger.info(
            "imdb: %d rows read, %d matched, %d written%s",
            result.rows_read,
            result.matched,
            result.written,
            f", {len(result.rejected)} refused" if result.rejected else "",
        )
        return result


def _to_wire(title_id: int, rating: ImdbRating) -> dict[str, Any]:
    """One matched row, with the link the score is credited by."""
    return {
        "title_id": title_id,
        "score_raw": rating.average,
        "vote_count": rating.votes,
        "url": TITLE_URL_TEMPLATE.format(imdb_id=rating.imdb_id),
    }


def _send(api: IngestClient, batch: list[dict[str, Any]], result: ImdbResult) -> None:
    """Store one chunk, and keep whatever the catalog would not take.

    Refusals are collected rather than raised. A score the far side rejects is
    one bad row in a dataset of a million, and the remaining nine hundred
    thousand are still worth writing - but it has to be *said*, or a parse that
    has quietly gone wrong looks like a pass that simply matched less tonight.
    """
    answer = api.store_imdb_ratings(batch)
    result.written += int(answer.get("ratings_written", 0))
    for rejection in answer.get("rejected") or []:
        message = str(rejection.get("reason") or rejection)
        logger.warning("imdb: a score was refused: %s", message)
        # Capped for the same reason FetchContext caps its own: these go into a
        # run row, and a systematically broken parse would otherwise write a
        # million of them into one JSON column.
        if len(result.rejected) < MAX_REPORTED_REJECTIONS:
            result.rejected.append(message)
