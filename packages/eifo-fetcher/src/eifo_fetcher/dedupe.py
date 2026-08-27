"""Merging titles the catalog holds twice.

The matcher no longer creates these, but the ones already stored do not remove
themselves. A duplicate is not a tidiness problem: it splits a title's ratings
and availability between two rows, so neither is complete and the one a search
happens to return is the one that looks worse.

Merging is destructive and irreversible, so the plan is printed and nothing is
written without ``--apply``. What it will merge on its own is deliberately the
narrow, boring case - same kind, names identical once normalised, years within
the usual tolerance - because a wrong merge silently loses a real title, which
is worse than the duplicate it was trying to fix. Everything else is counted and
described so somebody can look at it.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from eifo_core.models import (
    AggregateScore,
    Availability,
    Credit,
    EnrichAttempt,
    ExternalRating,
    MatchReview,
    Title,
    TitleGenre,
    TmdbAlias,
    UserItem,
)
from eifo_fetcher.match import REVIEW_YEAR_TOLERANCE, YEAR_TOLERANCE, normalise, years_match

logger = logging.getLogger("eifo.fetch.dedupe")

#: Title fields worth taking from a loser when the winner has nothing there.
FILLABLE = (
    "tmdb_id",
    "imdb_id",
    "name_he",
    "name_en",
    "year",
    "overview_he",
    "overview_en",
    "poster_path",
    "poster_source_url",
    "backdrop_path",
    "runtime_minutes",
    "seasons",
    "status",
    "original_language",
    "origin_countries",
)


@dataclass(slots=True)
class MergePlan:
    """One group of rows that are one title."""

    winner: Title
    losers: list[Title]

    @property
    def label(self) -> str:
        name = self.winner.name_he or self.winner.name_en or f"title#{self.winner.id}"
        return f"{name} ({self.winner.year or '-'})"

    def describe(self) -> str:
        losers = ", ".join(f"#{title.id}" for title in self.losers)
        return f"keep #{self.winner.id} {self.label}, merge {losers}"


@dataclass(slots=True)
class MergeTally:
    """What a dedupe run did."""

    groups: int = 0
    titles_removed: int = 0
    availability_moved: int = 0
    availability_folded: int = 0
    ratings_moved: int = 0
    credits_moved: int = 0
    user_items_moved: int = 0
    aliases_recorded: int = 0
    errors: list[str] = field(default_factory=list)

    def as_stats(self) -> dict[str, Any]:
        return {
            "groups": self.groups,
            "titles_removed": self.titles_removed,
            "availability_moved": self.availability_moved,
            "availability_folded": self.availability_folded,
            "ratings_moved": self.ratings_moved,
            "credits_moved": self.credits_moved,
            "user_items_moved": self.user_items_moved,
            "aliases_recorded": self.aliases_recorded,
            "errors": self.errors,
        }


# -- finding ----------------------------------------------------------------


def plan_merges(session: Session) -> list[MergePlan]:
    """Groups of titles that are confidently the same work.

    Confidently means: same kind, both names agreeing once normalised, and years
    close enough to be the same release. Anything looser is somebody's judgement
    call rather than a script's - see :func:`needs_a_human`.
    """
    by_key: dict[tuple[str, str], list[Title]] = defaultdict(list)
    for title in session.scalars(select(Title).order_by(Title.id)).all():
        for name in (title.name_he, title.name_en):
            if name:
                by_key[(title.type, normalise(name))].append(title)

    plans: list[MergePlan] = []
    claimed: set[int] = set()
    for group in by_key.values():
        members = _unclaimed(group, claimed)
        if len(members) < 2:
            continue
        for plan in _split_by_year(members):
            plans.append(plan)
            claimed.update(title.id for title in plan.losers)
            claimed.add(plan.winner.id)
    return plans


def _unclaimed(group: list[Title], claimed: set[int]) -> list[Title]:
    """Distinct titles from a group, skipping any already being merged."""
    seen: dict[int, Title] = {}
    for title in group:
        if title.id not in claimed:
            seen.setdefault(title.id, title)
    return list(seen.values())


def _split_by_year(members: list[Title]) -> list[MergePlan]:
    """Break a same-name group into runs whose years agree.

    Same name and an incompatible year is a remake, not a duplicate.
    """
    plans = []
    remaining = list(members)
    while len(remaining) > 1:
        head, *rest = remaining
        same = [head] + [
            title for title in rest if years_match(head.year, title.year, tolerance=YEAR_TOLERANCE)
        ]
        remaining = [title for title in rest if title not in same]
        if len(same) > 1:
            winner = pick_winner(same)
            plans.append(MergePlan(winner=winner, losers=[t for t in same if t is not winner]))
    return plans


def pick_winner(candidates: list[Title]) -> Title:
    """Which row the others fold into.

    An IMDb id first, because it is the identity the ratings pass joins on and
    the hardest to recover once dropped; then the row that already knows the
    most, since that is the one whose page is worth keeping; then the oldest,
    which is the one anything else pointing at a title is likeliest to mean.
    """

    def rank(title: Title) -> tuple[int, int, int, int]:
        return (
            1 if title.imdb_id else 0,
            1 if title.tmdb_id else 0,
            len(title.ratings) + len(title.credits),
            -title.id,
        )

    return max(candidates, key=rank)


def needs_a_human(session: Session) -> dict[str, int]:
    """Count the duplicate-looking pairs this tool will not decide by itself.

    Reported rather than merged: one catalog filing a work as a film while
    another files it as a series, and same-name titles whose years disagree.
    Both are real duplicates often enough to be worth mentioning and different
    works often enough that a script must not choose.

    The year does the filtering in both cases, because a shared name alone means
    very little - the catalog holds Spider-Man the 2002 film and Spider-Man the
    1994 series, The Flash from 2014 and from 1986, and a dozen more like them.
    A cross-kind pair is only interesting when the years agree, and a year gap
    only when it is small enough to be a disagreement rather than a remake.
    """
    by_name: dict[str, list[Title]] = defaultdict(list)
    for title in session.scalars(select(Title)).all():
        for name in (title.name_he, title.name_en):
            if name:
                by_name[normalise(name)].append(title)

    cross_kind = 0
    year_gap = 0
    for group in by_name.values():
        for index, left in enumerate(group):
            for right in group[index + 1 :]:
                if left.id == right.id:
                    continue
                if left.type != right.type:
                    if years_match(left.year, right.year, tolerance=YEAR_TOLERANCE):
                        cross_kind += 1
                elif not years_match(
                    left.year, right.year, tolerance=YEAR_TOLERANCE
                ) and years_match(left.year, right.year, tolerance=REVIEW_YEAR_TOLERANCE):
                    year_gap += 1
    return {"cross_kind": cross_kind, "year_gap": year_gap}


# -- merging ----------------------------------------------------------------


def apply_merges(session: Session, plans: list[MergePlan]) -> MergeTally:
    """Perform the merges, one committed transaction per group."""
    tally = MergeTally()
    for plan in plans:
        try:
            _merge_group(session, plan, tally)
            session.commit()
            tally.groups += 1
        except Exception as exc:
            session.rollback()
            logger.exception("merging %s failed", plan.describe())
            tally.errors.append(f"{plan.describe()}: {type(exc).__name__}: {exc}")
    return tally


def _merge_group(session: Session, plan: MergePlan, tally: MergeTally) -> None:
    for loser in plan.losers:
        logger.info("merging title %s into %s (%s)", loser.id, plan.winner.id, plan.label)
        _remember_the_losing_id(session, plan.winner, loser, tally)
        _fill_gaps(plan.winner, loser)
        _move_availability(session, plan.winner, loser, tally)
        _move_ratings(session, plan.winner, loser, tally)
        _move_credits(session, plan.winner, loser, tally)
        _move_genres(session, plan.winner, loser)
        _move_user_items(session, plan.winner, loser, tally)
        _repoint_reviews(session, plan.winner, loser)
        session.flush()
        # The loser's relationships cascade delete-orphan, and its loaded
        # collections still hold the rows just moved off it - deleting it now
        # would take them along. Expiring first makes it reload what actually
        # still points at it, which by here is nothing.
        session.expire(loser)
        session.delete(loser)
        tally.titles_removed += 1

    # Whatever the winner now holds, its aggregate no longer describes: the
    # ratings pass recomputes it, and a stale score is worse than none.
    stale = session.get(AggregateScore, plan.winner.id)
    if stale is not None:
        session.delete(stale)


def _remember_the_losing_id(
    session: Session, winner: Title, loser: Title, tally: MergeTally
) -> None:
    """Keep the loser's TMDB id pointed at the winner.

    Without this the merge undoes itself: the availability feed offers that id
    again the next night, no title owns it, and the duplicate comes back.
    """
    for alias in list(loser.tmdb_aliases):
        alias.title_id = winner.id

    if loser.tmdb_id is None or loser.tmdb_id == winner.tmdb_id:
        return
    # Keyed with the kind: the number alone belongs to two works, one in each
    # of TMDB's namespaces.
    if session.get(TmdbAlias, (loser.type, loser.tmdb_id)) is not None:
        return
    session.add(TmdbAlias(type=loser.type, tmdb_id=loser.tmdb_id, title_id=winner.id))
    tally.aliases_recorded += 1


def _fill_gaps(winner: Title, loser: Title) -> None:
    """Take anything the winner is missing and the loser knows."""
    for field_name in FILLABLE:
        if getattr(winner, field_name, None) in (None, ""):
            value = getattr(loser, field_name, None)
            if value not in (None, ""):
                setattr(winner, field_name, value)


def _move_availability(session: Session, winner: Title, loser: Title, tally: MergeTally) -> None:
    """Repoint offers, folding any the winner already has for the same service."""
    held = {
        (row.source_id, row.offer_type): row
        for row in session.scalars(
            select(Availability).where(Availability.title_id == winner.id)
        ).all()
    }

    for offer in session.scalars(
        select(Availability).where(Availability.title_id == loser.id)
    ).all():
        existing = held.get((offer.source_id, offer.offer_type))
        if existing is None:
            offer.title_id = winner.id
            held[(offer.source_id, offer.offer_type)] = offer
            tally.availability_moved += 1
            continue

        # Two rows for one offer: keep the longest history and the most
        # optimistic view of whether it is still carried.
        existing.first_seen = min(existing.first_seen, offer.first_seen)
        existing.last_seen = max(existing.last_seen, offer.last_seen)
        existing.is_current = existing.is_current or offer.is_current
        existing.miss_count = min(existing.miss_count, offer.miss_count)
        existing.deep_link_url = existing.deep_link_url or offer.deep_link_url
        session.delete(offer)
        tally.availability_folded += 1


def _move_ratings(session: Session, winner: Title, loser: Title, tally: MergeTally) -> None:
    """Repoint scores, keeping the freshest where both rows have one provider."""
    held = {
        row.provider: row
        for row in session.scalars(
            select(ExternalRating).where(ExternalRating.title_id == winner.id)
        ).all()
    }

    for rating in session.scalars(
        select(ExternalRating).where(ExternalRating.title_id == loser.id)
    ).all():
        existing = held.get(rating.provider)
        if existing is None:
            rating.title_id = winner.id
            held[rating.provider] = rating
            tally.ratings_moved += 1
        elif rating.fetched_at > existing.fetched_at:
            session.delete(existing)
            # Flushed before the move: (title_id, provider) is unique, and the
            # ORM would otherwise order the update ahead of the delete and
            # collide with the row it is about to remove.
            session.flush()
            rating.title_id = winner.id
            held[rating.provider] = rating
            tally.ratings_moved += 1
        else:
            session.delete(rating)


def _move_credits(session: Session, winner: Title, loser: Title, tally: MergeTally) -> None:
    held = {
        (row.person_id, row.role, row.character)
        for row in session.scalars(select(Credit).where(Credit.title_id == winner.id)).all()
    }

    for credit in session.scalars(select(Credit).where(Credit.title_id == loser.id)).all():
        key = (credit.person_id, credit.role, credit.character)
        if key in held:
            session.delete(credit)
            continue
        credit.title_id = winner.id
        held.add(key)
        tally.credits_moved += 1


def _move_genres(session: Session, winner: Title, loser: Title) -> None:
    held = {
        row.genre_id
        for row in session.scalars(select(TitleGenre).where(TitleGenre.title_id == winner.id)).all()
    }

    for link in session.scalars(select(TitleGenre).where(TitleGenre.title_id == loser.id)).all():
        if link.genre_id in held:
            session.delete(link)
        else:
            link.title_id = winner.id
            held.add(link.genre_id)


def _move_user_items(session: Session, winner: Title, loser: Title, tally: MergeTally) -> None:
    """Repoint somebody's list entry, unless they already have the winner.

    Their rating and their note are the only things here nobody can regenerate,
    so where both exist the one on the title being kept stands.
    """
    held = {
        row.user_id
        for row in session.scalars(select(UserItem).where(UserItem.title_id == winner.id)).all()
    }

    for entry in session.scalars(select(UserItem).where(UserItem.title_id == loser.id)).all():
        if entry.user_id in held:
            session.delete(entry)
            continue
        entry.title_id = winner.id
        held.add(entry.user_id)
        tally.user_items_moved += 1


def _repoint_reviews(session: Session, winner: Title, loser: Title) -> None:
    """A ruling somebody made still stands; it just points somewhere else now."""
    for review in session.scalars(
        select(MatchReview).where(MatchReview.resolved_title_id == loser.id)
    ).all():
        review.resolved_title_id = winner.id

    stale = session.get(EnrichAttempt, loser.id)
    if stale is not None:
        session.delete(stale)


def dangling_references(session: Session) -> list[str]:
    """Whatever SQLite's own integrity check has to say about the result.

    A merge repoints rows across half the schema; asking the database whether it
    believes the outcome costs nothing and is the only check that covers the
    parts this module forgot about.
    """
    if session.bind is None or session.bind.dialect.name != "sqlite":
        return []
    rows = session.execute(text("PRAGMA foreign_key_check")).all()
    return [str(tuple(row)) for row in rows]
