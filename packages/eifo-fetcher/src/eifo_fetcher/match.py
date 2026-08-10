"""Resolving a source listing to a canonical title.

Order of attempts, first hit wins (docs.internal/05-fetcher.md):

1. an external id the item already carries,
2. a TMDB lookup by name and year,
3. a fuzzy comparison against titles already in the database,
4. otherwise create a local title, or park the item for review.

Matching is deterministic and every decision is counted, so a regression shows
up as a changed ``matched_by`` histogram in a single ``fetch_runs`` row.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from rapidfuzz import fuzz
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from eifo_core.enums import TitleKind
from eifo_core.models import MatchReview, Title
from eifo_fetcher.sources.base import RawItem
from eifo_fetcher.tmdb import TmdbClient, TmdbTitle

logger = logging.getLogger("eifo.fetch.match")

#: Normalised-name similarity required to accept a match.
SIMILARITY_THRESHOLD = 90.0
#: Similarity close enough to be suspicious but too weak to accept: a human
#: decides. Below this band an item is simply a title we have not seen.
AMBIGUOUS_THRESHOLD = 75.0
#: A release year may disagree by this much across catalogs.
YEAR_TOLERANCE = 1

_HEBREW = re.compile(r"[֐-׿]")
#: Dropped outright: they join words rather than separate them, so turning
#: "Marvel's" into "marvel s" would be wrong.
_JOINERS = re.compile(r"['’׳`]")  # noqa: RUF001 - matching lookalikes is the intent
_PUNCTUATION = re.compile(r"[^\w\s]", re.UNICODE)
_WHITESPACE = re.compile(r"\s+")
_LEADING_ARTICLES = ("the ", "a ", "an ", "ה")


class MatchMethod(StrEnum):
    """How a title was resolved - the keys of the ``matched_by`` histogram."""

    EXTERNAL_ID = "external_id"
    TMDB = "tmdb"
    FUZZY = "fuzzy"
    CREATED = "created"
    REVIEW = "review"
    #: Matched from a ruling somebody made in the review queue.
    RESOLVED = "resolved"


@dataclass(slots=True)
class MatchResult:
    """Outcome of resolving one item."""

    title: Title | None
    method: MatchMethod

    @property
    def resolved(self) -> bool:
        return self.title is not None


@dataclass(slots=True)
class MatchStats:
    """Per-run tally of how items were resolved."""

    counts: Counter[str] = field(default_factory=Counter)

    def record(self, method: MatchMethod) -> None:
        self.counts[method.value] += 1

    def as_dict(self) -> dict[str, int]:
        return dict(self.counts)


def is_hebrew(text: str) -> bool:
    """Whether a string contains Hebrew letters."""
    return bool(_HEBREW.search(text))


def normalise(name: str) -> str:
    """Fold a title down to what two catalogs are likely to agree on.

    Casing, punctuation, diacritics, runs of whitespace and a leading article
    all vary between services and none of them carry meaning for matching.
    """
    text = unicodedata.normalize("NFKD", name)
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = _JOINERS.sub("", text.casefold())
    text = _PUNCTUATION.sub(" ", text)
    text = _WHITESPACE.sub(" ", text).strip()
    for article in _LEADING_ARTICLES:
        if text.startswith(article) and len(text) > len(article):
            text = text[len(article) :]
            break
    return text.strip()


def similarity(left: str, right: str) -> float:
    """Similarity of two names, 0-100, after normalisation."""
    return fuzz.ratio(normalise(left), normalise(right))


def years_match(left: int | None, right: int | None) -> bool:
    """Whether two release years are close enough to be the same title.

    A missing year on either side is not evidence of a mismatch - plenty of
    local listings omit it - so it does not veto an otherwise strong name match.
    """
    if left is None or right is None:
        return True
    return abs(left - right) <= YEAR_TOLERANCE


def names_of(item: RawItem) -> tuple[str | None, str | None]:
    """Split an item's names into (Hebrew, English) by script."""
    hebrew: str | None = None
    english: str | None = None
    for candidate in (item.name, item.name_alt):
        if not candidate:
            continue
        if is_hebrew(candidate):
            hebrew = hebrew or candidate
        else:
            english = english or candidate
    return hebrew, english


class TitleMatcher:
    """Resolves :class:`RawItem` values to :class:`Title` rows."""

    def __init__(
        self,
        session: Session,
        *,
        tmdb: TmdbClient | None = None,
        stats: MatchStats | None = None,
    ) -> None:
        self._session = session
        self._tmdb = tmdb
        self.stats = stats or MatchStats()

    def match(self, item: RawItem) -> MatchResult:
        """Resolve one item, creating or parking it when nothing matches."""
        result = self._by_external_id(item) or self._by_tmdb(item) or self._by_local_fuzzy(item)
        if result is not None:
            self.stats.record(result.method)
            return result

        # Somebody has already ruled on this exact item. Asking again every
        # night would make the review queue regrow no matter how diligently it
        # is worked, and would silently discard the answer they gave.
        decided = self._prior_decision(item)
        if decided is not None:
            self.stats.record(decided.method)
            return decided

        # Nothing matched confidently. A near-miss is the interesting case: it
        # is probably a title we already hold under a slightly different name,
        # and guessing either way corrupts the catalog - so a human decides.
        candidate, score = self._best_local_candidate(item)
        if candidate is not None and score >= AMBIGUOUS_THRESHOLD:
            self._park_for_review(item, candidate=candidate, score=score)
            self.stats.record(MatchMethod.REVIEW)
            return MatchResult(title=None, method=MatchMethod.REVIEW)

        title = self._create_title(item)
        self.stats.record(MatchMethod.CREATED)
        return MatchResult(title=title, method=MatchMethod.CREATED)

    def _prior_decision(self, item: RawItem) -> MatchResult | None:
        """Honour an earlier ``eifo-fetch review`` ruling on this same item.

        Two rulings are possible and both are meaningful:

        * **resolved to a title** - attach the offer to it, every sync from now on.
        * **skipped** - the near-miss was wrong, so stop offering it and let the
          item become a title of its own.

        Returns None when nobody has ruled. A title that has since been deleted
        leaves the item to be treated as new again rather than parked forever.
        """
        review = self._session.scalars(
            select(MatchReview)
            .where(
                MatchReview.source_key == item.source_key,
                MatchReview.resolved_at.is_not(None),
                MatchReview.raw_payload["name"].as_string() == item.name,
                MatchReview.raw_payload["kind"].as_string() == item.kind.value,
            )
            .order_by(MatchReview.resolved_at.desc())
            .limit(1)
        ).one_or_none()

        if review is None:
            return None

        if review.resolved_title_id is None:
            # Skipped: not the candidate we suggested. Let it stand on its own.
            return MatchResult(title=self._create_title(item), method=MatchMethod.CREATED)

        title = self._session.get(Title, review.resolved_title_id)
        if title is None:
            return None
        return MatchResult(title=title, method=MatchMethod.RESOLVED)

    # -- strategies -------------------------------------------------------

    def _by_external_id(self, item: RawItem) -> MatchResult | None:
        if item.tmdb_id is not None:
            title = self._session.scalar(select(Title).where(Title.tmdb_id == item.tmdb_id))
            if title is not None:
                return MatchResult(title=title, method=MatchMethod.EXTERNAL_ID)
        if item.imdb_id:
            title = self._session.scalar(select(Title).where(Title.imdb_id == item.imdb_id))
            if title is not None:
                return MatchResult(title=title, method=MatchMethod.EXTERNAL_ID)
        return None

    def _by_tmdb(self, item: RawItem) -> MatchResult | None:
        """Resolve through TMDB, then fold the hit into any existing title."""
        if item.tmdb_id is not None:
            # The item named a TMDB id no local title owns yet: adopt it.
            return MatchResult(title=self._create_title(item), method=MatchMethod.TMDB)

        if self._tmdb is None:
            return None

        hit = self._search_tmdb(item)
        if hit is None:
            return None

        existing = self._session.scalar(select(Title).where(Title.tmdb_id == hit.tmdb_id))
        if existing is not None:
            return MatchResult(title=existing, method=MatchMethod.TMDB)

        return MatchResult(title=self._create_from_tmdb(item, hit), method=MatchMethod.TMDB)

    def _search_tmdb(self, item: RawItem) -> TmdbTitle | None:
        """Best TMDB candidate for an item, or None if none is convincing."""
        assert self._tmdb is not None
        for query in filter(None, (item.name, item.name_alt)):
            try:
                candidates = self._tmdb.search(item.kind, query, year=item.year)
            except Exception:
                logger.exception("TMDB search failed for %r", query)
                return None

            for candidate in candidates:
                if not years_match(item.year, candidate.year):
                    continue
                names = (candidate.name, candidate.original_name)
                if any(name and similarity(query, name) >= SIMILARITY_THRESHOLD for name in names):
                    return candidate
        return None

    def _best_local_candidate(self, item: RawItem) -> tuple[Title | None, float]:
        """Closest stored title of the same kind, with its similarity score."""
        hebrew, english = names_of(item)
        candidates = self._session.scalars(select(Title).where(Title.type == item.kind)).all()

        best: Title | None = None
        best_score = 0.0
        for candidate in candidates:
            if not years_match(item.year, candidate.year):
                continue
            pairs = ((hebrew, candidate.name_he), (english, candidate.name_en))
            for left, right in pairs:
                if not left or not right:
                    continue
                score = similarity(left, right)
                if score > best_score:
                    best, best_score = candidate, score

        return best, best_score

    def _by_local_fuzzy(self, item: RawItem) -> MatchResult | None:
        """Accept a stored title only when the name match is convincing."""
        best, score = self._best_local_candidate(item)
        if best is not None and score >= SIMILARITY_THRESHOLD:
            return MatchResult(title=best, method=MatchMethod.FUZZY)
        return None

    # -- fallbacks --------------------------------------------------------

    def _create_title(self, item: RawItem) -> Title:
        hebrew, english = names_of(item)
        title = Title(
            type=item.kind,
            tmdb_id=item.tmdb_id,
            imdb_id=item.imdb_id or None,
            name_he=hebrew,
            name_en=english,
            year=item.year,
            poster_source_url=item.poster_url,
        )
        self._session.add(title)
        self._session.flush()
        return title

    def _create_from_tmdb(self, item: RawItem, hit: TmdbTitle) -> Title:
        """Create a title anchored on a TMDB hit, keeping both names."""
        hebrew, english = names_of(item)
        if is_hebrew(hit.name):
            hebrew = hebrew or hit.name
            english = english or hit.original_name
        else:
            english = english or hit.name

        title = Title(
            type=item.kind,
            tmdb_id=hit.tmdb_id,
            name_he=hebrew,
            name_en=english,
            year=hit.year or item.year,
            overview_he=hit.overview if hit.overview and is_hebrew(hit.overview) else None,
            overview_en=hit.overview if hit.overview and not is_hebrew(hit.overview) else None,
        )
        self._session.add(title)
        self._session.flush()
        return title

    def _park_for_review(
        self,
        item: RawItem,
        *,
        candidate: Title | None = None,
        score: float = 0.0,
    ) -> None:
        """Store an unresolved item, with what it nearly matched, for review.

        Replaces any earlier *unresolved* park of the same item first, so a
        re-sync refreshes the near-miss rather than piling up a duplicate row
        for something nobody has ruled on yet. A resolved review is never
        touched - that decision is honoured by ``_prior_decision`` and must
        survive. This makes the open queue converge to one row per item on
        every sync, healing duplicates a previous run may have left.
        """
        self._session.execute(
            delete(MatchReview).where(
                MatchReview.source_key == item.source_key,
                MatchReview.resolved_at.is_(None),
                MatchReview.raw_payload["name"].as_string() == item.name,
                MatchReview.raw_payload["kind"].as_string() == item.kind.value,
            )
        )

        candidates: dict[str, Any] = {}
        if candidate is not None:
            candidates["closest"] = {
                "title_id": candidate.id,
                "name_he": candidate.name_he,
                "name_en": candidate.name_en,
                "year": candidate.year,
                "similarity": round(score, 1),
            }

        self._session.add(
            MatchReview(
                source_key=item.source_key,
                raw_payload={
                    "name": item.name,
                    "name_alt": item.name_alt,
                    "year": item.year,
                    "kind": item.kind.value,
                    "deep_link_url": item.deep_link_url,
                    "poster_url": item.poster_url,
                    "extra": dict(item.extra),
                },
                candidates=candidates,
            )
        )
        # Sessions here run with autoflush off; flush so the row is visible to
        # anything querying the review queue later in the same transaction.
        self._session.flush()


def title_kind_from(value: str) -> TitleKind:
    """Parse a stored title kind, defaulting to movie for unknown values."""
    try:
        return TitleKind(value)
    except ValueError:
        return TitleKind.MOVIE
