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
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from rapidfuzz import fuzz
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from eifo_core.enums import MatchDecision, TitleKind
from eifo_core.models import MatchReview, Title, TmdbAlias
from eifo_core.naming import is_hebrew, latin_script, split_by_script
from eifo_fetcher.sources.base import RawItem, plausible_year
from eifo_fetcher.tmdb import TmdbClient, TmdbTitle

logger = logging.getLogger("eifo.fetch.match")

#: Normalised-name similarity required to accept a match.
SIMILARITY_THRESHOLD = 90.0
#: Similarity close enough to be suspicious but too weak to accept: a human
#: decides. Below this band an item is simply a title we have not seen.
#:
#: Was 75, which put almost everything it caught in front of somebody for the
#: same answer: of the first 78 rulings made by hand, 77 said the suggestion was
#: wrong. "אודטה" against "פאודה" scores 80 and they are unrelated. Parking is
#: not free - a parked item is not in the catalog at all - so the band has to be
#: narrow enough that being asked means something.
AMBIGUOUS_THRESHOLD = 85.0
#: A release year may disagree by this much across catalogs.
YEAR_TOLERANCE = 1
#: The bar for deciding that two TMDB ids name one work.
#:
#: Higher than an ordinary match on purpose. An item carrying its own id is
#: asserting an identity, and TMDB is usually right about that; overruling it
#: needs names that agree letter for letter once normalised, not merely closely.
#: Every duplicate pair found in the deployed catalog cleared this - they were
#: the same title entered twice, not two titles with similar names.
ALIAS_SIMILARITY_THRESHOLD = 100.0

#: How far apart two years may be and still be worth a human's attention.
#:
#: Wider than matching accepts, and used only to decide what reaches the review
#: queue. A series has no single year: catalogs date it by whichever season
#: they carry, so the same show is 2010 in one listing and 2012 in another.
#: Widening the *matching* rule to cover that would make the year useless for
#: telling a remake from its original, so the gap becomes a reason to ask
#: rather than a reason to accept.
REVIEW_YEAR_TOLERANCE = 8

#: Marks that are decoration on a letter: Latin diacritics and Hebrew pointing.
#:
#: Deliberately not every combining character. In Devanagari, Malayalam and
#: Tamil the vowel signs are combining marks too, and they are letters rather
#: than decoration - dropping them turned "ജോജി" (Joji) and "ജോ & ജോ" (Jo & Jo)
#: into the same two-consonant string, a similarity of 100 between two
#: different films.
_DECORATIVE_MARKS = re.compile(r"[\u0300-\u036f\u0591-\u05c7]")
#: Dropped outright: they join words rather than separate them, so turning
#: "Marvel's" into "marvel s" would be wrong.
_JOINERS = re.compile(r"['’׳`]")  # noqa: RUF001 - matching lookalikes is the intent
#: Kept when punctuation is stripped: letters, numbers and combining marks.
#:
#: Marks have to be named explicitly because Python's ``\w`` does not count
#: them as word characters, so a class built from it treats a Malayalam vowel
#: sign as punctuation and throws it away.
_KEPT_CATEGORIES = frozenset("LNM")
_WHITESPACE = re.compile(r"\s+")
_LEADING_ARTICLES = ("the ", "a ", "an ", "ה")


class MatchMethod(StrEnum):
    """How a title was resolved - the keys of the ``matched_by`` histogram."""

    EXTERNAL_ID = "external_id"
    #: Resolved through a TMDB id already known to be a second record of a
    #: title we hold.
    ALIAS = "alias"
    TMDB = "tmdb"
    FUZZY = "fuzzy"
    CREATED = "created"
    REVIEW = "review"
    #: Matched from a ruling somebody made in the review queue.
    RESOLVED = "resolved"
    #: Somebody ruled this is not catalog content - a trailer, a promo, a recap.
    DISMISSED = "dismissed"


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


def normalise(name: str) -> str:
    """Fold a title down to what two catalogs are likely to agree on.

    Casing, punctuation, diacritics, runs of whitespace and a leading article
    all vary between services and none of them carry meaning for matching.
    """
    text = unicodedata.normalize("NFKD", name)
    text = _DECORATIVE_MARKS.sub("", text)
    text = _JOINERS.sub("", text.casefold())
    text = _strip_punctuation(text)
    text = _WHITESPACE.sub(" ", text).strip()
    for article in _LEADING_ARTICLES:
        if text.startswith(article) and len(text) > len(article):
            text = text[len(article) :]
            break
    return text.strip()


def _strip_punctuation(text: str) -> str:
    """Replace anything that is not a letter, number or mark with a space."""
    return "".join(
        char if char.isspace() or unicodedata.category(char)[0] in _KEPT_CATEGORIES else " "
        for char in text
    )


#: Floor for the fallback that reads a name past its decoration. Higher than
#: SIMILARITY_THRESHOLD because token scores run higher than whole-string ones:
#: dropping decoration is the whole point, so near-misses score near-perfect.
TOKEN_MATCH_THRESHOLD = 95.0


def _acronymish(text: str) -> bool:
    """Whether a name is all one-and-two-letter tokens, where token scoring lies.

    "T O T's" against "O.T.T." is a perfect token-set score and a different
    film. A name like this gets no token credit; only the plain ratio counts.
    """
    tokens = normalise(text).split()
    return not tokens or all(len(token) <= 2 for token in tokens)


def confident_tmdb_choice(
    pairs: Sequence[tuple[str, TmdbTitle]],
    year: int | None,
) -> tuple[str, list[TmdbTitle]]:
    """Judge search candidates the way a person reading both names would.

    The plain ratio cannot see that "Marvel Studios Thor Ragnarok" is
    "Thor: Ragnarok" - decoration around a name costs about twenty points -
    so candidates are also scored on their tokens. Token scores are generous,
    which is why every acceptance carries a guard:

    * **Direction.** A token-set score forgives extra words on either side,
      and the two directions are not equally trustworthy. Our name carrying
      words around theirs is the pattern decoration makes. Ours being a
      fragment of theirs is how "Air Crash Investigation" matched its own
      spin-off - so that direction needs the plain ratio or the year to agree.
    * **Acronyms** score on the plain ratio alone.
    * **Ambiguity.** Two different records qualifying - both Dumbos, when
      nothing says which - is not a match. Guessing between remakes is how a
      catalog quietly lies; measured against 2,059 known answers, refusing to
      guess was the difference between 99.3% and no errors at all.

    Returns ``("auto", [hit])``, ``("ambiguous", hits)`` or ``("none", [])``.
    """
    qualifying: dict[int, TmdbTitle] = {}
    for query, hit in pairs:
        if not years_match(year, hit.year):
            continue
        corroborated = year is not None and hit.year is not None
        normalised_query = normalise(query)
        query_tokens = set(normalised_query.split())
        for name in (hit.name, hit.original_name):
            if not name:
                continue
            normalised_name = normalise(name)
            plain = fuzz.ratio(normalised_query, normalised_name)
            if _acronymish(query) or _acronymish(name):
                score = plain
            else:
                score = max(plain, fuzz.token_set_ratio(normalised_query, normalised_name))
            if score < TOKEN_MATCH_THRESHOLD:
                continue
            decorated_ours = set(normalised_name.split()) <= query_tokens
            if plain >= SIMILARITY_THRESHOLD or decorated_ours or corroborated:
                qualifying.setdefault(hit.tmdb_id, hit)
    if not qualifying:
        return ("none", [])
    if len(qualifying) == 1:
        return ("auto", list(qualifying.values()))
    return ("ambiguous", list(qualifying.values()))


def adopt_tmdb_hit(session: Session, title: Title, hit: TmdbTitle) -> None:
    """Anchor a title we already held on the TMDB record that matches it."""
    title.tmdb_id = hit.tmdb_id
    if is_hebrew(hit.name):
        title.name_he = title.name_he or hit.name
    else:
        title.name_en = title.name_en or hit.name
    title.year = title.year or plausible_year(hit.year)
    session.flush()


def similarity(left: str, right: str) -> float:
    """Similarity of two names, 0-100, after normalisation."""
    return fuzz.ratio(normalise(left), normalise(right))


def years_match(
    left: int | None,
    right: int | None,
    *,
    tolerance: int = YEAR_TOLERANCE,
) -> bool:
    """Whether two release years are close enough to be the same title.

    A missing year on either side is not evidence of a mismatch - plenty of
    local listings omit it - so it does not veto an otherwise strong name match.
    """
    if left is None or right is None:
        return True
    return abs(left - right) <= tolerance


def names_of(item: RawItem) -> tuple[str | None, str | None]:
    """Split an item's names into (Hebrew, English) by script.

    A name in a third script is neither, and is returned as neither. Callers
    that must store something decide what to do with that; see
    :func:`fallback_name`.
    """
    return split_by_script(item.name, item.name_alt)


def fallback_name(item: RawItem) -> str:
    """Something to call a title whose name is in neither of our two scripts.

    A row must carry a name, so a Japanese or Tamil title goes into the English
    column for want of anywhere better - which is what the schema has always
    done, only now knowingly and only when there is no alternative. The metadata
    enricher replaces it with a real English title on its first visit, and TMDB
    has one for very nearly all of them.
    """
    return item.name


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
        candidate, score = self._review_candidate(item)
        if candidate is not None and score >= AMBIGUOUS_THRESHOLD:
            self._park_for_review(item, candidate=candidate, score=score)
            self.stats.record(MatchMethod.REVIEW)
            return MatchResult(title=None, method=MatchMethod.REVIEW)

        title = self._create_title(item)
        self.stats.record(MatchMethod.CREATED)
        return MatchResult(title=title, method=MatchMethod.CREATED)

    def create_title(self, item: RawItem) -> Title:
        """Create a title for an item somebody has ruled we do not already hold."""
        return self._create_title(item)

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

        if review.decision is MatchDecision.DISMISSED:
            # Not a title. Dropping it silently is the point: it is the only
            # answer that keeps a trailer out of a catalog of films.
            return MatchResult(title=None, method=MatchMethod.DISMISSED)

        if review.resolved_title_id is None:
            # Not the candidate we suggested. Let it stand on its own.
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
            aliased = self._by_alias(item.tmdb_id)
            if aliased is not None:
                return MatchResult(title=aliased, method=MatchMethod.ALIAS)
        if item.imdb_id:
            title = self._session.scalar(select(Title).where(Title.imdb_id == item.imdb_id))
            if title is not None:
                return MatchResult(title=title, method=MatchMethod.EXTERNAL_ID)
        return None

    def _by_tmdb(self, item: RawItem) -> MatchResult | None:
        """Resolve through TMDB, then fold the hit into any existing title.

        Both halves of this used to create a title the moment TMDB offered an id
        nothing local owned, without ever asking whether we already held the
        work under another id or none at all. That is where the catalog's
        duplicates came from: TMDB holding one show twice, and a second source
        arriving with an id for a title an earlier source had created without
        one.
        """
        if item.tmdb_id is not None:
            # An id no title owns. Before taking it at face value, ask whether
            # this is a work we already hold - TMDB duplicates its own records,
            # and both ids come round again every night.
            best, score = self._best_local_candidate(item)
            if best is not None and score >= ALIAS_SIMILARITY_THRESHOLD:
                self._remember_alias(item.tmdb_id, best)
                return MatchResult(title=best, method=MatchMethod.ALIAS)
            return MatchResult(title=self._create_title(item), method=MatchMethod.TMDB)

        if self._tmdb is None:
            return None

        hit = self._search_tmdb(item)
        if hit is None:
            return None

        existing = self._session.scalar(select(Title).where(Title.tmdb_id == hit.tmdb_id))
        if existing is not None:
            return MatchResult(title=existing, method=MatchMethod.TMDB)

        # TMDB knows this work, and so, possibly, do we - under a name a local
        # source gave it and no id at all. Attaching the hit to that title both
        # avoids the duplicate and gives an id-less Hebrew listing the anchor
        # every later enrichment needs.
        best, score = self._best_local_candidate(item)
        if best is not None and score >= SIMILARITY_THRESHOLD and best.tmdb_id is None:
            self._adopt_tmdb_hit(best, hit)
            return MatchResult(title=best, method=MatchMethod.FUZZY)

        return MatchResult(title=self._create_from_tmdb(item, hit), method=MatchMethod.TMDB)

    def _by_alias(self, tmdb_id: int) -> Title | None:
        """The title a known-duplicate TMDB id belongs to."""
        alias = self._session.get(TmdbAlias, tmdb_id)
        return None if alias is None else self._session.get(Title, alias.title_id)

    def _remember_alias(self, tmdb_id: int, title: Title) -> None:
        """Record that this TMDB id is a second record of a title we hold.

        Without it the merge undoes itself: the feed offers the same id
        tomorrow, nothing owns it, and a new title appears.
        """
        if self._session.get(TmdbAlias, tmdb_id) is not None:
            return
        logger.info("tmdb %s is a second record of title %s", tmdb_id, title.id)
        self._session.add(TmdbAlias(tmdb_id=tmdb_id, title_id=title.id))
        self._session.flush()

    def _adopt_tmdb_hit(self, title: Title, hit: TmdbTitle) -> None:
        adopt_tmdb_hit(self._session, title, hit)

    def _search_tmdb(self, item: RawItem) -> TmdbTitle | None:
        """Best TMDB candidate for an item, or None if none is convincing.

        Searched twice when a year is known: TMDB filters on it exactly, and
        catalogs routinely disagree about a title's year - a series most of all,
        since each one dates it by whichever season it carries. A miss on the
        constrained search is therefore worth retrying unconstrained, where the
        name still has to clear the same bar and the year is then checked here
        with the tolerance this project uses rather than TMDB's none.
        """
        assert self._tmdb is not None
        seen: list[tuple[str, TmdbTitle]] = []
        for query in filter(None, (item.name, item.name_alt)):
            for year in _search_years(item.year):
                try:
                    candidates = self._tmdb.search(item.kind, query, year=year)
                except Exception:
                    logger.exception("TMDB search failed for %r", query)
                    return None

                for candidate in candidates:
                    seen.append((query, candidate))
                    if not years_match(item.year, candidate.year):
                        continue
                    names = (candidate.name, candidate.original_name)
                    if any(
                        name and similarity(query, name) >= SIMILARITY_THRESHOLD for name in names
                    ):
                        return candidate

        # Nothing cleared the plain bar. Read the same candidates past their
        # decoration before giving up - strictly additive: it only ever fires
        # where this method used to return None.
        verdict, hits = confident_tmdb_choice(seen, item.year)
        return hits[0] if verdict == "auto" else None

    def _best_local_candidate(
        self,
        item: RawItem,
        *,
        kind: TitleKind | None = None,
        year_tolerance: int = YEAR_TOLERANCE,
    ) -> tuple[Title | None, float]:
        """Closest stored title, with its similarity score.

        Args:
            kind: which kind to compare against, defaulting to the item's own.
                Only the review path looks across kinds.
            year_tolerance: how far the years may be apart. Wider than the
                default is for deciding what a human should see, never for
                deciding a match.
        """
        hebrew, english = names_of(item)
        wanted = kind or item.kind
        candidates = self._session.scalars(select(Title).where(Title.type == wanted)).all()

        best: Title | None = None
        best_score = 0.0
        for candidate in candidates:
            if not years_match(item.year, candidate.year, tolerance=year_tolerance):
                continue
            pairs = ((hebrew, candidate.name_he), (english, candidate.name_en))
            for left, right in pairs:
                if not left or not right:
                    continue
                score = similarity(left, right)
                if score > best_score:
                    best, best_score = candidate, score

        return best, best_score

    def _review_candidate(self, item: RawItem) -> tuple[Title | None, float]:
        """The closest title worth a human's attention, looking wider than matching.

        Matching is strict on purpose, and a near-miss it rejected is precisely
        what the queue is for. Two kinds of near-miss were being thrown away
        instead of asked about: a year gap too wide to accept but too narrow to
        dismiss, and a title one catalog files as a film while another files it
        as a series - a one-off documentary, most often, which the same-kind
        comparison could never see.
        """
        best, score = self._best_local_candidate(item, year_tolerance=REVIEW_YEAR_TOLERANCE)
        if best is not None and score >= AMBIGUOUS_THRESHOLD:
            return best, score

        other = TitleKind.SERIES if item.kind is TitleKind.MOVIE else TitleKind.MOVIE
        crossed, cross_score = self._best_local_candidate(
            item, kind=other, year_tolerance=REVIEW_YEAR_TOLERANCE
        )
        # A higher bar across kinds than within one: the names are all there is
        # to go on once the kinds already disagree.
        if crossed is not None and cross_score >= SIMILARITY_THRESHOLD:
            return crossed, cross_score
        return best, score

    def _by_local_fuzzy(self, item: RawItem) -> MatchResult | None:
        """Accept a stored title only when the name match is convincing."""
        best, score = self._best_local_candidate(item)
        if best is not None and score >= SIMILARITY_THRESHOLD:
            return MatchResult(title=best, method=MatchMethod.FUZZY)
        return None

    # -- fallbacks --------------------------------------------------------

    def _create_title(self, item: RawItem) -> Title:
        hebrew, english = names_of(item)
        if hebrew is None and english is None:
            english = fallback_name(item)
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
        """Create a title anchored on a TMDB hit, keeping both names.

        The script decides which column a name goes in, never which field TMDB
        returned it in. Asking for Hebrew and taking whatever comes back as
        Hebrew, then calling the original title English, is how Israeli films
        ended up with the same Hebrew string in both columns - and how a
        Japanese original title ended up labelled English.
        """
        hebrew, english = names_of(item)
        for value in (hit.name, hit.original_name):
            if not value:
                continue
            if is_hebrew(value):
                hebrew = hebrew or value
            elif latin_script(value):
                english = english or value
        if hebrew is None and english is None:
            english = fallback_name(item)

        title = Title(
            type=item.kind,
            tmdb_id=hit.tmdb_id,
            name_he=hebrew,
            name_en=english,
            year=plausible_year(hit.year) or item.year,
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
                    # Carried so a ruling can record where to watch the title,
                    # rather than only that the title exists.
                    "offer_type": item.offer_type.value,
                    "tmdb_id": item.tmdb_id,
                    "imdb_id": item.imdb_id,
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


def _search_years(year: int | None) -> tuple[int | None, ...]:
    """The years to search TMDB for: the known one, then no constraint."""
    return (None,) if year is None else (year, None)


def title_kind_from(value: str) -> TitleKind:
    """Parse a stored title kind, defaulting to movie for unknown values."""
    try:
        return TitleKind(value)
    except ValueError:
        return TitleKind.MOVIE
